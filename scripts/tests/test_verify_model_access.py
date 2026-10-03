import base64
import contextlib
import io
import json
import os
import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from scripts import verify_model_access as verifier

TENANT = "11111111-1111-4111-8111-111111111111"
AUDIENCE = "22222222-2222-4222-8222-222222222222"
MODEL_CLIENT = "33333333-3333-4333-8333-333333333333"
APP_CLIENT = "44444444-4444-4444-8444-444444444444"
USER_OID = "55555555-5555-4555-8555-555555555555"
STRANGER_OID = "66666666-6666-4666-8666-666666666666"
APP_OID = "77777777-7777-4777-8777-777777777777"
ADMIN_OID = "88888888-8888-4888-8888-888888888888"
OTHER_OID = "99999999-9999-4999-8999-999999999999"
AGENT_OID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
GROUP_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
APP_SECRET = "fixture-client-secret"
MODEL_OUTPUT = "MODEL-OUTPUT-FIXTURE"
ECHO = "ECHOED-REQUEST-FIXTURE"
ORIGIN = "https://gateway.example"
# The fake clock's wall time when a test starts. Tokens stay valid for two hours from then.
WALL_CLOCK = 1_700_000_000
START = 1_000.0


def jwt(**claims: Any) -> str:
    """A token with these claims. It expires in two hours unless `exp` says otherwise or is None."""
    claims = {"exp": WALL_CLOCK + 7_200, **claims}
    claims = {name: value for name, value in claims.items() if value is not None}

    def encode(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode(claims)}.fixture-signature"


def user_token(oid: str, audience: str = AUDIENCE, **claims: Any) -> str:
    defaults = {"aud": audience, "oid": oid, "tid": TENANT, "scp": "Models.Invoke", "ver": "2.0"}
    return jwt(**{**defaults, **claims})


def app_token(**claims: Any) -> str:
    defaults = {"aud": AUDIENCE, "oid": APP_OID, "tid": TENANT, "ver": "2.0"}
    return jwt(**{**defaults, "roles": ["Models.Invoke.Application"], **claims})


def claims_of(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3 or parts[2] != "fixture-signature":
        return None
    decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    return decoded


def gateway_429(
    limit: str, seconds: int, *, retry_after: bool = True, after_prompt: bool = False
) -> httpx.Response:
    """The 429 that the gateway's own call ("Rate") or token ("Token") limit returns. A token limit
    that refuses a prompt because it would spend more than is left says "will exceed" instead."""
    verb = "will exceed" if after_prompt else "is exceeded"
    return httpx.Response(
        429,
        headers={"Retry-After": str(seconds)} if retry_after else {},
        json={
            "statusCode": 429,
            "message": f"{limit} limit {verb}. Try again in {seconds} seconds.",
        },
    )


USER_CONTROL = jwt(aud="api://mosaic-api", oid=USER_OID, tid=TENANT)
ADMIN_CONTROL = jwt(aud="api://mosaic-api", oid=ADMIN_OID, tid=TENANT)
ENV = {
    verifier.USER_CONTROL_TOKEN: USER_CONTROL,
    verifier.ADMIN_CONTROL_TOKEN: ADMIN_CONTROL,
    verifier.USER_RUNTIME_TOKEN: user_token(USER_OID),
    verifier.APPLICATION_RUNTIME_TOKEN: app_token(),
    verifier.UNGRANTED_USER_RUNTIME_TOKEN: user_token(STRANGER_OID),
    verifier.AGENT_RUNTIME_TOKEN: app_token(oid=AGENT_OID),
    verifier.GROUP_MEMBER_RUNTIME_TOKEN: user_token(USER_OID, groups=[GROUP_ID]),
}
BASE_ARGS = [
    "--api-base-url",
    "https://mosaic.example",
    "--gateway-origin",
    ORIGIN,
    "--api-version",
    "2024-10-21",
    "--models-api-version",
    "2024-05-01-preview",
    "--send-model-requests",
]
# API path, deployment, operation name and operation path, as MOSAIC publishes them.
PUBLICATIONS = {
    "aoai": (
        "/aoai",
        "gpt-4o-mini",
        "chat-completions",
        "/openai/deployments/gpt-4o-mini/chat/completions",
    ),
    "foundry": (
        "/foundry",
        "Llama-3.3-70B-Instruct",
        "chat-completions",
        "/models/chat/completions",
    ),
    "claude": ("/claude", "claude-haiku-4-5", "messages", "/anthropic/v1/messages"),
}


def without(*names: str) -> dict[str, str]:
    return {name: value for name, value in ENV.items() if name not in names}


@dataclass
class FakeGrant:
    id: str
    kind: str
    publication: str
    oid: str
    limits: dict[str, Any] | None = None
    status: str = "applied"
    # When an administrator disables the grant (Revoke in the console) and when it's deleted.
    revoke_at: float | None = None
    deleted_at: float | None = None
    principal_kind: str | None = None
    required_app_role: str = "Models.Invoke.Application"
    keys_available: bool | None = None
    # False until the grant's key is created: applies don't create keys.
    key_exists: bool = True
    calls: dict[str, list[float]] = field(default_factory=dict)
    tokens_used: int = 0

    @property
    def primary(self) -> str:
        return f"primary-key-{self.id}"

    @property
    def secondary(self) -> str:
        return f"secondary-key-{self.id}"


class FakeClock:
    def __init__(self) -> None:
        self.now = START
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return WALL_CLOCK + self.now - START

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeWorld:
    """MOSAIC's control plane, a gateway that applies the governed policy, and Entra sign-in."""

    def __init__(self, *grants: FakeGrant) -> None:
        self.clock = FakeClock()
        self.grants = {grant.id: grant for grant in grants}
        self.methods = {name: {"keys": True, "entra": True} for name in PUBLICATIONS}
        self.model_client: str | None = MODEL_CLIENT
        self.accept_mismatched_credentials = False
        self.split_budget = False
        self.retry_after = True
        self.foreign_reveal_status = 404
        # What MOSAIC wrongly shows the user of grants that other users hold: "list" puts them in
        # the user's lists, "connection" returns their details, "key" reveals their key, "usage"
        # puts them in the user's usage report, and "usage-timeline" only in its timeline.
        self.foreign_user_leaks: set[str] = set()
        # The user's usage report. None serves it, 404 is a MOSAIC from before it, and any other
        # status is an error. A defect breaks its shape: "no-timeline" or "unnamed-row".
        self.usage_status: int | None = None
        self.usage_defect: str | None = None
        self.usage_periods: list[str | None] = []
        # Whether the gateway validates tokens "always", only "without-key" (skipping it when a
        # key comes too), or "never", leaving only the grant lookup to refuse tokens.
        self.token_validation = "always"
        # A gateway that refuses any token sent with a key, before looking either up.
        self.refuse_tokens_with_keys = False
        # A gateway that serves calls with no credential at all.
        self.open_to_anonymous = False
        # Whether a successful call carries the model's reply, or some other body.
        self.model_replies = True
        # Calls the gateway serves before a call limit outside MOSAIC's policy, such as a
        # product's rate limit, refuses them.
        self.gateway_call_limit: int | None = None
        # The model's own token limit, which the gateway applies to each grant.
        # None is what MOSAIC reports for a model its gateway can't token-meter.
        self.publication_limits: dict[str, Any] | None = {
            "counterKeyExpression": "grant",
            "tokensPerMinute": 1000,
        }
        # Model calls the deployments serve before they throttle with their own 429s.
        self.backend_capacity: int | None = None
        # The tokens the gateway estimates a prompt will spend. When set, a token limit refuses a
        # prompt that would spend more than the grant has left, before the window is spent.
        self.prompt_estimate: int | None = None
        # A revocation's timeline: MOSAIC shows it pending, then applies the model's plan. While
        # the plan applies, the gateway refuses every call to the model.
        self.apply_delay = 20.0
        self.apply_seconds = 30.0
        self.revocation_reaches_gateway = True
        # Gateway units that pick up a revocation unevenly: each credential's calls land on two
        # units in turn, and one still has the old policy.
        self.revocation_flaps = False
        self.unit_calls: dict[str, int] = {}
        self.revoked_token_status = 403
        self.drop_entra_on_revoke = False
        self.call_times: list[float] = []
        # For each device sign-in in turn: who signs in, and the poll errors before they finish.
        self.device_logins: list[tuple[str, list[str]]] = []
        self.device_error: dict[str, Any] | None = None
        self.requests: list[httpx.Request] = []
        self.model_calls: list[httpx.Request] = []
        self.login_forms: list[tuple[str, dict[str, str]]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "mosaic.example":
            return self.control(request)
        if request.url.host == "gateway.example":
            self.model_calls.append(request)
            return self.gateway(request)
        if request.url.host == "login.microsoftonline.com":
            return self.login(request)
        raise AssertionError("unexpected host")

    def control(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1/")
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if path in {"me/entitlements", "portal/entitlements"}:
            if token != USER_CONTROL:
                return httpx.Response(401)
            listed = [
                self.entitlement(grant)
                for grant in self.grants.values()
                if not self.deleted(grant)
                and grant.kind == "user"
                and (grant.oid == USER_OID or "list" in self.foreign_user_leaks)
            ]
            if path.startswith("portal/"):
                return httpx.Response(
                    200, json=[{"entitlement": e, "via": "direct"} for e in listed]
                )
            return httpx.Response(200, json=listed)
        if path == "me/usage":
            if token != USER_CONTROL:
                return httpx.Response(401)
            if self.usage_status is not None:
                return httpx.Response(self.usage_status)
            self.usage_periods.append(request.url.params.get("period"))
            return httpx.Response(200, json=self.usage())
        if path.startswith("principals/"):
            if token != ADMIN_CONTROL:
                return httpx.Response(401)
            oid = path.removeprefix("principals/principal-")
            kinds = {grant.oid: grant.kind for grant in self.grants.values()}
            if oid not in kinds:
                return httpx.Response(404)
            return httpx.Response(
                200, json={"id": f"principal-{oid}", "objectId": oid, "kind": kinds[oid]}
            )
        parts = path.split("/")
        mine = parts[0] == "me"
        parts = parts[1:] if mine else parts
        grant = self.grants.get(parts[1]) if len(parts) > 1 else None
        if grant is not None and self.deleted(grant):
            grant = None
        foreign = grant is not None and grant.kind == "user" and grant.oid != USER_OID
        if mine and token == USER_CONTROL:
            visible = grant is not None and grant.kind == "user" and grant.oid == USER_OID
        elif not mine and token == ADMIN_CONTROL:
            visible = grant is not None
        else:
            return httpx.Response(401)
        if grant is not None and visible and parts[2:] == ["keys"] and request.method == "POST":
            if grant.key_exists:
                return httpx.Response(409)
            grant.key_exists = True
            return httpx.Response(201, json={"entitlementId": grant.id, "exists": True})
        if grant is not None and parts[2:] == ["keys", "reveal"]:
            if visible and not grant.key_exists:
                return httpx.Response(409)
            if not mine and grant.kind == "securityGroup":
                return httpx.Response(409)
            if mine and grant.kind == "application":
                return httpx.Response(self.foreign_reveal_status, json={"key": grant.primary})
            if mine and foreign and "key" in self.foreign_user_leaks:
                visible = True
            if not visible:
                return httpx.Response(404)
            slot = json.loads(request.content)["slot"]
            key = grant.primary if slot == "primary" else grant.secondary
            return httpx.Response(200, json={"key": key}, headers={"Cache-Control": "no-store"})
        if mine and foreign and "connection" in self.foreign_user_leaks:
            visible = True
        if grant is not None and visible and parts[2:] == ["connection"]:
            return httpx.Response(200, json=self.connection(grant))
        if grant is not None and visible and not mine and len(parts) == 2:
            return httpx.Response(200, json=self.entitlement(grant))
        return httpx.Response(404)

    def entitlement(self, grant: FakeGrant) -> dict[str, Any]:
        return {
            "id": grant.id,
            "subject": {"kind": grant.kind, "id": f"principal-{grant.oid}"},
            "resource": {"kind": "modelApi", "id": grant.publication},
        }

    def usage(self) -> dict[str, Any]:
        """The user's usage report: a row and a day for each grant the user holds."""
        held = [
            grant
            for grant in self.grants.values()
            if not self.deleted(grant) and grant.kind == "user"
        ]
        own = [grant.id for grant in held if grant.oid == USER_OID]
        foreign = [grant.id for grant in held if grant.oid != USER_OID]
        rows = [*own, *(foreign if "usage" in self.foreign_user_leaks else [])]
        days = [*rows, *(foreign if "usage-timeline" in self.foreign_user_leaks else [])]
        report: dict[str, Any] = {
            "dataSource": "simulated",
            "byResource": [{"entitlementId": identifier, "requests": 3} for identifier in rows],
            "timeline": [
                {"date": "2026-09-29", "entitlementId": identifier, "requests": 3}
                for identifier in days
            ],
            "notes": ["Figures are simulated."],
        }
        if self.usage_defect == "no-timeline":
            del report["timeline"]
        elif self.usage_defect == "unnamed-row":
            report["byResource"].append({"requests": 1})
        return report

    def deleted(self, grant: FakeGrant) -> bool:
        return grant.deleted_at is not None and self.clock.now >= grant.deleted_at

    def phase(self, grant: FakeGrant) -> str:
        """The runtime status MOSAIC reports for the grant now."""
        if grant.revoke_at is None or self.clock.now < grant.revoke_at:
            return grant.status
        elapsed = self.clock.now - grant.revoke_at
        if elapsed < self.apply_delay:
            return "revocationPending"
        if elapsed < self.apply_delay + self.apply_seconds:
            return "applying"
        return "revoked"

    def entra_applied(self, name: str) -> bool:
        revoked = any(
            self.phase(grant) == "revoked"
            for grant in self.grants.values()
            if grant.publication == name
        )
        return self.methods[name]["entra"] and not (revoked and self.drop_entra_on_revoke)

    def connection(self, grant: FakeGrant) -> dict[str, Any]:
        path, deployment, operation, operation_path = PUBLICATIONS[grant.publication]
        methods = self.methods[grant.publication]
        user = grant.kind == "user"
        keys_enabled = methods["keys"] and grant.kind != "securityGroup"
        principal_kind = grant.principal_kind or grant.kind
        keys_available = grant.keys_available
        if keys_available is None:
            keys_available = keys_enabled
        return {
            "entitlementId": grant.id,
            "publicationId": grant.publication,
            "principalKind": principal_kind,
            "gatewayId": "gateway",
            "endpoint": f"{ORIGIN}{path}",
            "deploymentName": deployment,
            "tenantId": TENANT,
            "runtime": {"publicationId": grant.publication, "status": self.phase(grant)},
            "appliedMethods": {
                "keysEnabled": keys_enabled,
                "entraEnabled": self.entra_applied(grant.publication),
            },
            "keysAvailable": keys_available,
            "keyExists": grant.key_exists,
            "entraAudience": AUDIENCE,
            "entraScope": f"api://{AUDIENCE}/{'Models.Invoke' if user else '.default'}",
            "entraClientId": self.model_client if user else None,
            "requiredAppRole": grant.required_app_role,
            "subscriptionHeader": verifier.SUBSCRIPTION_HEADER,
            "operations": [{"name": operation, "method": "POST", "path": operation_path}],
            "publicationLimits": self.publication_limits,
            "grantLimits": grant.limits,
        }

    def gateway(self, request: httpx.Request) -> httpx.Response:
        name = next(
            (key for key, spec in PUBLICATIONS.items() if request.url.path == spec[0] + spec[3]),
            None,
        )
        if name is None:
            return httpx.Response(404)
        self.call_times.append(self.clock.now)
        _, deployment, operation, _ = PUBLICATIONS[name]
        methods = self.methods[name]
        mine = [grant for grant in self.grants.values() if grant.publication == name]
        if any(self.phase(grant) == "applying" for grant in mine):
            return httpx.Response(403, json={"statusCode": 403, "message": "Access unavailable"})
        key = request.headers.get(verifier.SUBSCRIPTION_HEADER)
        authorization = request.headers.get("Authorization")
        stale = not self.revocation_reaches_gateway
        if self.revocation_flaps:
            credential = key or authorization or ""
            self.unit_calls[credential] = self.unit_calls.get(credential, 0) + 1
            stale = self.unit_calls[credential] % 2 == 0
        live = [
            grant
            for grant in mine
            if not self.deleted(grant) and (self.phase(grant) != "revoked" or stale)
        ]
        if key is None and authorization is None:
            return self.reply(operation) if self.open_to_anonymous else httpx.Response(401)
        key_grant = token_grant = None
        if key is not None:
            if not methods["keys"]:
                return httpx.Response(401)
            key_grant = next((g for g in live if key in (g.primary, g.secondary)), None)
            if key_grant is None:
                return httpx.Response(401)
        if authorization is not None:
            if not self.entra_applied(name):
                return httpx.Response(401)
            if key is not None and self.refuse_tokens_with_keys:
                return httpx.Response(401)
            claims = claims_of(authorization.removeprefix("Bearer "))
            validates = self.token_validation == "always" or (
                self.token_validation == "without-key" and key is None
            )
            if validates and (
                claims is None
                or claims.get("aud") != AUDIENCE
                or claims.get("ver") != "2.0"
                or claims.get("exp", 0) <= self.clock.time()
            ):
                return httpx.Response(401)
            claims = claims or {}
            if "Models.Invoke" in str(claims.get("scp", "")).split():
                kind = "user"
            elif "Models.Invoke.Application" in claims.get("roles", []):
                kind = "application"
            else:
                return httpx.Response(403)
            if kind == "user":
                groups = claims.get("groups")
                token_grant = next(
                    (
                        g
                        for g in live
                        if (g.oid == claims.get("oid") and g.kind == "user")
                        or (
                            g.kind == "securityGroup"
                            and isinstance(groups, list)
                            and g.oid in groups
                        )
                    ),
                    None,
                )
            else:
                token_grant = next(
                    (
                        g
                        for g in live
                        if g.oid == claims.get("oid") and g.kind in {"application", "agentIdentity"}
                    ),
                    None,
                )
            if token_grant is None:
                revoked = any(
                    g.oid == claims.get("oid") and g.kind == kind and self.phase(g) == "revoked"
                    for g in mine
                )
                return httpx.Response(self.revoked_token_status if revoked else 403)
        if (
            key_grant is not None
            and token_grant is not None
            and key_grant is not token_grant
            and not self.accept_mismatched_credentials
        ):
            return httpx.Response(403)
        grant = key_grant or token_grant
        assert grant is not None
        body = json.loads(request.content)
        if name != "aoai" and body.get("model") != deployment:
            return httpx.Response(403)
        if operation == "messages":
            assert "api-version" not in request.url.params
            assert request.headers.get("anthropic-version") == verifier.ANTHROPIC_VERSION
        else:
            assert request.url.params.get("api-version")
        limits = grant.limits or {}
        if self.gateway_call_limit is not None:
            if self.gateway_call_limit <= 0:
                return gateway_429("Rate", 60)
            self.gateway_call_limit -= 1
        if requests := limits.get("requests"):
            counter = (key or authorization or "") if self.split_budget else "grant"
            window = [
                at
                for at in grant.calls.get(counter, [])
                if self.clock.now - at < requests["renewalPeriodSeconds"]
            ]
            if len(window) >= requests["calls"]:
                return gateway_429("Rate", 300)
            grant.calls[counter] = [*window, self.clock.now]
        if tokens := limits.get("tokens"):
            remaining = tokens["tokensPerMinute"] - grant.tokens_used
            if remaining <= 0:
                return gateway_429("Token", 60, retry_after=self.retry_after)
            if self.prompt_estimate is not None and self.prompt_estimate > remaining:
                return gateway_429("Token", 60, retry_after=self.retry_after, after_prompt=True)
        if self.backend_capacity is not None:
            if self.backend_capacity <= 0:
                # The deployment's own 429 passes through the gateway unchanged.
                return httpx.Response(
                    429,
                    headers={"Retry-After": "10"},
                    json={"error": {"code": "429", "message": ECHO}},
                )
            self.backend_capacity -= 1
        if tokens:
            grant.tokens_used += 20
        return self.reply(operation)

    def reply(self, operation: str) -> httpx.Response:
        if not self.model_replies:
            return httpx.Response(200, json={"status": "ok"})
        if operation == "messages":
            content = [{"type": "text", "text": MODEL_OUTPUT}]
            return httpx.Response(200, json={"type": "message", "content": content})
        return httpx.Response(200, json={"choices": [{"message": {"content": MODEL_OUTPUT}}]})

    def login(self, request: httpx.Request) -> httpx.Response:
        segments = request.url.path.split("/")
        assert segments[1] == TENANT
        form = {name: values[0] for name, values in parse_qs(request.content.decode()).items()}
        self.login_forms.append((segments[-1], form))
        if segments[-1] == "devicecode":
            number = sum(1 for endpoint, _ in self.login_forms if endpoint == "devicecode")
            return httpx.Response(
                200,
                json={
                    "device_code": f"device-code-{number}",
                    "user_code": f"CODE{number:04d}",
                    "verification_uri": "https://microsoft.com/devicelogin",
                    "expires_in": 900,
                    "interval": 5,
                    "message": ECHO,
                },
            )
        if form["grant_type"] == verifier.DEVICE_CODE_GRANT:
            if self.device_error is not None:
                return httpx.Response(400, json=self.device_error)
            oid, outcomes = self.device_logins[int(form["device_code"].rsplit("-", 1)[1]) - 1]
            if outcomes:
                return httpx.Response(
                    400, json={"error": outcomes.pop(0), "error_description": ECHO}
                )
            return httpx.Response(
                200, json={"access_token": user_token(oid), "token_type": "Bearer"}
            )
        if form["grant_type"] == "client_credentials":
            if form.get("client_id") != APP_CLIENT or form.get("client_secret") != APP_SECRET:
                return httpx.Response(
                    401,
                    json={
                        "error": "invalid_client",
                        "error_description": f"AADSTS7000215: Invalid client secret. {ECHO}",
                        "error_codes": [7000215],
                    },
                )
            return httpx.Response(200, json={"access_token": app_token(), "token_type": "Bearer"})
        raise AssertionError("unexpected sign-in request")


def standard_world() -> FakeWorld:
    return FakeWorld(
        FakeGrant("user-aoai", "user", "aoai", USER_OID),
        FakeGrant("user-foundry", "user", "foundry", USER_OID),
        FakeGrant("user-claude", "user", "claude", USER_OID),
        FakeGrant("app-aoai", "application", "aoai", APP_OID),
    )


def isolation_world() -> FakeWorld:
    """The standard world, plus a grant that another user holds."""
    world = standard_world()
    world.grants["other-aoai"] = FakeGrant("other-aoai", "user", "aoai", OTHER_OID)
    return world


def chat_connection(
    path: str, *, name: str = "chat-completions", deployment: str = "gpt-4o-mini"
) -> dict[str, Any]:
    return {
        "endpoint": f"{ORIGIN}/api",
        "deploymentName": deployment,
        "operations": [{"name": name, "method": "POST", "path": path}],
    }


AOAI_PATH = "/openai/deployments/gpt-4o-mini/chat/completions"


class ModelAccessVerifierTests(unittest.TestCase):
    def verify(
        self,
        world: FakeWorld,
        arguments: list[str],
        env: dict[str, str] | None = None,
        *,
        acknowledge: bool = True,
    ) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        base = [arg for arg in BASE_ARGS if acknowledge or arg != "--send-model-requests"]
        with (
            patch.dict(os.environ, ENV if env is None else env, clear=True),
            patch.object(verifier, "time", world.clock),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = verifier.main([*base, *arguments], transport=world.transport())
        return code, stdout.getvalue(), stderr.getvalue()

    def assert_nothing_secret(self, world: FakeWorld, text: str) -> None:
        secrets = [MODEL_OUTPUT, ECHO, APP_SECRET, *ENV.values()]
        for grant in world.grants.values():
            secrets += [grant.primary, grant.secondary]
        for secret in secrets:
            self.assertNotIn(secret, text)

    def test_gateway_origin_is_explicit_and_https(self) -> None:
        for url in ("http://gateway.example", "https://user:password@gateway.example"):
            with self.assertRaises(verifier.VerificationFailed):
                verifier.https_origin(url)
        connection = {**chat_connection(AOAI_PATH), "endpoint": "https://unexpected.example/model"}
        with self.assertRaises(verifier.VerificationFailed):
            verifier.model_route(connection, "https://approved.example", api_version="test")

    def test_model_route_never_invents_a_provider_operation(self) -> None:
        read_only = [{"name": "chat-completions", "method": "GET", "path": AOAI_PATH}]
        for connection in (
            {**chat_connection(AOAI_PATH), "operations": []},
            {**chat_connection(AOAI_PATH), "operations": read_only},
            chat_connection("/openai/../chat/completions"),
            chat_connection("/openai/deployments/gpt-4o-mini/embeddings", name="embeddings"),
        ):
            with self.assertRaisesRegex(verifier.VerificationFailed, "neither"):
                verifier.model_route(connection, ORIGIN, api_version="test")
        with self.assertRaisesRegex(verifier.VerificationFailed, "doesn't recognize"):
            verifier.model_route(chat_connection("/v2/chat/completions"), ORIGIN, api_version="t")

    def test_model_route_follows_the_published_operation(self) -> None:
        prompt = [{"role": "user", "content": "Reply with OK."}]
        aoai = verifier.model_route(chat_connection(AOAI_PATH), ORIGIN, api_version="2024-10-21")
        self.assertEqual(aoai.url, f"{ORIGIN}/api{AOAI_PATH}?api-version=2024-10-21")
        self.assertEqual(
            aoai.payload,
            {
                "model": "gpt-4o-mini",
                "messages": prompt,
                "max_completion_tokens": 8,
                "stream": False,
            },
        )
        self.assertEqual(aoai.headers, {})
        foundry = verifier.model_route(
            chat_connection("/models/chat/completions", deployment="Llama-3.3-70B-Instruct"),
            ORIGIN,
            api_version=None,
            models_api_version="2024-05-01-preview",
        )
        self.assertEqual(
            foundry.url, f"{ORIGIN}/api/models/chat/completions?api-version=2024-05-01-preview"
        )
        self.assertEqual(foundry.payload["model"], "Llama-3.3-70B-Instruct")
        self.assertEqual(foundry.payload["max_tokens"], 8)
        older = verifier.model_route(
            chat_connection(AOAI_PATH),
            ORIGIN,
            api_version="2024-06-01",
            token_parameter="max_tokens",
        )
        self.assertEqual(older.payload["max_tokens"], 8)
        self.assertNotIn("max_completion_tokens", older.payload)
        claude = verifier.model_route(
            chat_connection(
                "/anthropic/v1/messages", name="messages", deployment="claude-haiku-4-5"
            ),
            ORIGIN,
            api_version=None,
        )
        self.assertEqual(claude.url, f"{ORIGIN}/api/anthropic/v1/messages")
        self.assertEqual(claude.headers, {"anthropic-version": "2023-06-01"})
        self.assertEqual(
            claude.payload, {"model": "claude-haiku-4-5", "messages": prompt, "max_tokens": 8}
        )
        self.assertTrue(claude.reached_model({"type": "message", "content": []}))
        self.assertFalse(claude.reached_model({"choices": [{}]}))
        self.assertTrue(aoai.reached_model({"choices": [{}]}))
        self.assertFalse(aoai.reached_model({"choices": []}))

    def test_model_route_needs_the_route_s_api_version(self) -> None:
        with self.assertRaisesRegex(verifier.VerificationFailed, "Supply --api-version"):
            verifier.model_route(chat_connection(AOAI_PATH), ORIGIN, api_version=None)
        with self.assertRaisesRegex(verifier.VerificationFailed, "Supply --models-api-version"):
            verifier.model_route(
                chat_connection("/models/chat/completions"), ORIGIN, api_version="2024-10-21"
            )

    def test_custom_payload_names_each_grant_s_deployment(self) -> None:
        custom = '{"model": "other", "messages": [], "max_tokens": 4}'
        with patch.dict(os.environ, {verifier.PAYLOAD: custom}, clear=True):
            payload = verifier.custom_payload()
        route = verifier.model_route(
            chat_connection("/models/chat/completions", deployment="Llama-3.3-70B-Instruct"),
            ORIGIN,
            api_version=None,
            models_api_version="2024-05-01-preview",
            payload=payload,
        )
        self.assertEqual(
            route.payload, {"model": "Llama-3.3-70B-Instruct", "messages": [], "max_tokens": 4}
        )
        for invalid in ("[]", "not json", '{"stream": true}'):
            with (
                patch.dict(os.environ, {verifier.PAYLOAD: invalid}, clear=True),
                self.assertRaises(verifier.VerificationFailed),
            ):
                verifier.custom_payload()

    def test_failure_never_echoes_an_error_body(self) -> None:
        response = httpx.Response(500, json={"error": "sensitive-fixture-value"})
        with self.assertRaises(verifier.VerificationFailed) as raised:
            verifier.expect(response, {200}, "Read")
        self.assertNotIn("sensitive-fixture-value", str(raised.exception))

    def test_key_retrieval_requires_no_store(self) -> None:
        with (
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, json={"key": "fixture-key"})
                )
            ) as client,
            self.assertRaises(verifier.VerificationFailed),
        ):
            verifier.reveal(
                client, "https://mosaic.example/api/v1", "fixture-token", "grant", "primary"
            )

    def test_missing_credentials_stop_before_network(self) -> None:
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stderr(output):
            result = verifier.main(
                [
                    "--api-base-url",
                    "https://mosaic.example",
                    "--gateway-origin",
                    "https://gateway.example",
                    "--user-entitlement",
                    "user-grant",
                    "--application-entitlement",
                    "application-grant",
                    "--api-version",
                    "test",
                    "--send-model-requests",
                ]
            )
        self.assertEqual(result, 1)
        self.assertIn("MOSAIC_SMOKE_USER_CONTROL_TOKEN", output.getvalue())

    def test_full_run_checks_every_grant_without_printing_secrets(self) -> None:
        world = standard_world()
        code, out, err = self.verify(
            world,
            [
                *("--user-entitlement", "user-aoai", "--user-entitlement", "user-foundry"),
                *("--user-entitlement", "user-claude", "--application-entitlement", "app-aoai"),
                "--check-ungranted-user",
            ],
        )
        self.assertEqual(code, 0, err)
        for label in (
            "User grant 1 (gpt-4o-mini)",
            "User grant 2 (Llama-3.3-70B-Instruct)",
            "User grant 3 (claude-haiku-4-5)",
            "Application grant 1 (gpt-4o-mini)",
        ):
            self.assertIn(f"PASS: {label} reached the model with its key", out)
            self.assertIn(f"PASS: {label} reached the model with its Entra token", out)
        self.assertIn(
            "PASS: User grant 1 (gpt-4o-mini) rejected an anonymous call, an invalid key, a "
            "MOSAIC control-plane token, an invalid token with a valid key, the application's "
            "token with this grant's key, the ungranted user's token",
            out,
        )
        self.assertIn("PASS: the end user can't retrieve an application grant's key", out)
        self.assertIn("Live checks passed for 4 grant(s)", out)
        self.assertNotIn("SKIP", out)
        self.assert_nothing_secret(world, out + err)

    def test_a_grant_without_a_key_gets_one_before_its_key_is_read(self) -> None:
        world = standard_world()
        for grant_id in ("user-aoai", "app-aoai"):
            world.grants[grant_id].key_exists = False
        code, out, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--application-entitlement", "app-aoai"]
        )
        self.assertEqual(code, 0, err)
        created = [
            (request.method, request.url.path)
            for request in world.requests
            if request.url.path.endswith("/keys")
        ]
        self.assertEqual(
            created,
            [
                ("POST", "/api/v1/me/entitlements/user-aoai/keys"),
                ("POST", "/api/v1/entitlements/app-aoai/keys"),
            ],
        )
        self.assertIn("INFO: User grant 1 (gpt-4o-mini) had no key, so the check created one", out)
        self.assertIn("PASS: User grant 1 (gpt-4o-mini) reached the model with its key", out)
        self.assert_nothing_secret(world, out + err)

    def test_a_cost_center_without_keys_leaves_the_grant_to_its_token(self) -> None:
        world = standard_world()
        connection = {
            **world.connection(world.grants["user-aoai"]),
            "keysAllowedByCostCenter": False,
        }
        grant = verifier.grant_from(
            connection,
            kind="user",
            entitlement_id="user-aoai",
            label="User grant 1",
            control_token="fixture-token",
            options=verifier.Options(
                origin=ORIGIN,
                api_version="2024-10-21",
                models_api_version=None,
                token_parameter=None,
                payload=None,
                proof=None,
            ),
        )
        self.assertFalse(grant.keys_enabled)

    def test_a_gateway_that_accepts_another_subject_s_token_fails(self) -> None:
        world = standard_world()
        world.accept_mismatched_credentials = True
        code, _, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--application-entitlement", "app-aoai"]
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (gpt-4o-mini) rejecting the application's token with this grant's key: "
            "unexpected HTTP 200",
            err,
        )

    def test_anonymous_access_and_replies_without_a_model_fail(self) -> None:
        world = standard_world()
        world.open_to_anonymous = True
        code, _, err = self.verify(world, ["--user-entitlement", "user-aoai"])
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (gpt-4o-mini) rejecting an anonymous call: unexpected HTTP 200", err
        )

        # A success that isn't the model's reply, such as a mocked response, proves nothing.
        world = standard_world()
        world.model_replies = False
        code, _, err = self.verify(world, ["--user-entitlement", "user-aoai"])
        self.assertEqual(code, 1)
        self.assertIn("User grant 1 (gpt-4o-mini): the key call returned no model response", err)

    def test_an_end_user_must_not_read_an_application_key(self) -> None:
        world = standard_world()
        world.foreign_reveal_status = 200
        code, out, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--application-entitlement", "app-aoai"]
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "retrieving Application grant 1 (gpt-4o-mini)'s key: unexpected HTTP 200", err
        )
        self.assertEqual(world.model_calls, [])
        self.assert_nothing_secret(world, out + err)

    def test_another_user_s_grant_stays_out_of_the_user_s_reach(self) -> None:
        world = isolation_world()
        code, out, err = self.verify(
            world, ["--foreign-user-entitlement", "other-aoai"], acknowledge=False
        )
        self.assertEqual(code, 0, err)
        self.assertIn(
            "PASS: the user can't list, read or retrieve the key of 1 grant(s) held by someone "
            "else",
            out,
        )
        self.assertIn("Isolation checks passed for 1 grant(s) held by someone else.", out)
        self.assertIn(
            "PASS: the user's usage report leaves out 1 grant(s) held by someone else", out
        )
        self.assertEqual(world.usage_periods, ["90d"])
        # The admin confirms the grant is real and someone else's before the user's refusals count.
        calls = [
            (
                request.method,
                request.url.path,
                request.headers["Authorization"].removeprefix("Bearer "),
            )
            for request in world.requests
        ]
        self.assertEqual(
            calls,
            [
                ("GET", "/api/v1/entitlements/other-aoai", ADMIN_CONTROL),
                ("GET", f"/api/v1/principals/principal-{OTHER_OID}", ADMIN_CONTROL),
                ("GET", "/api/v1/me/entitlements", USER_CONTROL),
                ("GET", "/api/v1/portal/entitlements", USER_CONTROL),
                ("GET", "/api/v1/me/usage", USER_CONTROL),
                ("GET", "/api/v1/me/entitlements/other-aoai/connection", USER_CONTROL),
                ("POST", "/api/v1/me/entitlements/other-aoai/keys/reveal", USER_CONTROL),
            ],
        )
        self.assert_nothing_secret(world, out + err)

    def test_isolation_is_checked_before_any_model_call(self) -> None:
        world = isolation_world()
        code, out, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--foreign-user-entitlement", "other-aoai"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("held by someone else", out)
        self.assertIn("Live checks passed for 1 grant(s)", out)
        paths = [request.url.path for request in world.requests]
        first_model_call = world.requests.index(world.model_calls[0])
        self.assertLess(
            paths.index("/api/v1/me/entitlements/other-aoai/keys/reveal"), first_model_call
        )

    def test_another_user_s_grant_that_leaks_fails_before_model_calls(self) -> None:
        for leak, message in (
            ("list", "MOSAIC lists Another person's grant 1 among the user's own grants"),
            (
                "connection",
                "The user reading Another person's grant 1's connection details: unexpected "
                "HTTP 200",
            ),
            ("key", "The user retrieving Another person's grant 1's key: unexpected HTTP 200"),
            ("usage", "The user's usage report includes Another person's grant 1"),
            ("usage-timeline", "The user's usage report includes Another person's grant 1"),
        ):
            with self.subTest(leak=leak):
                world = isolation_world()
                world.foreign_user_leaks = {leak}
                code, out, err = self.verify(
                    world,
                    ["--user-entitlement", "user-aoai", "--foreign-user-entitlement", "other-aoai"],
                )
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertEqual(world.model_calls, [])
                self.assert_nothing_secret(world, out + err)

    def test_isolation_skips_a_usage_report_this_mosaic_does_not_have(self) -> None:
        world = isolation_world()
        world.usage_status = 404
        code, out, err = self.verify(
            world, ["--foreign-user-entitlement", "other-aoai"], acknowledge=False
        )
        self.assertEqual(code, 0, err)
        self.assertIn("SKIP: the user's usage report: this MOSAIC has none", out)
        self.assertNotIn("PASS: the user's usage report", out)
        self.assertIn("Isolation checks passed for 1 grant(s) held by someone else.", out)

    def test_a_broken_usage_report_fails_before_model_calls(self) -> None:
        for status, defect, message in (
            (500, None, "The user's usage report: unexpected HTTP 500"),
            (403, None, "The user's usage report: unexpected HTTP 403"),
            (None, "no-timeline", "The user's usage report has no timeline list"),
            (None, "unnamed-row", "The user's usage report has a byResource row with no grant"),
        ):
            with self.subTest(status=status, defect=defect):
                world = isolation_world()
                world.usage_status = status
                world.usage_defect = defect
                code, out, err = self.verify(
                    world,
                    ["--user-entitlement", "user-aoai", "--foreign-user-entitlement", "other-aoai"],
                )
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertEqual(world.model_calls, [])
                self.assert_nothing_secret(world, out + err)

    def test_isolation_needs_a_real_grant_that_someone_else_holds(self) -> None:
        for identifier, message in (
            ("missing", "The admin reading Another person's grant 1: unexpected HTTP 404"),
            (
                "user-aoai",
                "Another person's grant 1 is held by the user. Name a grant that someone else "
                "holds",
            ),
            ("app-aoai", "Another person's grant 1 isn't a user grant"),
        ):
            with self.subTest(identifier=identifier):
                world = isolation_world()
                code, _, err = self.verify(
                    world, ["--foreign-user-entitlement", identifier], acknowledge=False
                )
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertFalse(
                    [r for r in world.requests if r.url.path.startswith("/api/v1/me/")]
                )

    def test_isolation_needs_both_control_tokens_and_no_acknowledgement(self) -> None:
        for missing in (verifier.USER_CONTROL_TOKEN, verifier.ADMIN_CONTROL_TOKEN):
            with self.subTest(missing=missing):
                world = isolation_world()
                code, _, err = self.verify(
                    world,
                    ["--foreign-user-entitlement", "other-aoai"],
                    without(missing),
                    acknowledge=False,
                )
                self.assertEqual(code, 1)
                self.assertIn(missing, err)
                self.assertEqual(world.requests, [])
        # Only a run that sends model requests needs the acknowledgement.
        world = isolation_world()
        code, _, err = self.verify(
            world,
            ["--user-entitlement", "user-aoai", "--foreign-user-entitlement", "other-aoai"],
            acknowledge=False,
        )
        self.assertEqual(code, 1)
        self.assertIn("Add --send-model-requests", err)
        self.assertEqual(world.requests, [])

    def test_unapplied_access_stops_before_model_calls(self) -> None:
        world = standard_world()
        world.grants["user-foundry"].status = "revocationPending"
        code, _, err = self.verify(world, ["--user-entitlement", "user-foundry"])
        self.assertEqual(code, 1)
        self.assertIn("User grant 1 (Llama-3.3-70B-Instruct): Access must be applied", err)
        self.assertIn("(status: revocationPending)", err)
        self.assertEqual(world.model_calls, [])

    def test_device_code_sign_in_asks_only_for_the_model_scope(self) -> None:
        world = standard_world()
        world.device_logins = [(USER_OID, ["authorization_pending", "slow_down"])]
        code, out, err = self.verify(
            world,
            [
                *("--user-entitlement", "user-aoai", "--user-entitlement", "user-foundry"),
                *("--user-token-source", "device-code"),
            ],
            without(verifier.USER_RUNTIME_TOKEN),
        )
        self.assertEqual(code, 0, err)
        starts = [form for endpoint, form in world.login_forms if endpoint == "devicecode"]
        self.assertEqual(
            starts, [{"client_id": MODEL_CLIENT, "scope": f"api://{AUDIENCE}/Models.Invoke"}]
        )
        self.assertEqual(world.clock.sleeps, [5, 5, 10])
        self.assertIn(
            "SIGN IN as the user who holds these grants: open https://microsoft.com/devicelogin "
            "and enter the code CODE0001",
            err,
        )
        self.assertIn(
            "PASS: User grant 2 (Llama-3.3-70B-Instruct) reached the model with its Entra token",
            out,
        )
        self.assert_nothing_secret(world, out + err)

    def test_device_code_sign_in_as_another_user_fails(self) -> None:
        world = standard_world()
        world.device_logins = [(STRANGER_OID, [])]
        code, _, err = self.verify(
            world,
            ["--user-entitlement", "user-aoai", "--user-token-source", "device-code"],
            without(verifier.USER_RUNTIME_TOKEN),
        )
        self.assertEqual(code, 1)
        self.assertIn("belongs to a different account", err)
        self.assertEqual(world.model_calls, [])

    def test_device_code_needs_a_model_client(self) -> None:
        world = standard_world()
        world.model_client = None
        code, _, err = self.verify(
            world,
            ["--user-entitlement", "user-aoai", "--user-token-source", "device-code"],
            without(verifier.USER_RUNTIME_TOKEN),
        )
        self.assertEqual(code, 1)
        self.assertIn("names no model client for this grant's audience", err)
        self.assertIn(verifier.USER_RUNTIME_TOKEN, err)
        self.assertEqual(world.login_forms, [])
        self.assertEqual(world.model_calls, [])

    def test_sign_in_failures_report_codes_only(self) -> None:
        world = standard_world()
        world.device_error = {
            "error": "invalid_grant",
            "error_description": f"AADSTS50105: The signed in user is not assigned. {ECHO}",
            "error_codes": [50105],
        }
        code, out, err = self.verify(
            world,
            ["--user-entitlement", "user-aoai", "--user-token-source", "device-code"],
            without(verifier.USER_RUNTIME_TOKEN),
        )
        self.assertEqual(code, 1)
        self.assertIn("failed: invalid_grant, AADSTS50105. See docs/call-models-with-entra", err)
        self.assert_nothing_secret(world, out + err)

    def test_ungranted_user_signs_in_separately_and_is_rejected(self) -> None:
        world = standard_world()
        world.device_logins = [(USER_OID, []), (STRANGER_OID, [])]
        code, out, err = self.verify(
            world,
            [
                *("--user-entitlement", "user-aoai", "--user-token-source", "device-code"),
                "--check-ungranted-user",
            ],
            without(verifier.USER_RUNTIME_TOKEN, verifier.UNGRANTED_USER_RUNTIME_TOKEN),
        )
        self.assertEqual(code, 0, err)
        self.assertIn("SIGN IN as a different user, one with no grant for these models", err)
        self.assertIn("the ungranted user's token", out)
        rejected = [
            call
            for call in world.model_calls
            if call.headers.get("Authorization") == f"Bearer {user_token(STRANGER_OID)}"
        ]
        self.assertEqual(len(rejected), 1)

    def test_ungranted_user_must_be_someone_else(self) -> None:
        world = standard_world()
        env = {**ENV, verifier.UNGRANTED_USER_RUNTIME_TOKEN: user_token(USER_OID, "other")}
        code, _, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--check-ungranted-user"], env
        )
        self.assertEqual(code, 1)
        self.assertIn("Sign in as a different user", err)
        self.assertEqual(world.model_calls, [])

    def test_client_credentials_sign_in_the_application(self) -> None:
        env = {
            **without(verifier.APPLICATION_RUNTIME_TOKEN),
            verifier.APPLICATION_CLIENT_ID: APP_CLIENT,
            verifier.APPLICATION_CLIENT_SECRET: APP_SECRET,
        }
        arguments = [
            *("--application-entitlement", "app-aoai"),
            *("--application-token-source", "client-credentials"),
        ]
        world = standard_world()
        code, out, err = self.verify(world, arguments, env)
        self.assertEqual(code, 0, err)
        self.assertEqual(
            [form for endpoint, form in world.login_forms if endpoint == "token"],
            [
                {
                    "grant_type": "client_credentials",
                    "client_id": APP_CLIENT,
                    "client_secret": APP_SECRET,
                    "scope": f"api://{AUDIENCE}/.default",
                }
            ],
        )
        self.assertIn(
            "Application grant 1 (gpt-4o-mini) reached the model with its Entra token", out
        )
        self.assert_nothing_secret(world, out + err)

        world = standard_world()
        wrong = {**env, verifier.APPLICATION_CLIENT_SECRET: "wrong-fixture-secret"}
        code, out, err = self.verify(world, arguments, wrong)
        self.assertEqual(code, 1)
        self.assertIn("Signing in the application failed: invalid_client, AADSTS7000215", err)
        self.assertNotIn("wrong-fixture-secret", out + err)
        self.assert_nothing_secret(world, out + err)

        world = standard_world()
        missing = {k: v for k, v in env.items() if k != verifier.APPLICATION_CLIENT_SECRET}
        code, _, err = self.verify(world, arguments, missing)
        self.assertEqual(code, 1)
        self.assertIn(verifier.APPLICATION_CLIENT_SECRET, err)
        self.assertEqual(world.requests, [])

    def test_application_only_run_skips_checks_that_need_the_user(self) -> None:
        world = standard_world()
        env = without(verifier.USER_CONTROL_TOKEN, verifier.USER_RUNTIME_TOKEN)
        code, out, err = self.verify(world, ["--application-entitlement", "app-aoai"], env)
        self.assertEqual(code, 0, err)
        self.assertIn(
            f"SKIP: an end user retrieving an application's key: set {verifier.USER_CONTROL_TOKEN}",
            out,
        )
        self.assertIn(
            "SKIP: Application grant 1 (gpt-4o-mini) the user's token with this grant's key: "
            "no user token in this run",
            out,
        )
        self.assertIn(
            "Application grant 1 (gpt-4o-mini) reached the model with its Entra token", out
        )
        self.assert_nothing_secret(world, out + err)

    def test_runtime_tokens_are_checked_before_model_calls(self) -> None:
        unscoped = user_token(USER_OID, scp="User.Read")
        for name, token, message in (
            (verifier.USER_RUNTIME_TOKEN, user_token(USER_OID, "other"), "different audience"),
            (verifier.USER_RUNTIME_TOKEN, unscoped, "lacks the Models.Invoke scope"),
            (
                verifier.USER_RUNTIME_TOKEN,
                user_token(USER_OID, ver="1.0"),
                "the user token isn't an Entra version 2.0 token",
            ),
            (verifier.USER_RUNTIME_TOKEN, user_token(STRANGER_OID), "a different account"),
            (verifier.APPLICATION_RUNTIME_TOKEN, user_token(APP_OID), "Models.Invoke.Application"),
            (
                verifier.APPLICATION_RUNTIME_TOKEN,
                app_token(roles=["Models.Read"]),
                "the application token lacks the Models.Invoke.Application role",
            ),
            (
                verifier.USER_RUNTIME_TOKEN,
                user_token(USER_OID, exp=WALL_CLOCK - 1),
                "the user token has expired. Get a new one",
            ),
            (
                verifier.APPLICATION_RUNTIME_TOKEN,
                app_token(exp=WALL_CLOCK + 30),
                "the application token expires in 30 seconds. Get a new one",
            ),
            (verifier.USER_RUNTIME_TOKEN, user_token(USER_OID, exp=None), "has no expiry time"),
        ):
            with self.subTest(message):
                world = standard_world()
                code, _, err = self.verify(
                    world,
                    ["--user-entitlement", "user-aoai", "--application-entitlement", "app-aoai"],
                    {**ENV, name: token},
                )
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertEqual(world.model_calls, [])

    def test_token_limit_proof(self) -> None:
        limits = {"tokens": {"counterKeyExpression": "grant", "tokensPerMinute": 100}}
        world = FakeWorld(FakeGrant("user-aoai", "user", "aoai", USER_OID, limits=limits))
        world.publication_limits = {"counterKeyExpression": "grant", "tokensPerMinute": 1000}
        code, out, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--prove-token-limit"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn(
            "PASS: User grant 1 (gpt-4o-mini) returned 429 with Retry-After after 4 more call(s) "
            "spent its 100 tokens per minute",
            out,
        )

        world = FakeWorld(FakeGrant("user-aoai", "user", "aoai", USER_OID, limits=limits))
        world.retry_after = False
        code, _, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--prove-token-limit"]
        )
        self.assertEqual(code, 1)
        self.assertIn("had no Retry-After in seconds", err)

        # The gateway refused a prompt that would spend more than the grant had left. That is the
        # grant's token limit too, though its 429 says "will exceed".
        world = FakeWorld(FakeGrant("user-aoai", "user", "aoai", USER_OID, limits=limits))
        world.prompt_estimate = 25
        code, out, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--prove-token-limit"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn(
            "PASS: User grant 1 (gpt-4o-mini) returned 429 with Retry-After after 3 more call(s) "
            "spent its 100 tokens per minute",
            out,
        )

        # The deployment's own 429 says nothing about the grant's limit.
        world = FakeWorld(FakeGrant("user-aoai", "user", "aoai", USER_OID, limits=limits))
        world.backend_capacity = 3
        code, out, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--prove-token-limit"]
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (gpt-4o-mini): the model deployment throttled a call itself, so this "
            "run can't prove the grant's token limit",
            err,
        )
        self.assertNotIn("PASS: User grant 1 (gpt-4o-mini) returned 429", out)
        self.assert_nothing_secret(world, out + err)

        # A call limit outside MOSAIC's policy, such as a product's, refused a call first.
        world = FakeWorld(FakeGrant("user-aoai", "user", "aoai", USER_OID, limits=limits))
        world.gateway_call_limit = 3
        code, out, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--prove-token-limit"]
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (gpt-4o-mini): a call limit refused a call before its token limit did",
            err,
        )
        self.assertNotIn("PASS: User grant 1 (gpt-4o-mini) returned 429", out)

        # The model's own limit for each grant would refuse as soon as the grant's does.
        world = FakeWorld(FakeGrant("user-aoai", "user", "aoai", USER_OID, limits=limits))
        world.publication_limits = {"counterKeyExpression": "grant", "tokensPerMinute": 100}
        code, _, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--prove-token-limit"]
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "needs the model's own limit for each grant (100 tokens per minute) to be higher "
            "than the grant's (100)",
            err,
        )
        self.assertEqual(world.model_calls, [])

        # Claude on a classic tier: its grants can't carry token limits, so there's none to prove.
        world = FakeWorld(FakeGrant("user-claude", "user", "claude", USER_OID))
        world.publication_limits = None
        code, _, err = self.verify(
            world, ["--user-entitlement", "user-claude", "--prove-token-limit"]
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "MOSAIC applies no token limits to this model, because its gateway can't meter them",
            err,
        )
        self.assertEqual(world.model_calls, [])

        for invalid, message in (
            (
                {**limits, "requests": {"calls": 5, "renewalPeriodSeconds": 60}},
                "without call limits",
            ),
            ({"tokens": {"tokensPerMinute": 1000}}, "at most 100 tokens per minute"),
            (None, "at most 100 tokens per minute"),
        ):
            with self.subTest(message):
                world = FakeWorld(FakeGrant("user-aoai", "user", "aoai", USER_OID, limits=invalid))
                code, _, err = self.verify(
                    world, ["--user-entitlement", "user-aoai", "--prove-token-limit"]
                )
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertEqual(world.model_calls, [])

    def test_shared_budget_proof(self) -> None:
        limits = {
            "requests": {"counterKeyExpression": "grant", "calls": 2, "renewalPeriodSeconds": 300}
        }
        world = FakeWorld(FakeGrant("user-aoai", "user", "aoai", USER_OID, limits=limits))
        code, out, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--prove-shared-budget"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn(
            "PASS: User grant 1 (gpt-4o-mini) primary key, Entra token and secondary key share one "
            "budget",
            out,
        )

        world = FakeWorld(FakeGrant("user-aoai", "user", "aoai", USER_OID, limits=limits))
        world.split_budget = True
        code, _, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--prove-shared-budget"]
        )
        self.assertEqual(code, 1)
        self.assertIn("secondary key after the budget was spent: unexpected HTTP 200", err)

        # The gateway let the third call through, and only the deployment's own 429 stopped it.
        world = FakeWorld(FakeGrant("user-aoai", "user", "aoai", USER_OID, limits=limits))
        world.split_budget = True
        world.backend_capacity = 2
        code, out, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--prove-shared-budget"]
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (gpt-4o-mini): the secondary key's call reached the model deployment, "
            "which throttled it itself, so the gateway didn't apply the grant's shared budget",
            err,
        )
        self.assert_nothing_secret(world, out + err)

        # The keys don't share one budget, and only the grant's token limit stopped the third call.
        tokens = {"tokens": {"counterKeyExpression": "grant", "tokensPerMinute": 40}}
        world = FakeWorld(
            FakeGrant("user-aoai", "user", "aoai", USER_OID, limits={**limits, **tokens})
        )
        world.split_budget = True
        code, out, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--prove-shared-budget"]
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (gpt-4o-mini): a token limit refused the secondary key's call, so this "
            "run can't prove the shared call budget",
            err,
        )
        self.assertNotIn("share one budget", out)

        world = FakeWorld(FakeGrant("user-aoai", "user", "aoai", USER_OID))
        code, _, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--prove-shared-budget"]
        )
        self.assertEqual(code, 1)
        self.assertIn("limited to 2 calls per 300 seconds", err)

    def watch(self, world: FakeWorld, timeout: int = 300, **env: str) -> tuple[int, str, str]:
        arguments = [
            *("--user-entitlement", "user-aoai", "--watch-revocation", "user-aoai"),
            *("--revocation-timeout", str(timeout), "--revocation-interval", "30"),
        ]
        return self.verify(world, arguments, {**ENV, **env})

    def test_revocation_watch_waits_for_mosaic_then_the_gateway(self) -> None:
        world = standard_world()
        world.grants["user-aoai"].revoke_at = START + 45
        code, out, err = self.watch(world)
        self.assertEqual(code, 0, err)
        self.assertIn(
            "WAIT: revoke User grant 1 (gpt-4o-mini) in MOSAIC's console, which disables it, and "
            "apply its model's access plan",
            out,
        )
        for status in ("revocationPending", "applying", "revoked"):
            self.assertIn(f"WAIT: MOSAIC reports User grant 1 (gpt-4o-mini) as {status}", out)
        self.assertIn("PASS: User grant 1 (gpt-4o-mini) rejects its key after revocation", out)
        self.assertIn(
            "PASS: User grant 1 (gpt-4o-mini) rejects its Entra token after revocation", out
        )
        self.assertIn("check key rotation separately", out)
        # No model call while the revocation was pending or applying, then two per method.
        self.assertEqual(world.clock.sleeps, [30] * 5)
        self.assertEqual([at for at in world.call_times if at > START], [1120.0] * 2 + [1150.0] * 2)

        world = standard_world()
        world.grants["user-aoai"].revoke_at = START + 45
        world.drop_entra_on_revoke = True
        code, out, err = self.watch(world)
        self.assertEqual(code, 0, err)
        self.assertIn(
            "PASS: User grant 1 (gpt-4o-mini) rejects its Entra token after revocation", out
        )

    def test_revocation_watch_ignores_rejections_it_cannot_attribute(self) -> None:
        # While the plan applies, the gateway refuses every call, whatever the outcome will be.
        world = standard_world()
        world.grants["user-aoai"].revoke_at = START + 45
        world.apply_seconds = 10_000
        code, out, err = self.watch(world, timeout=120)
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (gpt-4o-mini): after 120 seconds, MOSAIC reports it as applying, not "
            "revoked",
            err,
        )
        self.assertNotIn("PASS: User grant 1 (gpt-4o-mini) rejects", out)
        self.assertEqual([at for at in world.call_times if at > START], [])

        world = standard_world()
        world.grants["user-aoai"].revoke_at = START + 45
        world.revocation_reaches_gateway = False
        code, _, err = self.watch(world)
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (gpt-4o-mini): MOSAIC reports it revoked, but after 300 seconds calls "
            "with its key and Entra token weren't rejected 2 times in a row (last: key HTTP 200, "
            "Entra token HTTP 200)",
            err,
        )

        # One of two gateway units still serves the grant, so rejections alternate with successes.
        world = standard_world()
        world.grants["user-aoai"].revoke_at = START + 45
        world.revocation_flaps = True
        code, out, err = self.watch(world)
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (gpt-4o-mini): MOSAIC reports it revoked, but after 300 seconds calls "
            "with its key and Entra token weren't rejected 2 times in a row",
            err,
        )
        self.assertNotIn("PASS: User grant 1 (gpt-4o-mini) rejects", out)

        # With Entra still applied, only the grant lookup's 403 shows the revocation. A 401 means
        # token validation refused the call first.
        world = standard_world()
        world.grants["user-aoai"].revoke_at = START + 45
        world.revoked_token_status = 401
        code, out, err = self.watch(world)
        self.assertEqual(code, 1)
        self.assertIn("PASS: User grant 1 (gpt-4o-mini) rejects its key after revocation", out)
        self.assertIn(
            "calls with its Entra token weren't rejected 2 times in a row "
            "(last: Entra token HTTP 401)",
            err,
        )

    def test_revocation_watch_needs_tokens_that_outlast_it_and_a_disabled_grant(self) -> None:
        world = standard_world()
        short = user_token(USER_OID, exp=WALL_CLOCK + 600)
        code, out, err = self.watch(world, timeout=1200, **{verifier.USER_RUNTIME_TOKEN: short})
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (gpt-4o-mini): the user token expires in 600 seconds, and watching for "
            "revocation can take 1290 seconds. Get a new one, or lower --revocation-timeout",
            err,
        )
        self.assertNotIn("WAIT", out)
        self.assertEqual(world.clock.sleeps, [])

        world = standard_world()
        world.grants["user-aoai"].deleted_at = START + 45
        code, _, err = self.watch(world)
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (gpt-4o-mini): MOSAIC no longer finds this grant, so the watch can't "
            "tell when the gateway applies its removal. Disable a grant (Revoke in the console) "
            "instead of deleting it",
            err,
        )

        world = standard_world()
        code, _, err = self.watch(world, timeout=60)
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (gpt-4o-mini): after 60 seconds, MOSAIC reports it as applied, not "
            "revoked",
            err,
        )

    def test_rejections_must_come_from_the_rule_under_test(self) -> None:
        # Token validation refuses the first two with 401, so a 403 means it was skipped and only
        # the grant lookup refused them. The third passes validation and names another grant than
        # the key does, so a 401 means the gateway never compared them.
        for setting, value, check, status in (
            ("token_validation", "never", "a MOSAIC control-plane token", 403),
            ("token_validation", "without-key", "an invalid token with a valid key", 403),
            (
                "refuse_tokens_with_keys",
                True,
                "the application's token with this grant's key",
                401,
            ),
        ):
            with self.subTest(check):
                world = standard_world()
                setattr(world, setting, value)
                code, _, err = self.verify(world, ["--user-entitlement", "user-aoai"])
                self.assertEqual(code, 1)
                self.assertIn(
                    f"User grant 1 (gpt-4o-mini) rejecting {check}: unexpected HTTP {status}", err
                )

        # Validation would refuse these tokens before the grant rule, so they prove nothing.
        for token, reason in (
            (app_token(exp=WALL_CLOCK - 1), "has expired"),
            (app_token(aud="other"), "is for a different audience from this grant"),
            (app_token(exp=WALL_CLOCK + 30), "expires in 30 seconds"),
        ):
            with self.subTest(reason):
                world = standard_world()
                code, out, err = self.verify(
                    world,
                    ["--user-entitlement", "user-aoai"],
                    {**ENV, verifier.APPLICATION_RUNTIME_TOKEN: token},
                )
                self.assertEqual(code, 0, err)
                self.assertIn(
                    "SKIP: User grant 1 (gpt-4o-mini) the application's token with this grant's "
                    f"key: the application token in this run {reason}",
                    out,
                )

        world = standard_world()
        env = {**ENV, verifier.UNGRANTED_USER_RUNTIME_TOKEN: user_token(STRANGER_OID, exp=1)}
        code, _, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--check-ungranted-user"], env
        )
        self.assertEqual(code, 1)
        self.assertIn("The ungranted user's token has expired. Get a new one", err)
        self.assertEqual(world.model_calls, [])

        # The gateway can't validate this user's token, so its 401 says nothing about grants.
        forged = user_token(STRANGER_OID).rsplit(".", 1)[0] + ".forged-signature"
        world = standard_world()
        env = {**ENV, verifier.UNGRANTED_USER_RUNTIME_TOKEN: forged}
        code, _, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--check-ungranted-user"], env
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (gpt-4o-mini) rejecting the ungranted user's token: unexpected HTTP 401",
            err,
        )

    def test_keys_only_grant_needs_no_runtime_token_and_refuses_tokens(self) -> None:
        world = standard_world()
        world.methods["aoai"]["entra"] = False
        env = {
            verifier.USER_CONTROL_TOKEN: USER_CONTROL,
            verifier.USER_RUNTIME_TOKEN: user_token(USER_OID),
        }
        code, out, err = self.verify(world, ["--user-entitlement", "user-aoai"], env)
        self.assertEqual(code, 0, err)
        self.assertIn("a token while Entra tokens are off", out)
        self.assertIn("PASS: User grant 1 (gpt-4o-mini) reached the model with its key", out)
        self.assertNotIn("with its Entra token", out)
        self.assertIn(
            "SKIP: User grant 1 (gpt-4o-mini) the application's token with this grant's key: "
            "no application token in this run",
            out,
        )

        world = standard_world()
        world.methods["aoai"]["entra"] = False
        env = {verifier.USER_CONTROL_TOKEN: USER_CONTROL}
        code, out, err = self.verify(world, ["--user-entitlement", "user-aoai"], env)
        self.assertEqual(code, 0, err)
        self.assertIn("a token while Entra tokens are off: no user token in this run", out)

        # With Entra tokens off, the gateway refuses every token before looking up a grant.
        world = standard_world()
        world.methods["aoai"]["entra"] = False
        code, out, err = self.verify(
            world, ["--user-entitlement", "user-aoai", "--check-ungranted-user"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn(
            "PASS: User grant 1 (gpt-4o-mini) rejected an anonymous call, an invalid key, a "
            "MOSAIC control-plane token, an invalid token with a valid key, the application's "
            "token with this grant's key, the ungranted user's token, a token while Entra tokens "
            "are off",
            out,
        )

    def test_agent_entitlement_check_reaches_the_model(self) -> None:
        world = FakeWorld(FakeGrant("agent-aoai", "agentIdentity", "aoai", AGENT_OID))
        code, out, err = self.verify(world, ["--agent-entitlement", "agent-aoai"])
        self.assertEqual(code, 0, err)
        self.assertIn("PASS: Agent identity token reached the model", out)
        self.assertIn("Live checks passed for 1 grant(s)", out)
        self.assertEqual(len(world.model_calls), 1)
        self.assert_nothing_secret(world, out + err)

    def test_agent_entitlement_requires_agent_identity_application_role_and_applied_access(
        self,
    ) -> None:
        cases = (
            (
                FakeGrant(
                    "agent-aoai",
                    "agentIdentity",
                    "aoai",
                    AGENT_OID,
                    principal_kind="application",
                ),
                "principalKind was not agentIdentity",
            ),
            (
                FakeGrant(
                    "agent-aoai",
                    "agentIdentity",
                    "aoai",
                    AGENT_OID,
                    required_app_role="Models.Read",
                ),
                "required app role",
            ),
            (
                FakeGrant("agent-aoai", "agentIdentity", "aoai", AGENT_OID, status="applying"),
                "status: applying",
            ),
        )
        for grant, message in cases:
            with self.subTest(message=message):
                world = FakeWorld(grant)
                code, _, err = self.verify(world, ["--agent-entitlement", "agent-aoai"])
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertEqual(world.model_calls, [])

    def test_group_entitlement_warns_refuses_keys_and_reaches_the_model(self) -> None:
        world = FakeWorld(FakeGrant("group-aoai", "securityGroup", "aoai", GROUP_ID))
        code, out, err = self.verify(world, ["--group-entitlement", "group-aoai"])
        self.assertEqual(code, 0, err)
        self.assertIn("PASS: Security-group member token reached the model", out)
        self.assertNotIn("WARN:", err)
        self.assertEqual(
            [
                (request.method, request.url.path)
                for request in world.requests
                if request.url.host == "mosaic.example"
            ],
            [
                ("GET", "/api/v1/entitlements/group-aoai/connection"),
                ("POST", "/api/v1/entitlements/group-aoai/keys/reveal"),
            ],
        )
        self.assertEqual(len(world.model_calls), 1)
        self.assert_nothing_secret(world, out + err)

    def test_group_entitlement_warns_on_missing_groups_claim_and_overage(self) -> None:
        for claims, warning in (
            ({}, "has no groups claim"),
            ({"_claim_names": {"groups": "src1"}}, "signals group overage"),
            ({"hasgroups": True}, "signals group overage"),
            ({"groups:src1": "fixture"}, "signals group overage"),
        ):
            with self.subTest(claims=claims):
                world = FakeWorld(FakeGrant("group-aoai", "securityGroup", "aoai", GROUP_ID))
                token = user_token(USER_OID, groups=[GROUP_ID], **claims)
                if claims == {}:
                    token = user_token(USER_OID)
                code, _, err = self.verify(
                    world,
                    ["--group-entitlement", "group-aoai"],
                    {**ENV, verifier.GROUP_MEMBER_RUNTIME_TOKEN: token},
                )
                if claims != {}:
                    self.assertEqual(code, 0, err)
                self.assertIn(warning, err)

    def test_group_entitlement_requires_no_keys_and_reveal_refusal(self) -> None:
        for keys_available, reveal_status, message in (
            (True, 409, "keysAvailable false"),
            (False, 200, "key reveal refusal"),
        ):
            with self.subTest(message=message):
                grant = FakeGrant(
                    "group-aoai",
                    "securityGroup",
                    "aoai",
                    GROUP_ID,
                    keys_available=keys_available,
                )
                world = FakeWorld(grant)
                if reveal_status == 200:
                    grant.kind = "application"
                    grant.principal_kind = "securityGroup"
                    grant.oid = GROUP_ID
                code, _, err = self.verify(world, ["--group-entitlement", "group-aoai"])
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertEqual(world.model_calls, [])

    def test_agent_and_group_options_are_optional(self) -> None:
        world = standard_world()
        env = without(verifier.AGENT_RUNTIME_TOKEN, verifier.GROUP_MEMBER_RUNTIME_TOKEN)
        code, out, err = self.verify(world, ["--user-entitlement", "user-aoai"], env)
        self.assertEqual(code, 0, err)
        self.assertNotIn("Agent identity", out)
        self.assertNotIn("Security-group", out)

    def test_arguments_are_checked_before_network(self) -> None:
        for message, arguments in (
            ("at least one", []),
            ("only once", ["--user-entitlement", "a", "--application-entitlement", "a"]),
            ("only once", ["--user-entitlement", "a", "--foreign-user-entitlement", "a"]),
            ("not a URL", ["--user-entitlement", "https://mosaic.example/grant"]),
            ("not a URL", ["--user-entitlement", "grant id"]),
            ("not a URL", ["--foreign-user-entitlement", "grant/connection"]),
            (
                "needs a --user-entitlement",
                ["--application-entitlement", "a", "--check-ungranted-user"],
            ),
            (
                "needs a --user-entitlement",
                ["--foreign-user-entitlement", "a", "--check-ungranted-user"],
            ),
            ("A proof needs", ["--foreign-user-entitlement", "a", "--prove-shared-budget"]),
            (
                "must name a --user-entitlement or --application-entitlement",
                ["--user-entitlement", "a", "--watch-revocation", "b"],
            ),
            (
                "must name a --user-entitlement or --application-entitlement",
                [
                    *("--user-entitlement", "a", "--foreign-user-entitlement", "b"),
                    *("--watch-revocation", "b"),
                ],
            ),
            ("--revocation-timeout", ["--user-entitlement", "a", "--revocation-timeout", "5"]),
            ("--revocation-interval", ["--user-entitlement", "a", "--revocation-interval", "1"]),
        ):
            with self.subTest(arguments=arguments):
                world = standard_world()
                code, _, err = self.verify(world, arguments)
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertEqual(world.requests, [])
        with (
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            verifier.main(
                [
                    *BASE_ARGS,
                    "--user-entitlement",
                    "a",
                    "--prove-shared-budget",
                    "--prove-token-limit",
                ]
            )


if __name__ == "__main__":
    unittest.main()
