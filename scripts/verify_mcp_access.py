"""Opt-in live verification of MCP servers MOSAIC publishes through a real API Management gateway.

The script reads each MCP grant's connection details from MOSAIC, then talks to the gateway as a
real MCP client over streamable HTTP: ``initialize``, the ``notifications/initialized``
notification, ``tools/list`` and ``tools/call``, with the protocol version and session headers the
transport requires, accepting JSON and event-stream responses alike. It checks sign-in discovery,
that granted people and applications reach their tools, and that anonymous, ungranted,
wrong-audience and wrong-scope calls are refused. Optional checks prove a grant's call limit and a
cost center's pooled call quota, wait for a revocation to take effect, and call a server whose tool
calls a governed model on the person's behalf, printing what's needed to find that call's
attribution in the gateway's logs. That call can wait until the model caller's grant is applied,
so the grant is open only for the call.

Credentials come from environment variables, or from sign-ins the script starts: the device code
flow for people and client credentials for an application. They stay in memory and are never
printed, and neither is any tool's output. The script doesn't provision resources, change grants or
save credentials. Proving a pooled quota spends the cost center's calls on the server for the rest
of the month.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

if not __package__:
    # Run as a script: make the repository root importable, as entra_bootstrap.py does.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import verify_model_access as model

VerificationFailed = model.VerificationFailed
Kind = model.Kind

TROUBLESHOOTING = "docs/connect-to-mcp-servers.md#troubleshooting"
# The handshake era of MCP, which API Management's MCP passthrough speaks. MOSAIC's own client
# offers and accepts the same revisions (mosaic_api.domain.MCP_SUPPORTED_PROTOCOL_VERSIONS).
PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"})
# MCP-Protocol-Version was introduced in 2025-06-18, so it isn't sent to a server on an earlier one.
PROTOCOL_HEADER_MINIMUM = "2025-06-18"
PROTOCOL_HEADER = "MCP-Protocol-Version"
SESSION_HEADER = "Mcp-Session-Id"
ACCEPT = "application/json, text/event-stream"
CLIENT_INFO = {"name": "mosaic-mcp-verifier", "version": "1.0"}
MCP_SCOPE = "Mcp.Invoke"
MCP_ROLE = "Mcp.Invoke.Application"
COST_CENTER_HEADER = "x-mosaic-cost-center"
REMAINING_CALLS_HEADER = "x-mosaic-remaining-calls"
METADATA_PREFIX = "/.well-known/oauth-protected-resource"
ECHO_TOOL = "echo"
ADD_TOOL = "add"
ASK_MODEL_TOOL = "ask_model"
PROMPT = model.PROMPT
# A response is read against a byte budget, and tools/list against a page cap, so a misbehaving
# server can't keep the verifier reading.
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ERROR_BYTES = 16 * 1024
MAX_TOOL_PAGES = 20
CLIENT_TIMEOUT_SECONDS = 30
ASK_MODEL_TIMEOUT_SECONDS = 120
# The calls a grant's tool checks make: initialize, the initialized notification, tools/list, echo
# and add. A call-limit proof needs a limit above them, and few enough calls to spend quickly.
FLOW_CALLS = 5
CALL_LIMIT_CEILING = 30
CALL_LIMIT_PERIODS = (60, 300)
# DELETE is governed too: retry one rate refusal, never wait out a monthly pool.
CLEANUP_MAX_WAIT_SECONDS = 300
CLEANUP_REQUEST_TIMEOUT_SECONDS = 30
# With the model caller's grant open, ask_model gives up on its call this long after it starts: no
# request starts, and no response is read, after that, and a read already waiting ends within its
# own timeout, at most the call's. It's as long as the requests before the call could take, at the
# client's timeout each: initialize, the initialized notification and every tools/list page.
ASK_MODEL_DEADLINE_SECONDS = (2 + MAX_TOOL_PAGES) * CLIENT_TIMEOUT_SECONDS
# Its cleanup then gets a deadline of its own, as long as one DELETE and the rate-limit wait: the
# retry starts before it, and a read already waiting ends within the DELETE's timeout.
CLEANUP_DEADLINE_SECONDS = CLEANUP_REQUEST_TIMEOUT_SECONDS + CLEANUP_MAX_WAIT_SECONDS
# The longest ask_model can then use the person's token: each deadline and the read after it.
ASK_MODEL_LONGEST_SECONDS = (
    ASK_MODEL_DEADLINE_SECONDS
    + ASK_MODEL_TIMEOUT_SECONDS
    + CLEANUP_DEADLINE_SECONDS
    + CLEANUP_REQUEST_TIMEOUT_SECONDS
)
LATE = "the server took longer than the verifier waits"
# A pooled-quota proof spends the whole pool, so the pool must be small.
POOL_CALL_CEILING = 50
# On a stateful server the session's DELETE must be the last call the pool allows, so the pool must
# fit the shortest run that ends with it: a probe, initialize, the initialized notification, one
# tool call and the DELETE.
POOL_CALL_FLOOR = 5
# What a pooled-quota proof probes the pool with: a request without a session, which can't create
# server state. A stateful server refuses it with 400, and a stateless one answers it.
POOL_PROBE = "ping"
# API Management's counters are distributed, so a spent pool can let a call or two more through
# before it refuses. A proof sends at most this many more probes for the refusal.
POOL_OVERSHOOT_PROBES = 2
# How API Management words the 403 a spent quota-by-key returns. A rate limit's 429 says
# "Rate limit is exceeded" (verify_model_access.GATEWAY_LIMIT_MESSAGES).
QUOTA_MESSAGE = "Out of call volume quota"
DEFAULT_REVOCATION_TIMEOUT = 900
DEFAULT_REVOCATION_INTERVAL = 30
DEFAULT_ATTRIBUTION_TIMEOUT = 1800
DEFAULT_ATTRIBUTION_INTERVAL = 60
DEFAULT_MODEL_GRANT_TIMEOUT = 600
DEFAULT_MODEL_GRANT_INTERVAL = 15
USAGE_PERIOD = "7d"
REVOKE_MODEL_GRANT = "Revoke the model caller's grant now and apply its model's access plan"

USER_CONTROL_TOKEN = "MOSAIC_SMOKE_USER_CONTROL_TOKEN"
ADMIN_CONTROL_TOKEN = "MOSAIC_SMOKE_ADMIN_CONTROL_TOKEN"
USER_RUNTIME_TOKEN = "MOSAIC_SMOKE_MCP_USER_RUNTIME_TOKEN"
APPLICATION_RUNTIME_TOKEN = "MOSAIC_SMOKE_MCP_APPLICATION_RUNTIME_TOKEN"
UNGRANTED_USER_RUNTIME_TOKEN = "MOSAIC_SMOKE_MCP_UNGRANTED_USER_RUNTIME_TOKEN"
# The user's model-runtime token, which the model verifier reads too. With only Models.Invoke, it's
# a valid token for the gateway that lacks MCP's scope.
MODEL_RUNTIME_TOKEN = "MOSAIC_SMOKE_USER_RUNTIME_TOKEN"
APPLICATION_CLIENT_ID = "MOSAIC_SMOKE_APPLICATION_CLIENT_ID"
APPLICATION_CLIENT_SECRET = "MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET"

# How the script names the people it signs in. The live driver maps each to a persona.
WHO_USER = "the user who holds these grants"
WHO_STRANGER = "a different user, one with no grant for these MCP servers"

COST_CENTER_CODE = re.compile(r"[A-Za-z0-9._-]{1,64}")
RESOURCE_METADATA = re.compile(r'resource_metadata="([^"]*)"')
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}")
PROTOCOL_REVISION = re.compile(r"\d{4}-\d{2}-\d{2}")
# Mcp-Session-Id must be visible ASCII.
SESSION_ID = re.compile(r"[\x21-\x7e]{1,512}")

Flow = Literal["tools", "call-limit", "pooled-quota"]

say = model.say
bearer = model.bearer
credential = model.credential


def https_url(value: object, *, label: str, origin: str) -> str:
    """An HTTPS URL on the approved gateway origin, without a query or fragment."""

    if not isinstance(value, str) or not value:
        raise VerificationFailed(f"The connection has no {label}")
    if model.https_origin(value) != origin:
        raise VerificationFailed(f"The {label} isn't on the approved gateway origin")
    parts = urlsplit(value)
    if parts.query or parts.fragment:
        raise VerificationFailed(f"The {label} must not contain a query or fragment")
    return value.rstrip("/")


def utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def grant_trace_key(tenant_id: str, publication_id: str, entitlement_id: str) -> str:
    """The ``g=`` an MCP call's attribution trace carries for this grant.

    The same hash as mosaic_api.integrations.access_policy.grant_counter_identity.
    """

    identity = json.dumps(
        [tenant_id, publication_id, entitlement_id], ensure_ascii=True, separators=(",", ":")
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class McpGrant:
    kind: Kind
    entitlement_id: str
    label: str
    server_id: str
    publication_id: str | None
    tenant_id: str
    audience: str
    server_url: str
    metadata_url: str
    # The scope a token is requested for: Mcp.Invoke for people, .default for applications.
    scope: str
    client_id: str | None
    cost_center: str | None
    via_group: bool
    limits: dict[str, Any]
    control_token: str = field(repr=False)
    application_object_id: str | None = None

    @property
    def mcp_scope(self) -> str:
        """The delegated scope the gateway's metadata advertises and its tokens must carry."""

        return f"api://{self.audience}/{MCP_SCOPE}"


def grant_route(kind: Kind, entitlement_id: str) -> str:
    return f"{'me/entitlements' if kind == 'user' else 'entitlements'}/{entitlement_id}"


def load_grant(
    client: httpx.Client,
    base: str,
    *,
    kind: Kind,
    label: str,
    entitlement_id: str,
    control_token: str,
    origin: str,
) -> McpGrant:
    route = f"{grant_route(kind, entitlement_id)}/mcp-connection"
    response = client.get(f"{base}/{route}", headers=bearer(control_token))
    model.expect(response, {200}, f"{label} connection details")
    connection = model.object_body(response, f"{label} connection details")
    name = connection.get("displayName")
    if isinstance(name, str) and SAFE_NAME.fullmatch(name):
        label = f"{label} ({name})"
    try:
        grant = grant_from(
            connection,
            kind=kind,
            label=label,
            entitlement_id=entitlement_id,
            control_token=control_token,
            origin=origin,
        )
        if kind == "application":
            snapshot = read_publication(client, base, control_token, grant)
            compiled = applied_grant(snapshot, grant)
            if compiled is None:
                raise VerificationFailed("The server's last apply doesn't include this grant")
            subject = model.mapping(compiled.get("subject"))
            if subject.get("kind") == "securityGroup":
                raise VerificationFailed(
                    "The selected grant is a security-group grant, not a direct application "
                    "grant. This verifier cannot prove which group grant the gateway selects; "
                    "select a direct application entitlement"
                )
            object_id = model.guid(compiled.get("objectId"))
            if subject.get("kind") != "application" or object_id is None:
                raise VerificationFailed("The applied grant lacks a usable application identity")
            grant = replace(grant, application_object_id=object_id)
        return grant
    except VerificationFailed as error:
        raise VerificationFailed(f"{label}: {error}") from None


def grant_from(
    connection: dict[str, Any],
    *,
    kind: Kind,
    label: str,
    entitlement_id: str,
    control_token: str,
    origin: str,
) -> McpGrant:
    status = model.mapping(connection.get("runtime")).get("status")
    if status != "applied" or connection.get("enforced") is not True:
        shown = status if status in model.RUNTIME_STATUSES else "not set up"
        raise VerificationFailed(
            "Access must be applied to API Management with nothing pending, so the gateway "
            f"enforces it (status: {shown})"
        )
    if connection.get("transport") != "streamable":
        raise VerificationFailed("The server isn't a streamable HTTP MCP server")
    server_url = https_url(connection.get("serverUrl"), label="MCP server URL", origin=origin)
    server_path = urlsplit(server_url).path
    if not server_path.endswith("/mcp"):
        raise VerificationFailed("The MCP server URL doesn't end in /mcp")
    metadata_url = https_url(
        connection.get("resourceMetadataUrl"), label="resource metadata URL", origin=origin
    )
    # RFC 9728 inserts the well-known prefix before the protected resource's path.
    if urlsplit(metadata_url).path != f"{METADATA_PREFIX}{server_path}":
        raise VerificationFailed("The resource metadata URL isn't the MCP server URL's")
    tenant = model.guid(connection.get("tenantId"))
    audience = model.guid(connection.get("entraAudience"))
    if tenant is None or audience is None:
        raise VerificationFailed("The connection lacks a usable Entra tenant or audience")
    if kind == "user":
        scope = connection.get("delegatedScope")
        expected = f"api://{audience}/{MCP_SCOPE}"
    else:
        scope = connection.get("applicationScope")
        expected = f"api://{audience}/.default"
        if connection.get("requiredAppRole") != MCP_ROLE:
            raise VerificationFailed(f"The connection doesn't require the {MCP_ROLE} app role")
    if not isinstance(scope, str) or scope.casefold() != expected.casefold():
        raise VerificationFailed(f"The connection's scope isn't {expected}")
    header = connection.get("costCenterHeader")
    if header is not None and header != COST_CENTER_HEADER:
        raise VerificationFailed("The connection names a different cost-center header")
    code = model.mapping(connection.get("costCenter")).get("code")
    if code is not None and not (isinstance(code, str) and COST_CENTER_CODE.fullmatch(code)):
        raise VerificationFailed("The connection's cost center has no usable code")
    server_id = connection.get("mcpServerId")
    if not isinstance(server_id, str) or not server_id:
        raise VerificationFailed("The connection names no MCP server")
    publication = connection.get("publicationId")
    return McpGrant(
        kind=kind,
        entitlement_id=entitlement_id,
        label=label,
        server_id=server_id,
        publication_id=publication if isinstance(publication, str) and publication else None,
        tenant_id=tenant,
        audience=audience,
        server_url=server_url,
        metadata_url=metadata_url,
        scope=expected,
        client_id=model.guid(connection.get("clientId")),
        cost_center=code,
        via_group=bool(connection.get("viaGroupId")),
        limits=model.mapping(model.mapping(connection.get("limits")).get("requests")),
        control_token=control_token,
    )


@dataclass
class Exchange:
    """One HTTP request to the MCP endpoint and what came back."""

    method: str
    status: int
    headers: httpx.Headers
    request_id: int | None = None
    message: dict[str, Any] | None = None
    body: bytes = b""
    event_stream: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def header_int(self, name: str) -> int | None:
        value = self.headers.get(name, "").strip()
        return int(value) if model.SECONDS.fullmatch(value) else None

    def challenge(self) -> str:
        return ", ".join(self.headers.get_list("www-authenticate"))

    def gateway_message(self) -> str:
        """The message of API Management's own JSON error body, or ""."""

        try:
            body = json.loads(self.body)
        except ValueError:
            return ""
        message = model.mapping(body).get("message")
        return message if isinstance(message, str) else ""

    def refused_by(self) -> Literal["rate", "quota"] | None:
        """Which of the gateway's limits refused the call, if one did."""

        message = self.gateway_message()
        if self.status == 429 and message.startswith(model.GATEWAY_LIMIT_MESSAGES["calls"]):
            return "rate"
        if self.status == 403 and message.startswith(QUOTA_MESSAGE):
            return "quota"
        return None

    def cost_center_refusal(self) -> bool:
        """A 403 from the cost-center rule: it names the header and asks for no other scope."""

        return (
            self.status == 403
            and COST_CENTER_HEADER in self.body.decode("utf-8", "replace")
            and "insufficient_scope" not in self.challenge()
        )

    def lookup_refusal(self, *, named_cost_center: bool) -> bool:
        """A 403 from the grant lookup, which found no grant the caller holds for this call.

        A call that names a cost center gets the cost-center rule's 403, and one that names none
        a 403 asking for the scope. A quota's or a budget's 403 means the lookup found a grant.
        """

        if named_cost_center:
            return self.cost_center_refusal()
        return self.status == 403 and "insufficient_scope" in self.challenge()


def status_text(exchange: Exchange) -> str:
    reason = {
        "rate": ", the gateway's call limit",
        "quota": ", a call quota at the gateway",
        None: "",
    }[exchange.refused_by()]
    return f"HTTP {exchange.status}{reason}"


def describe(exchange: Exchange) -> str:
    return f"unexpected {status_text(exchange)}"


def late(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


class DeadlineStream(httpx.SyncByteStream):
    """A response's body, abandoned at the first chunk that arrives after a deadline.

    It's checked for each chunk from the network, so a server trickling bytes, with or without
    line breaks, is cut off within one read of the deadline.
    """

    def __init__(self, stream: httpx.SyncByteStream, deadline: float) -> None:
        self._stream = stream
        self._deadline = deadline

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._stream:
            if late(self._deadline):
                raise VerificationFailed(LATE)
            yield chunk

    def close(self) -> None:
        self._stream.close()


def bound(response: httpx.Response, deadline: float | None) -> None:
    """Abandon the response's body at the first chunk that arrives after ``deadline``.

    Its headers have arrived by now. They come from the gateway, which passes a response on only
    once it has the server's headers, so they took one read within the request's timeout. Only a
    gateway trickling headers of its own could take longer.
    """

    if deadline is not None and isinstance(response.stream, httpx.SyncByteStream):
        response.stream = DeadlineStream(response.stream, deadline)


def read_bounded(response: httpx.Response, limit: int, *, strict: bool) -> bytes:
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            if strict:
                raise VerificationFailed("the response was larger than the verifier reads")
            break
        chunks.append(chunk)
    return b"".join(chunks)


def read_into(exchange: Exchange, response: httpx.Response) -> None:
    """Read what the verifier needs of a response: a refusal's body, or a request's answer."""

    if not exchange.ok:
        exchange.body = read_bounded(response, MAX_ERROR_BYTES, strict=False)
        return
    if exchange.request_id is None:
        return
    content_type = response.headers.get("content-type", "")
    if content_type.split(";")[0].strip().casefold() == "text/event-stream":
        exchange.event_stream = True
        exchange.message = read_event_stream(response, exchange.request_id)
        return
    body = read_bounded(response, MAX_RESPONSE_BYTES, strict=True)
    try:
        value = json.loads(body)
    except ValueError:
        raise VerificationFailed("the server answered with something other than JSON") from None
    exchange.message = value if isinstance(value, dict) else None


def read_event_stream(response: httpx.Response, request_id: int) -> dict[str, Any]:
    """The first event carrying the response to ``request_id``.

    The server may send notifications or requests of its own first, so other events are skipped.
    """

    data: list[str] = []
    budget = MAX_RESPONSE_BYTES

    def dispatch() -> dict[str, Any] | None:
        if not data:
            return None
        raw = "\n".join(data)
        data.clear()
        try:
            message = json.loads(raw)
        except ValueError:
            return None
        if (
            isinstance(message, dict)
            and message.get("id") == request_id
            and ("result" in message or "error" in message)
        ):
            return message
        return None

    for line in response.iter_lines():
        budget -= len(line) + 1
        if budget < 0:
            raise VerificationFailed("the event stream was longer than the verifier reads")
        if not line:
            found = dispatch()
            if found is not None:
                return found
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        if name == "data":
            data.append(value.removeprefix(" "))
    found = dispatch()
    if found is not None:
        return found
    raise VerificationFailed("the event stream ended without answering")


class Session:
    """One MCP conversation with a published server, through the gateway.

    ``auth`` is sent on every request. ``cost_center``, when set, is sent as the cost-center header,
    and may change between requests: the gateway authorizes each request on its own. ``deadline``,
    when set, is the ``time.monotonic()`` time after which no request starts and no response is
    read. Cleanup then gets a deadline of its own.
    """

    def __init__(
        self,
        client: httpx.Client,
        url: str,
        *,
        auth: dict[str, str],
        cost_center: str | None,
        label: str,
    ) -> None:
        self.client = client
        self.url = url
        self.auth = auth
        self.cost_center = cost_center
        self.label = label
        self.session_id: str | None = None
        self.protocol: str | None = None
        self.exchanges: list[Exchange] = []
        # The answer to the last DELETE, once one came back.
        self.closed_by: Exchange | None = None
        self.deadline: float | None = None
        self._next_id = 0

    def headers(self, *, initializing: bool = False) -> dict[str, str]:
        headers = {"Accept": ACCEPT, "Content-Type": "application/json", **self.auth}
        if self.cost_center is not None:
            headers[COST_CENTER_HEADER] = self.cost_center
        if (
            not initializing
            and self.protocol is not None
            and self.protocol >= PROTOCOL_HEADER_MINIMUM
        ):
            headers[PROTOCOL_HEADER] = self.protocol
        if self.session_id is not None:
            headers[SESSION_HEADER] = self.session_id
        return headers

    def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        notification: bool = False,
        timeout: float | None = None,
    ) -> Exchange:
        """Send one JSON-RPC message. A refusal is returned, not raised."""

        if late(self.deadline):
            raise VerificationFailed(f"{self.label} {method}: {LATE}")
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        request_id: int | None = None
        if not notification:
            self._next_id += 1
            request_id = self._next_id
            payload["id"] = request_id
        initializing = method == "initialize"
        with self.client.stream(
            "POST",
            self.url,
            headers=self.headers(initializing=initializing),
            content=json.dumps(payload).encode("utf-8"),
            timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout,
        ) as response:
            exchange = Exchange(method, response.status_code, response.headers, request_id)
            bound(response, self.deadline)
            try:
                read_into(exchange, response)
            except VerificationFailed as error:
                raise VerificationFailed(f"{self.label} {method}: {error}") from None
        if initializing and exchange.ok:
            session = exchange.headers.get(SESSION_HEADER)
            if session is not None:
                if not SESSION_ID.fullmatch(session):
                    raise VerificationFailed(f"{self.label}: the server's session ID isn't valid")
                self.session_id = session
        self.exchanges.append(exchange)
        return exchange

    def result(self, exchange: Exchange) -> dict[str, Any]:
        """The result of a request that succeeded, or why it didn't."""

        method = exchange.method
        if not exchange.ok:
            raise VerificationFailed(f"{self.label} {method}: {describe(exchange)}")
        message = exchange.message
        if message is None:
            raise VerificationFailed(f"{self.label} {method}: no JSON-RPC response came back")
        if message.get("id") != exchange.request_id:
            raise VerificationFailed(
                f"{self.label} {method}: the response answered another request"
            )
        error = message.get("error")
        if error is not None:
            code = model.mapping(error).get("code")
            shown = f" {code}" if isinstance(code, int) and not isinstance(code, bool) else ""
            raise VerificationFailed(
                f"{self.label} {method}: the server answered with JSON-RPC error{shown}"
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise VerificationFailed(f"{self.label} {method}: the response has no result")
        return result

    def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> dict[str, Any]:
        return self.result(self.send(method, params, timeout=timeout))

    def initialize(self) -> Exchange:
        """Open the session, or return the refusal. Anything else wrong with it fails."""

        exchange = self.send(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
        )
        if not exchange.ok:
            return exchange
        result = self.result(exchange)
        version = result.get("protocolVersion")
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            shown = (
                version
                if isinstance(version, str) and PROTOCOL_REVISION.fullmatch(version)
                else "?"
            )
            raise VerificationFailed(
                f"{self.label}: the server negotiated MCP revision {shown}, which the verifier "
                "doesn't speak"
            )
        self.protocol = version
        if "tools" not in model.mapping(result.get("capabilities")):
            raise VerificationFailed(
                f"{self.label}: the server doesn't declare the tools capability"
            )
        return self.send("notifications/initialized", notification=True)

    def close(self) -> None:
        """End a session, retaining its identity unless DELETE succeeds or is unsupported.

        Gateway limits also govern DELETE. Only a recognizable rate-limit refusal with a
        bounded Retry-After permits one retry, using the same credentials and cost center. A
        session with a deadline gives its cleanup one of its own, CLEANUP_DEADLINE_SECONDS away:
        no response is read after it, and the retry waits only if it can start before it.
        """

        if self.session_id is None:
            return
        deadline = (
            time.monotonic() + CLEANUP_DEADLINE_SECONDS if self.deadline is not None else None
        )
        for attempt in range(2):
            self.closed_by = None
            try:
                with self.client.stream(
                    "DELETE",
                    self.url,
                    headers=self.headers(),
                    timeout=CLEANUP_REQUEST_TIMEOUT_SECONDS,
                ) as response:
                    exchange = Exchange("DELETE", response.status_code, response.headers)
                    self.closed_by = exchange
                    bound(response, deadline)
                    read_into(exchange, response)
            except httpx.HTTPError:
                raise VerificationFailed(
                    f"{self.label}: unresolved session cleanup (HTTP transport failure). "
                    "DELETE was not confirmed; verification is incomplete"
                ) from None
            except VerificationFailed as error:
                raise VerificationFailed(
                    f"{self.label}: unresolved session cleanup ({error}). "
                    "DELETE was not confirmed; verification is incomplete"
                ) from None
            if exchange.ok or exchange.status == 405:
                self.session_id = None
                if exchange.status == 405:
                    say(f"INFO: {self.label}: the server does not support session DELETE (405)")
                return
            retry = exchange.header_int("Retry-After")
            if (
                attempt == 0
                and exchange.refused_by() == "rate"
                and retry is not None
                and 0 < retry <= CLEANUP_MAX_WAIT_SECONDS
                and (deadline is None or time.monotonic() + retry < deadline)
            ):
                say(
                    f"INFO: {self.label}: session DELETE reached the call limit; waiting "
                    f"{retry} seconds before one cleanup retry"
                )
                time.sleep(retry)
                continue
            raise VerificationFailed(
                f"{self.label}: unresolved session cleanup ({status_text(exchange)}). "
                "DELETE was not confirmed; verification is incomplete"
            )


@contextmanager
def closing_session(session: Session) -> Iterator[None]:
    """Always attempt cleanup, preserving both a failed check and a failed cleanup."""

    try:
        yield
    finally:
        problem = sys.exception()
        try:
            session.close()
        except VerificationFailed as cleanup:
            if isinstance(problem, VerificationFailed):
                raise VerificationFailed(f"{problem}; {cleanup}") from None
            raise


def open_session(session: Session) -> None:
    exchange = session.initialize()
    if not exchange.ok:
        raise VerificationFailed(f"{session.label} {exchange.method}: {describe(exchange)}")


def list_tools(session: Session, first: Exchange | None = None) -> dict[str, dict[str, Any]]:
    """Every tool the server lists, following its cursor to a fixed page cap.

    ``first`` is a tools/list already sent, whose result is the first page.
    """

    tools: dict[str, dict[str, Any]] = {}
    result = session.result(first) if first is not None else session.request("tools/list", {})
    for _ in range(MAX_TOOL_PAGES):
        for entry in result.get("tools") or []:
            name = model.mapping(entry).get("name")
            if isinstance(name, str) and name:
                tools[name] = model.mapping(entry)
        cursor = result.get("nextCursor")
        if not isinstance(cursor, str) or not cursor:
            return tools
        result = session.request("tools/list", {"cursor": cursor})
    raise VerificationFailed(
        f"{session.label}: the server lists more tool pages than the verifier reads"
    )


def texts(result: dict[str, Any]) -> list[str]:
    return [
        item["text"]
        for item in result.get("content") or []
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]


def structured(result: dict[str, Any]) -> object:
    return model.mapping(result.get("structuredContent")).get("result")


def number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def tool_result(exchange: Exchange, session: Session, name: str) -> dict[str, Any]:
    result = session.result(exchange)
    if result.get("isError") is True:
        raise VerificationFailed(f"{session.label}: the {name} tool returned an error")
    return result


def check_echo(session: Session, exchange: Exchange, text: str) -> None:
    result = tool_result(exchange, session, ECHO_TOOL)
    if not any(text in item for item in texts(result)) and structured(result) != text:
        raise VerificationFailed(f"{session.label}: the {ECHO_TOOL} tool didn't return its text")


def check_add(session: Session, exchange: Exchange, total: int) -> None:
    result = tool_result(exchange, session, ADD_TOOL)
    values = [number(item) for item in texts(result)] + [number(structured(result))]
    if float(total) not in values:
        raise VerificationFailed(f"{session.label}: the {ADD_TOOL} tool didn't return the sum")


def echo_text() -> str:
    return f"mosaic-verifier-{secrets.token_hex(6)}"


@dataclass
class Spent:
    """How far a run of tool calls got: the tools listed, and the refusal that stopped it."""

    tools: int = 0
    refusal: Exchange | None = None


def succeeded(session: Session) -> int:
    """The calls in a session that the gateway let through. Each counts against its limits."""

    return sum(1 for exchange in session.exchanges if exchange.ok)


def use_tools(
    session: Session,
    cost_center: str | None,
    *,
    until_refused: bool = False,
    max_calls: int = 0,
) -> Spent:
    """Open the session, list the tools, and call echo and add, checking their results.

    The session names ``cost_center`` as it opens, and then the same code in the other case,
    which the gateway must accept because it compares codes without case. With ``until_refused``,
    echo is called again until the gateway refuses a call or ``max_calls`` calls have gone
    through, and the refusal is returned wherever it comes. Otherwise any refusal fails.
    """

    spent = Spent()

    def refused(exchange: Exchange) -> bool:
        if exchange.ok:
            return False
        if not until_refused:
            raise VerificationFailed(f"{session.label} {exchange.method}: {describe(exchange)}")
        spent.refusal = exchange
        return True

    session.cost_center = cost_center
    if refused(session.initialize()):
        return spent
    if cost_center is not None:
        session.cost_center = cost_center.swapcase()
    listed = session.send("tools/list", {})
    if refused(listed):
        return spent
    tools = list_tools(session, listed)
    spent.tools = len(tools)
    missing = [name for name in (ECHO_TOOL, ADD_TOOL) if name not in tools]
    if missing:
        raise VerificationFailed(
            f"{session.label}: the server doesn't list {' or '.join(missing)}, which the checks "
            "call"
        )
    text = echo_text()
    echoed = session.send("tools/call", {"name": ECHO_TOOL, "arguments": {"text": text}})
    if refused(echoed):
        return spent
    check_echo(session, echoed, text)
    first, second = secrets.randbelow(900) + 100, secrets.randbelow(900) + 100
    added = session.send("tools/call", {"name": ADD_TOOL, "arguments": {"a": first, "b": second}})
    if refused(added):
        return spent
    check_add(session, added, first + second)
    while until_refused and succeeded(session) < max_calls:
        text = echo_text()
        echoed = session.send("tools/call", {"name": ECHO_TOOL, "arguments": {"text": text}})
        if refused(echoed):
            return spent
        check_echo(session, echoed, text)
    return spent


def protocol_note(session: Session) -> str:
    opened = any(
        exchange.method == "initialize" and exchange.headers.get(SESSION_HEADER)
        for exchange in session.exchanges
    )
    streams = {exchange.event_stream for exchange in session.exchanges if exchange.message}
    answers = {
        frozenset({True, False}): "event streams and JSON",
        frozenset({True}): "event streams",
    }.get(frozenset(streams), "JSON")
    return (
        f"negotiated MCP {session.protocol}, with {'a session' if opened else 'no session'}, and "
        f"the server answered with {answers}"
    )


class Tokens:
    """MCP runtime tokens, from the environment or one sign-in per person, tenant and scope."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        user_source: str,
        application_source: str,
        person: str | None,
    ) -> None:
        self._client = client
        self._user_source = user_source
        self._application_source = application_source
        self._person = person
        self._signed_in: dict[tuple[str, str, str, str], str] = {}
        self._stranger: str | None = None

    def own(self, grant: McpGrant) -> str:
        return self.user(grant) if grant.kind == "user" else self.application(grant)

    def user(self, grant: McpGrant) -> str:
        if self._user_source == "env":
            token = credential(USER_RUNTIME_TOKEN)
        else:
            token = self._device_code(grant, WHO_USER)
        problem = model.token_problem(
            token,
            kind="user",
            audience=grant.audience,
            valid_for=model.TOKEN_MARGIN_SECONDS,
            scope=MCP_SCOPE,
        )
        if problem is not None:
            raise VerificationFailed(
                model.token_failure(f"{grant.label}: the user's MCP token", problem)
            )
        signed_in = model.object_id(token)
        if signed_in is None:
            raise VerificationFailed(f"{grant.label}: the user's MCP token names no object ID")
        if self._person is not None and signed_in != self._person:
            raise VerificationFailed(
                f"{grant.label}: the user's MCP token belongs to a different account from "
                f"{USER_CONTROL_TOKEN}. Sign in as the user who holds these grants"
            )
        return token

    def application(self, grant: McpGrant) -> str:
        if self._application_source == "env":
            token = credential(APPLICATION_RUNTIME_TOKEN)
        else:
            key = ("application", grant.tenant_id, grant.scope, "")
            if key not in self._signed_in:
                self._signed_in[key] = model.client_credentials_token(
                    self._client,
                    tenant=grant.tenant_id,
                    scope=grant.scope,
                    troubleshooting=TROUBLESHOOTING,
                )
            token = self._signed_in[key]
        problem = model.token_problem(
            token,
            kind="application",
            audience=grant.audience,
            valid_for=model.TOKEN_MARGIN_SECONDS,
            role=MCP_ROLE,
        )
        if problem is None and delegated_scopes(token):
            problem = "carries a delegated scope, so the gateway reads it as a person's token"
        if problem is None:
            if grant.application_object_id is None:
                problem = "cannot be bound to the selected applied application's identity"
            elif model.object_id(token) != grant.application_object_id:
                problem = "does not name the selected applied application's object ID"
        if problem is not None:
            raise VerificationFailed(
                model.token_failure(f"{grant.label}: the application's MCP token", problem)
            )
        return token

    def stranger(self, grant: McpGrant, held: list[str]) -> str:
        """A signed-in person who holds none of these grants."""

        if self._stranger is not None:
            return self._stranger
        if self._user_source == "env":
            token = credential(UNGRANTED_USER_RUNTIME_TOKEN)
        else:
            token = self._device_code(grant, WHO_STRANGER)
        # Its audience is compared with each grant's when the check runs.
        problem = model.token_problem(
            token,
            kind="user",
            audience=None,
            valid_for=model.TOKEN_MARGIN_SECONDS,
            scope=MCP_SCOPE,
        )
        if problem is not None:
            raise VerificationFailed(model.token_failure("The ungranted user's MCP token", problem))
        people = {self._person, *(model.object_id(token) for token in held)} - {None}
        if token in held or model.object_id(token) in people:
            raise VerificationFailed(
                "The ungranted user's MCP token belongs to the user who holds these grants. "
                "Sign in as a different user"
            )
        self._stranger = token
        return token

    def missing_scope(self, grant: McpGrant) -> str:
        """The grant holder's own token without Mcp.Invoke, such as a model token."""

        token = credential(MODEL_RUNTIME_TOKEN)
        problem = missing_scope_problem(token, grant, self._person)
        if problem is not None:
            raise VerificationFailed(f"{MODEL_RUNTIME_TOKEN} {problem}")
        return token

    def _device_code(self, grant: McpGrant, who: str) -> str:
        if grant.client_id is None:
            variable = USER_RUNTIME_TOKEN if who == WHO_USER else UNGRANTED_USER_RUNTIME_TOKEN
            raise VerificationFailed(
                f"{grant.label}: MOSAIC names no client to sign in with for this grant's "
                f"audience, so the verifier can't sign in. Set {variable} instead"
            )
        key = (who, grant.tenant_id, grant.scope, grant.client_id)
        if key not in self._signed_in:
            self._signed_in[key] = model.device_code_token(
                self._client,
                tenant=grant.tenant_id,
                client_id=grant.client_id,
                scope=grant.scope,
                who=who,
                troubleshooting=TROUBLESHOOTING,
            )
        return self._signed_in[key]


def delegated_scopes(token: str) -> list[str]:
    scopes = model.token_claims(token).get("scp")
    if not isinstance(scopes, str):
        return []
    return [scope for scope in scopes.split() if scope and scope != "/"]


def missing_scope_problem(token: str, grant: McpGrant, person: str | None) -> str | None:
    """Why this token can't show the gateway refusing a token without Mcp.Invoke, or None.

    Validation must accept it, and it must be the grant holder's, so only the scope rule is left
    to refuse it.
    """

    claims = model.token_claims(token)
    if not claims:
        return "can't be read"
    if model.token_audience(token) != grant.audience:
        return "is for a different audience from these grants"
    if claims.get("ver") != "2.0":
        return "isn't an Entra version 2.0 token, the only kind the gateway accepts"
    scopes = delegated_scopes(token)
    if not scopes:
        return "has no delegated scope, so it isn't a person's token"
    if MCP_SCOPE in scopes:
        return (
            f"carries {MCP_SCOPE}, so the gateway would accept it. Entra puts every scope a "
            "client is consented for in its tokens: use a client that isn't consented for "
            f"{MCP_SCOPE}"
        )
    left = model.seconds_left(token)
    if left is None:
        return "has no expiry time"
    if left < model.TOKEN_MARGIN_SECONDS:
        return (
            "has expired. Get a new one"
            if left <= 0
            else f"expires in {left} seconds. Get a new one"
        )
    if person is None or model.object_id(token) != person:
        return "belongs to someone other than the user who holds these grants"
    return None


def probe(client: httpx.Client, grant: McpGrant, auth: dict[str, str]) -> Exchange:
    """An initialize the gateway should refuse. A session it opens anyway is closed."""

    session = Session(client, grant.server_url, auth=auth, cost_center=None, label=grant.label)
    exchange = session.send(
        "initialize",
        {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
    )
    session.close()
    return exchange


def check_discovery(client: httpx.Client, grant: McpGrant, checked: set[str]) -> None:
    """An anonymous call gets 401 naming the metadata, which names where and how to sign in."""

    exchange = probe(client, grant, {})
    if exchange.status != 401:
        raise VerificationFailed(f"{grant.label} rejecting an anonymous call: {describe(exchange)}")
    named = RESOURCE_METADATA.search(exchange.challenge())
    if named is None:
        raise VerificationFailed(
            f"{grant.label}: the anonymous call's 401 doesn't name the resource metadata"
        )
    if named.group(1).rstrip("/") != grant.metadata_url:
        raise VerificationFailed(
            f"{grant.label}: the anonymous call's 401 names a different resource metadata URL "
            "from the connection details"
        )
    if grant.metadata_url in checked:
        return
    response = client.get(grant.metadata_url)
    model.expect(response, {200}, f"{grant.label} resource metadata")
    metadata = model.object_body(response, f"{grant.label} resource metadata")
    resource = metadata.get("resource")
    if not isinstance(resource, str) or resource.rstrip("/") != grant.server_url:
        raise VerificationFailed(f"{grant.label}: the resource metadata names a different resource")
    servers = metadata.get("authorization_servers")
    authority = f"{model.LOGIN_ORIGIN}/{grant.tenant_id}/v2.0".casefold()
    if not isinstance(servers, list) or not any(
        isinstance(item, str) and item.rstrip("/").casefold() == authority for item in servers
    ):
        raise VerificationFailed(
            f"{grant.label}: the resource metadata doesn't name the tenant's authorization server"
        )
    scopes = metadata.get("scopes_supported")
    if not isinstance(scopes, list) or not any(
        isinstance(item, str) and item.casefold() == grant.mcp_scope.casefold() for item in scopes
    ):
        raise VerificationFailed(f"{grant.label}: the resource metadata doesn't offer {MCP_SCOPE}")
    checked.add(grant.metadata_url)
    say(
        f"PASS: {grant.label} answers an anonymous call with 401 and its resource metadata, which "
        f"names the server, the tenant's authorization server and {MCP_SCOPE}"
    )


@dataclass(frozen=True)
class Refusal:
    name: str
    auth: dict[str, str] = field(repr=False)
    status: int
    # "scope" also needs insufficient_scope in the challenge; "cost-center" the cost-center rule.
    rule: Literal["", "scope", "cost-center"] = ""


def unknown_cost_center() -> str:
    return f"verifier-unknown-{secrets.token_hex(4)}"


def check_refusals(
    client: httpx.Client,
    grant: McpGrant,
    token: str,
    *,
    stranger: str | None,
    missing_scope: str | None,
) -> None:
    """Calls the gateway must refuse, each by the rule under test.

    None of them reaches a limit, because the gateway refuses them before counting a call.
    """

    own = bearer(token)
    checks = [
        # Token validation refuses it with 401, because its audience is MOSAIC's API. A 403 would
        # mean validation was skipped and only the grant lookup refused it.
        Refusal("a MOSAIC control-plane token", bearer(grant.control_token), 401),
        Refusal(
            "an x-mosaic-cost-center header that isn't a code",
            {**own, COST_CENTER_HEADER: "not a code!"},
            403,
            "cost-center",
        ),
        Refusal(
            "a cost center it holds no grant under",
            {**own, COST_CENTER_HEADER: unknown_cost_center()},
            403,
            "cost-center",
        ),
    ]
    skipped: list[str] = []
    if stranger is not None:
        name = "the ungranted user's token"
        if model.token_audience(stranger) == grant.audience:
            checks.append(Refusal(name, bearer(stranger), 403, "scope"))
        else:
            skipped.append(f"{name}: it's for a different audience")
    if missing_scope is not None and grant.kind == "user":
        checks.append(
            Refusal(f"the user's token without {MCP_SCOPE}", bearer(missing_scope), 403, "scope")
        )
    challenges: list[str] = []
    for check in checks:
        exchange = probe(client, grant, check.auth)
        label = f"{grant.label} rejecting {check.name}"
        if exchange.status != check.status:
            raise VerificationFailed(f"{label}: {describe(exchange)}")
        if check.rule == "scope" and "insufficient_scope" not in exchange.challenge():
            raise VerificationFailed(
                f"{label}: the 403 didn't ask for {MCP_SCOPE} (insufficient_scope)"
            )
        if check.rule == "cost-center" and not exchange.cost_center_refusal():
            raise VerificationFailed(f"{label}: the 403 didn't come from the cost-center rule")
        if check.status == 401:
            challenges.append(exchange.challenge())
    say(f"PASS: {grant.label} rejected an anonymous call, {', '.join(c.name for c in checks)}")
    for reason in skipped:
        say(f"SKIP: {grant.label} {reason}")
    # ADR 0017 leaves open whether the API's on-error handler adds the challenge to the 401
    # token validation returns, so it's reported rather than required.
    named = all(RESOURCE_METADATA.search(challenge) for challenge in challenges)
    say(
        f"INFO: {grant.label}'s 401 for a token for another audience "
        f"{'names' if named else 'does not name'} the resource metadata"
    )


def new_session(client: httpx.Client, grant: McpGrant, token: str) -> Session:
    return Session(
        client, grant.server_url, auth=bearer(token), cost_center=None, label=grant.label
    )


def check_tools(client: httpx.Client, grant: McpGrant, token: str) -> None:
    """M5: list and call tools, selecting the grant with its cost center's code in either case."""

    session = new_session(client, grant, token)
    with closing_session(session):
        spent = use_tools(session, grant.cost_center)
    code = grant.cost_center
    selected = (
        ""
        if code is None
        else f", selecting its grant with {COST_CENTER_HEADER}"
        + (" in either case" if code.swapcase() != code else "")
    )
    say(
        f"PASS: {grant.label} listed {spent.tools} tool(s), and {ECHO_TOOL} and {ADD_TOOL} "
        f"returned the expected results{selected}"
    )
    say(f"INFO: {grant.label} {protocol_note(session)}")


def call_limit_problem(grant: McpGrant) -> str | None:
    """Why this grant can't prove its call limit, or None."""

    calls = grant.limits.get("calls")
    period = grant.limits.get("renewalPeriodSeconds")
    if not isinstance(calls, int) or isinstance(calls, bool) or not isinstance(period, int):
        return "Proving the call limit needs a grant with a call rate limit"
    if not FLOW_CALLS < calls <= CALL_LIMIT_CEILING:
        return (
            f"Proving the call limit needs a grant limited to {FLOW_CALLS + 1} to "
            f"{CALL_LIMIT_CEILING} calls, so its tool checks fit inside the limit"
        )
    low, high = CALL_LIMIT_PERIODS
    if not low <= period <= high:
        return f"Proving the call limit needs a limit per {low} to {high} seconds"
    if grant.limits.get("callQuota") is not None:
        return (
            "Proving the call limit needs a grant without a call quota, which refuses with 403 "
            "first"
        )
    return None


def prove_call_limit(client: httpx.Client, grant: McpGrant, token: str) -> None:
    """M6: calls spend x-mosaic-remaining-calls down to the gateway's own 429."""

    calls = int(grant.limits["calls"])
    period = int(grant.limits["renewalPeriodSeconds"])
    session = new_session(client, grant, token)
    started = time.monotonic()
    with closing_session(session):
        spent = use_tools(session, grant.cost_center, until_refused=True, max_calls=calls + 1)
        elapsed = time.monotonic() - started
        # Measure only proof calls: cleanup may legitimately wait for the next rate window.
        if elapsed >= period / 2:
            raise VerificationFailed(
                f"{grant.label}: the calls took too long to prove a limit per {period} seconds. "
                "Retry"
            )
        through = [exchange for exchange in session.exchanges if exchange.ok]
        refusal = spent.refusal
        if refusal is None:
            raise VerificationFailed(
                f"{grant.label}: {len(through)} calls went through, more than its limit of {calls} "
                f"calls per {period} seconds allows"
            )
        refused_by = refusal.refused_by()
        if refused_by == "quota":
            raise VerificationFailed(
                f"{grant.label}: a call quota refused a call before its call limit did, so this "
                "run can't prove the call limit"
            )
        if refused_by is None:
            raise VerificationFailed(
                f"{grant.label}: a call was refused with HTTP {refusal.status}, not the gateway's "
                "call-limit 429, so this run can't prove the call limit"
            )
        if refusal.header_int("Retry-After") is None:
            raise VerificationFailed(
                f"{grant.label}: its call-limit 429 had no Retry-After in seconds"
            )
        remaining: list[int] = []
        for exchange in through:
            value = exchange.header_int(REMAINING_CALLS_HEADER)
            if value is None:
                # A notification's 202 has no body, and may carry no header. A request's must.
                if exchange.request_id is not None:
                    raise VerificationFailed(
                        f"{grant.label}: a successful {exchange.method} carried no "
                        f"{REMAINING_CALLS_HEADER}"
                    )
                continue
            remaining.append(value)
        if not remaining or remaining[0] >= calls:
            raise VerificationFailed(
                f"{grant.label}: {REMAINING_CALLS_HEADER} didn't start below its limit of {calls}"
            )
        # This session is the grant's first use in the run that its limit counts: every call before
        # it was refused first. So in a fresh window the first call leaves one fewer than the limit,
        # which ties the limit the gateway enforces to the one MOSAIC applied.
        if remaining[0] != calls - 1:
            raise VerificationFailed(
                f"{grant.label}: its first call left {remaining[0]} of its {calls} calls, not "
                f"{calls - 1}. Either the gateway enforces a smaller limit than MOSAIC applied, or "
                f"calls from the last {period} seconds still count. Wait {period} seconds without "
                "calling it, then retry"
            )
        if any(later >= earlier for earlier, later in pairwise(remaining)):
            shown = ", ".join(str(value) for value in remaining)
            raise VerificationFailed(
                f"{grant.label}: {REMAINING_CALLS_HEADER} didn't fall on each response ({shown})"
            )
        if remaining[-1] != 0:
            raise VerificationFailed(
                f"{grant.label}: the gateway refused a call while {REMAINING_CALLS_HEADER} still "
                f"said {remaining[-1]} were left"
            )
    say(
        f"PASS: {grant.label} spent {REMAINING_CALLS_HEADER} from {remaining[0]} to 0, one call "
        f"at a time, then got the gateway's 429 with Retry-After: its limit of {calls} calls per "
        f"{period} seconds"
    )


def read_publication(
    client: httpx.Client, base: str, admin_control: str, grant: McpGrant
) -> dict[str, Any]:
    """The administrator's view of the grant's MCP publication: what its last apply compiled."""

    if grant.publication_id is None:
        raise VerificationFailed(f"{grant.label}: the connection names no MCP publication")
    response = client.get(
        f"{base}/mcp-publications/{grant.publication_id}", headers=bearer(admin_control)
    )
    model.expect(response, {200}, f"{grant.label}'s MCP publication")
    publication = model.object_body(response, f"{grant.label}'s MCP publication")
    return model.mapping(publication.get("appliedAccess"))


def applied_grant(snapshot: dict[str, Any], grant: McpGrant) -> dict[str, Any] | None:
    for item in snapshot.get("grants") or []:
        entry = model.mapping(item)
        if entry.get("entitlementId") == grant.entitlement_id and entry.get("enabled") is True:
            return entry
    return None


@dataclass(frozen=True)
class PoolPlan:
    calls: int
    other: McpGrant


def plan_pooled_quota(
    client: httpx.Client,
    base: str,
    admin_control: str,
    grant: McpGrant,
    grants: list[McpGrant],
) -> PoolPlan:
    """Check that the grant's cost center has a small pool on the server, and find a control."""

    if grant.cost_center is None:
        raise VerificationFailed(
            f"{grant.label}: proving a pooled quota needs a grant with a cost center"
        )
    snapshot = read_publication(client, base, admin_control, grant)
    code = grant.cost_center.casefold()
    pool = next(
        (
            model.mapping(item)
            for item in snapshot.get("pools") or []
            if str(model.mapping(item).get("costCenterCode", "")).casefold() == code
        ),
        None,
    )
    calls = pool.get("monthlyCalls") if pool is not None else None
    if not isinstance(calls, int) or isinstance(calls, bool):
        raise VerificationFailed(
            f"{grant.label}: its cost center has no pooled monthly call quota applied on this "
            "server"
        )
    if calls > POOL_CALL_CEILING:
        raise VerificationFailed(
            f"{grant.label}: proving the pooled quota spends it, so it needs a pool of at most "
            f"{POOL_CALL_CEILING} calls a month, not {calls}"
        )
    if calls < POOL_CALL_FLOOR:
        raise VerificationFailed(
            f"{grant.label}: proving the pooled quota needs a pool of at least {POOL_CALL_FLOOR} "
            "calls a month, for a probe, initialize, the initialized notification, a tool call "
            f"and the session's DELETE, not {calls}"
        )
    compiled = applied_grant(snapshot, grant)
    if compiled is None:
        raise VerificationFailed(
            f"{grant.label}: the server's last apply doesn't include this grant"
        )
    if model.mapping(compiled.get("enforcement")).get("requests"):
        raise VerificationFailed(
            f"{grant.label}: proving the pooled quota needs a grant with no call limits of its "
            "own, so only the pool can refuse it"
        )
    other = next(
        (
            item
            for item in grants
            if item is not grant
            and item.publication_id == grant.publication_id
            and item.cost_center is not None
            and item.cost_center.casefold() != code
        ),
        None,
    )
    if other is None:
        raise VerificationFailed(
            f"{grant.label}: proving the pooled quota needs another grant in this run on the same "
            "MCP server, under a different cost center, to show it still works"
        )
    return PoolPlan(calls=calls, other=other)


def pool_probe(client: httpx.Client, grant: McpGrant, token: str, protocol: str | None) -> Exchange:
    """A ping under the pooled grant and its cost center, naming no session.

    The pool counts it like any call, but it can't create server state: a stateful server refuses
    a request other than initialize that names no session, and a stateless one answers it. The MCP
    Python SDK's refusal is a 400 naming a session it has already discarded, which isn't used.
    ``protocol`` is the revision a session negotiated, once one has.
    """

    session = new_session(client, grant, token)
    session.cost_center = grant.cost_center
    session.protocol = protocol
    return session.send(POOL_PROBE)


def require_quota(grant: McpGrant, refusal: Exchange) -> None:
    """Fail unless the gateway's quota made the refusal: anything else refused before the pool."""

    refused_by = refusal.refused_by()
    if refused_by == "rate":
        raise VerificationFailed(
            f"{grant.label}: a call limit refused a call before the pool did, so this run can't "
            "prove the pooled quota"
        )
    if refused_by is None:
        raise VerificationFailed(
            f"{grant.label}: a call was refused with HTTP {refusal.status}, not the gateway's "
            "quota 403, so this run can't prove the pooled quota"
        )


def probe_through(grant: McpGrant, exchange: Exchange) -> bool:
    """Whether a probe got through to the server, or the pool refused it. Nothing else may.

    MOSAIC's policy and the gateway's limits never answer 400, so a 400 is the server's.
    """

    if exchange.ok or exchange.status == 400:
        return True
    require_quota(grant, exchange)
    return False


def not_fresh(grant: McpGrant, plan: PoolPlan, call: int, what: str) -> VerificationFailed:
    """The failure for a pool that refused a call this run planned within it."""

    return VerificationFailed(
        f"{grant.label}: its cost center's pool refused {what}, call {call} of the {plan.calls} "
        "it allows a month, with the gateway's quota 403, so something had already spent part "
        "of it this month. The cost center wasn't fresh: use one made for this proof, whose pool "
        "nothing has called this month"
    )


def spend_pool(session: Session, cost_center: str | None, calls: int, *, before: int) -> Spent:
    """Open the session and spend the pool on echo calls, until it refuses one or must stop.

    ``before`` calls went through before the session, and the pool allows ``calls``. On a stateful
    server, which names a session, the calls stop with one left for the session's DELETE. On a
    stateless one, they stop once one more call than the pool allows has gone through. There's no
    tools/list: the pool counts each page, and how many there are isn't known until they're read.
    """

    spent = Spent()
    session.cost_center = cost_center
    opened = session.initialize()
    if not opened.ok:
        spent.refusal = opened
        return spent
    if cost_center is not None:
        session.cost_center = cost_center.swapcase()
    last = calls - 1 if session.session_id is not None else calls + 1
    while before + succeeded(session) < last:
        text = echo_text()
        echoed = session.send("tools/call", {"name": ECHO_TOOL, "arguments": {"text": text}})
        if not echoed.ok:
            spent.refusal = echoed
            break
        check_echo(session, echoed, text)
    return spent


def check_control(client: httpx.Client, other: McpGrant, token: str) -> None:
    """The other grant, under another cost center, still opens a session and calls echo."""

    control = new_session(client, other, token)
    control.cost_center = other.cost_center
    with closing_session(control):
        exchange = control.initialize()
        if not exchange.ok:
            raise VerificationFailed(
                f"{other.label}, under another cost center, after the pool was spent: "
                f"{describe(exchange)}"
            )
        text = echo_text()
        check_echo(
            control,
            control.send("tools/call", {"name": ECHO_TOOL, "arguments": {"text": text}}),
            text,
        )


def prove_pooled_quota(
    client: httpx.Client, grant: McpGrant, token: str, plan: PoolPlan, other_token: str
) -> None:
    """M6: a cost center's pool refuses once spent, and another cost center's grant still works.

    The pool counts every call that gets past the grant lookup, whatever the server answers: the
    probes, the initialized notification and the session's DELETE among them. So the run counts
    each call it makes under the grant. The first is a probe, which finds a spent pool before any
    session opens. On a stateful server, the session then spends all but one of the pool's calls,
    so that its DELETE is the last call the pool allows, and a probe after it must get the quota's
    403. On a stateless server there's no session to close, so echo is called until the pool
    refuses.
    """

    if not probe_through(grant, pool_probe(client, grant, token, None)):
        raise VerificationFailed(
            f"{grant.label}: its cost center's pool of {plan.calls} calls a month refused the "
            "first probe with the gateway's quota 403, before any session opened, so something "
            "had already spent it this month. Use a cost center made for this proof, whose pool "
            "nothing has called this month"
        )
    through = 1
    session = new_session(client, grant, token)
    planned = False
    try:
        with closing_session(session):
            spent = spend_pool(session, grant.cost_center, plan.calls, before=through)
            through += succeeded(session)
            stateful = session.session_id is not None
            if spent.refusal is not None:
                require_quota(grant, spent.refusal)
                # A stateful server's pool must allow every call planned. Without the session, a
                # refused initialize can't tell which kind of server this is.
                if stateful or session.protocol is None:
                    raise not_fresh(grant, plan, through + 1, spent.refusal.method)
            planned = True
    except VerificationFailed as error:
        deleted = session.closed_by
        if planned and deleted is not None and deleted.refused_by() == "quota":
            problem = not_fresh(grant, plan, through + 1, "the session's DELETE")
            raise VerificationFailed(f"{problem}; {error}") from None
        raise
    refusal = spent.refusal
    if stateful:
        # The session's DELETE went through as the pool's last call, so the pool must refuse now.
        through += 1
        for _ in range(1 + POOL_OVERSHOOT_PROBES):
            probed = pool_probe(client, grant, token, session.protocol)
            if not probe_through(grant, probed):
                refusal = probed
                break
            through += 1
    if refusal is None:
        allowance = (
            f", even with {POOL_OVERSHOOT_PROBES} more for API Management's distributed counters"
            if stateful
            else ""
        )
        raise VerificationFailed(
            f"{grant.label}: {through} calls went through, more than its cost center's pool of "
            f"{plan.calls} calls allows{allowance}"
        )
    other = plan.other
    check_control(client, other, other_token)
    if stateful:
        where = "at its limit" if through == plan.calls else "once spent"
        ended = "so the session was closed within the pool"
        if session.closed_by is None or not session.closed_by.ok:
            # A 405: the server doesn't support DELETE, so nothing confirms the session ended.
            ended = (
                "within the pool, but the server doesn't support it, so the session is left for "
                "the server to expire"
            )
        say(
            f"PASS: {grant.label}'s cost center's pool of {plan.calls} calls a month refused "
            f"{where} with the gateway's quota 403, after {through} calls in this run. The "
            f"session's DELETE was call {plan.calls}, {ended}, and {other.label}, under another "
            "cost center, still reached its tools"
        )
        if through > plan.calls:
            say(
                f"INFO: {grant.label}'s pool allowed {through - plan.calls} call(s) more than its "
                f"{plan.calls} before it refused: API Management's counters are distributed, so "
                "a spent quota can let a call or two through"
            )
    else:
        say(
            f"PASS: {grant.label}'s cost center's pool of {plan.calls} calls a month refused a "
            f"call with the gateway's quota 403 after {through} more call(s), while "
            f"{other.label}, under another cost center, still reached its tools"
        )
        if through < plan.calls:
            say(
                f"INFO: {grant.label}'s pool allowed {through} of its {plan.calls} calls in this "
                "run: something had spent the rest this month"
            )
    retry = refusal.header_int("Retry-After")
    say(
        f"INFO: {grant.label}'s quota 403 "
        f"{'had a Retry-After' if retry is not None else 'had no Retry-After in seconds'}"
    )


def runtime_status(client: httpx.Client, base: str, grant: McpGrant) -> str:
    route = f"{grant_route(grant.kind, grant.entitlement_id)}/mcp-connection"
    response = client.get(f"{base}/{route}", headers=bearer(grant.control_token))
    if response.status_code == 404:
        raise VerificationFailed(
            f"{grant.label}: MOSAIC no longer finds this grant, so the watch can't tell when the "
            "gateway applies its removal. Disable a grant (Revoke in the console) instead of "
            "deleting it"
        )
    model.expect(response, {200}, f"{grant.label} connection details")
    connection = model.object_body(response, f"{grant.label} connection details")
    status = model.mapping(connection.get("runtime")).get("status")
    return status if status in model.RUNTIME_STATUSES else "not set up"


def require_lifetime(
    label: str, held: list[tuple[str, str]], needed: int, flag: str, what: str = "the wait"
) -> None:
    for name, token in held:
        left = model.seconds_left(token)
        if left is not None and left < needed:
            when = "has expired" if left <= 0 else f"expires in {left} seconds"
            raise VerificationFailed(
                f"{label}: the {name} {when}, and {what} can take {needed} seconds. Get a new "
                f"one, or lower {flag}"
            )


def watch_revocation(
    client: httpx.Client,
    base: str,
    grant: McpGrant,
    token: str,
    *,
    timeout: int,
    interval: int,
) -> None:
    """M7: wait for MOSAIC to report the grant revoked, then for the gateway to refuse it.

    Only the grant lookup's refusals after MOSAIC reports the apply finished count, and only when
    they repeat. Each call names the grant's cost center, so the gateway can't fall back to another
    grant the same caller holds on the server.
    """

    require_lifetime(
        grant.label,
        [("MOSAIC control-plane token", grant.control_token), (f"{grant.kind}'s MCP token", token)],
        timeout + interval + model.TOKEN_MARGIN_SECONDS,
        "--revocation-timeout",
    )
    say(
        f"WAIT: revoke {grant.label} in MOSAIC's console, which disables it, and apply its MCP "
        f"server's access plan. Checking every {interval} seconds for up to {timeout} seconds"
    )
    auth = bearer(token)
    named = grant.cost_center is not None
    if grant.cost_center is not None:
        auth[COST_CENTER_HEADER] = grant.cost_center
    deadline = time.monotonic() + timeout
    status = "applied"
    streak = 0
    last = ""
    while True:
        if time.monotonic() >= deadline:
            if status != "revoked":
                raise VerificationFailed(
                    f"{grant.label}: after {timeout} seconds, MOSAIC reports it as {status}, not "
                    "revoked. Revoke it and apply its MCP server's access plan, then rerun"
                )
            raise VerificationFailed(
                f"{grant.label}: MOSAIC reports it revoked, but after {timeout} seconds the "
                "gateway's grant lookup hadn't rejected calls with its token "
                f"{model.REVOCATION_CONFIRMATIONS} times in a row (last: {last})"
            )
        time.sleep(interval)
        current = runtime_status(client, base, grant)
        if current != status:
            status = current
            say(f"WAIT: MOSAIC reports {grant.label} as {status}")
        if status != "revoked":
            streak = 0
            continue
        exchange = probe(client, grant, auth)
        # Only the grant lookup's 403 shows the grant gone. A 401 means token validation refused
        # the token, and a quota's or a budget's 403 that the lookup still found the grant.
        refused = exchange.lookup_refusal(named_cost_center=named)
        last = status_text(exchange)
        if exchange.status == 403 and not refused and exchange.refused_by() is None:
            last += ", not the grant lookup's"
        streak = streak + 1 if refused else 0
        if streak == model.REVOCATION_CONFIRMATIONS:
            say(f"PASS: {grant.label} rejects its token after revocation")
            return


@dataclass(frozen=True)
class OnBehalfUse:
    """The person's model use through one MCP server, as their own usage report shows it."""

    rows: dict[str, dict[str, Any]]

    @property
    def requests(self) -> int:
        return sum(requests_of(row) for row in self.rows.values())


def requests_of(row: dict[str, Any]) -> int:
    value = row.get("requests")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def on_behalf_use(
    client: httpx.Client, base: str, control: str, grant: McpGrant
) -> OnBehalfUse | str:
    """The person's on-behalf rows for the grant's server, or why there are none to read."""

    response = client.get(
        f"{base}/me/usage", params={"period": USAGE_PERIOD}, headers=bearer(control)
    )
    if response.status_code == 404:
        return "this MOSAIC has no usage report"
    model.expect(response, {200}, "The user's usage report")
    report = model.object_body(response, "The user's usage report")
    if report.get("dataSource") == "simulated":
        return (
            "this MOSAIC's usage is simulated, and simulated usage has no model use through MCP "
            "servers"
        )
    rows = report.get("onBehalf")
    if not isinstance(rows, list):
        return "this MOSAIC's usage report has no model use through MCP servers"
    found: dict[str, dict[str, Any]] = {}
    for item in rows:
        row = model.mapping(item)
        if model.mapping(row.get("mcpServer")).get("id") == grant.server_id:
            key = row.get("key")
            found[key if isinstance(key, str) else json.dumps(row.get("resource"))] = row
    return OnBehalfUse(found)


@dataclass(frozen=True)
class ModelGrant:
    resource_kind: str
    resource_id: str
    cost_center_id: str
    subject_id: str | None


def model_grant_body(
    client: httpx.Client, base: str, admin_control: str, entitlement_id: str
) -> dict[str, Any]:
    """The model caller's model grant, as MOSAIC's administrator routes return it."""

    label = "The model caller's model grant"
    response = client.get(f"{base}/entitlements/{entitlement_id}", headers=bearer(admin_control))
    model.expect(response, {200}, label)
    return model.object_body(response, label)


def read_model_grant(
    client: httpx.Client, base: str, admin_control: str, entitlement_id: str
) -> ModelGrant:
    label = "The model caller's model grant"
    entitlement = model_grant_body(client, base, admin_control, entitlement_id)
    resource = model.mapping(entitlement.get("resource"))
    kind, identifier = resource.get("kind"), resource.get("id")
    if kind not in {"modelApi", "poolModel"} or not isinstance(identifier, str):
        raise VerificationFailed(f"{label} isn't a grant on a model")
    subject = model.mapping(entitlement.get("subject"))
    if subject.get("kind") != "application":
        raise VerificationFailed(f"{label} isn't an application's")
    cost_center = entitlement.get("costCenterId")
    return ModelGrant(
        resource_kind=kind,
        resource_id=identifier,
        cost_center_id=cost_center if isinstance(cost_center, str) else "",
        subject_id=subject.get("id") if isinstance(subject.get("id"), str) else None,
    )


def model_grant_status(entitlement: dict[str, Any]) -> str:
    """How far MOSAIC has applied the model caller's grant, or "disabled" if it isn't enabled."""

    if entitlement.get("enabled") is not True:
        return "disabled"
    status = model.mapping(entitlement.get("runtime")).get("status")
    return status if status in model.RUNTIME_STATUSES else "not set up"


@dataclass(frozen=True)
class ModelGrantWait:
    """--await-model-grant: the model caller's grant to wait for before the call, and how long."""

    entitlement_id: str
    admin_control: str = field(repr=False)
    timeout: int
    interval: int


def await_model_grant(client: httpx.Client, base: str, wait: ModelGrantWait) -> None:
    """Wait until MOSAIC reports the model caller's grant enabled and applied.

    A server open to anyone lets anyone reach a model through its model caller's grant, so the
    grant is opened only for the call: the run waits for it here, after every sign-in and check,
    rather than holding it open while the person signs in.
    """

    def read() -> str:
        return model_grant_status(
            model_grant_body(client, base, wait.admin_control, wait.entitlement_id)
        )

    applied = "INFO: MOSAIC reports the model caller's grant as enabled and applied"
    status = read()
    if status == "applied":
        say(applied)
        return
    require_lifetime(
        "The model caller's grant",
        [("administrator's MOSAIC control-plane token", wait.admin_control)],
        wait.timeout + wait.interval + model.TOKEN_MARGIN_SECONDS,
        "--model-grant-timeout",
    )
    say(
        "WAIT: re-enable the model caller's grant in MOSAIC's console and apply its model's "
        f"access plan. Checking every {wait.interval} seconds for up to {wait.timeout} seconds"
    )
    deadline = time.monotonic() + wait.timeout
    try:
        while time.monotonic() < deadline:
            time.sleep(wait.interval)
            current = read()
            if current == "applied":
                say(applied)
                return
            if current != status:
                status = current
                say(f"WAIT: MOSAIC reports the model caller's grant as {status}")
    except BaseException:
        # Whoever was asked to open the grant may have done so: tell them to close it again.
        say(
            "INFO: The run stopped while waiting for the model caller's grant. If you re-enabled "
            "it, revoke it again and apply its model's access plan"
        )
        raise
    raise VerificationFailed(
        f"The model caller's grant: after {wait.timeout} seconds, MOSAIC reports it as {status}, "
        f"not applied, so {ASK_MODEL_TOOL} wasn't called. If you re-enabled it, revoke it again "
        "and apply its model's access plan before you rerun"
    )


def check_model_caller(
    client: httpx.Client, base: str, admin_control: str | None, grant: McpGrant
) -> dict[str, Any] | None:
    """The application the server calls models as, as its last apply compiled it."""

    if admin_control is None:
        say(
            f"SKIP: {grant.label} checking that its server names an applied model caller: set "
            f"{ADMIN_CONTROL_TOKEN}"
        )
        return None
    caller = model.mapping(read_publication(client, base, admin_control, grant).get("modelCaller"))
    if model.guid(caller.get("objectId")) is None:
        raise VerificationFailed(
            f"{grant.label}: its server's last apply names no model caller, so the server "
            "receives no reference and its model calls can't be attributed. Name the "
            "application under Calls models as, then plan and apply the server"
        )
    say(f"PASS: {grant.label}'s server passes each call's reference to the application it names")
    return caller


def ask_model(
    client: httpx.Client, grant: McpGrant, token: str, *, limit: int | None = None
) -> tuple[float, float]:
    """Call the server's ask_model tool as the person, and return when the call started and ended.

    The answer is model output, so it's never printed: it only has to exist. With ``limit``, the
    call gives up that many seconds after it starts.
    """

    session = new_session(client, grant, token)
    session.cost_center = grant.cost_center
    started = time.time()
    if limit is not None:
        session.deadline = time.monotonic() + limit
    with closing_session(session):
        open_session(session)
        if ASK_MODEL_TOOL not in list_tools(session):
            raise VerificationFailed(f"{grant.label}: the server doesn't list {ASK_MODEL_TOOL}")
        exchange = session.send(
            "tools/call",
            {"name": ASK_MODEL_TOOL, "arguments": {"question": PROMPT}},
            timeout=ASK_MODEL_TIMEOUT_SECONDS,
        )
        result = tool_result(exchange, session, ASK_MODEL_TOOL)
        if not any(item.strip() for item in texts(result)):
            raise VerificationFailed(f"{grant.label}: {ASK_MODEL_TOOL} returned no answer")
    return started, time.time()


def report_facts(
    grant: McpGrant,
    token: str,
    window: tuple[float, float],
    caller: dict[str, Any] | None,
) -> None:
    """What finds the call in the gateway's logs: when, who, and the trace its grant writes."""

    person = model.object_id(token) or ""
    azp = model.token_claims(token).get("azp")
    client_app = azp.casefold() if isinstance(azp, str) else ""
    started, ended = window
    say(f"INFO: {grant.label} called {ASK_MODEL_TOOL} between {utc(started)} and {utc(ended)} UTC")
    say(f"INFO: {grant.label} was called by object ID {person}, through client {client_app}")
    if grant.publication_id is None:
        return
    key = grant_trace_key(grant.tenant_id, grant.publication_id, grant.entitlement_id)
    # A security-group grant's trace names its member; a direct grant's names none.
    member = person if grant.via_group else ""
    trace = f"mosaic-attribution v=1 g={key} m={member} a={client_app}"
    caller_id = model.guid(caller.get("objectId")) if caller else None
    if caller_id is not None:
        trace += f" r=<request ID> i={caller_id}"
    say(f"INFO: {grant.label}'s MCP call trace reads: {trace}")


def await_attribution(
    client: httpx.Client,
    base: str,
    grant: McpGrant,
    baseline: OnBehalfUse,
    model_grant: ModelGrant | None,
    *,
    timeout: int,
    interval: int,
) -> None:
    """Wait until the person's usage report attributes new model use through the server to them.

    The report is the person's own, so a row in it is their attribution. With the model caller's
    grant, each new row must also name that grant's model and cost center.
    """

    say(
        f"WAIT: {grant.label} waiting for MOSAIC to attribute the model call to the person. "
        f"Checking every {interval} seconds for up to {timeout} seconds"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(interval)
        found = on_behalf_use(client, base, grant.control_token, grant)
        if isinstance(found, str):
            raise VerificationFailed(f"{grant.label}: {found}")
        grew = [
            row
            for key, row in found.rows.items()
            if requests_of(row) > requests_of(baseline.rows.get(key, {}))
        ]
        if not grew:
            continue
        say(
            f"PASS: {grant.label}: MOSAIC attributed {found.requests - baseline.requests} model "
            "call(s) through its server to the person, in their own usage report"
        )
        for row in grew:
            resource = model.mapping(row.get("resource"))
            cost_center = model.mapping(row.get("costCenter"))
            if model_grant is None:
                name, code = resource.get("displayName"), cost_center.get("code")
                shown_name = name if isinstance(name, str) and SAFE_NAME.fullmatch(name) else "?"
                shown_code = (
                    code if isinstance(code, str) and COST_CENTER_CODE.fullmatch(code) else "?"
                )
                say(
                    f"INFO: {grant.label}'s attributed calls were to the model {shown_name}, "
                    f"charged to cost center {shown_code}"
                )
            elif (
                resource.get("kind") != model_grant.resource_kind
                or resource.get("id") != model_grant.resource_id
                or cost_center.get("id") != model_grant.cost_center_id
            ):
                raise VerificationFailed(
                    f"{grant.label}: MOSAIC attributed the model call to a model or cost center "
                    "other than the model caller's grant"
                )
        if model_grant is not None:
            say(
                f"PASS: {grant.label}: the attributed calls were charged to the model caller's "
                "grant, on its model and under its cost center"
            )
        return
    raise VerificationFailed(
        f"{grant.label}: after {timeout} seconds, the person's usage report shows no new model use "
        "through its server. Attribution follows the gateway's logs and MOSAIC's usage rollup, "
        "so check again later for the window above"
    )


def call_on_behalf(
    client: httpx.Client,
    base: str,
    grant: McpGrant,
    token: str,
    *,
    caller: dict[str, Any] | None,
    model_grant: ModelGrant | None,
    attribution: tuple[int, int] | None,
    grant_wait: ModelGrantWait | None = None,
) -> None:
    """M9: a person's call to a tool that calls a governed model as the server's application.

    With ``grant_wait``, the call waits until the model caller's grant is applied. Once it is, the
    run says to revoke the grant again as soon as the call is made, or the run stops.
    """

    if attribution is not None:
        timeout, interval = attribution
        needed = timeout + interval + model.TOKEN_MARGIN_SECONDS
        flag = "--attribution-timeout"
        if grant_wait is not None:
            # The usage report is read after the wait for the grant, so the token must last both.
            needed += grant_wait.timeout + grant_wait.interval
            flag = "--model-grant-timeout or --attribution-timeout"
        require_lifetime(
            grant.label, [("MOSAIC control-plane token", grant.control_token)], needed, flag
        )
    if grant_wait is not None:
        # The person's token makes the call once the grant is applied: it must last the wait and
        # the call, or the grant would be opened for nothing.
        require_lifetime(
            grant.label,
            [("user's MCP token", token)],
            grant_wait.timeout
            + grant_wait.interval
            + ASK_MODEL_LONGEST_SECONDS
            + model.TOKEN_MARGIN_SECONDS,
            "--model-grant-timeout",
            "the wait and the call",
        )
        await_model_grant(client, base, grant_wait)
    baseline: OnBehalfUse | None = None
    try:
        if attribution is not None:
            found = on_behalf_use(client, base, grant.control_token, grant)
            if isinstance(found, str):
                say(f"SKIP: {grant.label} waiting for its attribution: {found}")
            else:
                baseline = found
        # With the grant open, the call must end in time: the person's token was checked against
        # that, and the grant stays open until the run says to revoke it.
        limit = ASK_MODEL_DEADLINE_SECONDS if grant_wait is not None else None
        window = ask_model(client, grant, token, limit=limit)
    except BaseException:
        if grant_wait is not None:
            say(
                f"INFO: {grant.label} stopped after the model caller's grant was applied. "
                f"{REVOKE_MODEL_GRANT}"
            )
        raise
    say(f"PASS: {grant.label} answered through {ASK_MODEL_TOOL}, which calls a governed model")
    report_facts(grant, token, window, caller)
    if grant_wait is not None:
        goes_on = "; the attribution wait goes on" if baseline is not None else ""
        say(f"INFO: {grant.label} made its model call. {REVOKE_MODEL_GRANT}{goes_on}")
    if attribution is not None and baseline is not None:
        timeout, interval = attribution
        await_attribution(
            client, base, grant, baseline, model_grant, timeout=timeout, interval=interval
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
        help="A user's MCP grant on a server with the echo and add tools. Repeat it for each "
        "grant; they must share one user",
    )
    parser.add_argument(
        "--application-entitlement",
        action="append",
        default=[],
        metavar="ID",
        help="An application's MCP grant on a server with the echo and add tools. Repeat it for "
        "each grant of the same application",
    )
    parser.add_argument(
        "--on-behalf-entitlement",
        metavar="ID",
        help="The user's MCP grant on a server whose ask_model tool calls a governed model",
    )
    parser.add_argument(
        "--model-caller-entitlement",
        metavar="ID",
        help="The model grant of the application that server calls models as, for "
        "--await-attribution to compare with",
    )
    parser.add_argument("--user-token-source", choices=("env", "device-code"), default="env")
    parser.add_argument(
        "--application-token-source", choices=("env", "client-credentials"), default="env"
    )
    parser.add_argument(
        "--check-ungranted-user",
        action="store_true",
        help="Also check that a different, ungranted user's token is refused",
    )
    parser.add_argument(
        "--check-missing-scope",
        action="store_true",
        help=f"Also check that the user's token without Mcp.Invoke, from {MODEL_RUNTIME_TOKEN}, "
        "is refused",
    )
    proofs = parser.add_mutually_exclusive_group()
    proofs.add_argument(
        "--prove-call-limit",
        metavar="ID",
        help=f"For a grant in this run limited to {FLOW_CALLS + 1} to {CALL_LIMIT_CEILING} calls "
        "per 60 to 300 seconds: calls must spend x-mosaic-remaining-calls to the gateway's 429",
    )
    proofs.add_argument(
        "--prove-pooled-quota",
        metavar="ID",
        help="For a grant in this run whose cost center has a pooled call quota on the server: "
        "spend the pool, which must then refuse, while another grant in this run under a "
        "different cost center still works",
    )
    parser.add_argument(
        "--watch-revocation",
        metavar="ID",
        help="After the checks, wait until revoking this listed grant takes effect",
    )
    parser.add_argument(
        "--revocation-timeout", type=int, default=DEFAULT_REVOCATION_TIMEOUT, metavar="SECONDS"
    )
    parser.add_argument(
        "--revocation-interval", type=int, default=DEFAULT_REVOCATION_INTERVAL, metavar="SECONDS"
    )
    parser.add_argument(
        "--send-model-requests",
        action="store_true",
        help="Acknowledge that ask_model sends a real, billed model request. Needed with "
        "--on-behalf-entitlement",
    )
    parser.add_argument(
        "--await-attribution",
        action="store_true",
        help="After the ask_model call, wait until the user's usage report attributes it",
    )
    parser.add_argument(
        "--attribution-timeout", type=int, default=DEFAULT_ATTRIBUTION_TIMEOUT, metavar="SECONDS"
    )
    parser.add_argument(
        "--attribution-interval", type=int, default=DEFAULT_ATTRIBUTION_INTERVAL, metavar="SECONDS"
    )
    parser.add_argument(
        "--await-model-grant",
        action="store_true",
        help="After every sign-in and check, wait until the --model-caller-entitlement grant is "
        "enabled and applied before calling ask_model, so it's open only for the call",
    )
    parser.add_argument(
        "--model-grant-timeout", type=int, default=DEFAULT_MODEL_GRANT_TIMEOUT, metavar="SECONDS"
    )
    parser.add_argument(
        "--model-grant-interval", type=int, default=DEFAULT_MODEL_GRANT_INTERVAL, metavar="SECONDS"
    )
    return parser.parse_args(argv)


@dataclass(frozen=True)
class Setup:
    origin: str
    user_control: str | None = field(repr=False)
    admin_control: str | None = field(repr=False)


def validate(args: argparse.Namespace) -> Setup:
    """Check arguments and credentials before any network call."""

    model.https_origin(args.api_base_url)
    if urlsplit(args.api_base_url).query:
        raise VerificationFailed("The control-plane base URL must not contain a query string")
    origin = model.https_origin(args.gateway_origin)
    if urlsplit(args.gateway_origin).path not in {"", "/"}:
        raise VerificationFailed("Supply the gateway origin without an API path")
    own = [*args.user_entitlement, *args.application_entitlement]
    listed = [*own, *([args.on_behalf_entitlement] if args.on_behalf_entitlement else [])]
    named = [*listed, *([args.model_caller_entitlement] if args.model_caller_entitlement else [])]
    if not listed:
        raise VerificationFailed(
            "Supply at least one --user-entitlement, --application-entitlement or "
            "--on-behalf-entitlement"
        )
    if not all(model.ENTITLEMENT.fullmatch(identifier) for identifier in named):
        raise VerificationFailed("Supply an entitlement identifier, not a URL")
    if len(set(named)) != len(named):
        raise VerificationFailed("List each entitlement only once")
    if args.on_behalf_entitlement and not args.send_model_requests:
        raise VerificationFailed(
            "Add --send-model-requests to acknowledge that ask_model sends a real, billed model "
            "request"
        )
    if (args.await_attribution or args.model_caller_entitlement) and not args.on_behalf_entitlement:
        raise VerificationFailed(
            "--await-attribution and --model-caller-entitlement need an --on-behalf-entitlement"
        )
    if args.model_caller_entitlement and not args.await_attribution:
        raise VerificationFailed("--model-caller-entitlement is only used with --await-attribution")
    if args.await_model_grant and not args.model_caller_entitlement:
        raise VerificationFailed("--await-model-grant needs a --model-caller-entitlement")
    if (args.check_ungranted_user or args.check_missing_scope) and not args.user_entitlement:
        raise VerificationFailed(
            "--check-ungranted-user and --check-missing-scope need a --user-entitlement"
        )
    for flag, value in (
        ("--prove-call-limit", args.prove_call_limit),
        ("--prove-pooled-quota", args.prove_pooled_quota),
        ("--watch-revocation", args.watch_revocation),
    ):
        if value is not None and value not in own:
            raise VerificationFailed(
                f"{flag} must name a --user-entitlement or --application-entitlement in this run"
            )
    if args.watch_revocation is not None and args.await_attribution:
        raise VerificationFailed(
            "Wait for one thing per run: --watch-revocation or --await-attribution"
        )
    if not 60 <= args.revocation_timeout <= 3600:
        raise VerificationFailed("--revocation-timeout must be from 60 to 3600 seconds")
    if not 10 <= args.revocation_interval <= 300:
        raise VerificationFailed("--revocation-interval must be from 10 to 300 seconds")
    if not 60 <= args.attribution_timeout <= 3600:
        raise VerificationFailed("--attribution-timeout must be from 60 to 3600 seconds")
    if not 30 <= args.attribution_interval <= 600:
        raise VerificationFailed("--attribution-interval must be from 30 to 600 seconds")
    if not 60 <= args.model_grant_timeout <= 1800:
        raise VerificationFailed("--model-grant-timeout must be from 60 to 1800 seconds")
    if not 10 <= args.model_grant_interval <= 120:
        raise VerificationFailed("--model-grant-interval must be from 10 to 120 seconds")
    people = bool(args.user_entitlement or args.on_behalf_entitlement)
    user_control = (
        credential(USER_CONTROL_TOKEN) if people else model.optional_credential(USER_CONTROL_TOKEN)
    )
    needs_admin = bool(
        args.application_entitlement or args.prove_pooled_quota or args.model_caller_entitlement
    )
    admin_control = (
        credential(ADMIN_CONTROL_TOKEN)
        if needs_admin
        else model.optional_credential(ADMIN_CONTROL_TOKEN)
    )
    if people and args.user_token_source == "env":
        credential(USER_RUNTIME_TOKEN)
    if args.application_entitlement:
        if args.application_token_source == "client-credentials":
            if model.guid(credential(APPLICATION_CLIENT_ID)) is None:
                raise VerificationFailed(
                    f"{APPLICATION_CLIENT_ID} must be the application's client ID"
                )
            credential(APPLICATION_CLIENT_SECRET)
        else:
            credential(APPLICATION_RUNTIME_TOKEN)
    if args.check_ungranted_user and args.user_token_source == "env":
        credential(UNGRANTED_USER_RUNTIME_TOKEN)
    if args.check_missing_scope:
        credential(MODEL_RUNTIME_TOKEN)
    return Setup(origin=origin, user_control=user_control, admin_control=admin_control)


def run(client: httpx.Client, args: argparse.Namespace, setup: Setup) -> int:
    base = f"{args.api_base_url.rstrip('/')}/api/v1"
    grants: list[McpGrant] = []
    subjects: list[tuple[Kind, str, list[str], str | None]] = [
        ("user", "User", args.user_entitlement, setup.user_control),
        ("application", "Application", args.application_entitlement, setup.admin_control),
    ]
    for kind, noun, identifiers, control in subjects:
        for index, identifier in enumerate(identifiers, start=1):
            grants.append(
                load_grant(
                    client,
                    base,
                    kind=kind,
                    label=f"{noun} grant {index}",
                    entitlement_id=identifier,
                    control_token=control or "",
                    origin=setup.origin,
                )
            )
    on_behalf = (
        load_grant(
            client,
            base,
            kind="user",
            label="On-behalf grant",
            entitlement_id=args.on_behalf_entitlement,
            control_token=setup.user_control or "",
            origin=setup.origin,
        )
        if args.on_behalf_entitlement
        else None
    )
    by_id = {grant.entitlement_id: grant for grant in grants}
    flows: dict[str, Flow] = {}
    pool_plan: PoolPlan | None = None
    if args.prove_call_limit is not None:
        grant = by_id[args.prove_call_limit]
        problem = call_limit_problem(grant)
        if problem is not None:
            raise VerificationFailed(f"{grant.label}: {problem}")
        flows[grant.entitlement_id] = "call-limit"
    if args.prove_pooled_quota is not None:
        grant = by_id[args.prove_pooled_quota]
        pool_plan = plan_pooled_quota(client, base, setup.admin_control or "", grant, grants)
        flows[grant.entitlement_id] = "pooled-quota"
    model_grant = (
        read_model_grant(client, base, setup.admin_control or "", args.model_caller_entitlement)
        if args.model_caller_entitlement
        else None
    )
    tokens = Tokens(
        client,
        user_source=args.user_token_source,
        application_source=args.application_token_source,
        person=model.object_id(setup.user_control) if setup.user_control else None,
    )
    # Every sign-in happens before the first MCP call, so none can split a call-limit window.
    held = {grant.entitlement_id: tokens.own(grant) for grant in grants}
    on_behalf_token = tokens.user(on_behalf) if on_behalf is not None else None
    users = [grant for grant in grants if grant.kind == "user"]
    stranger = (
        tokens.stranger(users[0], [held[grant.entitlement_id] for grant in users])
        if args.check_ungranted_user
        else None
    )
    missing_scope = tokens.missing_scope(users[0]) if args.check_missing_scope else None
    caller = (
        check_model_caller(client, base, setup.admin_control, on_behalf)
        if on_behalf is not None
        else None
    )
    if (
        model_grant is not None
        and caller is not None
        and model_grant.subject_id != caller.get("principalId")
    ):
        raise VerificationFailed(
            "The model caller's model grant belongs to a different application from the one "
            "the on-behalf server calls models as"
        )
    checked: set[str] = set()
    for grant in grants:
        token = held[grant.entitlement_id]
        check_discovery(client, grant, checked)
        check_refusals(client, grant, token, stranger=stranger, missing_scope=missing_scope)
        flow = flows.get(grant.entitlement_id, "tools")
        if flow == "call-limit":
            prove_call_limit(client, grant, token)
        elif flow == "pooled-quota" and pool_plan is not None:
            prove_pooled_quota(
                client, grant, token, pool_plan, held[pool_plan.other.entitlement_id]
            )
        else:
            check_tools(client, grant, token)
    if on_behalf is not None and on_behalf_token is not None:
        check_discovery(client, on_behalf, checked)
        call_on_behalf(
            client,
            base,
            on_behalf,
            on_behalf_token,
            caller=caller,
            model_grant=model_grant,
            attribution=(args.attribution_timeout, args.attribution_interval)
            if args.await_attribution
            else None,
            grant_wait=ModelGrantWait(
                entitlement_id=args.model_caller_entitlement,
                admin_control=setup.admin_control or "",
                timeout=args.model_grant_timeout,
                interval=args.model_grant_interval,
            )
            if args.await_model_grant
            else None,
        )
    if args.watch_revocation is not None:
        watched = by_id[args.watch_revocation]
        watch_revocation(
            client,
            base,
            watched,
            held[watched.entitlement_id],
            timeout=args.revocation_timeout,
            interval=args.revocation_interval,
        )
    return len(grants) + (1 if on_behalf is not None else 0)


def main(argv: list[str] | None = None, *, transport: httpx.BaseTransport | None = None) -> int:
    args = parse_arguments(argv)
    try:
        setup = validate(args)
        with httpx.Client(
            timeout=CLIENT_TIMEOUT_SECONDS, follow_redirects=False, transport=transport
        ) as client:
            count = run(client, args, setup)
    except KeyboardInterrupt:
        print("STOPPED: interrupted before the checks finished", file=sys.stderr)
        return 130
    except (VerificationFailed, httpx.HTTPError) as error:
        # HTTP exceptions can carry request headers or URLs; never print their representation.
        message = str(error) if isinstance(error, VerificationFailed) else "HTTP transport failure"
        print(f"FAIL: {message}", file=sys.stderr)
        return 1
    later = "" if args.watch_revocation else " Check revocation separately with --watch-revocation."
    say(f"Live MCP checks passed for {count} grant(s).{later}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
