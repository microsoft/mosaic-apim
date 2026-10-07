"""Tests for scripts/verify_mcp_access.py, against a fake MOSAIC, Entra, gateway and MCP servers.

The fake gateway applies the MCP policy of ADR 0017, as mosaic_api's MCP access policy compiles
it: 401 with the protected resource metadata for an anonymous call, 401 when token validation
fails, 403 for a caller without a grant or without Mcp.Invoke, cost-center selection and its
refusals, call limits, pooled quotas and revocation. Behind it, the fake MCP servers speak
streamable HTTP strictly: sessions, the protocol version header, the initialized notification, and
JSON or event-stream responses. Each switch on the fake breaks one rule, so each check the verifier
makes has a test that fails without it.
"""

import base64
import contextlib
import io
import json
import os
import re
import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx

from scripts import verify_mcp_access as verifier
from scripts import verify_model_access as model_verifier

TENANT = "11111111-1111-4111-8111-111111111111"
# The model-runtime registration's client ID: the audience of MCP and model tokens alike.
AUDIENCE = "22222222-2222-4222-8222-222222222222"
MODEL_CLIENT = "33333333-3333-4333-8333-333333333333"
APP_CLIENT = "44444444-4444-4444-8444-444444444444"
USER_OID = "55555555-5555-4555-8555-555555555555"
STRANGER_OID = "66666666-6666-4666-8666-666666666666"
APP_OID = "77777777-7777-4777-8777-777777777777"
ADMIN_OID = "88888888-8888-4888-8888-888888888888"
# The application the agent server calls models as.
AGENT_OID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
APP_SECRET = "fixture-client-secret"
MODEL_OUTPUT = "MODEL-OUTPUT-FIXTURE"
ECHO = "ECHOED-REQUEST-FIXTURE"
ORIGIN = "https://gateway.example"
API = "https://mosaic.example"
MODEL_GRANT = "agent-model"
COST_CENTER_DENIED = (
    "Access denied. You hold no grant under the cost center the x-mosaic-cost-center header "
    "names, or the header isn't a cost center code."
)
BUDGET_DENIED = (
    "Access denied. Cost center {code} has used its monthly budget, so its calls are refused "
    "until an administrator raises the budget or the month ends."
)
WALL_CLOCK = 1_700_000_000
START = 1_000.0
# The session a stateful server names when it refuses a request without one. It's already gone.
DISCARDED_SESSION = "discarded-session"


def jwt(**claims: Any) -> str:
    """A token with these claims. It expires in two hours unless `exp` says otherwise or is None."""
    claims = {"exp": WALL_CLOCK + 7_200, **claims}
    claims = {name: value for name, value in claims.items() if value is not None}

    def encode(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode(claims)}.fixture-signature"


def person_token(oid: str | None = USER_OID, **claims: Any) -> str:
    defaults = {
        "aud": AUDIENCE,
        "oid": oid,
        "tid": TENANT,
        "scp": "Mcp.Invoke",
        "ver": "2.0",
        "azp": MODEL_CLIENT,
    }
    return jwt(**{**defaults, **claims})


def app_token(oid: str | None = APP_OID, **claims: Any) -> str:
    defaults = {
        "aud": AUDIENCE,
        "oid": oid,
        "tid": TENANT,
        "roles": ["Mcp.Invoke.Application"],
        "ver": "2.0",
        "azp": APP_CLIENT,
    }
    return jwt(**{**defaults, **claims})


def claims_of(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3 or parts[2] != "fixture-signature":
        return None
    decoded: dict[str, Any] = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    return decoded


def call_name(request: httpx.Request) -> str:
    """What a call to the MCP endpoint was: its JSON-RPC method, or DELETE."""
    if request.method == "POST":
        return str(json.loads(request.content).get("method"))
    return request.method


USER_CONTROL = jwt(aud="api://mosaic-api", oid=USER_OID, tid=TENANT)
ADMIN_CONTROL = jwt(aud="api://mosaic-api", oid=ADMIN_OID, tid=TENANT)
ENV = {
    verifier.USER_CONTROL_TOKEN: USER_CONTROL,
    verifier.ADMIN_CONTROL_TOKEN: ADMIN_CONTROL,
    verifier.USER_RUNTIME_TOKEN: person_token(),
    verifier.APPLICATION_RUNTIME_TOKEN: app_token(),
    verifier.UNGRANTED_USER_RUNTIME_TOKEN: person_token(STRANGER_OID),
    # The user's model token: valid at the gateway, but without Mcp.Invoke.
    verifier.MODEL_RUNTIME_TOKEN: person_token(scp="Models.Invoke"),
}
BASE_ARGS = ["--api-base-url", API, "--gateway-origin", ORIGIN]
TOOLS = {
    "echo": {
        "name": "echo",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
    "utc_now": {"name": "utc_now", "inputSchema": {"type": "object", "properties": {}}},
    "add": {
        "name": "add",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
        },
    },
    "ask_model": {
        "name": "ask_model",
        "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}},
    },
}


def without(*names: str) -> dict[str, str]:
    return {name: value for name, value in ENV.items() if name not in names}


@dataclass
class FakeServer:
    key: str
    api_path: str
    display_name: str
    tools: tuple[str, ...]
    # The application its tools call models as, once applied.
    model_caller: str | None = None
    event_stream: bool = False
    sessions: bool = True
    revision: str = "2025-11-25"
    tools_capability: bool = True

    @property
    def publication_id(self) -> str:
        return f"mcppub-{self.key}"

    @property
    def server_id(self) -> str:
        return f"mcpsrv-{self.key}"

    @property
    def url(self) -> str:
        return f"{ORIGIN}/{self.api_path}/mcp"

    @property
    def metadata_url(self) -> str:
        return f"{ORIGIN}/.well-known/oauth-protected-resource/{self.api_path}/mcp"


def default_servers() -> list[FakeServer]:
    return [
        FakeServer("tools", "mcp/m-tools", "M-tools", ("echo", "utc_now", "add")),
        # On Functions behind the gateway's managed identity: stateless, answering in streams.
        FakeServer(
            "protected",
            "mcp/m-protected",
            "M-protected",
            ("echo", "utc_now", "add"),
            event_stream=True,
            sessions=False,
        ),
        FakeServer("agent", "mcp/m-agent", "M-agent", ("ask_model",), model_caller=AGENT_OID),
    ]


@dataclass
class FakeGrant:
    id: str
    kind: str
    server: str
    oid: str
    cost_center: str = "general"
    # Whether it's under its holder's default cost center, which a call naming none uses first.
    default: bool = True
    order: int = 0
    limits: dict[str, Any] | None = None
    status: str = "applied"
    # When an administrator disables it (Revoke in the console), and when it's deleted.
    revoke_at: float | None = None
    deleted_at: float | None = None

    @property
    def cost_center_id(self) -> str:
        return f"cc-{self.cost_center.lower()}"


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
    """MOSAIC's API, Entra sign-in, a gateway applying the MCP policy, and the MCP servers."""

    def __init__(self, *grants: FakeGrant) -> None:
        self.clock = FakeClock()
        self.servers = {server.key: server for server in default_servers()}
        self.grants = {grant.id: grant for grant in grants}
        self.model_client: str | None = MODEL_CLIENT
        self.user_control = USER_CONTROL
        # Overrides for every grant's connection details, to break one field at a time.
        self.connection_patch: dict[str, Any] = {}
        # Pooled monthly call quotas, by server and cost center code, and what's spent of them.
        self.pools: dict[tuple[str, str], int] = {}
        self.pool_spent: dict[tuple[str, str], int] = {}
        self.rate_calls: dict[str, list[float]] = {}
        self.quota_spent: dict[str, int] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.session_count = 0
        # The gateway's rules. Each switch breaks the one a check relies on.
        self.open_to_anonymous = False
        self.challenge_anonymous = True
        # A different resource metadata URL for the anonymous call's challenge to name.
        self.challenge_metadata_url: str | None = None
        self.metadata_patch: dict[str, Any] = {}
        self.metadata_json = True
        self.validate_tokens = True
        # Whether the 401 for an invalid token names the metadata, as the API's on-error adds it.
        self.challenge_invalid_tokens = True
        self.require_mcp_scope = True
        self.match_grants = True
        self.read_cost_center = True
        self.cost_center_case_sensitive = False
        self.refuse_malformed_cost_center = True
        # Whether a 403 for no grant asks for the scope, and a cost-center 403 names the header.
        self.scope_challenge = True
        self.name_cost_center_header = True
        self.rate_limits = True
        self.remaining_header = True
        self.remaining_falls = True
        # Added to the header's value: 1 reports the calls left before this one, not after.
        self.remaining_offset = 0
        # Refuse a call one early, while the header still says one is left.
        self.refuse_early = False
        self.retry_after = True
        # False applies a cost center's pool to every grant on the server.
        self.pools_by_cost_center = True
        self.enforce_pools = True
        # Calls a spent pool still lets through, as API Management's distributed counters can.
        self.pool_overshoot = 0
        # Calls the MCP servers serve before throttling with a 429 of their own.
        self.backend_capacity: int | None = None
        # Seconds each gateway call takes.
        self.call_seconds = 0.0
        # A revocation's timeline: MOSAIC shows it pending, then applies the server's plan.
        self.apply_delay = 20.0
        self.apply_seconds = 30.0
        self.revocation_reaches_gateway = True
        # Gateway units that pick up a revocation unevenly, so refusals alternate with successes.
        self.revocation_flaps = False
        self.unit_calls: dict[str, int] = {}
        self.revoked_status = 403
        # From when every cost center's budget blocks its calls (ADR 0023).
        self.budget_blocked_from: float | None = None
        # The MCP servers' behavior.
        self.echo_works = True
        self.add_works = True
        # False answers DELETE with 405, as a stateful server that can't end a session does.
        self.session_delete = True
        self.tool_error: str | None = None
        self.rpc_error: str | None = None
        self.wrong_id = False
        self.require_protocol_header = True
        self.require_session = True
        self.require_initialized = True
        self.leading_events = True
        self.answer_text = MODEL_OUTPUT
        self.tool_pages = 1
        # The agent server's model calls, and what the person's usage report makes of them.
        self.model_caller_applied = True
        self.model_calls: list[dict[str, Any]] = []
        self.attribution_delay = 600.0
        self.attribute = True
        self.attributed_resource: dict[str, Any] = {
            "kind": "modelApi",
            "id": "model-api-gpt",
            "displayName": "gpt-4o-mini",
        }
        self.attributed_cost_center: dict[str, Any] = {
            "id": "cc-agents",
            "name": "Agents",
            "code": "agents",
        }
        # The person's model use through the agent server earlier in the week.
        self.prior_requests = 3
        self.usage_status: int | None = None
        self.data_source = "measured"
        self.model_grant: dict[str, Any] = {
            "id": MODEL_GRANT,
            "subject": {"kind": "application", "id": "principal-agent"},
            "resource": {"kind": "modelApi", "id": "model-api-gpt"},
            "costCenterId": "cc-agents",
        }
        # For each device sign-in in turn: who signs in, and how each poll before they finish is
        # answered: an OAuth error with HTTP 400, a status with an HTML page and no OAuth error, or
        # a status with an OAuth error.
        self.device_logins: list[tuple[str, list[str | int | tuple[int, str]]]] = []
        self.device_error: dict[str, Any] | None = None
        self.issued: list[str] = []
        self.login_forms: list[tuple[str, dict[str, str]]] = []
        # What happened.
        self.requests: list[httpx.Request] = []
        self.gateway_calls: list[httpx.Request] = []
        # The requests the gateway let through to a server.
        self.passed: list[httpx.Request] = []
        self.forwarded: list[httpx.Headers] = []
        self.messages: list[dict[str, Any]] = []
        self.deletes: list[str] = []
        self.call_times: list[float] = []
        # By cost center, each call past the grant lookup and the budget, and the status a limit
        # refused it with, or None. Notifications and DELETE count like any other call.
        self.cost_center_calls: dict[str, list[tuple[str, int | None]]] = {}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "mosaic.example":
            return self.control(request)
        if request.url.host == "gateway.example":
            return self.gateway(request)
        if request.url.host == "login.microsoftonline.com":
            return self.login(request)
        raise AssertionError("unexpected host")

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

    def on(self, server: FakeServer) -> list[FakeGrant]:
        return [
            grant
            for grant in self.grants.values()
            if grant.server == server.key and not self.deleted(grant)
        ]

    def control(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1/")
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if path == "me/usage":
            if token != self.user_control:
                return httpx.Response(401)
            assert request.url.params.get("period") == "7d"
            if self.usage_status is not None:
                return httpx.Response(self.usage_status)
            return httpx.Response(200, json=self.usage(USER_OID))
        if path.startswith("mcp-publications/"):
            if token != ADMIN_CONTROL:
                return httpx.Response(401)
            identifier = path.removeprefix("mcp-publications/")
            server = next(
                (item for item in self.servers.values() if item.publication_id == identifier),
                None,
            )
            if server is None:
                return httpx.Response(404)
            return httpx.Response(200, json=self.publication(server))
        if path == f"entitlements/{MODEL_GRANT}":
            if token != ADMIN_CONTROL:
                return httpx.Response(401)
            return httpx.Response(200, json=self.model_grant)
        parts = path.split("/")
        mine = parts[0] == "me"
        parts = parts[1:] if mine else parts
        if len(parts) != 3 or parts[0] != "entitlements" or parts[2] != "mcp-connection":
            return httpx.Response(404)
        grant = self.grants.get(parts[1])
        if grant is None or self.deleted(grant):
            return httpx.Response(404)
        if mine:
            if token != self.user_control:
                return httpx.Response(401)
            if grant.kind != "user" or grant.oid != USER_OID:
                return httpx.Response(404)
        elif token != ADMIN_CONTROL:
            return httpx.Response(401)
        return httpx.Response(200, json=self.connection(grant))

    def connection(self, grant: FakeGrant) -> dict[str, Any]:
        server = self.servers[grant.server]
        user = grant.kind == "user"
        phase = self.phase(grant)
        limits = grant.limits
        return {
            "entitlementId": grant.id,
            "mcpServerId": server.server_id,
            "publicationId": server.publication_id,
            "gatewayId": "gateway",
            "displayName": server.display_name,
            "tenantId": TENANT,
            "serverUrl": server.url,
            "transport": "streamable",
            "enforced": phase == "applied",
            "statusMessage": ECHO,
            "runtime": {"publicationId": server.publication_id, "status": phase},
            "entraAudience": AUDIENCE,
            "delegatedScope": f"api://{AUDIENCE}/Mcp.Invoke" if user else None,
            "applicationScope": None if user else f"api://{AUDIENCE}/.default",
            "requiredAppRole": None if user else "Mcp.Invoke.Application",
            "clientId": self.model_client if user else None,
            "principalKind": "user" if user else "servicePrincipal",
            "resourceMetadataUrl": server.metadata_url,
            "limits": {"requests": {"counterKeyExpression": "grant", **limits}} if limits else None,
            "costCenter": {
                "id": grant.cost_center_id,
                "name": grant.cost_center.title(),
                "code": grant.cost_center,
            },
            "costCenterHeader": "x-mosaic-cost-center",
            **self.connection_patch,
        }

    def publication(self, server: FakeServer) -> dict[str, Any]:
        """The publication as its last apply compiled it, which only an administrator reads."""
        snapshot: dict[str, Any] = {
            "version": 1,
            "audience": AUDIENCE,
            "grants": [
                {
                    "entitlementId": grant.id,
                    "subject": {"kind": grant.kind, "id": f"principal-{grant.id}"},
                    "objectId": grant.oid,
                    "enabled": self.phase(grant) != "revoked",
                    "enforcement": {"requests": grant.limits} if grant.limits else None,
                    "costCenterId": grant.cost_center_id,
                    "costCenterCode": grant.cost_center,
                }
                for grant in self.on(server)
            ],
            "pools": [
                {"costCenterId": f"cc-{code}", "costCenterCode": code, "monthlyCalls": calls}
                for (key, code), calls in self.pools.items()
                if key == server.key
            ],
        }
        if server.model_caller and self.model_caller_applied:
            snapshot["modelCaller"] = {
                "principalId": "principal-agent",
                "objectId": server.model_caller,
                "displayName": "Agent application",
            }
        return {"id": server.publication_id, "appliedAccess": snapshot}

    def usage(self, oid: str) -> dict[str, Any]:
        """The person's usage report, with their model use through MCP servers (ADR 0025)."""
        agent = self.servers["agent"]
        attributed = [
            call
            for call in self.model_calls
            if self.attribute
            and call["person"] == oid
            and call["reference"]
            and call["application"] == AGENT_OID
            and self.clock.now >= call["at"] + self.attribution_delay
        ]
        rows: list[dict[str, Any]] = []
        requests = self.prior_requests + len(attributed)
        if requests:
            rows.append(
                {
                    "key": f"gateway/{agent.api_path}|modelApi:model-api-gpt|cc-agents",
                    "mcpServer": {"kind": "mcpServer", "id": agent.server_id},
                    "resource": self.attributed_resource,
                    "model": "gpt-4o-mini",
                    "requests": requests,
                    "costCenter": self.attributed_cost_center,
                }
            )
        # Another server's use, which the check must leave alone.
        rows.append(
            {
                "key": "gateway/other|modelApi:model-api-other|cc-general",
                "mcpServer": {"kind": "mcpServer", "id": "mcpsrv-other"},
                "resource": {"kind": "modelApi", "id": "model-api-other"},
                "requests": 40 + len(self.model_calls),
                "costCenter": {"id": "cc-general", "name": "General", "code": "general"},
            }
        )
        return {"dataSource": self.data_source, "period": "7d", "onBehalf": rows}

    def gateway(self, request: httpx.Request) -> httpx.Response:
        self.gateway_calls.append(request)
        self.call_times.append(self.clock.now)
        self.clock.now += self.call_seconds
        for server in self.servers.values():
            if request.url.path == urlsplit(server.metadata_url).path:
                return self.metadata(server, request)
            if request.url.path == f"/{server.api_path}/mcp":
                return self.mcp(server, request)
        return httpx.Response(404)

    def metadata(self, server: FakeServer, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "Authorization" not in request.headers
        if not self.metadata_json:
            return httpx.Response(200, text="<html>Not a metadata document</html>")
        document = {
            "resource": server.url,
            "authorization_servers": [f"https://login.microsoftonline.com/{TENANT}/v2.0"],
            "bearer_methods_supported": ["header"],
            "scopes_supported": [f"api://{AUDIENCE}/Mcp.Invoke"],
            **self.metadata_patch,
        }
        return httpx.Response(200, json=document, headers={"Cache-Control": "public"})

    def refuse(
        self, status: int, *, challenge: str = "", message: str = "MCP access denied."
    ) -> httpx.Response:
        headers = {"WWW-Authenticate": challenge} if challenge else {}
        return httpx.Response(status, headers=headers, text=message)

    def cost_center_denied(self) -> httpx.Response:
        return self.refuse(
            403, message=COST_CENTER_DENIED if self.name_cost_center_header else "Access denied."
        )

    def quota_refusal(self) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"Retry-After": "86400"} if self.retry_after else {},
            json={
                "statusCode": 403,
                "message": "Out of call volume quota. Quota will be replenished in 23:59:59.",
            },
        )

    def mcp(self, server: FakeServer, request: httpx.Request) -> httpx.Response:
        """The MCP API's policy, then the server. See mosaic_api's mcp_access_policy."""
        if any(self.phase(grant) == "applying" for grant in self.on(server)):
            # While a plan applies, the gateway refuses every call, whatever the outcome.
            return self.refuse(403, message="Access unavailable")
        metadata = server.metadata_url
        authorization = request.headers.get("Authorization")
        if authorization is None:
            if self.open_to_anonymous:
                return self.forward(server, request, caller=None, remaining=None)
            named = self.challenge_metadata_url or metadata
            challenge = f'Bearer resource_metadata="{named}"' if self.challenge_anonymous else ""
            return self.refuse(401, challenge=challenge)
        invalid = f'Bearer error="invalid_token", resource_metadata="{metadata}"'
        token = authorization.removeprefix("Bearer ")
        if not authorization.startswith("Bearer ") or not token.strip():
            return self.refuse(401, challenge=invalid)
        header = ""
        values = request.headers.get_list("x-mosaic-cost-center")
        if values:
            code = values[0].strip() if len(values) == 1 else ""
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", code):
                if self.refuse_malformed_cost_center:
                    return self.cost_center_denied()
            elif self.read_cost_center:
                header = code if self.cost_center_case_sensitive else code.lower()
        claims = claims_of(token)
        if self.validate_tokens and (
            claims is None
            or claims.get("aud") != AUDIENCE
            or claims.get("tid") != TENANT
            or claims.get("ver") != "2.0"
            or claims.get("exp", 0) <= self.clock.time()
        ):
            return self.refuse(401, challenge=invalid if self.challenge_invalid_tokens else "")
        claims = claims or {}
        scopes = [scope for scope in str(claims.get("scp", "")).split() if scope != "/"]
        delegated = "Mcp.Invoke" in scopes or (bool(scopes) and not self.require_mcp_scope)
        application = not scopes and "Mcp.Invoke.Application" in claims.get("roles", [])
        grant = self.lookup(server, request, claims.get("oid"), delegated, application, header)
        if grant is None:
            if self.revoked_status != 403 and self.revoked_for(server, claims.get("oid")):
                return self.refuse(self.revoked_status)
            if header:
                return self.cost_center_denied()
            challenge = (
                'Bearer error="insufficient_scope", '
                f'scope="api://{AUDIENCE}/Mcp.Invoke", resource_metadata="{metadata}"'
            )
            return self.refuse(403, challenge=challenge if self.scope_challenge else "")
        if self.budget_blocked_from is not None and self.clock.now >= self.budget_blocked_from:
            # The budget check follows the grant lookup and comes before the limits.
            return self.refuse(403, message=BUDGET_DENIED.format(code=grant.cost_center))
        limited = self.limit(server, grant)
        refused = limited if isinstance(limited, httpx.Response) else None
        self.cost_center_calls.setdefault(grant.cost_center.lower(), []).append(
            (call_name(request), refused.status_code if refused is not None else None)
        )
        if refused is not None:
            return refused
        if self.backend_capacity is not None:
            if self.backend_capacity <= 0:
                # The server's own 429 passes through the gateway unchanged.
                return httpx.Response(429, headers={"Retry-After": "5"}, json={"error": ECHO})
            self.backend_capacity -= 1
        return self.forward(server, request, caller=claims.get("oid"), remaining=limited)

    def lookup(
        self,
        server: FakeServer,
        request: httpx.Request,
        oid: object,
        delegated: bool,
        application: bool,
        header: str,
    ) -> FakeGrant | None:
        """The caller's grant under the cost center the call names: the default one, then the
        others oldest first."""
        stale = not self.revocation_reaches_gateway
        if self.revocation_flaps:
            credential = request.headers.get("Authorization", "")
            self.unit_calls[credential] = self.unit_calls.get(credential, 0) + 1
            stale = self.unit_calls[credential] % 2 == 0
        live = [grant for grant in self.on(server) if self.phase(grant) != "revoked" or stale]
        kind = "user" if delegated else "application" if application else None
        matches = [
            grant
            for grant in live
            if grant.kind == kind
            # A gateway that doesn't match grants admits anyone with a valid token.
            and (grant.oid == oid or not self.match_grants)
            and (
                not header
                or (
                    grant.cost_center
                    if self.cost_center_case_sensitive
                    else grant.cost_center.lower()
                )
                == header
            )
        ]
        matches.sort(key=lambda grant: (not grant.default, grant.order, grant.id))
        return matches[0] if matches else None

    def revoked_for(self, server: FakeServer, oid: object) -> bool:
        return any(grant.oid == oid and self.phase(grant) == "revoked" for grant in self.on(server))

    def limit(self, server: FakeServer, grant: FakeGrant) -> httpx.Response | int | None:
        """The grant's call limit, its call quota, then its cost center's pool, in policy order."""
        limits = grant.limits or {}
        remaining: int | None = None
        calls = limits.get("calls")
        if calls and self.rate_limits:
            period = limits["renewalPeriodSeconds"]
            window = [
                at for at in self.rate_calls.get(grant.id, []) if self.clock.now - at < period
            ]
            if len(window) >= (calls - 1 if self.refuse_early else calls):
                return httpx.Response(
                    429,
                    headers={"Retry-After": str(period)} if self.retry_after else {},
                    json={
                        "statusCode": 429,
                        "message": f"Rate limit is exceeded. Try again in {period} seconds.",
                    },
                )
            self.rate_calls[grant.id] = [*window, self.clock.now]
            remaining = calls - len(window) - 1 if self.remaining_falls else calls - 1
            remaining += self.remaining_offset
        quota = limits.get("callQuota")
        if quota:
            if self.quota_spent.get(grant.id, 0) >= quota:
                return self.quota_refusal()
            self.quota_spent[grant.id] = self.quota_spent.get(grant.id, 0) + 1
        if self.pools_by_cost_center:
            key = (server.key, grant.cost_center)
        else:
            key = next((item for item in self.pools if item[0] == server.key), ("", ""))
        pool = self.pools.get(key)
        if pool is not None and self.enforce_pools:
            if self.pool_spent.get(key, 0) >= pool + self.pool_overshoot:
                return self.quota_refusal()
            self.pool_spent[key] = self.pool_spent.get(key, 0) + 1
        return remaining

    def forward(
        self, server: FakeServer, request: httpx.Request, *, caller: object, remaining: int | None
    ) -> httpx.Response:
        """Strip the caller's credentials and cost-center header, then call the server."""
        stripped = {
            "authorization",
            "ocp-apim-subscription-key",
            "api-key",
            "x-mosaic-cost-center",
            "x-mosaic-on-behalf-of",
        }
        headers = httpx.Headers(
            {name: value for name, value in request.headers.items() if name not in stripped}
        )
        if server.model_caller and self.model_caller_applied:
            # The call's request ID, which the server passes on to its model calls (ADR 0025).
            headers["x-mosaic-on-behalf-of"] = (
                f"{len(self.forwarded):08x}-0000-4000-8000-000000000000"
            )
        self.forwarded.append(headers)
        self.passed.append(request)
        response = self.serve(server, request.method, headers, request.content, caller)
        if remaining is not None and self.remaining_header:
            response.headers["x-mosaic-remaining-calls"] = str(remaining)
        return response

    def serve(
        self,
        server: FakeServer,
        method: str,
        headers: httpx.Headers,
        content: bytes,
        caller: object,
    ) -> httpx.Response:
        """A strict streamable HTTP MCP server."""
        session_id = headers.get("mcp-session-id")
        if method == "DELETE":
            self.deletes.append(session_id or "")
            if not server.sessions or not self.session_delete:
                return httpx.Response(405)
            return httpx.Response(200 if self.sessions.pop(session_id or "", None) else 404)
        if method != "POST":
            return httpx.Response(405)
        accept = headers.get("accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            return httpx.Response(406)
        if headers.get("content-type", "").split(";")[0] != "application/json":
            return httpx.Response(415)
        message = json.loads(content)
        self.messages.append(message)
        assert message.get("jsonrpc") == "2.0"
        name = message.get("method")
        if name == "initialize":
            if session_id is not None:
                return httpx.Response(400)
            params = message["params"]
            assert params["capabilities"] == {} and params["clientInfo"]["name"]
            reply_headers: dict[str, str] = {}
            if server.sessions:
                self.session_count += 1
                session_id = f"session-{self.session_count}"
                self.sessions[session_id] = {"initialized": False, "revision": server.revision}
                reply_headers["Mcp-Session-Id"] = session_id
            capabilities = {"tools": {"listChanged": False}} if server.tools_capability else {}
            result = {
                "protocolVersion": server.revision,
                "capabilities": capabilities,
                "serverInfo": {"name": server.display_name, "version": "1.0"},
            }
            return self.answer(server, message, result=result, headers=reply_headers)
        state: dict[str, Any] | None = None
        if server.sessions:
            if session_id is None:
                if self.require_session:
                    # As the MCP Python SDK does, it names the session it opened for the request,
                    # which it has already discarded.
                    return httpx.Response(400, headers={"Mcp-Session-Id": DISCARDED_SESSION})
            else:
                state = self.sessions.get(session_id)
                if state is None:
                    return httpx.Response(404)
        revision = state["revision"] if state else server.revision
        version = headers.get("mcp-protocol-version")
        if self.require_protocol_header:
            # Revisions before 2025-06-18 never defined the header, so they may refuse it.
            if revision >= "2025-06-18" and version != revision:
                return httpx.Response(400)
            if revision < "2025-06-18" and version is not None:
                return httpx.Response(400)
        if name == "notifications/initialized":
            assert "id" not in message
            if state is not None:
                state["initialized"] = True
            return httpx.Response(202)
        if name == "ping":
            return self.answer(server, message, result={})
        if state is not None and self.require_initialized and not state["initialized"]:
            return httpx.Response(400)
        if name == self.rpc_error:
            return self.answer(server, message, error={"code": -32603, "message": ECHO})
        if name == "tools/list":
            return self.list_tools(server, message)
        if name == "tools/call":
            return self.call_tool(server, message, headers, caller)
        return self.answer(server, message, error={"code": -32601, "message": ECHO})

    def list_tools(self, server: FakeServer, message: dict[str, Any]) -> httpx.Response:
        page = int(message.get("params", {}).get("cursor") or 0)
        tools = [TOOLS[name] for name in server.tools]
        size = max(1, -(-len(tools) // self.tool_pages))
        result: dict[str, Any] = {"tools": tools[page * size : (page + 1) * size]}
        if (page + 1) * size < len(tools):
            result["nextCursor"] = str(page + 1)
        return self.answer(server, message, result=result)

    def call_tool(
        self, server: FakeServer, message: dict[str, Any], headers: httpx.Headers, caller: object
    ) -> httpx.Response:
        name = message["params"]["name"]
        arguments = message["params"].get("arguments", {})
        if name not in server.tools:
            return self.answer(server, message, error={"code": -32602, "message": ECHO})
        if name == self.tool_error:
            result: dict[str, Any] = {"content": [{"type": "text", "text": ECHO}], "isError": True}
        elif name == "echo":
            text = arguments["text"] if self.echo_works else ECHO
            result = {
                "content": [{"type": "text", "text": text}],
                "structuredContent": {"result": text},
            }
        elif name == "add":
            total = arguments["a"] + arguments["b"] + (0 if self.add_works else 1)
            result = {
                "content": [{"type": "text", "text": str(total)}],
                "structuredContent": {"result": total},
            }
        elif name == "ask_model":
            # The agent calls a governed model as its own application, passing the reference on.
            self.model_calls.append(
                {
                    "reference": headers.get("x-mosaic-on-behalf-of"),
                    "application": server.model_caller,
                    "person": caller,
                    "at": self.clock.now,
                }
            )
            result = {"content": [{"type": "text", "text": self.answer_text}]}
        else:
            result = {"content": [{"type": "text", "text": "2026-10-05T12:00:00Z"}]}
        return self.answer(server, message, result=result)

    def answer(
        self,
        server: FakeServer,
        message: dict[str, Any],
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": 999 if self.wrong_id else message["id"]}
        reply.update({"result": result} if error is None else {"error": error})
        if not server.event_stream:
            return httpx.Response(200, headers=headers or {}, json=reply)
        events: list[str] = []
        if self.leading_events:
            notice = {"jsonrpc": "2.0", "method": "notifications/message", "params": {"data": ECHO}}
            other = {"jsonrpc": "2.0", "id": 424242, "result": {"note": ECHO}}
            events += [": keep-alive\n\n", f"event: message\ndata: {json.dumps(notice)}\n\n"]
            events.append(f"data: {json.dumps(other)}\n\n")
        # A message can span data lines, which join with newlines: split it between two tokens.
        text = json.dumps(reply)
        cut = text.index(", ") + 1
        events.append(f"event: message\ndata: {text[:cut]}\ndata: {text[cut + 1 :]}\n\n")
        events.append(
            f"data: {json.dumps({'jsonrpc': '2.0', 'method': 'notifications/progress'})}\n\n"
        )
        return httpx.Response(
            200,
            headers={**(headers or {}), "Content-Type": "text/event-stream"},
            content="".join(events).encode(),
        )

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
        if form["grant_type"] == model_verifier.DEVICE_CODE_GRANT:
            if self.device_error is not None:
                return httpx.Response(400, json=self.device_error)
            oid, outcomes = self.device_logins[int(form["device_code"].rsplit("-", 1)[1]) - 1]
            if outcomes:
                outcome = outcomes.pop(0)
                if isinstance(outcome, int):
                    return httpx.Response(outcome, text=f"<html><body>{ECHO}</body></html>")
                status, error = (400, outcome) if isinstance(outcome, str) else outcome
                return httpx.Response(status, json={"error": error, "error_description": ECHO})
            # Entra puts every scope the client is consented for in the token.
            token = person_token(oid, scp="Mcp.Invoke Models.Invoke")
            self.issued.append(token)
            return httpx.Response(200, json={"access_token": token, "token_type": "Bearer"})
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
            token = app_token()
            self.issued.append(token)
            return httpx.Response(200, json={"access_token": token, "token_type": "Bearer"})
        raise AssertionError("unexpected sign-in request")


def standard_world() -> FakeWorld:
    return FakeWorld(
        FakeGrant("user-tools", "user", "tools", USER_OID),
        FakeGrant("user-protected", "user", "protected", USER_OID, cost_center="Research-01"),
        FakeGrant("app-tools", "application", "tools", APP_OID),
        FakeGrant("user-agent", "user", "agent", USER_OID),
    )


FULL = [
    *("--user-entitlement", "user-tools", "--user-entitlement", "user-protected"),
    *("--application-entitlement", "app-tools"),
]
TOOLS_ONLY = ["--user-entitlement", "user-tools"]


class McpAccessVerifierTests(unittest.TestCase):
    def verify(
        self, world: FakeWorld, arguments: list[str], env: dict[str, str] | None = None
    ) -> tuple[int, list[str], str]:
        """Run the verifier on the fake world. Returns its exit code, output lines and errors."""
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, ENV if env is None else env, clear=True),
            patch.object(verifier, "time", world.clock),
            patch.object(model_verifier, "time", world.clock),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = verifier.main([*BASE_ARGS, *arguments], transport=world.transport())
        output, errors = stdout.getvalue(), stderr.getvalue()
        self.assert_nothing_secret(world, output + errors)
        return code, output.splitlines(), errors

    def fails(
        self,
        world: FakeWorld,
        arguments: list[str],
        message: str,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        code, lines, errors = self.verify(world, arguments, env)
        self.assertEqual(code, 1, lines)
        self.assertIn(message, errors)
        return lines

    def assert_nothing_secret(self, world: FakeWorld, text: str) -> None:
        secrets = [MODEL_OUTPUT, ECHO, APP_SECRET, *ENV.values(), *world.issued]
        secrets += [f"device-code-{number}" for number in range(1, 6)]
        for secret in secrets:
            self.assertNotIn(secret, text)

    def tool_calls(self, world: FakeWorld) -> list[str]:
        return [
            message["params"]["name"]
            for message in world.messages
            if message.get("method") == "tools/call"
        ]

    def test_full_run_checks_every_grant_without_printing_secrets(self) -> None:
        world = standard_world()
        code, lines, errors = self.verify(
            world, [*FULL, "--check-ungranted-user", "--check-missing-scope"]
        )
        self.assertEqual(code, 0, errors)
        self.assertEqual(errors, "")
        refused = (
            "rejected an anonymous call, a MOSAIC control-plane token, an x-mosaic-cost-center "
            "header that isn't a code, a cost center it holds no grant under, the ungranted "
            "user's token"
        )
        self.assertIn(
            "PASS: User grant 1 (M-tools) answers an anonymous call with 401 and its resource "
            "metadata, which names the server, the tenant's authorization server and Mcp.Invoke",
            lines,
        )
        self.assertIn(
            f"PASS: User grant 1 (M-tools) {refused}, the user's token without Mcp.Invoke", lines
        )
        self.assertIn(f"PASS: Application grant 1 (M-tools) {refused}", lines)
        for label in (
            "User grant 1 (M-tools)",
            "User grant 2 (M-protected)",
            "Application grant 1 (M-tools)",
        ):
            self.assertIn(
                f"PASS: {label} listed 3 tool(s), and echo and add returned the expected results, "
                "selecting its grant with x-mosaic-cost-center in either case",
                lines,
            )
            self.assertIn(
                f"INFO: {label}'s 401 for a token for another audience names the resource metadata",
                lines,
            )
        self.assertIn(
            "INFO: User grant 1 (M-tools) negotiated MCP 2025-11-25, with a session, and the "
            "server answered with JSON",
            lines,
        )
        self.assertIn(
            "INFO: User grant 2 (M-protected) negotiated MCP 2025-11-25, with no session, and the "
            "server answered with event streams",
            lines,
        )
        self.assertEqual(
            lines[-1],
            "Live MCP checks passed for 3 grant(s). Check revocation separately with "
            "--watch-revocation.",
        )
        # Each server's metadata is read once, and each session the server opened is ended.
        metadata = [call for call in world.gateway_calls if "/.well-known/" in call.url.path]
        self.assertEqual(len(metadata), 2)
        self.assertEqual(world.deletes, ["session-1", "session-2"])
        self.assertEqual(world.sessions, {})
        self.assertEqual(self.tool_calls(world), ["echo", "add"] * 3)
        # Credentials and the cost-center header never reach a server.
        self.assertTrue(world.forwarded)
        for headers in world.forwarded:
            for name in ("authorization", "x-mosaic-cost-center", "x-mosaic-on-behalf-of"):
                self.assertNotIn(name, headers)

    def test_the_client_speaks_streamable_http(self) -> None:
        world = standard_world()
        code, _, errors = self.verify(world, TOOLS_ONLY)
        self.assertEqual(code, 0, errors)
        for call in world.gateway_calls:
            if call.method == "POST":
                self.assertEqual(call.headers["Accept"], "application/json, text/event-stream")
                self.assertEqual(call.headers["Content-Type"], "application/json")
        posts = [call for call in world.passed if call.method == "POST"]
        bodies = [json.loads(call.content) for call in posts]
        self.assertEqual(
            [body["method"] for body in bodies],
            ["initialize", "notifications/initialized", "tools/list", "tools/call", "tools/call"],
        )
        # Requests carry distinct IDs; the notification carries none.
        self.assertEqual([body.get("id") for body in bodies], [1, None, 2, 3, 4])
        initialize, *rest = posts
        self.assertNotIn("MCP-Protocol-Version", initialize.headers)
        self.assertNotIn("Mcp-Session-Id", initialize.headers)
        for call in rest:
            self.assertEqual(call.headers["MCP-Protocol-Version"], "2025-11-25")
            self.assertEqual(call.headers["Mcp-Session-Id"], "session-1")
        deletes = [call for call in world.passed if call.method == "DELETE"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0].headers["Mcp-Session-Id"], "session-1")
        self.assertEqual(deletes[0].headers["MCP-Protocol-Version"], "2025-11-25")

    def test_cleanup_retains_identity_on_denial_or_transport_failure(self) -> None:
        for status in (403, 429, 404, 500, None):
            with self.subTest(status=status):
                world = standard_world()
                gateway = world.gateway
                state = {"fail_cleanup": True}

                def failing_delete(
                    request: httpx.Request, status: int | None = status,
                    state: dict[str, bool] = state, gateway: Any = gateway,
                ) -> httpx.Response:
                    if request.method == "DELETE" and state["fail_cleanup"]:
                        if status is None:
                            raise httpx.ReadTimeout(f"{APP_SECRET} {USER_CONTROL}")
                        return httpx.Response(status, text=APP_SECRET)
                    return gateway(request)

                world.gateway = failing_delete  # type: ignore[method-assign]
                with httpx.Client(transport=world.transport()) as client:
                    session = verifier.Session(
                        client, world.servers["tools"].url, auth=verifier.bearer(person_token()),
                        cost_center="general", label="Fixture",
                    )
                    with patch.object(model_verifier, "time", world.clock):
                        session.initialize()
                    with self.assertRaisesRegex(
                        verifier.VerificationFailed, "unresolved"
                    ) as raised:
                        session.close()
                    self.assertNotIn(APP_SECRET, str(raised.exception))
                    self.assertNotIn(USER_CONTROL, str(raised.exception))
                    self.assertEqual(session.session_id, "session-1")
                    self.assertIn("session-1", world.sessions)
                    # A later legitimate attempt uses the retained identity, not a new session.
                    state["fail_cleanup"] = False
                    session.close()
                    self.assertIsNone(session.session_id)
                    self.assertEqual(world.sessions, {})

    def test_cleanup_retry_is_bounded_and_only_for_gateway_rate_refusals(self) -> None:
        for retry in (None, "invalid", "0", "-1", "301", "60"):
            with self.subTest(retry=retry):
                requests: list[httpx.Request] = []

                def denied(
                    request: httpx.Request, retry: str | None = retry,
                    requests: list[httpx.Request] = requests,
                ) -> httpx.Response:
                    requests.append(request)
                    return httpx.Response(
                        429, headers={"Retry-After": retry} if retry is not None else {},
                        json={"message": "Rate limit is exceeded. Try again."},
                    )

                clock = FakeClock()
                with httpx.Client(transport=httpx.MockTransport(denied)) as client:
                    session = verifier.Session(
                        client, f"{ORIGIN}/mcp", auth=verifier.bearer(person_token()),
                        cost_center="general", label="Fixture",
                    )
                    session.session_id = "fixture-session"
                    with (
                        patch.object(verifier, "time", clock),
                        contextlib.redirect_stdout(io.StringIO()),
                        self.assertRaisesRegex(verifier.VerificationFailed, "incomplete"),
                    ):
                        session.close()
                    self.assertEqual(session.session_id, "fixture-session")
                self.assertEqual(clock.sleeps, [60] if retry == "60" else [])
                self.assertEqual(len(requests), 2 if retry == "60" else 1)
                self.assertTrue(all(request.method == "DELETE" for request in requests))
                self.assertTrue(all(request.headers == requests[0].headers for request in requests))

    def test_server_can_explicitly_decline_session_deletion(self) -> None:
        world = standard_world()
        gateway = world.gateway

        def unsupported(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                return httpx.Response(405)
            return gateway(request)

        world.gateway = unsupported  # type: ignore[method-assign]
        code, lines, errors = self.verify(world, TOOLS_ONLY)
        self.assertEqual(code, 0, errors)
        self.assertTrue(any("does not support session DELETE (405)" in line for line in lines))

    def test_cleanup_transport_failure_prevents_pass(self) -> None:
        world = standard_world()
        gateway = world.gateway

        def broken(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                raise httpx.ConnectError(f"{APP_SECRET} {USER_CONTROL}")
            return gateway(request)

        world.gateway = broken  # type: ignore[method-assign]
        lines = self.fails(world, TOOLS_ONLY, "unresolved session cleanup (HTTP transport failure)")
        self.assertEqual(list(world.sessions), ["session-1"])
        self.assertFalse(any("listed 3 tool(s)" in line for line in lines))
        self.assertFalse(any("Live MCP checks passed" in line for line in lines))

    def test_an_older_revision_gets_no_protocol_header(self) -> None:
        world = standard_world()
        world.servers["tools"].revision = "2025-03-26"
        code, lines, errors = self.verify(world, TOOLS_ONLY)
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "INFO: User grant 1 (M-tools) negotiated MCP 2025-03-26, with a session, and the "
            "server answered with JSON",
            lines,
        )

    def test_a_revision_or_server_the_verifier_does_not_speak_fails(self) -> None:
        for revision, shown in (("2026-07-28", "2026-07-28"), ("latest", "?")):
            with self.subTest(revision):
                world = standard_world()
                world.servers["tools"].revision = revision
                self.fails(
                    world,
                    TOOLS_ONLY,
                    f"User grant 1 (M-tools): the server negotiated MCP revision {shown}, which "
                    "the verifier doesn't speak",
                )
        world = standard_world()
        world.servers["tools"].tools_capability = False
        self.fails(world, TOOLS_ONLY, "the server doesn't declare the tools capability")

    def test_event_streams_skip_other_messages_and_must_answer(self) -> None:
        world = standard_world()
        world.servers["tools"].event_stream = True
        code, lines, errors = self.verify(world, TOOLS_ONLY)
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "INFO: User grant 1 (M-tools) negotiated MCP 2025-11-25, with a session, and the "
            "server answered with event streams",
            lines,
        )
        world = standard_world()
        world.servers["tools"].event_stream = True
        world.wrong_id = True
        self.fails(
            world,
            TOOLS_ONLY,
            "User grant 1 (M-tools) initialize: the event stream ended without answering",
        )

    def test_tools_are_listed_across_pages(self) -> None:
        world = standard_world()
        world.tool_pages = 3
        code, lines, errors = self.verify(world, TOOLS_ONLY)
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "PASS: User grant 1 (M-tools) listed 3 tool(s), and echo and add returned the "
            "expected results, selecting its grant with x-mosaic-cost-center in either case",
            lines,
        )
        cursors = [
            message["params"].get("cursor")
            for message in world.messages
            if message.get("method") == "tools/list"
        ]
        self.assertEqual(cursors, [None, "1", "2"])

    def test_discovery_must_say_where_and_how_to_sign_in(self) -> None:
        for setting, value, message in (
            (
                "open_to_anonymous",
                True,
                "User grant 1 (M-tools) rejecting an anonymous call: unexpected HTTP 200",
            ),
            ("challenge_anonymous", False, "the anonymous call's 401 doesn't name the resource"),
            (
                "challenge_metadata_url",
                f"{ORIGIN}/.well-known/oauth-protected-resource/other/mcp",
                "names a different resource metadata URL from the connection details",
            ),
            (
                "metadata_patch",
                {"resource": f"{ORIGIN}/other/mcp"},
                "the resource metadata names a different resource",
            ),
            (
                "metadata_patch",
                {
                    "authorization_servers": [
                        f"https://login.microsoftonline.com/{STRANGER_OID}/v2.0"
                    ]
                },
                "the resource metadata doesn't name the tenant's authorization server",
            ),
            (
                "metadata_patch",
                {"scopes_supported": [f"api://{AUDIENCE}/Models.Invoke"]},
                "the resource metadata doesn't offer Mcp.Invoke",
            ),
            ("metadata_json", False, "User grant 1 (M-tools) resource metadata: response was not"),
        ):
            with self.subTest(setting=setting, value=value):
                world = standard_world()
                setattr(world, setting, value)
                self.fails(world, TOOLS_ONLY, message)
                self.assertEqual(self.tool_calls(world), [])

    def test_refusals_must_come_from_the_rule_under_test(self) -> None:
        checks = [*TOOLS_ONLY, "--check-ungranted-user", "--check-missing-scope"]
        for setting, value, message in (
            # Without audience validation, the grant lookup refuses the control-plane token: 403.
            (
                "validate_tokens",
                False,
                "rejecting a MOSAIC control-plane token: unexpected HTTP 403",
            ),
            (
                "refuse_malformed_cost_center",
                False,
                "rejecting an x-mosaic-cost-center header that isn't a code: unexpected HTTP 200",
            ),
            (
                "read_cost_center",
                False,
                "rejecting a cost center it holds no grant under: unexpected HTTP 200",
            ),
            (
                "name_cost_center_header",
                False,
                "rejecting an x-mosaic-cost-center header that isn't a code: the 403 didn't "
                "come from the cost-center rule",
            ),
            ("match_grants", False, "rejecting the ungranted user's token: unexpected HTTP 200"),
            (
                "scope_challenge",
                False,
                "rejecting the ungranted user's token: the 403 didn't ask for Mcp.Invoke "
                "(insufficient_scope)",
            ),
            (
                "require_mcp_scope",
                False,
                "rejecting the user's token without Mcp.Invoke: unexpected HTTP 200",
            ),
        ):
            with self.subTest(setting):
                world = standard_world()
                setattr(world, setting, value)
                self.fails(world, checks, f"User grant 1 (M-tools) {message}")
                self.assertEqual(self.tool_calls(world), [])

    def test_whether_an_invalid_token_s_401_names_the_metadata_is_reported(self) -> None:
        # ADR 0017 leaves it to live verification, so either way the run passes and says which.
        world = standard_world()
        world.challenge_invalid_tokens = False
        code, lines, errors = self.verify(world, TOOLS_ONLY)
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "INFO: User grant 1 (M-tools)'s 401 for a token for another audience does not name "
            "the resource metadata",
            lines,
        )

    def test_tools_must_return_what_they_should(self) -> None:
        for setting, value, message in (
            ("echo_works", False, "User grant 1 (M-tools): the echo tool didn't return its text"),
            ("add_works", False, "User grant 1 (M-tools): the add tool didn't return the sum"),
            ("tool_error", "echo", "User grant 1 (M-tools): the echo tool returned an error"),
            ("tool_error", "add", "User grant 1 (M-tools): the add tool returned an error"),
            (
                "rpc_error",
                "tools/list",
                "User grant 1 (M-tools) tools/list: the server answered with JSON-RPC error -32603",
            ),
            (
                "wrong_id",
                True,
                "User grant 1 (M-tools) initialize: the response answered another request",
            ),
        ):
            with self.subTest(setting=setting, value=value):
                world = standard_world()
                setattr(world, setting, value)
                self.fails(world, TOOLS_ONLY, message)
        world = standard_world()
        world.servers["tools"].tools = ("echo", "utc_now")
        self.fails(
            world,
            TOOLS_ONLY,
            "User grant 1 (M-tools): the server doesn't list add, which the checks call",
        )

    def test_the_protocol_s_headers_and_handshake_are_required(self) -> None:
        # A server is entitled to refuse a request without them, so the verifier must send them.
        world = standard_world()
        world.servers["tools"].revision = "2025-06-18"
        code, _, errors = self.verify(world, TOOLS_ONLY)
        self.assertEqual(code, 0, errors)
        protocol = [
            call.headers.get("MCP-Protocol-Version")
            for call in world.passed
            if call.method == "POST"
        ]
        self.assertEqual(protocol, [None, *["2025-06-18"] * 4])
        # A server that ends the session refuses the rest with 404.
        world = standard_world()
        original = world.serve

        def forget(*args: Any, **kwargs: Any) -> httpx.Response:
            response = original(*args, **kwargs)
            if len(world.messages) == 2:
                world.sessions.clear()
            return response

        world.serve = forget  # type: ignore[method-assign]
        self.fails(world, TOOLS_ONLY, "User grant 1 (M-tools) tools/list: unexpected HTTP 404")

    def test_the_cost_center_is_compared_without_case(self) -> None:
        # The session opens with the code as MOSAIC gives it, then sends it in the other case.
        world = standard_world()
        world.cost_center_case_sensitive = True
        self.fails(world, TOOLS_ONLY, "User grant 1 (M-tools) tools/list: unexpected HTTP 403")
        headers = [call.headers.get("x-mosaic-cost-center") for call in world.passed]
        self.assertEqual(headers, ["general", "general"])
        self.assertEqual(world.gateway_calls[-2].headers["x-mosaic-cost-center"], "GENERAL")

        world = FakeWorld(FakeGrant("user-tools", "user", "tools", USER_OID, cost_center="0042"))
        code, lines, errors = self.verify(world, TOOLS_ONLY)
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "PASS: User grant 1 (M-tools) listed 3 tool(s), and echo and add returned the "
            "expected results, selecting its grant with x-mosaic-cost-center",
            lines,
        )

    def test_runtime_tokens_are_checked_before_mcp_calls(self) -> None:
        user = verifier.USER_RUNTIME_TOKEN
        application = verifier.APPLICATION_RUNTIME_TOKEN
        for name, token, message in (
            (user, person_token(aud="other"), "for a different audience"),
            (
                user,
                person_token(scp="Models.Invoke"),
                "the user's MCP token lacks the Mcp.Invoke scope",
            ),
            (user, person_token(ver="1.0"), "isn't an Entra version 2.0 token"),
            (user, person_token(STRANGER_OID), "belongs to a different account from"),
            (user, person_token(oid=None), "the user's MCP token names no object ID"),
            (
                user,
                person_token(exp=WALL_CLOCK - 1),
                "the user's MCP token has expired. Get a new one",
            ),
            (user, person_token(exp=WALL_CLOCK + 30), "expires in 30 seconds. Get a new one"),
            (
                application,
                app_token(roles=["Models.Invoke.Application"]),
                "the application's MCP token lacks the Mcp.Invoke.Application role. An "
                "administrator assigns it to the application and grants consent",
            ),
            (
                application,
                app_token(scp="Mcp.Invoke"),
                "the application's MCP token carries a delegated scope",
            ),
        ):
            with self.subTest(message):
                world = standard_world()
                self.fails(world, FULL, message, {**ENV, name: token})
                self.assertEqual(world.gateway_calls, [])

    def test_the_token_without_mcp_invoke_must_be_the_user_s_and_lack_it(self) -> None:
        name = verifier.MODEL_RUNTIME_TOKEN
        for token, message in (
            (
                person_token(scp="Models.Invoke Mcp.Invoke"),
                "carries Mcp.Invoke, so the gateway would accept it. Entra puts every scope a "
                "client is consented for in its tokens",
            ),
            (person_token(STRANGER_OID, scp="Models.Invoke"), "belongs to someone other than"),
            (app_token(), "has no delegated scope, so it isn't a person's token"),
            (person_token(aud="other", scp="Models.Invoke"), "is for a different audience"),
            (person_token(scp="Models.Invoke", exp=WALL_CLOCK - 1), "has expired"),
            (person_token(scp="Models.Invoke", ver="1.0"), "isn't an Entra version 2.0 token"),
        ):
            with self.subTest(message):
                world = standard_world()
                self.fails(
                    world,
                    [*TOOLS_ONLY, "--check-missing-scope"],
                    f"{name} {message}",
                    {**ENV, name: token},
                )
                self.assertEqual(world.gateway_calls, [])

    def test_device_code_sign_ins_use_mosaic_s_client_and_mcp_s_scope(self) -> None:
        world = standard_world()
        world.device_logins = [
            (USER_OID, ["authorization_pending", "slow_down"]),
            (STRANGER_OID, []),
        ]
        env = without(verifier.USER_RUNTIME_TOKEN, verifier.UNGRANTED_USER_RUNTIME_TOKEN)
        code, lines, errors = self.verify(
            world,
            [
                *("--user-entitlement", "user-tools", "--user-entitlement", "user-protected"),
                *("--user-token-source", "device-code", "--check-ungranted-user"),
            ],
            env,
        )
        self.assertEqual(code, 0, errors)
        starts = [form for endpoint, form in world.login_forms if endpoint == "devicecode"]
        scope = {"client_id": MODEL_CLIENT, "scope": f"api://{AUDIENCE}/Mcp.Invoke"}
        # One sign-in for the user, whose token serves both grants, and one for the stranger.
        self.assertEqual(starts, [scope, scope])
        self.assertEqual(world.clock.sleeps, [5, 5, 10, 5])
        for who, code_shown in (
            ("the user who holds these grants", "CODE0001"),
            ("a different user, one with no grant for these MCP servers", "CODE0002"),
        ):
            self.assertIn(
                f"SIGN IN as {who}: open https://microsoft.com/devicelogin and enter the code "
                f"{code_shown}",
                errors,
            )
        # Every sign-in happens before the first call to the gateway.
        hosts = [request.url.host for request in world.requests]
        self.assertLess(
            max(index for index, host in enumerate(hosts) if host == "login.microsoftonline.com"),
            hosts.index("gateway.example"),
        )
        stranger = world.issued[1]
        refused = [
            call
            for call in world.gateway_calls
            if call.headers.get("Authorization") == f"Bearer {stranger}"
        ]
        self.assertEqual(len(refused), 2)
        self.assertIn(
            "PASS: User grant 2 (M-protected) listed 3 tool(s), and echo and add "
            "returned the expected results, selecting its grant with "
            "x-mosaic-cost-center in either case",
            lines,
        )

    def test_device_code_sign_in_failures(self) -> None:
        arguments = [*TOOLS_ONLY, "--user-token-source", "device-code"]
        env = without(verifier.USER_RUNTIME_TOKEN)
        world = standard_world()
        world.device_logins = [(STRANGER_OID, [])]
        self.fails(world, arguments, "the user's MCP token belongs to a different account", env)
        self.assertEqual(world.gateway_calls, [])

        world = standard_world()
        world.model_client = None
        self.fails(
            world,
            arguments,
            "MOSAIC names no client to sign in with for this grant's audience, so the verifier "
            "can't sign in. Set MOSAIC_SMOKE_MCP_USER_RUNTIME_TOKEN instead",
            env,
        )
        self.assertEqual(world.login_forms, [])

        world = standard_world()
        world.device_error = {
            "error": "invalid_grant",
            "error_description": f"AADSTS65001: Consent is needed. {ECHO}",
            "error_codes": [65001],
        }
        self.fails(
            world,
            arguments,
            "Signing in the user who holds these grants failed: invalid_grant, AADSTS65001. See "
            "docs/connect-to-mcp-servers.md#troubleshooting",
            env,
        )

    def test_device_code_sign_in_waits_through_a_transient_sign_in_error(self) -> None:
        # O50: a poll got HTTP 502 with no OAuth error while the person was still signing in.
        world = standard_world()
        world.device_logins = [(USER_OID, ["authorization_pending", 502, "authorization_pending"])]
        code, _, errors = self.verify(
            world,
            [*TOOLS_ONLY, "--user-token-source", "device-code"],
            without(verifier.USER_RUNTIME_TOKEN),
        )
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "INFO: The sign-in service answered HTTP 502; still waiting for the user who holds "
            "these grants to sign in",
            errors.splitlines(),
        )
        self.assertEqual(world.clock.sleeps, [5, 5, 5, 5])
        self.assertTrue(world.gateway_calls)

        world = standard_world()
        world.device_logins = [(USER_OID, [502] * model_verifier.TRANSIENT_SIGN_IN_ERRORS)]
        self.fails(
            world,
            [*TOOLS_ONLY, "--user-token-source", "device-code"],
            "Signing in the user who holds these grants failed: HTTP 502. See "
            "docs/connect-to-mcp-servers.md#troubleshooting",
            without(verifier.USER_RUNTIME_TOKEN),
        )
        self.assertEqual(world.gateway_calls, [])

    def test_the_ungranted_user_must_be_someone_else(self) -> None:
        world = standard_world()
        env = {**ENV, verifier.UNGRANTED_USER_RUNTIME_TOKEN: person_token(USER_OID, azp="other")}
        self.fails(
            world,
            [*TOOLS_ONLY, "--check-ungranted-user"],
            "The ungranted user's MCP token belongs to the user who holds these grants",
            env,
        )
        world = standard_world()
        env = {**ENV, verifier.UNGRANTED_USER_RUNTIME_TOKEN: person_token(STRANGER_OID, exp=1)}
        self.fails(
            world,
            [*TOOLS_ONLY, "--check-ungranted-user"],
            "The ungranted user's MCP token has expired. Get a new one",
            env,
        )
        self.assertEqual(world.gateway_calls, [])

    def test_application_token_is_bound_to_selected_applied_grant(self) -> None:
        arguments = ["--application-entitlement", "app-tools"]
        for oid in (AGENT_OID, None, "not-an-object-id", APP_OID.upper()):
            with self.subTest(oid=oid):
                world = standard_world()
                world.grants["other-app-tools"] = FakeGrant(
                    "other-app-tools", "application", "tools", AGENT_OID
                )
                token = app_token(oid=oid)
                env = {**ENV, verifier.APPLICATION_RUNTIME_TOKEN: token}
                code, lines, errors = self.verify(world, arguments, env)
                if oid == APP_OID.upper():
                    self.assertEqual(code, 0, errors)
                    self.assertTrue(any("selecting its grant" in line for line in lines))
                    self.assertTrue(world.passed)
                    for request in world.passed:
                        claims = claims_of(request.headers["Authorization"].removeprefix("Bearer "))
                        self.assertEqual(claims["oid"].lower(), APP_OID)
                    self.assertEqual(world.sessions, {})
                else:
                    self.assertEqual(code, 1, lines)
                    self.assertIn("selected applied application's object ID", errors)
                    self.assertFalse(any("PASS" in line for line in lines))
                    self.assertEqual(world.gateway_calls, [])
                self.assertNotIn(token, "\n".join(lines) + errors)

    def test_supplied_application_identity_needs_no_client_credentials(self) -> None:
        world = standard_world()
        env = {**ENV, verifier.APPLICATION_RUNTIME_TOKEN: app_token(azp=None)}
        code, _, errors = self.verify(world, ["--application-entitlement", "app-tools"], env)
        self.assertEqual(code, 0, errors)
        self.assertEqual(world.login_forms, [])
        self.assertEqual(world.sessions, {})

    def test_application_requires_an_applied_direct_identity(self) -> None:
        for change, message in (
            ({"objectId": None}, "lacks a usable application identity"),
            ({"objectId": "invalid"}, "lacks a usable application identity"),
            ({"subject": {}}, "lacks a usable application identity"),
            ({"subject": {"kind": "user"}}, "lacks a usable application identity"),
            ({"subject": {"kind": "securityGroup"}}, "security-group grant"),
            ({"enabled": False}, "last apply doesn't include this grant"),
            ({"entitlementId": "different"}, "last apply doesn't include this grant"),
        ):
            with self.subTest(change=change):
                world = standard_world()
                publication = world.publication

                def changed(
                    server: FakeServer, publication: Any = publication,
                    change: dict[str, Any] = change,
                ) -> dict[str, Any]:
                    document = publication(server)
                    for grant in document["appliedAccess"]["grants"]:
                        if grant["entitlementId"] == "app-tools":
                            grant.update(change)
                    return document

                world.publication = changed  # type: ignore[method-assign]
                self.fails(world, ["--application-entitlement", "app-tools"], message)
                self.assertEqual(world.gateway_calls, [])

    def test_client_credentials_sign_in_the_application(self) -> None:
        env = {
            **without(verifier.APPLICATION_RUNTIME_TOKEN),
            verifier.APPLICATION_CLIENT_ID: APP_CLIENT,
            verifier.APPLICATION_CLIENT_SECRET: APP_SECRET,
        }
        arguments = [
            *("--application-entitlement", "app-tools"),
            *("--application-token-source", "client-credentials"),
        ]
        world = standard_world()
        code, lines, errors = self.verify(world, arguments, env)
        self.assertEqual(code, 0, errors)
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
            "PASS: Application grant 1 (M-tools) listed 3 tool(s), and echo and add returned the "
            "expected results, selecting its grant with x-mosaic-cost-center in either case",
            lines,
        )
        # An application-only run needs no user, and checks the admin's control-plane token.
        self.assertIn(
            "PASS: Application grant 1 (M-tools) rejected an anonymous call, a MOSAIC "
            "control-plane token, an x-mosaic-cost-center header that isn't a code, a cost "
            "center it holds no grant under",
            lines,
        )

        world = standard_world()
        self.fails(
            world,
            arguments,
            "Signing in the application failed: invalid_client, AADSTS7000215. See "
            "docs/connect-to-mcp-servers.md#troubleshooting",
            {**env, verifier.APPLICATION_CLIENT_SECRET: "wrong-fixture-secret"},
        )
        self.assertEqual(world.gateway_calls, [])

    def test_arguments_and_credentials_are_checked_before_network(self) -> None:
        on_behalf = ["--on-behalf-entitlement", "user-agent", "--send-model-requests"]
        for arguments, env, message in (
            (
                [],
                ENV,
                "Supply at least one --user-entitlement, --application-entitlement or "
                "--on-behalf-entitlement",
            ),
            (["--user-entitlement", "https://mosaic.example/x"], ENV, "not a URL"),
            ([*TOOLS_ONLY, *TOOLS_ONLY], ENV, "List each entitlement only once"),
            (
                [*TOOLS_ONLY, "--on-behalf-entitlement", "user-tools", "--send-model-requests"],
                ENV,
                "List each entitlement only once",
            ),
            (
                ["--on-behalf-entitlement", "user-agent"],
                ENV,
                "Add --send-model-requests to acknowledge that ask_model sends a real, billed "
                "model request",
            ),
            ([*TOOLS_ONLY, "--await-attribution"], ENV, "need an --on-behalf-entitlement"),
            (
                [*on_behalf, "--model-caller-entitlement", MODEL_GRANT],
                ENV,
                "--model-caller-entitlement is only used with --await-attribution",
            ),
            (
                ["--application-entitlement", "app-tools", "--check-ungranted-user"],
                ENV,
                "need a --user-entitlement",
            ),
            (
                [*on_behalf, "--check-missing-scope"],
                ENV,
                "need a --user-entitlement",
            ),
            (
                [*TOOLS_ONLY, "--prove-call-limit", "user-other"],
                ENV,
                "--prove-call-limit must name a --user-entitlement or --application-entitlement",
            ),
            (
                [*on_behalf, "--prove-pooled-quota", "user-agent"],
                ENV,
                "--prove-pooled-quota must name a --user-entitlement or --application-entitlement",
            ),
            (
                [*on_behalf, "--watch-revocation", "user-agent"],
                ENV,
                "--watch-revocation must name a --user-entitlement or --application-entitlement",
            ),
            (
                [
                    *TOOLS_ONLY,
                    *on_behalf,
                    "--watch-revocation",
                    "user-tools",
                    "--await-attribution",
                ],
                ENV,
                "Wait for one thing per run",
            ),
            ([*TOOLS_ONLY, "--revocation-timeout", "59"], ENV, "from 60 to 3600 seconds"),
            ([*TOOLS_ONLY, "--revocation-interval", "301"], ENV, "from 10 to 300 seconds"),
            ([*TOOLS_ONLY, "--attribution-timeout", "3601"], ENV, "from 60 to 3600 seconds"),
            ([*TOOLS_ONLY, "--attribution-interval", "29"], ENV, "from 30 to 600 seconds"),
            (TOOLS_ONLY, without(verifier.USER_CONTROL_TOKEN), verifier.USER_CONTROL_TOKEN),
            (TOOLS_ONLY, without(verifier.USER_RUNTIME_TOKEN), verifier.USER_RUNTIME_TOKEN),
            (on_behalf, without(verifier.USER_RUNTIME_TOKEN), verifier.USER_RUNTIME_TOKEN),
            (
                ["--application-entitlement", "app-tools"],
                without(verifier.ADMIN_CONTROL_TOKEN),
                verifier.ADMIN_CONTROL_TOKEN,
            ),
            (
                ["--application-entitlement", "app-tools"],
                without(verifier.APPLICATION_RUNTIME_TOKEN),
                verifier.APPLICATION_RUNTIME_TOKEN,
            ),
            (
                [
                    *("--application-entitlement", "app-tools"),
                    *("--application-token-source", "client-credentials"),
                ],
                {**ENV, verifier.APPLICATION_CLIENT_ID: "not-a-guid"},
                "MOSAIC_SMOKE_APPLICATION_CLIENT_ID must be the application's client ID",
            ),
            (
                [
                    *("--application-entitlement", "app-tools"),
                    *("--application-token-source", "client-credentials"),
                ],
                {**ENV, verifier.APPLICATION_CLIENT_ID: APP_CLIENT},
                verifier.APPLICATION_CLIENT_SECRET,
            ),
            (
                [*TOOLS_ONLY, "--check-ungranted-user"],
                without(verifier.UNGRANTED_USER_RUNTIME_TOKEN),
                verifier.UNGRANTED_USER_RUNTIME_TOKEN,
            ),
            (
                [*TOOLS_ONLY, "--check-missing-scope", "--user-token-source", "device-code"],
                without(verifier.MODEL_RUNTIME_TOKEN),
                verifier.MODEL_RUNTIME_TOKEN,
            ),
            (
                [*TOOLS_ONLY, "--prove-pooled-quota", "user-tools"],
                without(verifier.ADMIN_CONTROL_TOKEN),
                verifier.ADMIN_CONTROL_TOKEN,
            ),
        ):
            with self.subTest(message=message, arguments=arguments):
                world = standard_world()
                self.fails(world, arguments, message, env)
                self.assertEqual(world.requests, [])
        for base, message in (
            (["--api-base-url", API, "--gateway-origin", "http://gateway.example"], "HTTPS"),
            (["--api-base-url", API, "--gateway-origin", f"{ORIGIN}/mcp"], "without an API path"),
            (["--api-base-url", f"{API}?x=1", "--gateway-origin", ORIGIN], "query string"),
        ):
            with self.subTest(message):
                stderr = io.StringIO()
                with patch.dict(os.environ, ENV, clear=True), contextlib.redirect_stderr(stderr):
                    code = verifier.main([*base, *TOOLS_ONLY])
                self.assertEqual(code, 1)
                self.assertIn(message, stderr.getvalue())
        # One proof per run.
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            verifier.parse_arguments(
                [
                    *BASE_ARGS,
                    *TOOLS_ONLY,
                    *("--prove-call-limit", "user-tools", "--prove-pooled-quota", "user-tools"),
                ]
            )
        self.assertEqual(raised.exception.code, 2)

    def test_access_must_be_applied_and_published_as_expected(self) -> None:
        for patch_, message in (
            (
                {"runtime": {"status": "revocationPending"}},
                "User grant 1 (M-tools): Access must be applied to API Management with nothing "
                "pending, so the gateway enforces it (status: revocationPending)",
            ),
            ({"enforced": False}, "so the gateway enforces it (status: applied)"),
            ({"transport": "sse"}, "The server isn't a streamable HTTP MCP server"),
            (
                {"serverUrl": "https://elsewhere.example/mcp/m-tools/mcp"},
                "The MCP server URL isn't on the approved gateway origin",
            ),
            ({"serverUrl": f"{ORIGIN}/mcp/m-tools/sse"}, "The MCP server URL doesn't end in /mcp"),
            (
                {"serverUrl": f"{ORIGIN}/mcp/m-tools/mcp?code=1"},
                "The MCP server URL must not contain a query or fragment",
            ),
            (
                {"resourceMetadataUrl": f"{ORIGIN}/.well-known/oauth-protected-resource/other/mcp"},
                "The resource metadata URL isn't the MCP server URL's",
            ),
            ({"tenantId": "contoso"}, "The connection lacks a usable Entra tenant or audience"),
            (
                {"delegatedScope": f"api://{AUDIENCE}/Models.Invoke"},
                f"The connection's scope isn't api://{AUDIENCE}/Mcp.Invoke",
            ),
            (
                {"costCenterHeader": "x-other"},
                "The connection names a different cost-center header",
            ),
            (
                {"costCenter": {"code": "not a code"}},
                "The connection's cost center has no usable code",
            ),
            ({"mcpServerId": None}, "The connection names no MCP server"),
        ):
            with self.subTest(message):
                world = standard_world()
                world.connection_patch = patch_
                self.fails(world, TOOLS_ONLY, message)
                self.assertEqual(world.gateway_calls, [])
        world = standard_world()
        world.connection_patch = {"requiredAppRole": "Models.Invoke.Application"}
        self.fails(
            world,
            ["--application-entitlement", "app-tools"],
            "Application grant 1 (M-tools): The connection doesn't require the "
            "Mcp.Invoke.Application app role",
        )

    def limited_world(self, **limits: Any) -> FakeWorld:
        return FakeWorld(
            FakeGrant(
                "user-limited",
                "user",
                "tools",
                USER_OID,
                limits={"calls": 8, "renewalPeriodSeconds": 60, **limits},
            )
        )

    LIMITED = ("--user-entitlement", "user-limited", "--prove-call-limit", "user-limited")

    def test_call_limit_proof(self) -> None:
        world = self.limited_world()
        code, lines, errors = self.verify(world, list(self.LIMITED))
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "PASS: User grant 1 (M-tools) spent x-mosaic-remaining-calls from 7 to 0, one call at "
            "a time, then got the gateway's 429 with Retry-After: its limit of 8 calls per 60 "
            "seconds",
            lines,
        )
        # The refusals before it don't count against the limit: the gateway refuses them first.
        # The proof spent eight calls; cleanup waits for renewal and consumes the first new one.
        self.assertEqual(len(world.rate_calls["user-limited"]), 1)
        self.assertEqual(world.clock.sleeps, [60])
        deletes = [call for call in world.gateway_calls if call.method == "DELETE"]
        self.assertEqual(len(deletes), 2)
        self.assertEqual(dict(deletes[0].headers), dict(deletes[1].headers))
        self.assertEqual(world.deletes, ["session-1"])
        self.assertEqual(world.sessions, {})
        self.assertEqual(self.tool_calls(world), ["echo", "add", "echo", "echo", "echo"])

        # Calls from before the window no longer count.
        world = self.limited_world()
        world.rate_calls["user-limited"] = [START - 70, START - 61]
        code, lines, errors = self.verify(world, list(self.LIMITED))
        self.assertEqual(code, 0, errors)
        self.assertIn("from 7 to 0", lines[-2])

    def test_call_limit_proof_needs_the_gateway_s_limit(self) -> None:
        for setting, value, message in (
            (
                "remaining_header",
                False,
                "a successful initialize carried no x-mosaic-remaining-calls",
            ),
            (
                "remaining_falls",
                False,
                "x-mosaic-remaining-calls didn't fall on each response (7, 7, 7",
            ),
            ("rate_limits", False, "9 calls went through, more than its limit of 8 calls per 60"),
            ("retry_after", False, "its call-limit 429 had no Retry-After in seconds"),
            (
                "refuse_early",
                True,
                "the gateway refused a call while x-mosaic-remaining-calls still said 1",
            ),
            (
                "remaining_offset",
                1,
                "x-mosaic-remaining-calls didn't start below its limit of 8",
            ),
            (
                "backend_capacity",
                6,
                "a call was refused with HTTP 429, not the gateway's call-limit 429",
            ),
            ("call_seconds", 5.0, "the calls took too long to prove a limit per 60 seconds. Retry"),
        ):
            with self.subTest(setting):
                world = self.limited_world()
                setattr(world, setting, value)
                self.fails(world, list(self.LIMITED), f"User grant 1 (M-tools): {message}")
        world = self.limited_world(callQuota=4, callQuotaPeriod="Daily")
        world.connection_patch = {"limits": {"requests": {"calls": 8, "renewalPeriodSeconds": 60}}}
        self.fails(
            world,
            list(self.LIMITED),
            "User grant 1 (M-tools): a call quota refused a call before its call limit did",
        )

    def test_call_limit_proof_needs_a_fresh_window_to_name_the_limit(self) -> None:
        # Calls an earlier run left in the window would hide a smaller limit at the gateway.
        world = self.limited_world()
        world.rate_calls["user-limited"] = [START - 10, START - 5]
        self.fails(
            world,
            list(self.LIMITED),
            "User grant 1 (M-tools): its first call left 5 of its 8 calls, not 7. Either the "
            "gateway enforces a smaller limit than MOSAIC applied, or calls from the last 60 "
            "seconds still count. Wait 60 seconds without calling it, then retry",
        )
        # MOSAIC applied 20 calls a minute, but the gateway enforces 8.
        world = self.limited_world()
        world.connection_patch = {"limits": {"requests": {"calls": 20, "renewalPeriodSeconds": 60}}}
        lines = self.fails(
            world,
            list(self.LIMITED),
            "User grant 1 (M-tools): its first call left 7 of its 20 calls, not 19",
        )
        self.assertFalse(any("spent x-mosaic-remaining-calls" in line for line in lines))

    def test_call_limit_proof_needs_a_small_rate_limit(self) -> None:
        for limits, message in (
            ({}, "needs a grant with a call rate limit"),
            ({"calls": 5}, "needs a grant limited to 6 to 30 calls"),
            ({"calls": 31}, "needs a grant limited to 6 to 30 calls"),
            ({"renewalPeriodSeconds": 30}, "needs a limit per 60 to 300 seconds"),
            ({"callQuota": 100, "callQuotaPeriod": "Daily"}, "needs a grant without a call quota"),
        ):
            with self.subTest(message):
                world = self.limited_world(**limits)
                if not limits:
                    world.grants["user-limited"].limits = None
                self.fails(world, list(self.LIMITED), f"Proving the call limit {message}")
                self.assertEqual(world.gateway_calls, [])

    def pooled_world(self, calls: int = 8) -> FakeWorld:
        world = FakeWorld(
            FakeGrant("user-tools", "user", "tools", USER_OID),
            FakeGrant(
                "user-pooled",
                "user",
                "tools",
                USER_OID,
                cost_center="pooled",
                default=False,
                order=1,
            ),
        )
        world.pools[("tools", "pooled")] = calls
        return world

    POOLED = (
        *("--user-entitlement", "user-tools", "--user-entitlement", "user-pooled"),
        *("--prove-pooled-quota", "user-pooled"),
    )

    def pooled_session_deletes(self, world: FakeWorld) -> list[httpx.Request]:
        return [
            call
            for call in world.gateway_calls
            if call.method == "DELETE" and call.headers.get("Mcp-Session-Id") == "session-2"
        ]

    def test_pooled_quota_proof_closes_a_stateful_session_within_the_pool(self) -> None:
        world = self.pooled_world()
        code, lines, errors = self.verify(world, list(self.POOLED))
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "PASS: User grant 2 (M-tools)'s cost center's pool of 8 calls a month refused at its "
            "limit with the gateway's quota 403, after 8 calls in this run. The session's DELETE "
            "was call 8, so the session was closed within the pool, and User grant 1 (M-tools), "
            "under another cost center, still reached its tools",
            lines,
        )
        self.assertIn("INFO: User grant 2 (M-tools)'s quota 403 had a Retry-After", lines)
        # The pool counted every call, the probe, the notification and the DELETE among them, so
        # the DELETE was the last call it allowed. Then a probe got its 403.
        self.assertEqual(
            world.cost_center_calls["pooled"],
            [
                ("ping", None),
                ("initialize", None),
                ("notifications/initialized", None),
                *[("tools/call", None)] * 4,
                ("DELETE", None),
                ("ping", 403),
            ],
        )
        self.assertEqual(world.pool_spent[("tools", "pooled")], 8)
        self.assertEqual(world.sessions, {})
        self.assertEqual(world.deletes, ["session-1", "session-2", "session-3"])
        # The DELETE used the session's own credentials and cost center, once.
        deletes = self.pooled_session_deletes(world)
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0].headers["x-mosaic-cost-center"].lower(), "pooled")
        # The probes named no session, not even the one a refusal named, and the one after the
        # session the revision it negotiated.
        probes = [call for call in world.gateway_calls if call_name(call) == "ping"]
        self.assertEqual(len(probes), 2)
        for probe in probes:
            self.assertNotIn("Mcp-Session-Id", probe.headers)
            self.assertEqual(probe.headers["x-mosaic-cost-center"], "pooled")
        self.assertNotIn("MCP-Protocol-Version", probes[0].headers)
        self.assertEqual(probes[1].headers["MCP-Protocol-Version"], "2025-11-25")
        self.assertFalse(
            any(
                call.headers.get("Mcp-Session-Id") == DISCARDED_SESSION
                for call in world.gateway_calls
            )
        )
        # The other grant's calls named its own cost center, after the pool was spent.
        control = world.passed[-4:]
        self.assertEqual(
            [call_name(call) for call in control],
            ["initialize", "notifications/initialized", "tools/call", "DELETE"],
        )
        self.assertEqual(
            {call.headers.get("x-mosaic-cost-center") for call in control}, {"general"}
        )

        # The smallest pool that fits the run: a probe, the handshake, one tool call and DELETE.
        world = self.pooled_world(calls=5)
        code, lines, errors = self.verify(world, list(self.POOLED))
        self.assertEqual(code, 0, errors)
        self.assertEqual(
            world.cost_center_calls["pooled"],
            [
                ("ping", None),
                ("initialize", None),
                ("notifications/initialized", None),
                ("tools/call", None),
                ("DELETE", None),
                ("ping", 403),
            ],
        )
        self.assertEqual(world.sessions, {})

    def test_pooled_quota_proof_says_when_the_server_cannot_delete_its_session(self) -> None:
        # A 405 resolves cleanup, as it does elsewhere, but nothing confirms the session ended.
        world = self.pooled_world()
        world.session_delete = False
        code, lines, errors = self.verify(world, list(self.POOLED))
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "INFO: User grant 2 (M-tools): the server does not support session DELETE (405)", lines
        )
        self.assertIn(
            "PASS: User grant 2 (M-tools)'s cost center's pool of 8 calls a month refused at its "
            "limit with the gateway's quota 403, after 8 calls in this run. The session's DELETE "
            "was call 8, within the pool, but the server doesn't support it, so the session is "
            "left for the server to expire, and User grant 1 (M-tools), under another cost "
            "center, still reached its tools",
            lines,
        )
        self.assertFalse(any("so the session was closed" in line for line in lines))
        # The DELETE reached the server, so the pool counted it.
        self.assertEqual(world.cost_center_calls["pooled"][-2:], [("DELETE", None), ("ping", 403)])
        self.assertIn("session-2", world.sessions)

    def test_a_spent_pool_fails_the_proof_before_a_session_opens(self) -> None:
        for sessions in (True, False):
            with self.subTest(sessions=sessions):
                world = self.pooled_world()
                world.servers["tools"].sessions = sessions
                world.pool_spent[("tools", "pooled")] = 8
                lines = self.fails(
                    world,
                    list(self.POOLED),
                    "User grant 2 (M-tools): its cost center's pool of 8 calls a month refused the "
                    "first probe with the gateway's quota 403, before any session opened, so "
                    "something had already spent it this month. Use a cost center made for this "
                    "proof, whose pool nothing has called this month",
                )
                self.assertEqual(world.cost_center_calls["pooled"], [("ping", 403)])
                # Only the other grant's checks opened a session, and closed it.
                self.assertEqual(world.session_count, 1 if sessions else 0)
                self.assertEqual(world.sessions, {})
                self.assertFalse(any("Live MCP checks passed" in line for line in lines))

    def test_a_partly_spent_pool_fails_the_proof_with_unresolved_cleanup(self) -> None:
        # Earlier calls this month spent part of the pool, so it refuses a call this run planned,
        # and the session's DELETE with it. Cleanup has no way around the quota.
        for spent, refused, call in ((3, "tools/call", 6), (1, "the session's DELETE", 8)):
            with self.subTest(spent=spent):
                world = self.pooled_world()
                world.pool_spent[("tools", "pooled")] = spent
                lines = self.fails(
                    world,
                    list(self.POOLED),
                    f"User grant 2 (M-tools): its cost center's pool refused {refused}, call "
                    f"{call} of the 8 it allows a month, with the gateway's quota 403, so "
                    "something had already spent part of it this month. The cost center wasn't "
                    "fresh: use one made for this proof, whose pool nothing has called this "
                    "month; User grant 2 (M-tools): unresolved session cleanup (HTTP 403, a call "
                    "quota at the gateway). DELETE was not confirmed; verification is incomplete",
                )
                self.assertEqual(list(world.sessions), ["session-2"])
                self.assertEqual(world.pool_spent[("tools", "pooled")], 8)
                self.assertEqual(world.clock.sleeps, [])
                self.assertFalse(any("pool of 8 calls a month refused" in line for line in lines))
                self.assertFalse(any("Live MCP checks passed" in line for line in lines))
                deletes = self.pooled_session_deletes(world)
                self.assertEqual(len(deletes), 1)
                self.assertEqual(deletes[0].headers["x-mosaic-cost-center"].lower(), "pooled")
                self.assertEqual(world.cost_center_calls["pooled"][-1], ("DELETE", 403))
        # A pool with only the probe's call left refuses initialize, so no session opens.
        world = self.pooled_world()
        world.pool_spent[("tools", "pooled")] = 7
        self.fails(
            world,
            list(self.POOLED),
            "User grant 2 (M-tools): its cost center's pool refused initialize, call 2 of the 8 "
            "it allows a month, with the gateway's quota 403, so something had already spent "
            "part of it this month. The cost center wasn't fresh",
        )
        self.assertEqual(world.session_count, 1)
        self.assertEqual(world.sessions, {})

    def test_pooled_quota_proof_allows_the_gateway_a_call_or_two_past_the_limit(self) -> None:
        for overshoot in (1, 2):
            with self.subTest(overshoot=overshoot):
                world = self.pooled_world()
                world.pool_overshoot = overshoot
                code, lines, errors = self.verify(world, list(self.POOLED))
                self.assertEqual(code, 0, errors)
                self.assertIn(
                    "PASS: User grant 2 (M-tools)'s cost center's pool of 8 calls a month "
                    f"refused once spent with the gateway's quota 403, after {8 + overshoot} "
                    "calls in this run. The session's DELETE was call 8, so the session was "
                    "closed within the pool, and User grant 1 (M-tools), under another cost "
                    "center, still reached its tools",
                    lines,
                )
                self.assertIn(
                    f"INFO: User grant 2 (M-tools)'s pool allowed {overshoot} call(s) more than "
                    "its 8 before it refused: API Management's counters are distributed, so a "
                    "spent quota can let a call or two through",
                    lines,
                )
                # Only probes, which can't create server state, went past the limit.
                self.assertEqual(
                    world.cost_center_calls["pooled"][7:],
                    [("DELETE", None), *[("ping", None)] * overshoot, ("ping", 403)],
                )
                self.assertEqual(world.sessions, {})

    def test_pooled_quota_proof_fails_when_the_pool_keeps_letting_calls_through(self) -> None:
        for setting, value in (("pool_overshoot", 3), ("enforce_pools", False)):
            with self.subTest(setting):
                world = self.pooled_world()
                setattr(world, setting, value)
                lines = self.fails(
                    world,
                    list(self.POOLED),
                    "User grant 2 (M-tools): 11 calls went through, more than its cost center's "
                    "pool of 8 calls allows, even with 2 more for API Management's distributed "
                    "counters",
                )
                self.assertEqual(
                    world.cost_center_calls["pooled"][7:], [("DELETE", None), *[("ping", None)] * 3]
                )
                self.assertEqual(world.sessions, {})
                self.assertFalse(any("Live MCP checks passed" in line for line in lines))

    def test_pooled_quota_proof(self) -> None:
        world = self.pooled_world()
        # A stateless server needs no DELETE through an exhausted monthly pool.
        world.servers["tools"].sessions = False
        code, lines, errors = self.verify(world, list(self.POOLED))
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "PASS: User grant 2 (M-tools)'s cost center's pool of 8 calls a month refused a call "
            "with the gateway's quota 403 after 8 more call(s), while User grant 1 (M-tools), "
            "under another cost center, still reached its tools",
            lines,
        )
        self.assertIn("INFO: User grant 2 (M-tools)'s quota 403 had a Retry-After", lines)
        self.assertEqual(world.pool_spent[("tools", "pooled")], 8)
        # The other grant's calls named its own cost center, after the pool was spent.
        last = [call for call in world.passed if call.method == "POST"][-3:]
        self.assertEqual(
            [call.headers.get("x-mosaic-cost-center") for call in last],
            ["general", "general", "general"],
        )
        # With no session to close, tool calls after the probe spend the pool until it refuses.
        self.assertEqual(
            world.cost_center_calls["pooled"],
            [
                ("ping", None),
                ("initialize", None),
                ("notifications/initialized", None),
                *[("tools/call", None)] * 5,
                ("tools/call", 403),
            ],
        )

        # A stateless server answers the probe. Earlier calls this month spent part of this pool,
        # which a stateless proof reports rather than fails.
        world = self.pooled_world()
        world.servers["tools"].sessions = False
        world.require_protocol_header = False
        world.pool_spent[("tools", "pooled")] = 3
        world.retry_after = False
        code, lines, errors = self.verify(world, list(self.POOLED))
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "PASS: User grant 2 (M-tools)'s cost center's pool of 8 calls a month refused a call "
            "with the gateway's quota 403 after 5 more call(s), while User grant 1 (M-tools), "
            "under another cost center, still reached its tools",
            lines,
        )
        self.assertIn(
            "INFO: User grant 2 (M-tools)'s pool allowed 5 of its 8 calls in this run: something "
            "had spent the rest this month",
            lines,
        )
        self.assertIn(
            "INFO: User grant 2 (M-tools)'s quota 403 had no Retry-After in seconds", lines
        )

    def test_pooled_quota_proof_needs_the_pool_to_refuse_only_its_cost_center(self) -> None:
        # On a stateless server, calls go on until the pool refuses, so a pool that never does
        # fails once one more call than it allows goes through.
        world = self.pooled_world()
        world.servers["tools"].sessions = False
        world.enforce_pools = False
        self.fails(
            world,
            list(self.POOLED),
            "User grant 2 (M-tools): 9 calls went through, more than its cost center's pool of 8 "
            "calls allows",
        )
        # A pool that refuses every cost center on the server fails the other grant's check. The
        # proof runs first here, so the other grant's own checks don't spend the pool before it.
        world = self.pooled_world()
        world.pools_by_cost_center = False
        self.fails(
            world,
            [
                *("--user-entitlement", "user-pooled", "--user-entitlement", "user-tools"),
                *("--prove-pooled-quota", "user-pooled"),
            ],
            "User grant 2 (M-tools), under another cost center, after the pool was spent: "
            "unexpected HTTP 403, a call quota at the gateway",
        )
        # The server's own 429 isn't the pool's. Grant 1's checks and DELETE take 6 of the calls
        # the server serves, and the proof's probe, initialize and notification take the last 3.
        world = self.pooled_world()
        world.backend_capacity = 9
        self.fails(
            world,
            list(self.POOLED),
            "User grant 2 (M-tools): a call was refused with HTTP 429, not the gateway's quota 403",
        )
        world = self.pooled_world()
        world.grants["user-pooled"].limits = {"calls": 3, "renewalPeriodSeconds": 60}
        world.connection_patch = {"limits": None}
        self.fails(
            world,
            list(self.POOLED),
            "User grant 2 (M-tools): proving the pooled quota needs a grant with no call limits "
            "of its own",
        )
        # A call limit the gateway applies all the same refuses first, which proves nothing.
        world = self.pooled_world()
        world.grants["user-pooled"].limits = {"calls": 6, "renewalPeriodSeconds": 60}
        world.connection_patch = {"limits": None}
        publication = world.publication

        def unlimited(server: FakeServer) -> dict[str, Any]:
            document = publication(server)
            for grant in document["appliedAccess"]["grants"]:
                grant["enforcement"] = None
            return document

        world.publication = unlimited  # type: ignore[method-assign]
        self.fails(
            world,
            list(self.POOLED),
            "User grant 2 (M-tools): a call limit refused a call before the pool did",
        )

    def test_pooled_quota_proof_needs_a_small_pool_and_another_cost_center(self) -> None:
        world = self.pooled_world()
        world.pools.clear()
        self.fails(
            world,
            list(self.POOLED),
            "its cost center has no pooled monthly call quota applied on this server",
        )
        world = self.pooled_world(calls=51)
        self.fails(world, list(self.POOLED), "needs a pool of at most 50 calls a month, not 51")
        world = self.pooled_world(calls=4)
        self.fails(
            world,
            list(self.POOLED),
            "User grant 2 (M-tools): proving the pooled quota needs a pool of at least 5 calls a "
            "month, for a probe, initialize, the initialized notification, a tool call and the "
            "session's DELETE, not 4",
        )
        self.assertEqual(world.gateway_calls, [])
        world = self.pooled_world()
        world.grants["user-tools"].cost_center = "pooled"
        world.pools[("tools", "pooled")] = 8
        self.fails(
            world,
            list(self.POOLED),
            "needs another grant in this run on the same MCP server, under a different cost center",
        )
        world = self.pooled_world()
        self.fails(
            world,
            ["--user-entitlement", "user-pooled", "--prove-pooled-quota", "user-pooled"],
            "needs another grant in this run on the same MCP server",
        )
        self.assertEqual(world.gateway_calls, [])
        world = self.pooled_world()
        world.connection_patch = {"costCenter": None}
        self.fails(
            world, list(self.POOLED), "proving a pooled quota needs a grant with a cost center"
        )
        world = self.pooled_world()
        publication = world.publication

        def unapplied(server: FakeServer) -> dict[str, Any]:
            document = publication(server)
            grants = document["appliedAccess"]["grants"]
            document["appliedAccess"]["grants"] = [
                grant for grant in grants if grant["entitlementId"] != "user-pooled"
            ]
            return document

        world.publication = unapplied  # type: ignore[method-assign]
        self.fails(world, list(self.POOLED), "the server's last apply doesn't include this grant")
        self.assertEqual(world.gateway_calls, [])

    def watch(
        self,
        world: FakeWorld,
        *,
        timeout: int = 300,
        grants: tuple[str, ...] = ("user-tools",),
        watched: str = "user-tools",
        flag: str = "--user-entitlement",
        env: dict[str, str] | None = None,
    ) -> tuple[int, list[str], str]:
        arguments = [argument for grant in grants for argument in (flag, grant)]
        arguments += ["--watch-revocation", watched, "--revocation-timeout", str(timeout)]
        arguments += ["--revocation-interval", "30"]
        return self.verify(world, arguments, {**ENV, **(env or {})})

    def test_revocation_watch_waits_for_mosaic_then_the_gateway(self) -> None:
        world = standard_world()
        world.grants["user-tools"].revoke_at = START + 45
        code, lines, errors = self.watch(world)
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "WAIT: revoke User grant 1 (M-tools) in MOSAIC's console, which disables it, and "
            "apply its MCP server's access plan. Checking every 30 seconds for up to 300 seconds",
            lines,
        )
        for status in ("revocationPending", "applying", "revoked"):
            self.assertIn(f"WAIT: MOSAIC reports User grant 1 (M-tools) as {status}", lines)
        self.assertIn("PASS: User grant 1 (M-tools) rejects its token after revocation", lines)
        self.assertEqual(lines[-1], "Live MCP checks passed for 1 grant(s).")
        self.assertEqual(world.clock.sleeps, [30] * 5)
        # No call while it was pending or applying, then two refusals in a row.
        self.assertEqual([at for at in world.call_times if at > START], [1120.0, 1150.0])

        world = standard_world()
        world.grants["app-tools"].revoke_at = START + 45
        code, lines, errors = self.watch(
            world, grants=("app-tools",), watched="app-tools", flag="--application-entitlement"
        )
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "PASS: Application grant 1 (M-tools) rejects its token after revocation", lines
        )

    def test_revocation_watch_names_the_grant_s_cost_center(self) -> None:
        # Without the header, the gateway would serve the call on the person's other grant.
        world = FakeWorld(
            FakeGrant("user-tools", "user", "tools", USER_OID),
            FakeGrant(
                "user-research",
                "user",
                "tools",
                USER_OID,
                cost_center="research",
                default=False,
                order=1,
            ),
        )
        world.grants["user-research"].revoke_at = START + 45
        code, lines, errors = self.watch(
            world, grants=("user-tools", "user-research"), watched="user-research"
        )
        self.assertEqual(code, 0, errors)
        self.assertIn("PASS: User grant 2 (M-tools) rejects its token after revocation", lines)
        probes = [call for call in world.gateway_calls if call.url.path == "/mcp/m-tools/mcp"]
        self.assertEqual(
            [call.headers.get("x-mosaic-cost-center") for call in probes[-2:]],
            ["research", "research"],
        )

    def test_revocation_watch_ignores_refusals_it_cannot_attribute(self) -> None:
        # While the plan applies, the gateway refuses every call, whatever the outcome will be.
        world = standard_world()
        world.grants["user-tools"].revoke_at = START + 45
        world.apply_seconds = 10_000
        code, lines, errors = self.watch(world, timeout=120)
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (M-tools): after 120 seconds, MOSAIC reports it as applying, not revoked",
            errors,
        )
        self.assertEqual([at for at in world.call_times if at > START], [])

        for setting, value, last in (
            ("revocation_reaches_gateway", False, "HTTP 200"),
            # One of two gateway units still serves the grant, so refusals alternate.
            ("revocation_flaps", True, "HTTP 403"),
            # Token validation's 401 says nothing about the grant.
            ("revoked_status", 401, "HTTP 401"),
            # Nor does a budget's 403, while the gateway still finds the grant.
            ("budget_blocked_from", START + 1, "HTTP 403, not the grant lookup's"),
        ):
            with self.subTest(setting):
                world = standard_world()
                world.grants["user-tools"].revoke_at = START + 45
                setattr(world, setting, value)
                # Isolate grant-lookup convergence from a flapping gateway denying DELETE.
                world.servers["tools"].sessions = False
                if setting == "budget_blocked_from":
                    world.revocation_reaches_gateway = False
                code, lines, errors = self.watch(world)
                self.assertEqual(code, 1)
                self.assertIn(
                    "User grant 1 (M-tools): MOSAIC reports it revoked, but after 300 seconds the "
                    "gateway's grant lookup hadn't rejected calls with its token 2 times in a row "
                    f"(last: {last})",
                    errors,
                )
                self.assertNotIn(
                    "PASS: User grant 1 (M-tools) rejects its token after revocation", lines
                )

    def test_revocation_watch_counts_only_the_grant_lookup_s_refusal(self) -> None:
        # The pooled proof spends the pool, so later calls get the quota's 403, revoked or not.
        arguments = [*self.POOLED, "--watch-revocation", "user-pooled"]
        arguments += ["--revocation-timeout", "300", "--revocation-interval", "30"]
        world = self.pooled_world()
        world.servers["tools"].sessions = False
        world.grants["user-pooled"].revoke_at = START + 45
        world.revocation_reaches_gateway = False
        code, lines, errors = self.verify(world, arguments)
        self.assertEqual(code, 1, lines)
        self.assertIn(
            "User grant 2 (M-tools): MOSAIC reports it revoked, but after 300 seconds the "
            "gateway's grant lookup hadn't rejected calls with its token 2 times in a row "
            "(last: HTTP 403, a call quota at the gateway)",
            errors,
        )
        # Once the revocation reaches the gateway, its lookup refuses before the quota.
        world = self.pooled_world()
        world.servers["tools"].sessions = False
        world.grants["user-pooled"].revoke_at = START + 45
        code, lines, errors = self.verify(world, arguments)
        self.assertEqual(code, 0, errors)
        self.assertIn("PASS: User grant 2 (M-tools) rejects its token after revocation", lines)

        # A call that names no cost center is refused by the lookup with a 403 that asks for the
        # scope. One that doesn't ask for it isn't the lookup's.
        for scope_challenge, code_expected in ((True, 0), (False, 1)):
            with self.subTest(scope_challenge=scope_challenge):
                world = standard_world()
                world.connection_patch = {"costCenter": None}
                world.grants["user-tools"].revoke_at = START + 45
                world.scope_challenge = scope_challenge
                code, lines, errors = self.watch(world)
                self.assertEqual(code, code_expected, errors)
                probes = [
                    call for call in world.gateway_calls if call.url.path == "/mcp/m-tools/mcp"
                ]
                self.assertNotIn("x-mosaic-cost-center", probes[-1].headers)
                if code_expected:
                    self.assertIn("(last: HTTP 403, not the grant lookup's)", errors)
                else:
                    self.assertIn(
                        "PASS: User grant 1 (M-tools) rejects its token after revocation", lines
                    )

    def test_revocation_watch_needs_tokens_that_outlast_it_and_a_disabled_grant(self) -> None:
        world = standard_world()
        short = person_token(exp=WALL_CLOCK + 600)
        code, lines, errors = self.watch(
            world, timeout=1200, env={verifier.USER_RUNTIME_TOKEN: short}
        )
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (M-tools): the user's MCP token expires in 600 seconds, and the wait "
            "can take 1290 seconds. Get a new one, or lower --revocation-timeout",
            errors,
        )
        self.assertFalse(any(line.startswith("WAIT") for line in lines))
        self.assertEqual(world.clock.sleeps, [])

        world = standard_world()
        world.grants["user-tools"].deleted_at = START + 45
        code, _, errors = self.watch(world)
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (M-tools): MOSAIC no longer finds this grant, so the watch can't tell "
            "when the gateway applies its removal. Disable a grant (Revoke in the console) "
            "instead of deleting it",
            errors,
        )

        world = standard_world()
        code, _, errors = self.watch(world, timeout=60)
        self.assertEqual(code, 1)
        self.assertIn(
            "User grant 1 (M-tools): after 60 seconds, MOSAIC reports it as applied, not revoked",
            errors,
        )

    ON_BEHALF = ("--on-behalf-entitlement", "user-agent", "--send-model-requests")
    AWAIT = (*ON_BEHALF, "--await-attribution", "--attribution-interval", "60")

    def test_a_person_s_call_reaches_a_governed_model_through_the_agent(self) -> None:
        world = standard_world()
        world.call_seconds = 1.0
        code, lines, errors = self.verify(world, list(self.ON_BEHALF))
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "PASS: On-behalf grant (M-agent)'s server passes each call's reference to the "
            "application it names",
            lines,
        )
        self.assertIn(
            "PASS: On-behalf grant (M-agent) answered through ask_model, which calls a governed "
            "model",
            lines,
        )
        # The anonymous call and the metadata take the first two seconds of the fake clock.
        self.assertIn(
            "INFO: On-behalf grant (M-agent) called ask_model between 2023-11-14T22:13:22Z and "
            "2023-11-14T22:13:27Z UTC",
            lines,
        )
        self.assertIn(
            f"INFO: On-behalf grant (M-agent) was called by object ID {USER_OID}, through client "
            f"{MODEL_CLIENT}",
            lines,
        )
        key = verifier.grant_trace_key(TENANT, "mcppub-agent", "user-agent")
        self.assertIn(
            f"INFO: On-behalf grant (M-agent)'s MCP call trace reads: mosaic-attribution v=1 "
            f"g={key} m= a={MODEL_CLIENT} r=<request ID> i={AGENT_OID}",
            lines,
        )
        self.assertEqual(
            lines[-1],
            "Live MCP checks passed for 1 grant(s). Check revocation separately with "
            "--watch-revocation.",
        )
        # The server passed on the reference the gateway gave it, for the person who called.
        self.assertEqual(len(world.model_calls), 1)
        self.assertEqual(world.model_calls[0]["person"], USER_OID)
        self.assertEqual(
            world.model_calls[0]["reference"], world.forwarded[-2]["x-mosaic-on-behalf-of"]
        )
        self.assertEqual(world.deletes, ["session-1"])

    def test_the_agent_must_answer(self) -> None:
        for setting, value, message in (
            (
                "tool_error",
                "ask_model",
                "On-behalf grant (M-agent): the ask_model tool returned an error",
            ),
            ("answer_text", "  ", "On-behalf grant (M-agent): ask_model returned no answer"),
        ):
            with self.subTest(setting):
                world = standard_world()
                setattr(world, setting, value)
                self.fails(world, list(self.ON_BEHALF), message)
        world = standard_world()
        world.servers["agent"].tools = ("echo",)
        self.fails(
            world,
            list(self.ON_BEHALF),
            "On-behalf grant (M-agent): the server doesn't list ask_model",
        )

    def test_the_agent_must_pass_on_its_callers_references(self) -> None:
        # Without an applied model caller, its model calls can't be attributed: fail before them.
        world = standard_world()
        world.model_caller_applied = False
        self.fails(
            world,
            list(self.ON_BEHALF),
            "On-behalf grant (M-agent): its server's last apply names no model caller, so the "
            "server receives no reference",
        )
        self.assertEqual(world.model_calls, [])
        # Only an administrator can read the publication, so without one it's skipped.
        world = standard_world()
        code, lines, errors = self.verify(
            world, list(self.ON_BEHALF), without(verifier.ADMIN_CONTROL_TOKEN)
        )
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "SKIP: On-behalf grant (M-agent) checking that its server names an applied model "
            "caller: set MOSAIC_SMOKE_ADMIN_CONTROL_TOKEN",
            lines,
        )
        trace = next(line for line in lines if "MCP call trace" in line)
        self.assertTrue(trace.endswith(f"a={MODEL_CLIENT}"), trace)

    def test_attribution_is_awaited_in_the_person_s_usage_report(self) -> None:
        world = standard_world()
        code, lines, errors = self.verify(world, list(self.AWAIT))
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "WAIT: On-behalf grant (M-agent) waiting for MOSAIC to attribute the model call to "
            "the person. Checking every 60 seconds for up to 1800 seconds",
            lines,
        )
        self.assertIn(
            "PASS: On-behalf grant (M-agent): MOSAIC attributed 1 model call(s) through its "
            "server to the person, in their own usage report",
            lines,
        )
        self.assertIn(
            "INFO: On-behalf grant (M-agent)'s attributed calls were to the model gpt-4o-mini, "
            "charged to cost center agents",
            lines,
        )
        self.assertEqual(world.clock.sleeps, [60] * 10)

        world = standard_world()
        code, lines, errors = self.verify(
            world, [*self.AWAIT, "--model-caller-entitlement", MODEL_GRANT]
        )
        self.assertEqual(code, 0, errors)
        self.assertIn(
            "PASS: On-behalf grant (M-agent): the attributed calls were charged to the model "
            "caller's grant, on its model and under its cost center",
            lines,
        )

    def test_attribution_must_be_to_the_person_and_the_model_caller_s_grant(self) -> None:
        with_grant = [*self.AWAIT, "--model-caller-entitlement", MODEL_GRANT]
        for setting, value in (
            ("attributed_cost_center", {"id": "cc-general", "code": "general"}),
            ("attributed_resource", {"kind": "modelApi", "id": "model-api-other"}),
        ):
            with self.subTest(setting):
                world = standard_world()
                setattr(world, setting, value)
                self.fails(
                    world,
                    with_grant,
                    "On-behalf grant (M-agent): MOSAIC attributed the model call to a model or "
                    "cost center other than the model caller's grant",
                )
        world = standard_world()
        world.attribute = False
        self.fails(
            world,
            list(self.AWAIT),
            "On-behalf grant (M-agent): after 1800 seconds, the person's usage report shows no "
            "new model use through its server",
        )
        world = standard_world()
        world.model_grant["subject"] = {"kind": "application", "id": "principal-other"}
        self.fails(
            world,
            with_grant,
            "The model caller's model grant belongs to a different application from the one the "
            "on-behalf server calls models as",
        )
        self.assertEqual(world.gateway_calls, [])
        world = standard_world()
        world.model_grant["resource"] = {"kind": "mcpServer", "id": "mcpsrv-agent"}
        self.fails(world, with_grant, "The model caller's model grant isn't a grant on a model")
        world = standard_world()
        world.model_grant["subject"] = {"kind": "user", "id": "principal-agent"}
        self.fails(world, with_grant, "The model caller's model grant isn't an application's")
        self.assertEqual(world.gateway_calls, [])

    def test_attribution_is_skipped_where_usage_cannot_show_it(self) -> None:
        for setting, value, reason in (
            ("usage_status", 404, "this MOSAIC has no usage report"),
            ("data_source", "simulated", "this MOSAIC's usage is simulated"),
        ):
            with self.subTest(setting):
                world = standard_world()
                setattr(world, setting, value)
                code, lines, errors = self.verify(world, list(self.AWAIT))
                self.assertEqual(code, 0, errors)
                self.assertTrue(
                    any(
                        line.startswith(
                            f"SKIP: On-behalf grant (M-agent) waiting for its attribution: {reason}"
                        )
                        for line in lines
                    ),
                    lines,
                )
                self.assertEqual(world.clock.sleeps, [])
        world = standard_world()
        world.user_control = jwt(
            aud="api://mosaic-api", oid=USER_OID, tid=TENANT, exp=WALL_CLOCK + 600
        )
        self.fails(
            world,
            list(self.AWAIT),
            "On-behalf grant (M-agent): the MOSAIC control-plane token expires in 600 seconds, "
            "and the wait can take 1920 seconds. Get a new one, or lower --attribution-timeout",
            {**ENV, verifier.USER_CONTROL_TOKEN: world.user_control},
        )
        self.assertEqual(world.model_calls, [])

    def test_the_verifier_matches_mosaic(self) -> None:
        try:
            from mosaic_api import domain
            from mosaic_api.integrations import access_policy
        except ImportError:
            self.skipTest("mosaic_api isn't installed")
        self.assertEqual(
            verifier.SUPPORTED_PROTOCOL_VERSIONS, domain.MCP_SUPPORTED_PROTOCOL_VERSIONS
        )
        self.assertEqual(verifier.PROTOCOL_VERSION, domain.MCP_PROTOCOL_VERSION)
        self.assertEqual(
            verifier.PROTOCOL_HEADER_MINIMUM, domain.MCP_PROTOCOL_VERSION_HEADER_MINIMUM
        )
        self.assertEqual(verifier.MCP_SCOPE, domain.MCP_DELEGATED_SCOPE)
        self.assertEqual(verifier.MCP_ROLE, domain.MCP_APPLICATION_ROLE)
        self.assertEqual(verifier.COST_CENTER_HEADER, domain.COST_CENTER_HEADER)
        self.assertEqual(verifier.COST_CENTER_CODE.pattern, domain.COST_CENTER_CODE_PATTERN.pattern)
        self.assertEqual(verifier.METADATA_PREFIX, f"/{domain.MCP_RESOURCE_METADATA_PREFIX}")
        self.assertEqual(verifier.REMAINING_CALLS_HEADER, access_policy.REMAINING_CALLS_HEADER)
        # The fake refuses an unknown cost center as the policy does, and the verifier reads it.
        self.assertEqual(COST_CENTER_DENIED, access_policy.COST_CENTER_DENIED)
        self.assertIn(verifier.COST_CENTER_HEADER, access_policy.COST_CENTER_DENIED)
        publication = SimpleNamespace(tenant_id=TENANT, id="mcppub-agent")
        grant = SimpleNamespace(entitlement_id="user-agent")
        self.assertEqual(
            verifier.grant_trace_key(TENANT, "mcppub-agent", "user-agent"),
            access_policy.grant_counter_identity(publication, grant),
        )

    def test_names_shared_with_the_model_verifier(self) -> None:
        for name in (
            "USER_CONTROL_TOKEN",
            "ADMIN_CONTROL_TOKEN",
            "APPLICATION_CLIENT_ID",
            "APPLICATION_CLIENT_SECRET",
        ):
            self.assertEqual(getattr(verifier, name), getattr(model_verifier, name))
        self.assertEqual(verifier.MODEL_RUNTIME_TOKEN, model_verifier.USER_RUNTIME_TOKEN)
        # MCP tokens have names of their own, so a model token is never sent as one.
        model_tokens = {
            model_verifier.USER_RUNTIME_TOKEN,
            model_verifier.APPLICATION_RUNTIME_TOKEN,
            model_verifier.UNGRANTED_USER_RUNTIME_TOKEN,
        }
        for name in (
            verifier.USER_RUNTIME_TOKEN,
            verifier.APPLICATION_RUNTIME_TOKEN,
            verifier.UNGRANTED_USER_RUNTIME_TOKEN,
        ):
            self.assertNotIn(name, model_tokens)

    def test_redirects_are_refused_and_transport_errors_print_nothing(self) -> None:
        world = standard_world()
        gateway = world.gateway

        def redirect(request: httpx.Request) -> httpx.Response:
            if "Authorization" in request.headers:
                return httpx.Response(307, headers={"Location": "https://elsewhere.example/mcp"})
            return gateway(request)

        world.gateway = redirect  # type: ignore[method-assign]
        self.fails(
            world,
            TOOLS_ONLY,
            "User grant 1 (M-tools) rejecting a MOSAIC control-plane token: unexpected HTTP 307",
        )

        world = standard_world()

        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        world.gateway = unreachable  # type: ignore[method-assign]
        code, _, errors = self.verify(world, TOOLS_ONLY)
        self.assertEqual(code, 1)
        self.assertEqual(errors, "FAIL: HTTP transport failure\n")

        world = standard_world()

        def interrupted(request: httpx.Request) -> httpx.Response:
            raise KeyboardInterrupt

        world.gateway = interrupted  # type: ignore[method-assign]
        code, _, errors = self.verify(world, TOOLS_ONLY)
        self.assertEqual(code, 130)
        self.assertEqual(errors, "STOPPED: interrupted before the checks finished\n")


if __name__ == "__main__":
    unittest.main()
