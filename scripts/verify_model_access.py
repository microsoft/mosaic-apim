"""Opt-in live verification of governed model access through a real API Management gateway.

The script reads each grant's connection details and keys from MOSAIC, then calls the gateway
directly. Every grant must reach its model with its own key and its own Entra token, and anonymous,
invalid, wrong-audience and cross-subject calls must be rejected. Optional checks cover an
ungranted user, the shared request budget, the tokens-per-minute limit and revocation, and that
grants held by other people stay out of the user's reach in MOSAIC.

Credentials come from environment variables, or from sign-ins the script starts: the device code
flow for users and client credentials for a workload. They stay in memory and are never printed,
and neither is model output. The script doesn't provision resources, rotate keys, or change grants
or authentication settings.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

LOGIN_ORIGIN = "https://login.microsoftonline.com"
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SUBSCRIPTION_HEADER = "Ocp-Apim-Subscription-Key"
ANTHROPIC_VERSION = "2023-06-01"
TROUBLESHOOTING = "docs/call-models-with-entra-tokens.md#troubleshooting"
PROMPT = "Reply with OK."
MAX_OUTPUT_TOKENS = 8
# The token-limit proof needs a limit that a few tiny calls can spend.
TOKEN_LIMIT_CEILING = 100
# Each call costs at least this many tokens: the prompt alone is longer.
MIN_TOKENS_PER_CALL = 8
BUDGET_CALLS = 2
BUDGET_PERIOD_SECONDS = 300
# How long a token must stay valid after a check that uses it starts.
TOKEN_MARGIN_SECONDS = 60
# Rejections in a row, after MOSAIC reports a grant revoked, that show the revocation took effect.
REVOCATION_CONFIRMATIONS = 2
# How API Management's own limit policies word their 429s. A model deployment's 429 passes through
# the gateway unchanged, with an {"error": ...} body instead.
GATEWAY_LIMIT_MESSAGES = {"calls": "Rate limit is exceeded", "tokens": "Token limit is exceeded"}

USER_CONTROL_TOKEN = "MOSAIC_SMOKE_USER_CONTROL_TOKEN"
ADMIN_CONTROL_TOKEN = "MOSAIC_SMOKE_ADMIN_CONTROL_TOKEN"
USER_RUNTIME_TOKEN = "MOSAIC_SMOKE_USER_RUNTIME_TOKEN"
APPLICATION_RUNTIME_TOKEN = "MOSAIC_SMOKE_APPLICATION_RUNTIME_TOKEN"
UNGRANTED_USER_RUNTIME_TOKEN = "MOSAIC_SMOKE_UNGRANTED_USER_RUNTIME_TOKEN"
AGENT_RUNTIME_TOKEN = "MOSAIC_SMOKE_AGENT_RUNTIME_TOKEN"
GROUP_MEMBER_RUNTIME_TOKEN = "MOSAIC_SMOKE_GROUP_MEMBER_RUNTIME_TOKEN"
APPLICATION_CLIENT_ID = "MOSAIC_SMOKE_APPLICATION_CLIENT_ID"
APPLICATION_CLIENT_SECRET = "MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET"
PAYLOAD = "MOSAIC_SMOKE_PAYLOAD"

GUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
AADSTS = re.compile(r"AADSTS\d{1,7}")
OAUTH_ERROR = re.compile(r"[a-z_]{1,64}")
DEPLOYMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SCOPE = re.compile(r"api://[^\s/?#]+/(?:Models\.Invoke|\.default)")
OPERATION_PATH = re.compile(r"(?:/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)+")
ENTITLEMENT = re.compile(r"[^/\\?#%\s]{1,200}")
USER_CODE = re.compile(r"[A-Za-z0-9-]{4,32}")
SECONDS = re.compile(r"[0-9]{1,6}")
RUNTIME_STATUSES = frozenset(
    {"pending", "applying", "applied", "revocationPending", "revoked", "failed", "unknown"}
)

Kind = Literal["user", "application"]
Proof = Literal["budget", "tokens"]
Check = tuple[str, dict[str, str], set[int]]


class VerificationFailed(RuntimeError):
    pass


def say(message: str) -> None:
    print(message, flush=True)


def https_origin(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise VerificationFailed("Endpoints must use HTTPS without embedded credentials")
    if parts.fragment:
        raise VerificationFailed("Endpoints must not contain fragments")
    return f"https://{parts.netloc.casefold()}"


def credential(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise VerificationFailed(f"Set {name} before running live verification")
    return value


def optional_credential(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def expect(response: httpx.Response, codes: set[int], label: str) -> None:
    if response.status_code not in codes:
        raise VerificationFailed(f"{label}: unexpected HTTP {response.status_code}")


def object_body(response: httpx.Response, label: str) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        raise VerificationFailed(f"{label}: response was not JSON") from None
    if not isinstance(value, dict):
        raise VerificationFailed(f"{label}: response was not an object")
    return value


def list_body(response: httpx.Response, label: str) -> list[Any]:
    try:
        value = response.json()
    except ValueError:
        raise VerificationFailed(f"{label}: response was not JSON") from None
    if not isinstance(value, list):
        raise VerificationFailed(f"{label}: response was not a list")
    return value


def mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def guid(value: object) -> str | None:
    return value.casefold() if isinstance(value, str) and GUID.fullmatch(value) else None


def token_claims(token: str) -> dict[str, Any]:
    """Read a JWT's claims without verifying it. Verifying tokens is the gateway's job."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except ValueError:
        return {}
    return mapping(claims)


def object_id(token: str) -> str | None:
    return guid(token_claims(token).get("oid"))


def token_audience(token: str) -> str | None:
    audience = token_claims(token).get("aud")
    return audience.removeprefix("api://").casefold() if isinstance(audience, str) else None


@dataclass(frozen=True)
class ModelRoute:
    operation: Literal["chat", "messages"]
    url: str
    payload: dict[str, Any]
    headers: dict[str, str]

    def reached_model(self, body: dict[str, Any]) -> bool:
        if self.operation == "messages":
            return body.get("type") == "message" and isinstance(body.get("content"), list)
        choices = body.get("choices")
        return isinstance(choices, list) and bool(choices)


def model_route(
    connection: dict[str, Any],
    expected_origin: str,
    *,
    api_version: str | None,
    models_api_version: str | None = None,
    token_parameter: str | None = None,
    payload: dict[str, Any] | None = None,
) -> ModelRoute:
    """Choose the published operation to call, and the smallest request it accepts."""
    endpoint = connection.get("endpoint")
    if not isinstance(endpoint, str) or https_origin(endpoint) != expected_origin:
        raise VerificationFailed("Connection metadata does not match the approved gateway origin")
    if urlsplit(endpoint).query:
        raise VerificationFailed("Model base endpoints must not contain query parameters")
    deployment = connection.get("deploymentName")
    if not isinstance(deployment, str) or not DEPLOYMENT.fullmatch(deployment):
        raise VerificationFailed("Connection metadata has no usable model deployment name")
    operations = [
        (operation.get("name"), operation["path"])
        for operation in connection.get("operations") or []
        if isinstance(operation, dict)
        and operation.get("method") == "POST"
        and isinstance(operation.get("path"), str)
        and OPERATION_PATH.fullmatch(operation["path"])
    ]
    chat = next(
        (
            path
            for name, path in operations
            if name == "chat-completions" or path.endswith("/chat/completions")
        ),
        None,
    )
    messages = next(
        (path for name, path in operations if name == "messages" or path.endswith("/v1/messages")),
        None,
    )
    base = endpoint.rstrip("/")
    prompt = [{"role": "user", "content": PROMPT}]
    if chat is not None:
        if chat.startswith("/openai/"):
            version, flag, parameter = api_version, "--api-version", "max_completion_tokens"
        elif chat.startswith("/models/"):
            version, flag, parameter = models_api_version, "--models-api-version", "max_tokens"
        else:
            raise VerificationFailed(f"The verifier doesn't recognize the {chat} route")
        if not version:
            raise VerificationFailed(f"Supply {flag} for its {chat} route")
        url = str(httpx.URL(f"{base}{chat}").copy_add_param("api-version", version))
        request: dict[str, Any] = {
            "model": deployment,
            "messages": prompt,
            token_parameter or parameter: MAX_OUTPUT_TOKENS,
            "stream": False,
        }
        route = ModelRoute("chat", url, request, {})
    elif messages is not None:
        request = {"model": deployment, "messages": prompt, "max_tokens": MAX_OUTPUT_TOKENS}
        route = ModelRoute(
            "messages", f"{base}{messages}", request, {"anthropic-version": ANTHROPIC_VERSION}
        )
    else:
        raise VerificationFailed(
            "The publication exposes neither a chat-completions nor a Messages operation"
        )
    if https_origin(route.url) != expected_origin:
        raise VerificationFailed("The model operation changed the approved gateway origin")
    if payload is not None:
        # Unscoped routes serve whichever deployment the body names, so always name this one.
        route = replace(route, payload={**payload, "model": deployment})
    return route


def custom_payload() -> dict[str, Any] | None:
    raw = os.environ.get(PAYLOAD)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        raise VerificationFailed(f"{PAYLOAD} must be valid JSON") from None
    if not isinstance(value, dict):
        raise VerificationFailed(f"{PAYLOAD} must be a JSON object")
    if value.get("stream"):
        raise VerificationFailed(f"{PAYLOAD} must not stream")
    return value


def reveal(client: httpx.Client, base: str, token: str, route: str, slot: str) -> str:
    response = client.post(
        f"{base}/{route}/keys/reveal", headers=bearer(token), json={"slot": slot}
    )
    expect(response, {200}, f"{slot} key retrieval")
    if "no-store" not in response.headers.get("Cache-Control", ""):
        raise VerificationFailed("Key responses must not be cacheable")
    value = object_body(response, "Key retrieval").get("key")
    if not isinstance(value, str) or not value:
        raise VerificationFailed("Key retrieval did not return a usable key")
    return value


@dataclass(frozen=True)
class Options:
    origin: str
    api_version: str | None
    models_api_version: str | None
    token_parameter: str | None
    payload: dict[str, Any] | None
    proof: Proof | None


@dataclass
class Grant:
    kind: Kind
    entitlement_id: str
    label: str
    route: ModelRoute
    keys_enabled: bool
    entra_enabled: bool
    tenant_id: str | None
    audience: str | None
    scope: str | None
    client_id: str | None
    tokens_per_minute: int | None
    control_token: str = field(repr=False)
    primary: str | None = field(default=None, repr=False)
    secondary: str | None = field(default=None, repr=False)


def grant_route(kind: Kind, entitlement_id: str) -> str:
    return f"{'me/entitlements' if kind == 'user' else 'entitlements'}/{entitlement_id}"


def load_grant(
    client: httpx.Client,
    base: str,
    *,
    kind: Kind,
    index: int,
    entitlement_id: str,
    control_token: str,
    options: Options,
) -> Grant:
    label = f"{'User' if kind == 'user' else 'Application'} grant {index}"
    route = grant_route(kind, entitlement_id)
    response = client.get(f"{base}/{route}/connection", headers=bearer(control_token))
    expect(response, {200}, f"{label} connection details")
    connection = object_body(response, f"{label} connection details")
    deployment = connection.get("deploymentName")
    if isinstance(deployment, str) and DEPLOYMENT.fullmatch(deployment):
        label = f"{label} ({deployment})"
    try:
        grant = grant_from(
            connection,
            kind=kind,
            entitlement_id=entitlement_id,
            label=label,
            control_token=control_token,
            options=options,
        )
        if grant.keys_enabled:
            grant.primary = reveal(client, base, control_token, route, "primary")
            if options.proof == "budget":
                grant.secondary = reveal(client, base, control_token, route, "secondary")
    except VerificationFailed as error:
        raise VerificationFailed(f"{label}: {error}") from None
    return grant


def grant_from(
    connection: dict[str, Any],
    *,
    kind: Kind,
    entitlement_id: str,
    label: str,
    control_token: str,
    options: Options,
) -> Grant:
    status = mapping(connection.get("runtime")).get("status")
    if status != "applied":
        shown = status if status in RUNTIME_STATUSES else "not set up"
        raise VerificationFailed(
            f"Access must be applied to APIM with nothing pending (status: {shown})"
        )
    methods = mapping(connection.get("appliedMethods"))
    keys = methods.get("keysEnabled") is True
    entra = methods.get("entraEnabled") is True
    if not keys and not entra:
        raise VerificationFailed("Neither keys nor Entra tokens are applied")
    route = model_route(
        connection,
        options.origin,
        api_version=options.api_version,
        models_api_version=options.models_api_version,
        token_parameter=options.token_parameter,
        payload=options.payload,
    )
    scope = connection.get("entraScope")
    suffix = "/Models.Invoke" if kind == "user" else "/.default"
    if not (isinstance(scope, str) and SCOPE.fullmatch(scope) and scope.endswith(suffix)):
        scope = None
    tenant = guid(connection.get("tenantId"))
    audience = guid(connection.get("entraAudience"))
    if entra and (tenant is None or audience is None or scope is None):
        raise VerificationFailed("The connection lacks a usable Entra tenant, audience or scope")
    limits = mapping(connection.get("grantLimits"))
    requests = mapping(limits.get("requests"))
    per_minute = mapping(limits.get("tokens")).get("tokensPerMinute")
    if options.proof == "budget":
        if not (keys and entra):
            raise VerificationFailed(
                "Proving the shared budget needs both keys and Entra tokens applied"
            )
        if (
            requests.get("calls") != BUDGET_CALLS
            or requests.get("renewalPeriodSeconds") != BUDGET_PERIOD_SECONDS
        ):
            raise VerificationFailed(
                "Proving the shared budget needs a fresh grant limited to 2 calls per 300 seconds"
            )
    if options.proof == "tokens":
        # MOSAIC requires the model's own token limit exactly when its gateway can meter tokens.
        if "publicationLimits" in connection and connection["publicationLimits"] is None:
            raise VerificationFailed(
                "MOSAIC applies no token limits to this model, because its gateway can't meter "
                "them (for example, Claude on a classic API Management tier), so there's no "
                "token limit to prove"
            )
        if requests:
            raise VerificationFailed(
                "Proving the token limit needs a grant without call limits, which return 429 first"
            )
        if (
            isinstance(per_minute, bool)
            or not isinstance(per_minute, int)
            or not 1 <= per_minute <= TOKEN_LIMIT_CEILING
        ):
            raise VerificationFailed(
                "Proving the token limit needs a grant limited to at most "
                f"{TOKEN_LIMIT_CEILING} tokens per minute"
            )
        # The gateway applies the model's own token limit to each grant as well.
        shared = mapping(connection.get("publicationLimits")).get("tokensPerMinute")
        if isinstance(shared, int) and not isinstance(shared, bool) and shared <= per_minute:
            raise VerificationFailed(
                "Proving the token limit needs the model's own limit for each grant "
                f"({shared} tokens per minute) to be higher than the grant's ({per_minute}), or "
                "this run can't tell which one refused"
            )
    return Grant(
        kind=kind,
        entitlement_id=entitlement_id,
        label=label,
        route=route,
        keys_enabled=keys,
        entra_enabled=entra,
        tenant_id=tenant,
        audience=audience,
        scope=scope,
        client_id=guid(connection.get("entraClientId")),
        tokens_per_minute=per_minute if options.proof == "tokens" else None,
        control_token=control_token,
    )


def bounded(value: object, low: int, high: int, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, low), high)


def oauth_error(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = mapping(body).get("error")
    return error if isinstance(error, str) and OAUTH_ERROR.fullmatch(error) else None


def sign_in_failure(response: httpx.Response, action: str) -> str:
    """Describe a failed sign-in by its codes only: descriptions can echo request details."""
    try:
        body = mapping(response.json())
    except ValueError:
        body = {}
    numbers = body.get("error_codes")
    codes = [
        f"AADSTS{number}"
        for number in (numbers if isinstance(numbers, list) else [])
        if isinstance(number, int) and not isinstance(number, bool) and 0 <= number < 10**7
    ]
    description = body.get("error_description")
    if not codes and isinstance(description, str):
        codes = AADSTS.findall(description)
    error = oauth_error(response)
    details = [error or f"HTTP {response.status_code}", *dict.fromkeys(codes)]
    return f"{action} failed: {', '.join(details)}. See {TROUBLESHOOTING}"


def access_token(response: httpx.Response, action: str) -> str:
    token = object_body(response, action).get("access_token")
    if not isinstance(token, str) or not token:
        raise VerificationFailed(f"{action} returned no access token")
    return token


def device_code_token(
    client: httpx.Client, *, tenant: str, client_id: str, scope: str, who: str
) -> str:
    """Sign a person in with the OAuth device authorization grant."""
    base = f"{LOGIN_ORIGIN}/{tenant}/oauth2/v2.0"
    # Only the model scope: no OpenID Connect scopes, so they need no consent.
    started = client.post(f"{base}/devicecode", data={"client_id": client_id, "scope": scope})
    if started.status_code != 200:
        raise VerificationFailed(sign_in_failure(started, f"Starting a sign-in for {who}"))
    flow = object_body(started, "Device sign-in")
    device_code = flow.get("device_code")
    user_code = flow.get("user_code")
    verification_uri = flow.get("verification_uri")
    if (
        not isinstance(device_code, str)
        or not device_code
        or not isinstance(user_code, str)
        or not USER_CODE.fullmatch(user_code)
        or not isinstance(verification_uri, str)
    ):
        raise VerificationFailed("The device sign-in response was incomplete")
    https_origin(verification_uri)
    interval = bounded(flow.get("interval"), 1, 60, 5)
    deadline = time.monotonic() + bounded(flow.get("expires_in"), 60, 1800, 900)
    print(
        f"SIGN IN as {who}: open {verification_uri} and enter the code {user_code}",
        file=sys.stderr,
        flush=True,
    )
    while time.monotonic() < deadline:
        time.sleep(interval)
        polled = client.post(
            f"{base}/token",
            data={
                "grant_type": DEVICE_CODE_GRANT,
                "client_id": client_id,
                "device_code": device_code,
            },
        )
        if polled.status_code == 200:
            return access_token(polled, "The sign-in")
        error = oauth_error(polled)
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = min(interval + 5, 60)
            continue
        raise VerificationFailed(sign_in_failure(polled, f"Signing in {who}"))
    raise VerificationFailed(f"Signing in {who} timed out")


def client_credentials_token(client: httpx.Client, *, tenant: str, scope: str) -> str:
    response = client.post(
        f"{LOGIN_ORIGIN}/{tenant}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": credential(APPLICATION_CLIENT_ID),
            "client_secret": credential(APPLICATION_CLIENT_SECRET),
            "scope": scope,
        },
    )
    if response.status_code != 200:
        raise VerificationFailed(sign_in_failure(response, "Signing in the application"))
    return access_token(response, "The application sign-in")


def seconds_left(token: str) -> int | None:
    """How long a token stays valid, or None if it names no expiry time."""
    expires = token_claims(token).get("exp")
    if isinstance(expires, bool) or not isinstance(expires, int | float):
        return None
    return int(expires - time.time())


def token_problem(token: str, *, kind: Kind, audience: str | None, valid_for: float) -> str | None:
    """Why token validation would refuse this model token, or None if it wouldn't.

    Validation runs before any grant rule, so a check that sent such a token would pass whatever
    the rule under test does.
    """
    claims = token_claims(token)
    if not claims:
        return "can't be read"
    if audience is not None and token_audience(token) != audience:
        return "is for a different audience from this grant"
    if claims.get("ver") != "2.0":
        return "isn't an Entra version 2.0 token, the only kind the gateway accepts"
    if kind == "user":
        scopes = claims.get("scp")
        if not isinstance(scopes, str) or "Models.Invoke" not in scopes.split():
            return "lacks the Models.Invoke scope"
    else:
        roles = claims.get("roles")
        if not isinstance(roles, list) or "Models.Invoke.Application" not in roles:
            return "lacks the Models.Invoke.Application role"
    left = seconds_left(token)
    if left is None:
        return "has no expiry time"
    if left <= 0:
        return "has expired"
    if left < valid_for:
        return f"expires in {left} seconds"
    return None


def token_failure(subject: str, problem: str) -> str:
    if problem.endswith("Application role"):
        return (
            f"{subject} {problem}. An administrator assigns it to the application and grants "
            "consent"
        )
    if "expire" in problem:
        return f"{subject} {problem}. Get a new one"
    return f"{subject} {problem}"


def check_runtime_token(grant: Grant, token: str) -> None:
    """Catch the wrong token before the gateway turns it into a bare 401 or 403."""
    if not token_claims(token):
        return
    problem = token_problem(
        token, kind=grant.kind, audience=grant.audience, valid_for=TOKEN_MARGIN_SECONDS
    )
    if problem is not None:
        raise VerificationFailed(token_failure(f"{grant.label}: the {grant.kind} token", problem))


@dataclass(frozen=True)
class Stranger:
    token: str = field(repr=False)
    audience: str | None


class RuntimeTokens:
    """Model-runtime tokens, from the environment or one sign-in per tenant and scope."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        user_source: str,
        application_source: str,
        granted_user: str | None,
    ) -> None:
        self._client = client
        self._user_source = user_source
        self._application_source = application_source
        self._granted_user = granted_user
        self._signed_in: dict[tuple[Kind, str, str], str] = {}

    def own(self, grant: Grant) -> str:
        """The token of the subject that holds the grant."""
        if grant.kind == "user":
            if self._user_source == "env":
                token = credential(USER_RUNTIME_TOKEN)
            else:
                token = self._sign_in(grant, "the user who holds these grants")
            signed_in = object_id(token)
            if self._granted_user and signed_in and signed_in != self._granted_user:
                raise VerificationFailed(
                    "The user's model token belongs to a different account from "
                    f"{USER_CONTROL_TOKEN}. Sign in as the user who holds these grants"
                )
        elif self._application_source == "env":
            token = credential(APPLICATION_RUNTIME_TOKEN)
        else:
            token = self._sign_in(grant, "the application")
        check_runtime_token(grant, token)
        return token

    def usable(self, kind: Kind, grant: Grant) -> tuple[str | None, str]:
        """A token of this kind, already in hand, that validation would accept for this grant.

        Returns the token, or None and why there isn't one. It never starts a sign-in.
        """
        source = self._user_source if kind == "user" else self._application_source
        if source == "env":
            held = optional_credential(
                USER_RUNTIME_TOKEN if kind == "user" else APPLICATION_RUNTIME_TOKEN
            )
            candidates = [held] if held else []
        else:
            candidates = [token for (of, _, _), token in self._signed_in.items() if of == kind]
        if not candidates:
            return None, f"no {kind} token in this run"
        problems: list[str] = []
        for token in candidates:
            problem = token_problem(
                token, kind=kind, audience=grant.audience, valid_for=TOKEN_MARGIN_SECONDS
            )
            if problem is None:
                return token, ""
            problems.append(problem)
        return None, f"the {kind} token in this run {problems[0]}"

    def stranger(self, grants: list[Grant]) -> Stranger:
        """A signed-in user who holds none of these grants."""
        users = [grant for grant in grants if grant.kind == "user" and grant.entra_enabled]
        if self._user_source == "env":
            token = credential(UNGRANTED_USER_RUNTIME_TOKEN)
            audience = token_audience(token)
        else:
            if not users:
                raise VerificationFailed(
                    "--check-ungranted-user signs in through a user grant that accepts Entra "
                    f"tokens, and none here does. Set {UNGRANTED_USER_RUNTIME_TOKEN} instead"
                )
            token = self._device_code(
                users[0], "a different user, one with no grant for these models"
            )
            audience = users[0].audience
        # Its audience is compared with each grant's when the check runs.
        problem = token_problem(token, kind="user", audience=None, valid_for=TOKEN_MARGIN_SECONDS)
        if problem is not None:
            raise VerificationFailed(token_failure("The ungranted user's token", problem))
        granted = {self.own(grant) for grant in users}
        people = {self._granted_user, *(object_id(held) for held in granted)} - {None}
        if token in granted or object_id(token) in people:
            raise VerificationFailed(
                "The ungranted user's token belongs to the user who holds these grants. "
                "Sign in as a different user"
            )
        return Stranger(token, audience)

    def _sign_in(self, grant: Grant, who: str) -> str:
        if grant.tenant_id is None or grant.scope is None:
            raise VerificationFailed(f"{grant.label}: the connection has no Entra sign-in details")
        key = (grant.kind, grant.tenant_id, grant.scope)
        if key not in self._signed_in:
            if grant.kind == "user":
                self._signed_in[key] = self._device_code(grant, who)
            else:
                self._signed_in[key] = client_credentials_token(
                    self._client, tenant=grant.tenant_id, scope=grant.scope
                )
        return self._signed_in[key]

    def _device_code(self, grant: Grant, who: str) -> str:
        if grant.tenant_id is None or grant.scope is None or grant.client_id is None:
            raise VerificationFailed(
                f"{grant.label}: MOSAIC names no model client for this grant's audience, so the "
                f"verifier can't sign in. Set {USER_RUNTIME_TOKEN} instead"
            )
        return device_code_token(
            self._client,
            tenant=grant.tenant_id,
            client_id=grant.client_id,
            scope=grant.scope,
            who=who,
        )


def denials(
    grant: Grant, tokens: RuntimeTokens, stranger: Stranger | None
) -> tuple[list[Check], list[str]]:
    """Calls the gateway must reject, and the ones this run has no credential for."""
    checks: list[Check] = [
        ("an anonymous call", {}, {401, 403}),
        ("an invalid key", {SUBSCRIPTION_HEADER: "invalid-fixture-key"}, {401, 403}),
        # Token validation refuses it with 401, because MOSAIC never uses its own audience for
        # model calls. A 403 would mean validation was skipped and only the grant lookup refused.
        ("a MOSAIC control-plane token", bearer(grant.control_token), {401}),
    ]
    skipped: list[str] = []
    if grant.primary is not None:
        key = {SUBSCRIPTION_HEADER: grant.primary}
        checks.append(
            (
                "an invalid token with a valid key",
                {**key, **bearer("invalid-fixture-token")},
                {401},
            )
        )
        other: Kind = "application" if grant.kind == "user" else "user"
        name = f"the {other}'s token with this grant's key"
        other_token, missing = tokens.usable(other, grant)
        if other_token:
            # The token passes validation, then names a different grant from the key: 403.
            refused = {403} if grant.entra_enabled else {401}
            checks.append((name, {**key, **bearer(other_token)}, refused))
        else:
            skipped.append(f"{name}: {missing}")
    if stranger is not None:
        name = "the ungranted user's token"
        if not grant.entra_enabled:
            checks.append((name, bearer(stranger.token), {401}))
        elif stranger.audience == grant.audience:
            checks.append((name, bearer(stranger.token), {403}))
        else:
            skipped.append(f"{name}: it's for a different audience")
    if not grant.entra_enabled:
        # A token the gateway would accept with Entra on, so a gateway still validating tokens
        # lets it through instead of refusing it.
        name = "a token while Entra tokens are off"
        own, missing = tokens.usable(grant.kind, grant)
        if own:
            checks.append((name, bearer(own), {401}))
        else:
            skipped.append(f"{name}: {missing}")
    return checks, skipped


def auth_headers(grant: Grant, tokens: RuntimeTokens) -> dict[str, dict[str, str]]:
    """The headers for each applied way the grant's subject authenticates."""
    found: dict[str, dict[str, str]] = {}
    if grant.primary is not None:
        found["key"] = {SUBSCRIPTION_HEADER: grant.primary}
    if grant.entra_enabled:
        found["Entra token"] = bearer(tokens.own(grant))
    return found


def gateway_limit(response: httpx.Response) -> str | None:
    """Which of the gateway's own limits refused a call: "calls", "tokens", or None."""
    if response.status_code != 429:
        return None
    try:
        body = mapping(response.json())
    except ValueError:
        return None
    message = body.get("message")
    if not isinstance(message, str):
        return None
    return next(
        (limit for limit, text in GATEWAY_LIMIT_MESSAGES.items() if message.startswith(text)),
        None,
    )


def verify_grant(
    client: httpx.Client,
    grant: Grant,
    tokens: RuntimeTokens,
    stranger: Stranger | None,
    proof: Proof | None,
) -> None:
    route = grant.route

    def call(headers: dict[str, str]) -> httpx.Response:
        return client.post(route.url, headers={**route.headers, **headers}, json=route.payload)

    # Rejections come first, so they can't spend a budget the proofs below depend on.
    checks, skipped = denials(grant, tokens, stranger)
    for name, headers, codes in checks:
        expect(call(headers), codes, f"{grant.label} rejecting {name}")
    say(f"PASS: {grant.label} rejected {', '.join(name for name, _, _ in checks)}")
    for reason in skipped:
        say(f"SKIP: {grant.label} {reason}")

    started = time.monotonic()
    ways = auth_headers(grant, tokens)
    for method, headers in ways.items():
        response = call(headers)
        expect(response, {200}, f"{grant.label} {method} call")
        if not route.reached_model(object_body(response, f"{grant.label} {method} call")):
            raise VerificationFailed(f"{grant.label}: the {method} call returned no model response")
        say(f"PASS: {grant.label} reached the model with its {method}")
    if proof == "budget":
        if time.monotonic() - started >= BUDGET_PERIOD_SECONDS / 2:
            raise VerificationFailed(
                f"{grant.label}: the calls took too long to prove the shared budget. Retry"
            )
        exhausted = call({SUBSCRIPTION_HEADER: grant.secondary or ""})
        expect(exhausted, {429}, f"{grant.label} secondary key after the budget was spent")
        refused_by = gateway_limit(exhausted)
        if refused_by is None:
            raise VerificationFailed(
                f"{grant.label}: the secondary key's call reached the model deployment, which "
                "throttled it itself, so the gateway didn't apply the grant's shared budget"
            )
        if refused_by == "tokens":
            raise VerificationFailed(
                f"{grant.label}: a token limit refused the secondary key's call, so this run "
                "can't prove the shared call budget. Use a grant with call limits only"
            )
        say(f"PASS: {grant.label} primary key, Entra token and secondary key share one budget")
    elif proof == "tokens" and grant.tokens_per_minute is not None:
        prove_token_limit(call, grant, next(iter(ways.values())), grant.tokens_per_minute)


def connection_route(entitlement_id: str) -> str:
    return f"entitlements/{entitlement_id}"


def route_from_connection(connection: dict[str, Any], options: Options) -> ModelRoute:
    return model_route(
        connection,
        options.origin,
        api_version=options.api_version,
        models_api_version=options.models_api_version,
        token_parameter=options.token_parameter,
        payload=options.payload,
    )


def require_applied(connection: dict[str, Any], label: str) -> None:
    status = mapping(connection.get("runtime")).get("status")
    if status != "applied":
        shown = status if status in RUNTIME_STATUSES else "not set up"
        raise VerificationFailed(
            f"{label}: access must be applied to APIM with nothing pending (status: {shown})"
        )


def verify_agent_entitlement(
    client: httpx.Client,
    base: str,
    entitlement_id: str,
    control_token: str,
    runtime_token: str,
    options: Options,
) -> None:
    label = "Agent identity grant"
    route = connection_route(entitlement_id)
    response = client.get(f"{base}/{route}/connection", headers=bearer(control_token))
    expect(response, {200}, f"{label} connection details")
    connection = object_body(response, f"{label} connection details")
    if connection.get("principalKind") != "agentIdentity":
        raise VerificationFailed(f"{label}: principalKind was not agentIdentity")
    scope = connection.get("entraScope")
    if not (isinstance(scope, str) and SCOPE.fullmatch(scope) and scope.endswith("/.default")):
        raise VerificationFailed(f"{label}: connection did not report a .default runtime scope")
    if connection.get("requiredAppRole") != "Models.Invoke.Application":
        raise VerificationFailed(f"{label}: connection did not report the required app role")
    if mapping(connection.get("appliedMethods")).get("entraEnabled") is not True:
        raise VerificationFailed(f"{label}: Entra tokens are not applied")
    require_applied(connection, label)
    route_to_model = route_from_connection(connection, options)
    response = client.post(
        route_to_model.url,
        headers={**route_to_model.headers, **bearer(runtime_token)},
        json=route_to_model.payload,
    )
    expect(response, {200}, "Agent model call")
    if not route_to_model.reached_model(object_body(response, "Agent model call")):
        raise VerificationFailed("Agent token did not return a model response")
    say("PASS: Agent identity token reached the model")


def warn_group_claim_diagnostics(token: str) -> None:
    claims = token_claims(token)
    claim_names = mapping(claims.get("_claim_names"))
    groups = claims.get("groups")
    if not isinstance(groups, list) or not groups:
        print(
            "WARN: group member runtime token has no groups claim; "
            "security-group grants cannot match without it.",
            file=sys.stderr,
        )
    if "groups" in claim_names or claims.get("hasgroups") is True or "groups:src1" in claims:
        print(
            "WARN: group member runtime token signals group overage; "
            "use a direct grant for this caller.",
            file=sys.stderr,
        )


def verify_group_entitlement(
    client: httpx.Client,
    base: str,
    entitlement_id: str,
    control_token: str,
    runtime_token: str,
    options: Options,
) -> None:
    warn_group_claim_diagnostics(runtime_token)
    label = "Security-group grant"
    route = connection_route(entitlement_id)
    response = client.get(f"{base}/{route}/connection", headers=bearer(control_token))
    expect(response, {200}, f"{label} connection details")
    connection = object_body(response, f"{label} connection details")
    if connection.get("keysAvailable") is not False:
        raise VerificationFailed(f"{label}: connection must report keysAvailable false")
    reveal_response = client.post(
        f"{base}/{route}/keys/reveal",
        headers=bearer(control_token),
        json={"slot": "primary"},
    )
    expect(reveal_response, {409}, "Security-group key reveal refusal")
    route_to_model = route_from_connection(connection, options)
    response = client.post(
        route_to_model.url,
        headers={**route_to_model.headers, **bearer(runtime_token)},
        json=route_to_model.payload,
    )
    expect(response, {200}, "Group member model call")
    if not route_to_model.reached_model(object_body(response, "Group member model call")):
        raise VerificationFailed("Group member token did not return a model response")
    say("PASS: Security-group member token reached the model")


def prove_token_limit(
    call: Callable[[dict[str, str]], httpx.Response],
    grant: Grant,
    headers: dict[str, str],
    limit: int,
) -> None:
    # A window can reset once mid-run, so allow twice the calls that one window needs.
    attempts = 2 * (limit // MIN_TOKENS_PER_CALL) + 2
    for attempt in range(1, attempts + 1):
        response = call(headers)
        if response.status_code == 429:
            refused_by = gateway_limit(response)
            if refused_by == "calls":
                raise VerificationFailed(
                    f"{grant.label}: a call limit refused a call before its token limit did, so "
                    "this run can't prove the token limit"
                )
            if refused_by is None:
                raise VerificationFailed(
                    f"{grant.label}: the model deployment throttled a call itself, so this run "
                    "can't prove the grant's token limit. Wait a minute and retry, or use a "
                    "deployment with more capacity"
                )
            if not SECONDS.fullmatch(response.headers.get("Retry-After", "")):
                raise VerificationFailed(
                    f"{grant.label}: its tokens-per-minute 429 had no Retry-After in seconds"
                )
            say(
                f"PASS: {grant.label} returned 429 with Retry-After after {attempt} more "
                f"call(s) spent its {limit} tokens per minute"
            )
            return
        expect(response, {200}, f"{grant.label} call within its token limit")
    raise VerificationFailed(
        f"{grant.label}: {attempts} more calls didn't reach its {limit} tokens-per-minute limit"
    )


def runtime_state(client: httpx.Client, base: str, grant: Grant) -> tuple[str, bool]:
    """MOSAIC's runtime status for a grant, and whether Entra tokens are still applied."""
    route = grant_route(grant.kind, grant.entitlement_id)
    response = client.get(f"{base}/{route}/connection", headers=bearer(grant.control_token))
    if response.status_code == 404:
        raise VerificationFailed(
            f"{grant.label}: MOSAIC no longer finds this grant, so the watch can't tell when the "
            "gateway applies its removal. Disable a grant (Revoke in the console) instead of "
            "deleting it"
        )
    expect(response, {200}, f"{grant.label} connection details")
    connection = object_body(response, f"{grant.label} connection details")
    status = mapping(connection.get("runtime")).get("status")
    entra = mapping(connection.get("appliedMethods")).get("entraEnabled") is True
    return (status if status in RUNTIME_STATUSES else "not set up"), entra


def watch_revocation(
    client: httpx.Client,
    base: str,
    grant: Grant,
    tokens: RuntimeTokens,
    *,
    timeout: int,
    interval: int,
) -> None:
    """Wait for MOSAIC to report the grant revoked, then for the gateway to refuse it.

    Every apply briefly refuses all calls to the model while it rewrites the policy, so only
    rejections after MOSAIC reports the apply finished count, and only when they repeat.
    """
    pending = auth_headers(grant, tokens)
    held = [("MOSAIC control-plane token", grant.control_token)]
    if "Entra token" in pending:
        held.append((f"{grant.kind} token", tokens.own(grant)))
    needed = timeout + interval + TOKEN_MARGIN_SECONDS
    for name, token in held:
        left = seconds_left(token)
        if left is not None and left < needed:
            when = "has expired" if left <= 0 else f"expires in {left} seconds"
            raise VerificationFailed(
                f"{grant.label}: the {name} {when}, and watching for revocation can take "
                f"{needed} seconds. Get a new one, or lower --revocation-timeout"
            )
    say(
        f"WAIT: revoke {grant.label} in MOSAIC's console, which disables it, and apply its "
        f"model's access plan. Checking every {interval} seconds for up to {timeout} seconds"
    )
    deadline = time.monotonic() + timeout
    status = "applied"
    streaks = dict.fromkeys(pending, 0)
    last: dict[str, int] = {}
    while pending:
        if time.monotonic() >= deadline:
            if status != "revoked":
                raise VerificationFailed(
                    f"{grant.label}: after {timeout} seconds, MOSAIC reports it as {status}, not "
                    "revoked. Revoke it and apply its model's access plan, then rerun"
                )
            codes = ", ".join(f"{method} HTTP {last[method]}" for method in pending)
            raise VerificationFailed(
                f"{grant.label}: MOSAIC reports it revoked, but after {timeout} seconds calls "
                f"with its {' and '.join(pending)} weren't rejected "
                f"{REVOCATION_CONFIRMATIONS} times in a row (last: {codes})"
            )
        time.sleep(interval)
        current, entra = runtime_state(client, base, grant)
        if current != status:
            status = current
            say(f"WAIT: MOSAIC reports {grant.label} as {status}")
        for method, headers in list(pending.items()):
            if status != "revoked":
                streaks[method] = 0
                continue
            response = client.post(
                grant.route.url,
                headers={**grant.route.headers, **headers},
                json=grant.route.payload,
            )
            last[method] = response.status_code
            # A token that still passes validation is refused by the grant lookup, with 403. A
            # 401 would mean validation refused it, which says nothing about the revocation.
            refused = {401, 403} if method == "key" else ({403} if entra else {401})
            streaks[method] = streaks[method] + 1 if response.status_code in refused else 0
            if streaks[method] == REVOCATION_CONFIRMATIONS:
                del pending[method]
                say(f"PASS: {grant.label} rejects its {method} after revocation")


def check_foreign_keys(
    client: httpx.Client, base: str, user_control_token: str | None, grants: list[Grant]
) -> None:
    applications = [grant for grant in grants if grant.kind == "application"]
    if not applications:
        return
    if user_control_token is None:
        say(f"SKIP: an end user retrieving an application's key: set {USER_CONTROL_TOKEN}")
        return
    for grant in applications:
        response = client.post(
            f"{base}/me/entitlements/{grant.entitlement_id}/keys/reveal",
            headers=bearer(user_control_token),
            json={"slot": "primary"},
        )
        expect(response, {403, 404}, f"The end user retrieving {grant.label}'s key")
    say("PASS: the end user can't retrieve an application grant's key")


def listed_grant_ids(client: httpx.Client, base: str, token: str) -> set[str]:
    """The grant IDs the caller's own lists show: MOSAIC's and the portal's My access."""
    response = client.get(f"{base}/me/entitlements", headers=bearer(token))
    expect(response, {200}, "The user's list of grants")
    mine = [mapping(item).get("id") for item in list_body(response, "The user's list of grants")]
    response = client.get(f"{base}/portal/entitlements", headers=bearer(token))
    expect(response, {200}, "The portal's list of the user's grants")
    portal = [
        mapping(mapping(item).get("entitlement")).get("id")
        for item in list_body(response, "The portal's list of the user's grants")
    ]
    return {value for value in [*mine, *portal] if isinstance(value, str)}


def usage_grant_ids(client: httpx.Client, base: str, token: str) -> set[str] | None:
    """The grant IDs in the caller's usage report, or None if this MOSAIC has no usage report."""
    response = client.get(f"{base}/me/usage", params={"period": "90d"}, headers=bearer(token))
    # MOSAIC before the usage report (ADR 0015) has no such route, and its 404 shows no grant.
    if response.status_code == 404:
        return None
    expect(response, {200}, "The user's usage report")
    report = object_body(response, "The user's usage report")
    ids: set[str] = set()
    for part in ("byResource", "timeline"):
        rows = report.get(part)
        if not isinstance(rows, list):
            raise VerificationFailed(f"The user's usage report has no {part} list")
        for row in rows:
            identifier = mapping(row).get("entitlementId")
            # A row that names no grant would make the check below pass without looking.
            if not isinstance(identifier, str):
                raise VerificationFailed(f"The user's usage report has a {part} row with no grant")
            ids.add(identifier)
    return ids


def check_foreign_user_grants(
    client: httpx.Client,
    base: str,
    user_control: str,
    admin_control: str,
    identifiers: list[str],
) -> None:
    """Grants held by other people must stay out of the user's lists, usage, details and keys.

    The admin first confirms that each grant exists and that someone else holds it, so a mistyped
    ID or the user's own grant can't pass as a refusal.
    """
    user = object_id(user_control)
    if user is None:
        raise VerificationFailed(f"{USER_CONTROL_TOKEN} names no user object ID")
    labels = {
        identifier: f"Another person's grant {index}"
        for index, identifier in enumerate(identifiers, start=1)
    }
    for identifier, label in labels.items():
        response = client.get(f"{base}/entitlements/{identifier}", headers=bearer(admin_control))
        expect(response, {200}, f"The admin reading {label}")
        subject = mapping(object_body(response, f"The admin reading {label}").get("subject"))
        holder_id = subject.get("id")
        if subject.get("kind") != "user" or not isinstance(holder_id, str):
            raise VerificationFailed(f"{label} isn't a user grant")
        if not ENTITLEMENT.fullmatch(holder_id):
            raise VerificationFailed(f"{label} names an unusable holder ID")
        response = client.get(f"{base}/principals/{holder_id}", headers=bearer(admin_control))
        expect(response, {200}, f"The admin reading {label}'s holder")
        holder = guid(object_body(response, f"The admin reading {label}'s holder").get("objectId"))
        if holder is None:
            raise VerificationFailed(f"{label}'s holder has no object ID")
        if holder == user:
            raise VerificationFailed(
                f"{label} is held by the user. Name a grant that someone else holds"
            )
    listed = listed_grant_ids(client, base, user_control)
    usage = usage_grant_ids(client, base, user_control)
    for identifier, label in labels.items():
        if identifier in listed:
            raise VerificationFailed(f"MOSAIC lists {label} among the user's own grants")
        if usage is not None and identifier in usage:
            raise VerificationFailed(f"The user's usage report includes {label}")
        route = f"{base}/me/entitlements/{identifier}"
        response = client.get(f"{route}/connection", headers=bearer(user_control))
        expect(response, {403, 404}, f"The user reading {label}'s connection details")
        # A refusal carries no key. Anything else is never printed, like every other response.
        response = client.post(
            f"{route}/keys/reveal", headers=bearer(user_control), json={"slot": "primary"}
        )
        expect(response, {403, 404}, f"The user retrieving {label}'s key")
    say(
        f"PASS: the user can't list, read or retrieve the key of {len(identifiers)} grant(s) "
        "held by someone else"
    )
    if usage is None:
        say("SKIP: the user's usage report: this MOSAIC has none")
    else:
        say(
            f"PASS: the user's usage report leaves out {len(identifiers)} grant(s) held by "
            "someone else"
        )


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base-url", required=True, help="MOSAIC API origin, without /api/v1")
    parser.add_argument(
        "--gateway-origin", required=True, help="Explicitly approved APIM HTTPS origin"
    )
    parser.add_argument(
        "--user-entitlement",
        action="append",
        default=[],
        metavar="ID",
        help="A user grant to verify. Repeat it for each grant; they must share one user",
    )
    parser.add_argument(
        "--application-entitlement",
        action="append",
        default=[],
        metavar="ID",
        help="An application grant to verify. Repeat it for each grant of the same application",
    )
    parser.add_argument(
        "--foreign-user-entitlement",
        action="append",
        default=[],
        metavar="ID",
        help="A grant held by someone other than the user, which the user must not list, read "
        "or retrieve a key for. Repeat it for each grant",
    )
    parser.add_argument(
        "--agent-entitlement",
        action="append",
        default=[],
        metavar="ID",
        help="An Entra Agent ID grant to verify with MOSAIC_SMOKE_AGENT_RUNTIME_TOKEN",
    )
    parser.add_argument(
        "--group-entitlement",
        action="append",
        default=[],
        metavar="ID",
        help="A security-group grant to verify with MOSAIC_SMOKE_GROUP_MEMBER_RUNTIME_TOKEN",
    )
    parser.add_argument("--api-version", help="Azure OpenAI API version, for /openai/ routes")
    parser.add_argument(
        "--models-api-version", help="Foundry Models API version, for /models/ routes"
    )
    parser.add_argument(
        "--chat-token-parameter",
        choices=("max_tokens", "max_completion_tokens"),
        help="The output-token cap to send on chat routes, instead of the route's default",
    )
    parser.add_argument("--user-token-source", choices=("env", "device-code"), default="env")
    parser.add_argument(
        "--application-token-source", choices=("env", "client-credentials"), default="env"
    )
    parser.add_argument(
        "--send-model-requests",
        action="store_true",
        help="Acknowledge that the checks send real, billed model requests. Needed when the run "
        "names a model-calling entitlement",
    )
    parser.add_argument(
        "--check-ungranted-user",
        action="store_true",
        help="Also check that a different, ungranted user's token is rejected",
    )
    proofs = parser.add_mutually_exclusive_group()
    proofs.add_argument(
        "--prove-shared-budget",
        action="store_true",
        help="For grants limited to 2 calls per 300 seconds: the secondary key must get 429",
    )
    proofs.add_argument(
        "--prove-token-limit",
        action="store_true",
        help=f"For grants limited to at most {TOKEN_LIMIT_CEILING} tokens per minute: "
        "calls must reach 429 with Retry-After",
    )
    parser.add_argument(
        "--watch-revocation",
        metavar="ID",
        help="After the checks, wait until revoking this listed grant takes effect",
    )
    parser.add_argument("--revocation-timeout", type=int, default=900, metavar="SECONDS")
    parser.add_argument("--revocation-interval", type=int, default=30, metavar="SECONDS")
    return parser.parse_args(argv)


def validate(
    args: argparse.Namespace,
) -> tuple[Options, str | None, str | None, str | None, str | None]:
    """Check arguments and credentials before any network call."""
    https_origin(args.api_base_url)
    if urlsplit(args.api_base_url).query:
        raise VerificationFailed("The control-plane base URL must not contain a query string")
    origin = https_origin(args.gateway_origin)
    if urlsplit(args.gateway_origin).path not in {"", "/"}:
        raise VerificationFailed("Supply the gateway origin without an API path")
    standard_identifiers = [*args.user_entitlement, *args.application_entitlement]
    identifiers = [
        *standard_identifiers,
        *args.agent_entitlement,
        *args.group_entitlement,
    ]
    listed = [*identifiers, *args.foreign_user_entitlement]
    if not listed:
        raise VerificationFailed(
            "Supply at least one --user-entitlement, --application-entitlement, "
            "--agent-entitlement, --group-entitlement or --foreign-user-entitlement"
        )
    if not all(ENTITLEMENT.fullmatch(identifier) for identifier in listed):
        raise VerificationFailed("Supply an entitlement identifier, not a URL")
    if len(set(listed)) != len(listed):
        raise VerificationFailed("List each entitlement only once")
    if identifiers and not args.send_model_requests:
        raise VerificationFailed(
            "Add --send-model-requests to acknowledge that the checks send real, billed model "
            "requests"
        )
    if args.check_ungranted_user and not args.user_entitlement:
        raise VerificationFailed(
            "--check-ungranted-user needs a --user-entitlement in the same run"
        )
    if (args.prove_shared_budget or args.prove_token_limit) and not standard_identifiers:
        raise VerificationFailed(
            "A proof needs a --user-entitlement or --application-entitlement to run on"
        )
    if args.watch_revocation is not None and args.watch_revocation not in standard_identifiers:
        raise VerificationFailed(
            "--watch-revocation must name a --user-entitlement or --application-entitlement in "
            "this run"
        )
    if not 60 <= args.revocation_timeout <= 3600:
        raise VerificationFailed("--revocation-timeout must be from 60 to 3600 seconds")
    if not 10 <= args.revocation_interval <= 300:
        raise VerificationFailed("--revocation-interval must be from 10 to 300 seconds")
    if args.user_entitlement or args.foreign_user_entitlement:
        user_control: str | None = credential(USER_CONTROL_TOKEN)
    else:
        user_control = optional_credential(USER_CONTROL_TOKEN)
    admin_control = (
        credential(ADMIN_CONTROL_TOKEN)
        if (
            args.application_entitlement
            or args.foreign_user_entitlement
            or args.agent_entitlement
            or args.group_entitlement
        )
        else None
    )
    agent_runtime = credential(AGENT_RUNTIME_TOKEN) if args.agent_entitlement else None
    group_runtime = credential(GROUP_MEMBER_RUNTIME_TOKEN) if args.group_entitlement else None
    if args.application_entitlement and args.application_token_source == "client-credentials":
        if guid(credential(APPLICATION_CLIENT_ID)) is None:
            raise VerificationFailed(f"{APPLICATION_CLIENT_ID} must be the application's client ID")
        credential(APPLICATION_CLIENT_SECRET)
    if args.check_ungranted_user and args.user_token_source == "env":
        credential(UNGRANTED_USER_RUNTIME_TOKEN)
    proof: Proof | None = (
        "budget" if args.prove_shared_budget else "tokens" if args.prove_token_limit else None
    )
    options = Options(
        origin=origin,
        api_version=args.api_version,
        models_api_version=args.models_api_version,
        token_parameter=args.chat_token_parameter,
        payload=custom_payload(),
        proof=proof,
    )
    return options, user_control, admin_control, agent_runtime, group_runtime


def run(
    client: httpx.Client,
    args: argparse.Namespace,
    options: Options,
    user_control: str | None,
    admin_control: str | None,
    agent_runtime: str | None,
    group_runtime: str | None,
) -> tuple[int, int]:
    base = f"{args.api_base_url.rstrip('/')}/api/v1"
    # Isolation needs no model call, so it runs first and can fail before any is billed.
    if args.foreign_user_entitlement:
        check_foreign_user_grants(
            client, base, user_control or "", admin_control or "", args.foreign_user_entitlement
        )
    grants: list[Grant] = []
    subjects: list[tuple[Kind, list[str], str | None]] = [
        ("user", args.user_entitlement, user_control),
        ("application", args.application_entitlement, admin_control),
    ]
    for kind, identifiers, control in subjects:
        for index, identifier in enumerate(identifiers, start=1):
            grants.append(
                load_grant(
                    client,
                    base,
                    kind=kind,
                    index=index,
                    entitlement_id=identifier,
                    control_token=control or "",
                    options=options,
                )
            )
    check_foreign_keys(client, base, user_control, grants)
    tokens = RuntimeTokens(
        client,
        user_source=args.user_token_source,
        application_source=args.application_token_source,
        granted_user=object_id(user_control) if user_control else None,
    )
    # Every sign-in happens before the first model call, so none can split a budget window.
    for grant in grants:
        if grant.entra_enabled:
            tokens.own(grant)
    stranger = tokens.stranger(grants) if args.check_ungranted_user else None
    for grant in grants:
        verify_grant(client, grant, tokens, stranger, options.proof)
    for identifier in args.agent_entitlement:
        verify_agent_entitlement(
            client, base, identifier, admin_control or "", agent_runtime or "", options
        )
    for identifier in args.group_entitlement:
        verify_group_entitlement(
            client, base, identifier, admin_control or "", group_runtime or "", options
        )
    if args.watch_revocation is not None:
        watched = next(grant for grant in grants if grant.entitlement_id == args.watch_revocation)
        watch_revocation(
            client,
            base,
            watched,
            tokens,
            timeout=args.revocation_timeout,
            interval=args.revocation_interval,
        )
    return (
        len(grants) + len(args.agent_entitlement) + len(args.group_entitlement),
        len(args.foreign_user_entitlement),
    )


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    args = parse_arguments(argv)
    try:
        options, user_control, admin_control, agent_runtime, group_runtime = validate(args)
        with httpx.Client(timeout=30, follow_redirects=False, transport=transport) as client:
            count, foreign = run(
                client, args, options, user_control, admin_control, agent_runtime, group_runtime
            )
    except KeyboardInterrupt:
        print("STOPPED: interrupted before the checks finished", file=sys.stderr)
        return 130
    except (VerificationFailed, httpx.HTTPError) as error:
        # HTTP exceptions can carry request headers or URLs; never print their representation.
        message = str(error) if isinstance(error, VerificationFailed) else "HTTP transport failure"
        print(f"FAIL: {message}", file=sys.stderr)
        return 1
    if not count:
        say(f"Isolation checks passed for {foreign} grant(s) held by someone else.")
        return 0
    later = "key rotation" if args.watch_revocation else "key rotation and revocation"
    say(
        f"Live checks passed for {count} grant(s). Rerun after each method toggle, and "
        f"check {later} separately."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
