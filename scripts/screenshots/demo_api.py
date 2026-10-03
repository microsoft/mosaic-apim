"""Run the real MOSAIC API against a fictional Contoso estate, for README screenshots.

The API is the production FastAPI app with in-memory repositories. Its Azure-facing services are
rebuilt on the fakes in :mod:`scripts.screenshots.demo_fakes`, and the estate is seeded through
MOSAIC's own services (register, sync, import, publish, grant, request, approve), so every page
renders exactly what the product would show for that estate.

Nothing here reaches Azure, Entra, Key Vault, or an MCP server. Every name, address, identifier,
and key is invented. Run it on its own to browse the demo estate with ``npm run dev``::

    python -m uv run python -m scripts.screenshots.demo_api

Requests from a portal origin are answered as the demo end user; everything else is answered as
the demo administrator. Local authentication is refused outside local and test environments, so
this cannot be pointed at a deployed MOSAIC.
"""

import argparse
import math
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

import httpx
import uvicorn
from azure.core.credentials_async import AsyncTokenCredential
from fastapi import FastAPI, Request
from mosaic_api.auth import AuthContext
from mosaic_api.budgets import BudgetAction, BudgetUpdate, EmailSettingsUpdate, EmailTest
from mosaic_api.config import (
    AuthMode,
    Environment,
    RepositoryBackend,
    Settings,
    UsageSourceMode,
)
from mosaic_api.cost_centers import (
    CostCenterCreate,
    CostCenterLimit,
    CostCenterLimitsUpdate,
    CostCenterUpdate,
    PersonLimits,
    PooledQuota,
)
from mosaic_api.domain import (
    AccessRequestApproval,
    AccessRequestCreate,
    AccessRequestState,
    CatalogEntryUpdate,
    DirectoryObject,
    Entitlement,
    EntitlementCreate,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementSubject,
    GatewayCreate,
    GatewayUpdate,
    GroupCreate,
    ImportRequest,
    McpEndpointCreate,
    McpModelCallerUpdate,
    McpPublicationCreate,
    ModelAccessSettings,
    ModelEndpointCreate,
    PrincipalCreate,
    PrincipalKind,
    PublicationCreate,
    PublishRunStatus,
    RequestEnforcement,
    TokenEnforcement,
    deterministic_id,
    general_cost_center_id,
    mcp_server_id,
    model_api_id,
    subject_kind_for,
    utc_now,
)
from mosaic_api.environments import EnvironmentColor, EnvironmentCreate
from mosaic_api.integrations.aoai import CognitiveServicesClient
from mosaic_api.integrations.aoai.backend_key_access import KeyVaultLocator
from mosaic_api.integrations.aoai.client import SubscriptionScanner
from mosaic_api.integrations.aoai.key_check import KeyCheckOutcome, KeyCheckResult
from mosaic_api.integrations.apim import ApimClient, ApimWriter, ArmClient
from mosaic_api.integrations.apim.credentials import ApimCredentialClient, ApimKeyManager
from mosaic_api.integrations.email import EmailMessage, EmailSendResult
from mosaic_api.integrations.graph.fake import FakeDirectoryLookup
from mosaic_api.main import create_app
from mosaic_api.model_pools import (
    ModelPoolCreate,
    ModelPoolType,
    ModelPoolUpdate,
    ModelPoolVisibility,
    PoolMemberSpec,
    PoolModelSpec,
    uses_backend_pools,
)
from mosaic_api.pricing import DeploymentPricingUpdate, EndpointPricingUpdate, PriceCreate
from mosaic_api.repositories import GatewayRepository, InMemoryEntitlementRepository
from mosaic_api.services import (
    DirectoryService,
    EntitlementService,
    EnvironmentService,
    GatewayService,
    McpEndpointService,
    ModelEndpointService,
    PublishingService,
    UsageService,
)
from mosaic_api.services import cost_centers as cost_center_service
from mosaic_api.services.analytics import AnalyticsService
from mosaic_api.services.budget_gate import BlockedListGate
from mosaic_api.services.budgets import BudgetService
from mosaic_api.services.cost_centers import CostCenterService
from mosaic_api.services.directory import Actor
from mosaic_api.services.mcp_endpoints import build_mcp_client_factory
from mosaic_api.services.mcp_publishing import McpPublishingService
from mosaic_api.services.model_pools import ModelPoolService
from mosaic_api.services.pool_health import PoolHealthService
from mosaic_api.services.portal_access import PortalAccessService
from mosaic_api.services.pricing import PricingService
from mosaic_api.services.telemetry import TelemetryService, member_host
from mosaic_api.services.usage import RollupUsageSource
from mosaic_api.services.usage_rollup import UsageRollupService

from scripts.screenshots.demo_fakes import (
    AI_RESOURCE_ID,
    DEV_GATEWAY_RESOURCE_ID,
    FOUNDRY_NORTH_CENTRAL_RESOURCE_ID,
    FOUNDRY_RESOURCE_ID,
    FOUNDRY_WEST_RESOURCE_ID,
    GATEWAY_RESOURCE_ID,
    KEY_VAULT_ID,
    PARTNER_GATEWAY_RESOURCE_ID,
    DemoApim,
    DemoLogs,
    FakeCredential,
    PoolMemberTraffic,
    TrafficStream,
    build_cognitive_accounts,
    build_key_store,
    build_key_vault,
    build_mcp_servers,
    cognitive_handler,
    gateway_handler,
    log_every_call,
    mcp_handler,
    send_logs_to_workspace,
)

TENANT_ID = "5f2d7c1e-8a3b-4c9d-b0e1-f2a3b4c5d6e7"
MOSAIC_PRINCIPAL_ID = "6e3f8d2a-9b4c-4d0e-a1f2-a3b4c5d6e7f8"
MODEL_RUNTIME_CLIENT_ID = "7f4a9e3b-0c5d-4e1f-b2a3-b4c5d6e7f809"
MODEL_CLIENT_ID = "8a5b0f4c-1d6e-4f2a-83b4-c5d6e7f8091a"
SUBSCRIPTION_COUNTER = "@(context.Subscription.Id)"
PARTNER_FOUNDRY_URL = (
    "https://fabrikam-foundry.services.ai.azure.com/api/projects/partner-models"
)
# Fictional. The console masks it, and MOSAIC keeps it only in the demo's Key Vault double.
PARTNER_FOUNDRY_DEMO_KEY = "demo-foundry-key-not-a-real-key-0000"

DEFAULT_API_PORT = 8000
DEFAULT_CONSOLE_PORT = 5173
DEFAULT_PORTAL_PORT = 5174


@dataclass(frozen=True)
class Person:
    label: str
    object_id: str
    kind: str = "user"


# Microsoft's long-standing fictional demo personas and invented workloads. None is a real person.
ADELE = Person("Adele Vance", "11a0f3c2-4b6d-4e8f-9a1b-2c3d4e5f6a7b")
ALEX = Person("Alex Wilber", "22b1e4d3-5c7e-4f90-8b2c-3d4e5f6a7b8c")
DIEGO = Person("Diego Siciliani", "33c2f5e4-6d8f-4a01-9c3d-4e5f6a7b8c9d")
MEGAN = Person("Megan Bowen", "44d3a6f5-7e90-4b12-8d4e-5f6a7b8c9d0e")
ISAIAH = Person("Isaiah Langer", "55e4b7a6-8fa1-4c23-9e5f-6a7b8c9d0e1f")
LIDIA = Person("Lidia Holloway", "66f5c8b7-90b2-4d34-8f6a-7b8c9d0e1f2a")
NESTOR = Person("Nestor Wilke", "77a6d9c8-a1c3-4e45-9a7b-8c9d0e1f2a3b")
PATTI = Person("Patti Fernandez", "88b7e0d9-b2d4-4f56-8b8c-9d0e1f2a3b4c")
PRADEEP = Person("Pradeep Gupta", "99c8f1ea-c3e5-4a67-9c9d-0e1f2a3b4c5d")
JOHANNA = Person("Johanna Lorenz", "a0d9a2fb-d4f6-4b78-8d0e-1f2a3b4c5d6e")
SUPPORT_COPILOT = Person(
    "Contoso Support Copilot", "b1eab30c-e5a7-4c89-9e1f-2a3b4c5d6e7f", "servicePrincipal"
)
CLAIMS_TRIAGE = Person(
    "Claims triage function", "c2fbc41d-f6b8-4d9a-8f2a-3b4c5d6e7f80", "managedIdentity"
)
DOCS_INDEXER = Person("Docs indexer", "d3acd52e-a7c9-4eab-9a3b-4c5d6e7f8091", "managedIdentity")
# The managed identity the Docs Search MCP server runs as. Its tools call GPT-4o mini, and Claude
# Sonnet through the Anthropic pool, as it, on its own grants, for the people who called the
# server. See ADR 0025.
DOCS_SEARCH_SERVICE = Person(
    "Docs Search service", "f6a7b8c9-d0e1-4f23-8a45-b6c7d8e9f0a1", "managedIdentity"
)
SALES_INSIGHTS = Person(
    "Sales insights bot", "e4bde63f-b8da-4fbc-8b4c-5d6e7f8091a2", "servicePrincipal"
)
MARKET_RESEARCH_AGENT = Person(
    "Market Research Agent", "1a2b3c4d-5e6f-4789-8abc-0d1e2f3a4b5c", "agentIdentity"
)
INVOICE_RECONCILIATION_AGENT = Person(
    "Invoice Reconciliation Agent", "3c4d5e6f-7081-49ab-8cde-2f3a4b5c6d7e", "agentIdentity"
)
SCHEDULING_ASSISTANT_AGENT = Person(
    "Scheduling Assistant Agent", "5e6f7081-92a3-4bcd-8ef0-4b5c6d7e8f90", "agentIdentity"
)
SCHEDULING_ASSISTANT = Person(
    "Scheduling Assistant", "708192a3-b4c5-4def-8012-6d7e8f901a2b", "agentUser"
)
AI_MODEL_USERS = Person(
    "AI Model Users", "8192a3b4-c5d6-4ef0-9123-7e8f901a2b3c", "securityGroup"
)
FINANCE_AI_PILOT = Person(
    "Finance AI Pilot", "92a3b4c5-d6e7-4f01-8234-8f901a2b3c4d", "securityGroup"
)
AGENT_BUILDERS = Person(
    "Agent Builders", "a3b4c5d6-e7f8-4012-9345-901a2b3c4d5e", "securityGroup"
)

AGENT_BLUEPRINTS = {
    MARKET_RESEARCH_AGENT.object_id: "2b3c4d5e-6f70-489a-9bcd-1e2f3a4b5c6d",
    INVOICE_RECONCILIATION_AGENT.object_id: "4d5e6f70-8192-4abc-9def-3a4b5c6d7e8f",
    SCHEDULING_ASSISTANT_AGENT.object_id: "6f708192-a3b4-4cde-9f01-5c6d7e8f901a",
}
AGENT_USER_PARENTS = {
    SCHEDULING_ASSISTANT.object_id: SCHEDULING_ASSISTANT_AGENT.object_id,
}

PEOPLE = [
    ADELE,
    ALEX,
    DIEGO,
    MEGAN,
    ISAIAH,
    LIDIA,
    NESTOR,
    PATTI,
    PRADEEP,
    JOHANNA,
    SUPPORT_COPILOT,
    CLAIMS_TRIAGE,
    DOCS_INDEXER,
    DOCS_SEARCH_SERVICE,
    SALES_INSIGHTS,
    MARKET_RESEARCH_AGENT,
    INVOICE_RECONCILIATION_AGENT,
    SCHEDULING_ASSISTANT_AGENT,
    SCHEDULING_ASSISTANT,
    AI_MODEL_USERS,
    FINANCE_AI_PILOT,
    AGENT_BUILDERS,
]

EXTRA_DIRECTORY_OBJECTS = [
    DirectoryObject(
        object_id="b4c5d6e7-f809-4123-8456-0a1b2c3d4e5f",
        kind=PrincipalKind.USER,
        display_name="Henrietta Mueller",
        detail="henrietta.mueller@contoso.com",
    ),
    DirectoryObject(
        object_id="c5d6e7f8-091a-4234-9567-1b2c3d4e5f60",
        kind=PrincipalKind.AGENT_IDENTITY,
        display_name="Benefits Bot Agent",
        detail="c5d6e7f8-091a-4234-9567-1b2c3d4e5f60",
        app_id="c5d6e7f8-091a-4234-9567-1b2c3d4e5f60",
        blueprint_id="d6e7f809-1a2b-4345-8678-2c3d4e5f6071",
    ),
    DirectoryObject(
        object_id="e7f8091a-2b3c-4456-9789-3d4e5f607182",
        kind=PrincipalKind.SECURITY_GROUP,
        display_name="Retail AI Champions",
        detail="retail-ai-champions",
    ),
]

DIRECTORY_GROUP_MEMBERS = {
    AI_MODEL_USERS.object_id: [
        MEGAN.object_id,
        ISAIAH.object_id,
        LIDIA.object_id,
        MARKET_RESEARCH_AGENT.object_id,
    ],
    FINANCE_AI_PILOT.object_id: [
        DIEGO.object_id,
        LIDIA.object_id,
        INVOICE_RECONCILIATION_AGENT.object_id,
    ],
    AGENT_BUILDERS.object_id: [
        MARKET_RESEARCH_AGENT.object_id,
        INVOICE_RECONCILIATION_AGENT.object_id,
        SCHEDULING_ASSISTANT_AGENT.object_id,
    ],
}

# The administrator signed in to the console, and the end user signed in to the portal.
ADMIN = ADELE
PORTAL_USER = MEGAN

GROUPS: list[tuple[str, str, list[Person]]] = [
    (
        "AI Platform Engineers",
        "Run the shared AI gateway and review access requests.",
        [ADELE, ALEX, PRADEEP],
    ),
    (
        "Data Science",
        "Analysts and data scientists building on shared models.",
        [MEGAN, ISAIAH, LIDIA, JOHANNA],
    ),
    (
        "Customer Support Agents",
        "Frontline support staff using assisted replies.",
        [PATTI, NESTOR],
    ),
    ("Finance Analysts", "Forecasting and reporting.", [DIEGO, LIDIA]),
]

# The cost centers Contoso charges its AI use to, beside the built-in General, which stays the
# tenant's default: code, name, description, owners, and whether their grants may use keys.
COST_CENTERS: list[tuple[str, str, str, list[str], bool]] = [
    (
        "CI-204",
        "Customer Insights",
        "Churn analysis, research notebooks and the market research agent.",
        ["megan.bowen@contoso.com", "lidia.holloway@contoso.com"],
        True,
    ),
    (
        "CS-110",
        "Customer Support",
        "The support copilot and the frontline support team.",
        ["patti.fernandez@contoso.com"],
        True,
    ),
    (
        "FIN-310",
        "Finance",
        "Forecasting, invoices and the finance AI pilot. Microsoft Entra tokens only.",
        ["diego.siciliani@contoso.com"],
        False,
    ),
    (
        "CLM-520",
        "Claims Operations",
        "Claims triage automation.",
        ["johanna.lorenz@contoso.com"],
        True,
    ),
]
# Who an administrator charged to which cost center when onboarding them. Everyone else gets the
# tenant's default, General.
DEFAULT_COST_CENTERS = {
    MEGAN.object_id: "CI-204",
    ISAIAH.object_id: "CI-204",
    MARKET_RESEARCH_AGENT.object_id: "CI-204",
    PATTI.object_id: "CS-110",
    NESTOR.object_id: "CS-110",
    SUPPORT_COPILOT.object_id: "CS-110",
    DIEGO.object_id: "FIN-310",
    INVOICE_RECONCILIATION_AGENT.object_id: "FIN-310",
    CLAIMS_TRIAGE.object_id: "CLM-520",
}
# Each cost center's listed members: people, applications, agents and Entra security groups.
COST_CENTER_MEMBERS: dict[str, list[Person]] = {
    "CI-204": [MEGAN, ISAIAH, MARKET_RESEARCH_AGENT, AI_MODEL_USERS],
    "CS-110": [SUPPORT_COPILOT, PATTI, NESTOR],
    "FIN-310": [DIEGO, INVOICE_RECONCILIATION_AGENT, FINANCE_AI_PILOT],
    "CLM-520": [CLAIMS_TRIAGE],
}

# Budget email goes through a Communication Services double that accepts every message and
# delivers none. Both names are invented.
DEMO_EMAIL_ENDPOINT = "https://contoso-mosaic.unitedstates.communication.azure.com"
DEMO_EMAIL_SENDER = "DoNotReply@notify.contoso.com"


@dataclass(frozen=True)
class DemoBudget:
    """A monthly budget, by cost center code, or None for the organization's."""

    code: str | None
    # How much of the budget the month's spend so far uses when the demo starts. The amount is
    # worked out from that spend, so every capture shows the same levels whatever the date.
    used: float
    action: BudgetAction
    recipients: list[str]


# Customer Insights near its limit, which Megan's portal warns about; Customer Support past its own
# and blocked; Finance on track; and the organization's, which only warns. Claims Operations and
# General have none.
BUDGETS = [
    DemoBudget(None, 0.64, "continue", ["ai-platform@contoso.com"]),
    DemoBudget("CI-204", 0.87, "block", ["insights-leads@contoso.com"]),
    DemoBudget("CS-110", 1.06, "block", ["support-leads@contoso.com"]),
    DemoBudget("FIN-310", 0.42, "continue", []),
]


class DemoEmail:
    """Azure Communication Services Email that accepts every message and sends none."""

    def __init__(self) -> None:
        self.accepted: list[EmailMessage] = []

    async def send(
        self, endpoint: str, sender: str, message: EmailMessage, *, operation_id: str
    ) -> EmailSendResult:
        self.accepted.append(message)
        return EmailSendResult(accepted=True, operation_id=operation_id, status_code=202)

    async def close(self) -> None:
        return None

# Every string that identifies a resource, endpoint, or account in the estate. Captures blur these
# wherever they render, alongside the generic patterns in capture.py.
SENSITIVE_LITERALS = [
    TENANT_ID,
    MOSAIC_PRINCIPAL_ID,
    MODEL_RUNTIME_CLIENT_ID,
    MODEL_CLIENT_ID,
    *(person.object_id for person in PEOPLE),
    *(blueprint for blueprint in AGENT_BLUEPRINTS.values()),
    *(item.object_id for item in EXTRA_DIRECTORY_OBJECTS),
    *(
        item.blueprint_id
        for item in EXTRA_DIRECTORY_OBJECTS
        if item.blueprint_id is not None
    ),
]


def _directory_detail(person: Person) -> str | None:
    if person.kind in {"user", "agentUser"}:
        first, last = person.label.split(" ", 1)
        return f"{first.lower()}.{last.lower()}@contoso.com"
    if person.kind in {"agentIdentity", "servicePrincipal", "managedIdentity"}:
        return person.object_id
    if person.kind == "securityGroup":
        return person.label.lower().replace(" ", "-")
    return None


def _directory_object(person: Person) -> DirectoryObject:
    kind = PrincipalKind(person.kind)
    return DirectoryObject(
        object_id=person.object_id,
        kind=kind,
        display_name=person.label,
        detail=_directory_detail(person),
        app_id=person.object_id if kind == PrincipalKind.AGENT_IDENTITY else None,
        identity_parent_id=AGENT_USER_PARENTS.get(person.object_id),
        blueprint_id=AGENT_BLUEPRINTS.get(person.object_id),
    )


def build_directory_lookup() -> FakeDirectoryLookup:
    return FakeDirectoryLookup(
        [*(_directory_object(person) for person in PEOPLE), *EXTRA_DIRECTORY_OBJECTS],
        DIRECTORY_GROUP_MEMBERS,
    )


class DemoAuthenticator:
    """Sign every console request in as the administrator and every portal request as the user.

    The browser names the calling SPA in the ``Origin`` header of each cross-origin request, which
    is all a local demo needs to tell the two apart.
    """

    def __init__(self, tenant_id: str, portal_origins: Iterable[str]) -> None:
        self._portal_origins = {origin.rstrip("/").casefold() for origin in portal_origins}
        self._admin = AuthContext(
            object_id=ADMIN.object_id,
            tenant_id=tenant_id,
            roles=frozenset({"Admin", "User"}),
        )
        self._user = AuthContext(
            object_id=PORTAL_USER.object_id,
            tenant_id=tenant_id,
            roles=frozenset({"User"}),
            group_ids=frozenset({AI_MODEL_USERS.object_id.casefold()}),
        )

    async def authenticate(self, request: Request) -> AuthContext:
        origin = (request.headers.get("origin") or "").rstrip("/").casefold()
        return self._user if origin in self._portal_origins else self._admin

    async def close(self) -> None:
        return None


async def _no_sleep(_seconds: float) -> None:
    return None


async def _demo_secret(_secret_uri: str) -> str:
    return "demo-mcp-token-not-a-real-secret"


async def _demo_model_key(_secret_uri: str) -> str:
    return "demo-foundry-key-not-a-real-key"


async def _demo_key_check(_origin: str, _key: str) -> KeyCheckResult:
    # The partner's Foundry resource accepts the key, as its real counterpart would.
    return KeyCheckResult(KeyCheckOutcome.ACCEPTED, 200)


async def _demo_token(_audience: str) -> str:
    return "demo-entra-token-not-a-real-token"


@dataclass
class DemoServices:
    """The fake-backed services that replace the Azure-facing ones in ``app.state``."""

    directory: DirectoryService
    gateways: GatewayService
    endpoints: ModelEndpointService
    publishing: PublishingService
    mcp_publishing: McpPublishingService
    mcp_endpoints: McpEndpointService
    entitlements: EntitlementService
    environments: EnvironmentService
    # The seed dates its grants in the past, which only the in-memory store lets it do.
    entitlement_repository: InMemoryEntitlementRepository
    gateway_repository: GatewayRepository
    logs: DemoLogs
    telemetry: TelemetryService
    usage_rollups: UsageRollupService
    pricing: PricingService
    cost_centers: CostCenterService
    portal_access: PortalAccessService
    analytics: AnalyticsService
    budgets: BudgetService
    pools: ModelPoolService
    clients: list[httpx.AsyncClient] = field(default_factory=list)

    async def aclose(self) -> None:
        await self.budgets.aclose()
        await self.usage_rollups.aclose()
        await self.gateways.aclose()
        await self.endpoints.aclose()
        await self.publishing.aclose()
        await self.mcp_publishing.aclose()
        await self.mcp_endpoints.aclose()
        await self.pools.aclose()
        for client in self.clients:
            await client.aclose()


def _arm(handler: Any) -> tuple[ArmClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    arm = ArmClient(cast(AsyncTokenCredential, FakeCredential()), client=http, sleep=_no_sleep)
    return arm, http


def install_demo_services(app: FastAPI, portal_origins: Iterable[str]) -> DemoServices:
    state = app.state
    settings: Settings = state.settings
    entitlement_repository = state.entitlement_repository
    if not isinstance(entitlement_repository, InMemoryEntitlementRepository):
        raise SeedError("The demo estate needs the in-memory entitlement repository")
    apim = DemoApim()
    dev_apim = DemoApim(DEV_GATEWAY_RESOURCE_ID, public_ip="203.0.113.25")
    # Both gateways already send their resource logs to Contoso's workspace, and log every call.
    send_logs_to_workspace(apim)
    send_logs_to_workspace(dev_apim)
    log_every_call(apim)
    log_every_call(dev_apim)
    gateway_arm, gateway_http = _arm(gateway_handler(apim, dev_apim))
    ai_arm, ai_http = _arm(cognitive_handler(build_cognitive_accounts()))
    vault_arm, vault_http = _arm(build_key_vault().handler)
    mcp_http = httpx.AsyncClient(transport=httpx.MockTransport(mcp_handler(build_mcp_servers())))
    lookup = build_directory_lookup()
    directory = DirectoryService(
        state.repository,
        gateway_repository=state.gateway_repository,
        entitlement_repository=state.entitlement_repository,
        directory_lookup=lookup,
        group_claims_enabled=settings.entra_group_claims,
        cost_center_repository=state.cost_center_repository,
    )

    # Built as create_app builds them, with only the Azure-facing clients swapped for fakes.
    gateways = GatewayService(
        state.gateway_repository,
        client_factory=lambda resource: ApimClient(gateway_arm, resource),
        principal_id=MOSAIC_PRINCIPAL_ID,
        environment_repository=state.environment_repository,
    )
    endpoints = ModelEndpointService(
        state.model_endpoint_repository,
        gateway_repository=state.gateway_repository,
        client_factory=lambda resource: CognitiveServicesClient(ai_arm, resource),
        scanner=SubscriptionScanner(ai_arm),
        principal_id=MOSAIC_PRINCIPAL_ID,
        environment_repository=state.environment_repository,
        secret_resolver=_demo_model_key,
        key_probe=_demo_key_check,
        vault_locator=KeyVaultLocator(vault_arm, known_vault_ids=[KEY_VAULT_ID]),
        key_store=build_key_store(),
    )
    # Every gateway MOSAIC manages holds the list of cost centers whose budgets block their calls.
    blocked_list = BlockedListGate(
        state.budget_repository, gateway_repository=state.gateway_repository
    )
    publishing = PublishingService(
        state.gateway_repository,
        endpoint_repository=state.model_endpoint_repository,
        client_factory=lambda resource: ApimClient(gateway_arm, resource),
        writer_factory=lambda resource: ApimWriter(gateway_arm, resource),
        directory_repository=state.repository,
        entitlement_repository=state.entitlement_repository,
        model_runtime_client_id=settings.model_runtime_client_id,
        environment_repository=state.environment_repository,
        cost_center_repository=state.cost_center_repository,
        blocked_list=blocked_list,
    )
    mcp_publishing = McpPublishingService(
        state.gateway_repository,
        mcp_endpoint_repository=state.mcp_endpoint_repository,
        entitlement_repository=state.entitlement_repository,
        directory_repository=state.repository,
        client_factory=lambda resource: ApimClient(gateway_arm, resource),
        writer_factory=lambda resource: ApimWriter(gateway_arm, resource),
        runtime_client_id=settings.model_runtime_client_id,
        security_group_claims=settings.entra_group_claims,
        environment_repository=state.environment_repository,
        cost_center_repository=state.cost_center_repository,
        blocked_list=blocked_list,
    )
    mcp_endpoints = McpEndpointService(
        state.mcp_endpoint_repository,
        gateway_repository=state.gateway_repository,
        entitlement_repository=state.entitlement_repository,
        client_factory=build_mcp_client_factory(mcp_http),
        secret_resolver=_demo_secret,
        token_resolver=_demo_token,
        require_https=True,
        allow_private_endpoints=False,
        environment_repository=state.environment_repository,
    )
    state.directory_lookup = lookup
    state.directory_service = directory
    state.entitlement_service._directory_lookup = lookup
    state.gateway_service = gateways
    state.model_endpoint_service = endpoints
    state.publishing_service = publishing
    state.mcp_publishing_service = mcp_publishing
    state.mcp_endpoint_service = mcp_endpoints
    pools = ModelPoolService(
        state.gateway_repository,
        endpoint_repository=state.model_endpoint_repository,
        client_factory=lambda resource: ApimClient(gateway_arm, resource),
        writer_factory=lambda resource: ApimWriter(gateway_arm, resource),
        environment_repository=state.environment_repository,
        directory_repository=state.repository,
        entitlement_repository=state.entitlement_repository,
        cost_center_repository=state.cost_center_repository,
        model_runtime_client_id=settings.model_runtime_client_id,
        security_group_claims=settings.entra_group_claims,
        blocked_list=blocked_list,
    )
    state.model_pool_service = pools
    portal_access = PortalAccessService(
        state.entitlement_service,
        repository=state.entitlement_repository,
        directory_repository=state.repository,
        gateway_repository=state.gateway_repository,
        credential_factory=lambda resource: ApimCredentialClient(gateway_arm, resource),
        model_runtime_client_id=settings.model_runtime_client_id,
        model_client_id=settings.model_client_id,
        key_manager_factory=lambda resource: ApimKeyManager(gateway_arm, resource),
    )
    state.portal_access_service = portal_access
    # Usage and analytics read the demo's gateway logs through MOSAIC's own rollup job.
    logs = DemoLogs()
    telemetry = TelemetryService(
        gateway_repository=state.gateway_repository,
        rollup_repository=state.usage_rollup_repository,
        entitlement_repository=state.entitlement_repository,
        client_factory=lambda resource: ApimClient(gateway_arm, resource),
        writer_factory=lambda resource: ApimWriter(gateway_arm, resource),
        logs=logs,
        rollups_enabled=True,
        interval_seconds=settings.usage_rollup_interval_seconds,
        principal_id=MOSAIC_PRINCIPAL_ID,
    )
    usage_rollups = UsageRollupService(
        state.usage_rollup_repository,
        gateway_repository=state.gateway_repository,
        entitlement_repository=state.entitlement_repository,
        directory_repository=state.repository,
        logs=logs,
        telemetry=telemetry,
        tenant_id=settings.tenant_id,
        interval_seconds=settings.usage_rollup_interval_seconds,
        retention_days=settings.usage_rollup_retention_days,
        backfill_max_days=settings.usage_rollup_backfill_max_days,
        cost_center_repository=state.cost_center_repository,
        endpoint_repository=state.model_endpoint_repository,
    )
    state.telemetry_service = telemetry
    state.usage_rollup_service = usage_rollups
    # A pool's health reads the attempts its policy traced, from the same demo logs.
    state.pool_health_service = PoolHealthService(
        pools,
        gateway_repository=state.gateway_repository,
        endpoint_repository=state.model_endpoint_repository,
        logs=logs,
        principal_id=MOSAIC_PRINCIPAL_ID,
    )
    # Costs come from MOSAIC's own price list: the shipped seed, plus what the estate sets below.
    pricing: PricingService = state.pricing_service
    state.usage_service = UsageService(
        state.portal_service,
        source=RollupUsageSource(
            state.usage_rollup_repository,
            interval_seconds=settings.usage_rollup_interval_seconds,
        ),
        gateway_repository=state.gateway_repository,
        endpoint_repository=state.model_endpoint_repository,
        environment_repository=state.environment_repository,
        pricing=pricing,
        entitlement_repository=state.entitlement_repository,
        cost_center_repository=state.cost_center_repository,
    )
    state.analytics_service = AnalyticsService(
        state.usage_rollup_repository,
        gateway_repository=state.gateway_repository,
        entitlement_repository=state.entitlement_repository,
        directory_repository=state.repository,
        environment_repository=state.environment_repository,
        endpoint_repository=state.model_endpoint_repository,
        rollups=usage_rollups,
        directory_lookup=lookup,
        pricing=pricing,
        configured=True,
        interval_seconds=settings.usage_rollup_interval_seconds,
        retention_days=settings.usage_rollup_retention_days,
        cost_center_repository=state.cost_center_repository,
    )
    # Budgets are judged by the demo's own check, run once the usage is seeded, against the same
    # gateway doubles. The production loop stays off, so nothing reaches Azure.
    budgets = BudgetService(
        state.budget_repository,
        cost_center_repository=state.cost_center_repository,
        gateway_repository=state.gateway_repository,
        gate=blocked_list,
        client_factory=lambda resource: ApimClient(gateway_arm, resource),
        writer_factory=lambda resource: ApimWriter(gateway_arm, resource),
        analytics=state.analytics_service,
        email=DemoEmail(),
        portal=state.portal_service,
        tenant_id=settings.tenant_id,
        suggested_endpoint=DEMO_EMAIL_ENDPOINT,
        suggested_sender=DEMO_EMAIL_SENDER,
    )
    state.budget_service = budgets
    state.authenticator = DemoAuthenticator(settings.tenant_id, portal_origins)
    return DemoServices(
        directory=directory,
        gateways=gateways,
        endpoints=endpoints,
        publishing=publishing,
        mcp_publishing=mcp_publishing,
        mcp_endpoints=mcp_endpoints,
        entitlements=state.entitlement_service,
        environments=state.environment_service,
        entitlement_repository=entitlement_repository,
        gateway_repository=state.gateway_repository,
        logs=logs,
        telemetry=telemetry,
        usage_rollups=usage_rollups,
        pricing=pricing,
        cost_centers=state.cost_center_service,
        portal_access=portal_access,
        analytics=state.analytics_service,
        budgets=budgets,
        pools=pools,
        clients=[gateway_http, ai_http, vault_http, mcp_http],
    )


def _tokens(
    per_minute: int | None = None, quota: int | None = None, period: str | None = None
) -> TokenEnforcement:
    return TokenEnforcement.model_validate(
        {
            "counter_key_expression": SUBSCRIPTION_COUNTER,
            "tokens_per_minute": per_minute,
            "token_quota": quota,
            "token_quota_period": period,
        }
    )


def _calls(
    per_window: int | None = None,
    window_seconds: int | None = None,
    quota: int | None = None,
    period: str | None = None,
) -> RequestEnforcement:
    return RequestEnforcement.model_validate(
        {
            "counter_key_expression": SUBSCRIPTION_COUNTER,
            "calls": per_window,
            "renewal_period_seconds": window_seconds,
            "call_quota": quota,
            "call_quota_period": period,
        }
    )


class SeedError(RuntimeError):
    pass


@dataclass
class Estate:
    """What the seed created, by the names the capture script and a reader care about."""

    gateway_id: str = ""
    dev_gateway_id: str = ""
    partner_gateway_id: str = ""
    aoai_endpoint_id: str = ""
    foundry_endpoint_id: str = ""
    foundry_north_central_endpoint_id: str = ""
    foundry_west_endpoint_id: str = ""
    partner_foundry_endpoint_id: str = ""
    publications: dict[str, str] = field(default_factory=dict)
    # Model pool IDs by display name.
    pools: dict[str, str] = field(default_factory=dict)
    model_apis: dict[str, str] = field(default_factory=dict)
    dev_model_apis: dict[str, str] = field(default_factory=dict)
    mcp_endpoints: dict[str, str] = field(default_factory=dict)
    mcp_servers: dict[str, str] = field(default_factory=dict)
    # The API name MOSAIC gave the Docs Search MCP server it published.
    docs_mcp_api: str = ""
    principals: dict[str, str] = field(default_factory=dict)
    groups: dict[str, str] = field(default_factory=dict)
    # Cost center IDs by code.
    cost_centers: dict[str, str] = field(default_factory=dict)


def _require_synced(run: Any, label: str) -> None:
    # A partial sync means a fake is missing a surface the collector reads; fix the fake rather
    # than capturing a degraded estate.
    if str(run.status) != "succeeded":
        raise SeedError(f"Syncing {label} ended {run.status}: {run.errors}")


async def _publish(services: DemoServices, actor: Actor, publication_id: str, label: str) -> None:
    plan = await services.publishing.plan(actor, publication_id)
    run = await services.publishing.apply(actor, publication_id, plan.id)
    await services.publishing.wait_for_idle()
    finished = await services.publishing.get_run(actor, run.id)
    if finished.status != PublishRunStatus.SUCCEEDED:
        raise SeedError(f"Publishing {label} ended {finished.status}: {finished.model_dump()}")


async def _publish_mcp(
    services: DemoServices, actor: Actor, publication_id: str, label: str
) -> None:
    plan = await services.mcp_publishing.plan(actor, publication_id)
    run = await services.mcp_publishing.apply(actor, publication_id, plan.id)
    await services.mcp_publishing.wait_for_idle()
    finished = await services.mcp_publishing.get_run(actor, publication_id, run.id)
    if finished.status != PublishRunStatus.SUCCEEDED:
        raise SeedError(f"Publishing MCP {label} ended {finished.status}: {finished.model_dump()}")


async def _publish_pool(services: DemoServices, actor: Actor, pool_id: str, label: str) -> None:
    plan = await services.pools.plan(actor, pool_id)
    run = await services.pools.apply(actor, pool_id, plan.id)
    await services.pools.wait_for_idle()
    finished = await services.pools.get_run(actor, pool_id, run.id)
    if finished.status != PublishRunStatus.SUCCEEDED:
        raise SeedError(f"Publishing pool {label} ended {finished.status}: {finished.model_dump()}")


# How long ago the estate's grants were made, other than those that came from a request.
GRANT_AGE = timedelta(days=120)


def _held_grant(
    services: DemoServices,
    estate: Estate,
    subject: Person,
    resource_id: str,
    cost_center: str | None = None,
) -> Entitlement:
    """The subject's enabled grant on a resource, under one cost center when it holds several."""

    subject_id = estate.principals[subject.label]
    wanted = estate.cost_centers[cost_center] if cost_center else None
    found = [
        item
        for item in services.entitlement_repository.entitlements.values()
        if item.enabled
        and item.subject.id == subject_id
        and item.resource.id == resource_id
        and (wanted is None or item.cost_center_id == wanted)
    ]
    if len(found) != 1:
        raise SeedError(
            f"{subject.label} holds {len(found)} grants on {resource_id} under {cost_center}"
        )
    return found[0]


def _date_history(
    repository: InMemoryEntitlementRepository, decisions: dict[str, timedelta]
) -> None:
    """Move the grants and decisions seeded so far into the past.

    MOSAIC stamps each record with the time it's written, so an estate seeded a moment ago would
    show every grant as made today beside a quarter of gateway traffic. ``decisions`` says how long
    ago each decided request was decided; the grant it produced dates from then, and every other
    grant from ``GRANT_AGE`` ago. Pending requests keep today's date.
    """
    now = utc_now()
    granted: dict[str, datetime] = {}
    for request_id, age in decisions.items():
        request = repository.access_requests[request_id]
        decided_at = now - age
        repository.access_requests[request_id] = request.model_copy(
            update={
                "created_at": decided_at - timedelta(days=1),
                "updated_at": decided_at,
                "decided_at": decided_at,
            }
        )
        if request.granted_entitlement_id:
            granted[request.granted_entitlement_id] = decided_at
    for entitlement_id, entitlement in list(repository.entitlements.items()):
        repository.entitlements[entitlement_id] = entitlement.model_copy(
            update={"created_at": granted.get(entitlement_id, now - GRANT_AGE)}
        )


@contextmanager
def _stable_cost_center_id(tenant_id: str, code: str) -> Iterator[None]:
    """Give the cost center created inside it an ID that's the same every run.

    A grant's ID includes its cost center's, and pages order grants by ID when nothing else tells
    them apart. Random IDs would reorder those pages, and every screenshot of them, on each run.
    Only the demo does this: MOSAIC itself names a new cost center at random.
    """

    original = cost_center_service.new_id

    def stable(prefix: str) -> str:
        if prefix == "costCenter":
            return deterministic_id(prefix, tenant_id, "demo", code)
        return original(prefix)

    cost_center_service.new_id = stable
    try:
        yield
    finally:
        cost_center_service.new_id = original


async def seed_estate(services: DemoServices, tenant_id: str) -> Estate:
    """Build the Contoso estate through MOSAIC's own services, in the order an operator would."""

    admin = Actor(ADMIN.object_id, tenant_id)
    estate = Estate()

    # Cost centers first, so onboarding can charge each person to theirs.
    estate.cost_centers["general"] = general_cost_center_id(tenant_id)
    for code, name, description, owners, keys_allowed in COST_CENTERS:
        with _stable_cost_center_id(tenant_id, code):
            cost_center = await services.cost_centers.create_cost_center(
                admin,
                CostCenterCreate(
                    name=name,
                    code=code,
                    description=description,
                    owners=owners,
                    keys_allowed=keys_allowed,
                ),
            )
        estate.cost_centers[code] = cost_center.id
    await services.cost_centers.update_cost_center(
        admin,
        estate.cost_centers["general"],
        CostCenterUpdate(owners=["adele.vance@contoso.com"]),
    )

    for person in PEOPLE:
        default = DEFAULT_COST_CENTERS.get(person.object_id)
        principal = await services.directory.create_principal(
            admin,
            PrincipalCreate.model_validate(
                {
                    "object_id": person.object_id,
                    "kind": person.kind,
                    "label": person.label,
                    "default_cost_center_id": estate.cost_centers[default] if default else None,
                }
            ),
        )
        estate.principals[person.label] = principal.id
    for code, members in COST_CENTER_MEMBERS.items():
        for member in members:
            await services.cost_centers.add_member(
                admin, estate.cost_centers[code], estate.principals[member.label]
            )
    for name, description, members in GROUPS:
        group = await services.directory.create_group(
            admin, GroupCreate(name=name, description=description)
        )
        estate.groups[name] = group.id
        for member in members:
            await services.directory.add_membership(
                admin, group.id, estate.principals[member.label]
            )

    # A custom environment beside the built-in ones. It faces partners, so it is production-class,
    # and its gateways may front the production model endpoints.
    await services.environments.create_environment(
        admin,
        EnvironmentCreate(
            key="partner",
            display_name="Partner",
            description="Gateways that serve Contoso's external partners.",
            color=EnvironmentColor.SEVERE,
            production=True,
            accepts_endpoints_from=["production"],
        ),
    )

    gateway = await services.gateways.register(
        admin,
        GatewayCreate(
            azure_resource_id=GATEWAY_RESOURCE_ID,
            name="Contoso AI Gateway",
            environment="production",
        ),
    )
    estate.gateway_id = gateway.id
    _require_synced(await services.gateways.sync_now(admin, gateway.id), gateway.name)
    await services.gateways.update(admin, gateway.id, GatewayUpdate(management_mode="manage"))
    partner = await services.gateways.register(
        admin,
        GatewayCreate(
            azure_resource_id=PARTNER_GATEWAY_RESOURCE_ID,
            name="Partner Gateway",
            environment="partner",
        ),
    )
    estate.partner_gateway_id = partner.id

    for model in await services.gateways.import_model_apis(
        admin, gateway.id, ImportRequest(api_names=["azure-openai", "foundry-inference"])
    ):
        estate.model_apis[model.api_name] = model.id
    for server in await services.gateways.import_mcp_servers(
        admin,
        gateway.id,
        ImportRequest(api_names=["orders-mcp", "docs-search-mcp", "service-desk-mcp"]),
    ):
        estate.mcp_servers[server.api_name] = server.id
    catalog_summaries = {
        "azure-openai": "The shared Azure OpenAI surface, with semantic caching and safety.",
        "foundry-inference": "Open-weight and partner models from Azure AI Foundry.",
    }
    for api_name, summary in catalog_summaries.items():
        await services.gateways.update_model_api_catalog(
            admin,
            model_api_id(tenant_id, gateway.id, api_name),
            CatalogEntryUpdate(summary=summary),
        )
    mcp_summaries = {
        "orders-mcp": ("catalog", "Look up orders, start returns, and track shipments."),
        "docs-search-mcp": ("catalog", "Search Contoso product and policy documentation."),
        "service-desk-mcp": ("catalog", "Open and track IT service desk tickets."),
    }
    for api_name, (visibility, summary) in mcp_summaries.items():
        await services.gateways.update_mcp_server_catalog(
            admin,
            mcp_server_id(tenant_id, gateway.id, api_name),
            CatalogEntryUpdate.model_validate({"visibility": visibility, "summary": summary}),
        )

    # The development copy of the gateway, which MOSAIC only observes. Its model APIs share their
    # production twins' names, so the portal offers each environment's access separately.
    dev_gateway = await services.gateways.register(
        admin,
        GatewayCreate(
            azure_resource_id=DEV_GATEWAY_RESOURCE_ID,
            name="Contoso AI Dev Gateway",
            environment="development",
        ),
    )
    estate.dev_gateway_id = dev_gateway.id
    _require_synced(await services.gateways.sync_now(admin, dev_gateway.id), dev_gateway.name)
    for model in await services.gateways.import_model_apis(
        admin, dev_gateway.id, ImportRequest(api_names=["azure-openai", "foundry-inference"])
    ):
        estate.dev_model_apis[model.api_name] = model.id
    dev_summaries = {
        "azure-openai": "Azure OpenAI for building and testing, before you ask for production.",
        "foundry-inference": "Foundry models for building and testing, before production.",
    }
    for api_name, summary in dev_summaries.items():
        await services.gateways.update_model_api_catalog(
            admin,
            model_api_id(tenant_id, dev_gateway.id, api_name),
            CatalogEntryUpdate(summary=summary),
        )

    aoai = await services.endpoints.register(
        admin,
        ModelEndpointCreate(
            azure_resource_id=AI_RESOURCE_ID,
            name="Contoso Azure OpenAI",
            environment="production",
        ),
    )
    estate.aoai_endpoint_id = aoai.id
    foundry = await services.endpoints.register(
        admin,
        ModelEndpointCreate(
            azure_resource_id=FOUNDRY_RESOURCE_ID,
            name="Contoso AI Foundry",
            environment="production",
        ),
    )
    estate.foundry_endpoint_id = foundry.id
    # Contoso runs Claude in two more regions, so a pool can spread it across all three.
    foundry_north_central = await services.endpoints.register(
        admin,
        ModelEndpointCreate(
            azure_resource_id=FOUNDRY_NORTH_CENTRAL_RESOURCE_ID,
            name="Contoso AI Foundry North Central",
            environment="production",
        ),
    )
    estate.foundry_north_central_endpoint_id = foundry_north_central.id
    foundry_west = await services.endpoints.register(
        admin,
        ModelEndpointCreate(
            azure_resource_id=FOUNDRY_WEST_RESOURCE_ID,
            name="Contoso AI Foundry West",
            environment="production",
        ),
    )
    estate.foundry_west_endpoint_id = foundry_west.id
    for endpoint in (aoai, foundry, foundry_north_central, foundry_west):
        _require_synced(await services.endpoints.sync_now(admin, endpoint.id), endpoint.name)

    # A partner's Foundry resource in its own Microsoft Entra tenant, which MOSAIC's managed
    # identity can't reach. It is registered by URL with its API key, which MOSAIC keeps in the
    # demo's Key Vault (ADR 0021), and its deployments are declared, because a key can't list them
    # (ADR 0018).
    partner_foundry = await services.endpoints.register(
        admin,
        ModelEndpointCreate.model_validate(
            {
                "endpoint": PARTNER_FOUNDRY_URL,
                "api_key": PARTNER_FOUNDRY_DEMO_KEY,
                "name": "Fabrikam partner Foundry",
                "environment": "production",
                "deployments": [
                    {
                        "deployment_name": "claude-sonnet-4-5",
                        "model_name": "claude-sonnet-4-5",
                        "api_shape": "anthropicMessages",
                    },
                    {
                        "deployment_name": "gpt-4-1-mini",
                        "model_name": "gpt-4.1-mini",
                        "api_shape": "azureOpenAi",
                    },
                ],
            }
        ),
    )
    if str(partner_foundry.status) != "connected":
        raise SeedError("The partner Foundry endpoint's key check didn't pass")
    estate.partner_foundry_endpoint_id = partner_foundry.id
    # A key can't read a declared deployment's type, so the administrator says what it is and
    # MOSAIC prices it. The partner's Claude deployment stays unpriced: nobody declared its type.
    await services.pricing.update_endpoint(
        admin,
        partner_foundry.id,
        EndpointPricingUpdate(
            deployments=[
                DeploymentPricingUpdate(
                    deployment_name="gpt-4-1-mini", deployment_type="GlobalStandard"
                )
            ]
        ),
    )
    # Contoso negotiated a lower GPT-4o rate from next month. A dated version changes nothing
    # before its date, so the Pricing page shows it as a scheduled change to the list price.
    month_start = utc_now().date().replace(day=1)
    await services.pricing.add_price(
        admin,
        PriceCreate(
            cloud="commercial",
            publisher="OpenAI",
            model="gpt-4o",
            version="2024-11-20",
            deployment_type="GlobalStandard",
            input_per_million=2.25,
            cached_input_per_million=1.125,
            output_per_million=9.0,
            effective_from=(month_start + timedelta(days=32)).replace(day=1),
            source_url="https://contoso.example/agreements/azure-enterprise-2026",
            note="Contoso's enterprise agreement: 10% off list price.",
            overrides="commercial.openai.gpt-4o.2024-11-20.globalstandard",
        ),
    )
    # The price list MOSAIC ships doesn't carry Claude, so Contoso records what it pays. Each
    # region a pool sends a call to is priced from its own deployment, like any other.
    for claude_model, input_price, output_price in (
        ("claude-opus-4-5", 5.0, 25.0),
        ("claude-sonnet-4-5", 3.0, 15.0),
        ("claude-haiku-4-5", 1.0, 5.0),
    ):
        await services.pricing.add_price(
            admin,
            PriceCreate(
                cloud="commercial",
                publisher="Anthropic",
                model=claude_model,
                deployment_type="GlobalStandard",
                input_per_million=input_price,
                cached_input_per_million=input_price / 10,
                output_per_million=output_price,
                effective_from=(month_start - timedelta(days=95)).replace(day=1),
                source_url="https://contoso.example/agreements/foundry-claude-2026",
                note="Claude in Microsoft Foundry, under Contoso's Marketplace agreement.",
            ),
        )

    # Sales CRM keeps only a legacy free-text label, so Settings has one resource to classify.
    mcp_endpoints = [
        McpEndpointCreate.model_validate(
            {
                "endpoint": "https://mcp.contoso.com/docs/mcp",
                "name": "Contoso Docs Search",
                "environment": "production",
            }
        ),
        McpEndpointCreate.model_validate(
            {
                "endpoint": "https://servicedesk.contoso.com/mcp",
                "name": "IT Service Desk",
                "environment": "production",
                "resource_audience": "api://contoso-servicedesk",
            }
        ),
        McpEndpointCreate.model_validate(
            {
                "endpoint": "https://crm.contoso.com/mcp",
                "name": "Sales CRM",
                "environment_label": "Pilot",
                "credential_secret_uri": (
                    "https://kv-contoso-ai.vault.azure.net/secrets/crm-mcp-token"
                ),
            }
        ),
    ]
    for request in mcp_endpoints:
        mcp_endpoint = await services.mcp_endpoints.register(admin, request)
        _require_synced(
            await services.mcp_endpoints.sync_now(admin, mcp_endpoint.id), mcp_endpoint.name
        )
        estate.mcp_endpoints[mcp_endpoint.name] = mcp_endpoint.id

    governed = ModelAccessSettings(keys_enabled=True, entra_enabled=True)
    publications: list[tuple[str, str, str, TokenEnforcement, ModelAccessSettings | None, str]] = [
        (
            aoai.id,
            "gpt-4o",
            "GPT-4o",
            _tokens(per_minute=50_000),
            governed,
            "General-purpose chat and reasoning for internal copilots.",
        ),
        (
            aoai.id,
            "gpt-4o-mini",
            "GPT-4o mini",
            _tokens(per_minute=100_000, quota=20_000_000, period="Monthly"),
            ModelAccessSettings(keys_enabled=True, entra_enabled=False),
            "Fast, low-cost chat for high-volume workloads.",
        ),
        (
            aoai.id,
            "text-embedding-3-large",
            "Text embeddings (large)",
            _tokens(per_minute=200_000),
            None,
            "Embeddings for search and retrieval-augmented generation.",
        ),
        (
            foundry.id,
            "Phi-4",
            "Phi-4",
            _tokens(per_minute=30_000),
            governed,
            "A small language model for classification and extraction.",
        ),
    ]
    for endpoint_id, deployment, display_name, limits, access, summary in publications:
        publication = await services.publishing.create(
            admin,
            PublicationCreate(
                gateway_id=gateway.id,
                model_endpoint_id=endpoint_id,
                deployment_name=deployment,
                display_name=display_name,
                enforcement=limits,
                governed_access=access,
            ),
        )
        await _publish(services, admin, publication.id, display_name)
        published = await services.publishing.get_publication(admin, publication.id)
        if not published.model_api_id:
            raise SeedError(f"{display_name} has no catalog model API after publishing")
        estate.publications[display_name] = publication.id
        estate.model_apis[display_name] = published.model_api_id
        await services.gateways.update_model_api_catalog(
            admin, published.model_api_id, CatalogEntryUpdate(summary=summary)
        )

    # Claude runs in three regions, and callers see one API with one name per model (ADR 0024).
    def claude_member(endpoint_id: str, deployment: str, weight: int = 1) -> PoolMemberSpec:
        return PoolMemberSpec(
            model_endpoint_id=endpoint_id, deployment_name=deployment, weight=weight
        )

    claude_pool = await services.pools.create(
        admin,
        ModelPoolCreate(
            gateway_id=gateway.id,
            display_name="Anthropic Claude",
            description=(
                "Claude for every team, across East US 2, North Central US, and West US 3. "
                "A region that throttles is skipped until it recovers."
            ),
            models=[
                PoolModelSpec(
                    display_name="Claude Opus 4.5",
                    members=[
                        claude_member(foundry.id, "claude-opus-4-5", weight=2),
                        claude_member(foundry_north_central.id, "claude-opus-4-5"),
                        claude_member(foundry_west.id, "claude-opus-4-5"),
                    ],
                ),
                PoolModelSpec(
                    display_name="Claude Sonnet 4.5",
                    members=[
                        claude_member(foundry.id, "claude-sonnet-4-5", weight=2),
                        claude_member(foundry_north_central.id, "claude-sonnet-4-5"),
                    ],
                ),
            ],
        ),
    )
    await _publish_pool(services, admin, claude_pool.id, claude_pool.display_name)
    estate.pools[claude_pool.display_name] = claude_pool.id
    # Then Contoso governs it: callers need a grant on each model they use, and the next apply
    # suspends the shared key the pool was first published with.
    claude_pool = await services.pools.update(
        admin,
        claude_pool.id,
        ModelPoolUpdate(
            governed_access=ModelAccessSettings(keys_enabled=True, entra_enabled=True)
        ),
    )
    claude_models = {model.display_name: model.id for model in claude_pool.models}
    # A second pool, saved but not yet published. Evaluation runs start in North Central US and
    # move to East US 2 only when it fails.
    evaluation_pool = await services.pools.create(
        admin,
        ModelPoolCreate(
            gateway_id=gateway.id,
            display_name="Claude evaluation",
            description="Offline evaluation runs, kept off the catalog.",
            visibility=ModelPoolVisibility.HIDDEN,
            pool_type=ModelPoolType.LINEAR,
            models=[
                PoolModelSpec(
                    display_name="Claude Sonnet 4.5",
                    members=[
                        claude_member(foundry_north_central.id, "claude-sonnet-4-5"),
                        claude_member(foundry.id, "claude-sonnet-4-5"),
                    ],
                )
            ],
        ),
    )
    estate.pools[evaluation_pool.display_name] = evaluation_pool.id

    async def grant(
        subject: Person | str,
        resource_kind: str,
        resource_id: str,
        enforcement: EntitlementEnforcement | None = None,
        notes: str | None = None,
        *,
        cost_center: str | None = None,
        scope_id: str | None = None,
    ) -> None:
        """Grant a resource, charged to ``cost_center`` by code, or to the subject's default.

        ``scope_id`` names the pool a pool model belongs to.
        """

        if isinstance(subject, Person):
            kind = subject_kind_for(PrincipalKind(subject.kind)).value
            subject_id = estate.principals[subject.label]
        else:
            kind = "group"
            subject_id = estate.groups[subject]
        await services.entitlements.create_entitlement(
            admin,
            EntitlementCreate(
                subject=EntitlementSubject.model_validate({"kind": kind, "id": subject_id}),
                resource=EntitlementResource.model_validate(
                    {"kind": resource_kind, "id": resource_id, "scopeId": scope_id}
                ),
                cost_center_id=estate.cost_centers[cost_center] if cost_center else None,
                enforcement=enforcement,
                notes=notes,
            ),
        )

    docs_publication = await services.mcp_publishing.create(
        admin,
        McpPublicationCreate(
            gateway_id=gateway.id,
            mcp_endpoint_id=estate.mcp_endpoints["Contoso Docs Search"],
        ),
    )
    await _publish_mcp(services, admin, docs_publication.id, docs_publication.display_name)
    published_docs = await services.mcp_publishing.get_publication(admin, docs_publication.id)
    estate.publications[published_docs.display_name] = published_docs.id
    estate.mcp_servers[published_docs.api_name] = published_docs.mcp_server_id
    estate.docs_mcp_api = published_docs.api_name
    await services.gateways.update_mcp_server_catalog(
        admin,
        published_docs.mcp_server_id,
        CatalogEntryUpdate.model_validate(
            {
                "visibility": "catalog",
                "summary": (
                    "Search Contoso product and policy documentation through a governed MCP "
                    "endpoint."
                ),
            }
        ),
    )

    gpt4o = estate.model_apis["GPT-4o"]
    gpt4o_mini = estate.model_apis["GPT-4o mini"]
    embeddings = estate.model_apis["Text embeddings (large)"]
    phi4 = estate.model_apis["Phi-4"]
    docs_mcp = estate.mcp_servers["docs-search-mcp"]
    published_docs_mcp = estate.mcp_servers[published_docs.api_name]
    orders_mcp = estate.mcp_servers["orders-mcp"]
    service_desk_mcp = estate.mcp_servers["service-desk-mcp"]

    # Per-person defaults for grants that set no limits of their own, and monthly quotas each
    # cost center's grants share on a model.
    def model(resource_id: str) -> EntitlementResource:
        return EntitlementResource.model_validate({"kind": "modelApi", "id": resource_id})

    def pool_model(resource_id: str) -> EntitlementResource:
        return EntitlementResource.model_validate(
            {"kind": "poolModel", "id": resource_id, "scopeId": claude_pool.id}
        )

    opus = claude_models["Claude Opus 4.5"]
    sonnet = claude_models["Claude Sonnet 4.5"]
    cost_center_limits: dict[str, list[CostCenterLimit]] = {
        "CI-204": [
            CostCenterLimit(
                resource=model(gpt4o),
                person=PersonLimits(tokens_per_minute=20_000),
                pool=PooledQuota(monthly_tokens=40_000_000),
            ),
            # One quota across every region the pool sends Claude Sonnet calls to.
            CostCenterLimit(
                resource=pool_model(sonnet),
                person=PersonLimits(tokens_per_minute=20_000),
                pool=PooledQuota(monthly_tokens=20_000_000),
            ),
            CostCenterLimit(
                resource=EntitlementResource.model_validate(
                    {"kind": "mcpServer", "id": published_docs_mcp}
                ),
                person=PersonLimits(calls_per_minute=60),
                pool=PooledQuota(monthly_calls=60_000),
            ),
        ],
        "CS-110": [
            CostCenterLimit(
                resource=model(gpt4o_mini), pool=PooledQuota(monthly_tokens=60_000_000)
            ),
        ],
        "FIN-310": [
            CostCenterLimit(
                resource=model(phi4),
                person=PersonLimits(
                    tokens_per_minute=15_000, token_quota=6_000_000, token_quota_period="Monthly"
                ),
                pool=PooledQuota(monthly_tokens=12_000_000),
            ),
        ],
        "CLM-520": [
            CostCenterLimit(resource=model(phi4), pool=PooledQuota(monthly_calls=200_000)),
        ],
    }
    for code, limits in cost_center_limits.items():
        await services.cost_centers.set_limits(
            admin, estate.cost_centers[code], CostCenterLimitsUpdate(limits=limits)
        )

    await grant(
        SUPPORT_COPILOT,
        "modelApi",
        gpt4o_mini,
        EntitlementEnforcement(tokens=_tokens(per_minute=80_000)),
        "Production support assistant.",
    )
    await grant(
        CLAIMS_TRIAGE,
        "modelApi",
        gpt4o,
        EntitlementEnforcement(
            tokens=_tokens(per_minute=20_000, quota=5_000_000, period="Monthly")
        ),
    )
    await grant(CLAIMS_TRIAGE, "modelApi", phi4)
    await grant(ALEX, "modelApi", gpt4o)
    await grant(
        DOCS_INDEXER,
        "modelApi",
        embeddings,
        EntitlementEnforcement(tokens=_tokens(per_minute=150_000)),
    )
    # The Docs Search server's own grant, which its tools' model calls are made on and charged to.
    await grant(
        DOCS_SEARCH_SERVICE,
        "modelApi",
        gpt4o_mini,
        EntitlementEnforcement(tokens=_tokens(per_minute=60_000)),
        "The Docs Search MCP server's tools summarize what they find.",
        cost_center="general",
    )
    await grant(
        SALES_INSIGHTS,
        "mcpServer",
        orders_mcp,
        EntitlementEnforcement(requests=_calls(per_window=120, window_seconds=60)),
    )
    await grant(
        PORTAL_USER,
        "mcpServer",
        docs_mcp,
        EntitlementEnforcement(requests=_calls(per_window=60, window_seconds=60)),
    )
    await grant(
        PORTAL_USER,
        "mcpServer",
        published_docs_mcp,
        EntitlementEnforcement(requests=_calls(per_window=60, window_seconds=60)),
    )
    await grant(
        AI_MODEL_USERS,
        "mcpServer",
        published_docs_mcp,
        EntitlementEnforcement(requests=_calls(quota=10_000, period="Monthly")),
        cost_center="CI-204",
    )
    await grant(
        MARKET_RESEARCH_AGENT,
        "mcpServer",
        published_docs_mcp,
        EntitlementEnforcement(requests=_calls(per_window=120, window_seconds=60)),
    )
    await grant(
        "Data Science",
        "mcpServer",
        orders_mcp,
        EntitlementEnforcement(requests=_calls(quota=10_000, period="Monthly")),
    )
    await grant("Finance Analysts", "modelApi", embeddings)
    await grant("AI Platform Engineers", "mcpServer", service_desk_mcp)
    # Both groups' GPT-4o grants are charged to General, so they overlap: someone in both gets
    # the more generous one.
    await grant(
        AI_MODEL_USERS,
        "modelApi",
        gpt4o,
        EntitlementEnforcement(tokens=_tokens(per_minute=10_000)),
        "Per-member access for the broad AI model user population.",
        cost_center="general",
    )
    await grant(
        FINANCE_AI_PILOT,
        "modelApi",
        gpt4o,
        EntitlementEnforcement(tokens=_tokens(per_minute=5_000)),
        "Finance pilot users share a smaller per-member allowance.",
        cost_center="general",
    )
    # Megan's own experiments, charged to General rather than to her team.
    await grant(
        PORTAL_USER,
        "modelApi",
        gpt4o,
        EntitlementEnforcement(tokens=_tokens(per_minute=5_000)),
        "Personal experiments, charged to General.",
        cost_center="general",
    )
    await grant(
        MARKET_RESEARCH_AGENT,
        "modelApi",
        gpt4o,
        EntitlementEnforcement(tokens=_tokens(per_minute=30_000)),
        "Autonomous market research workflows.",
    )
    await grant(
        INVOICE_RECONCILIATION_AGENT,
        "modelApi",
        phi4,
        EntitlementEnforcement(tokens=_tokens(per_minute=15_000)),
        "Invoice extraction and reconciliation.",
    )
    await grant(
        SCHEDULING_ASSISTANT,
        "modelApi",
        phi4,
        EntitlementEnforcement(tokens=_tokens(per_minute=8_000)),
        "Delegated scheduling assistant prompts.",
    )

    async def request_access(
        person: Person,
        kind: str,
        resource_id: str,
        justification: str,
        *,
        scope_id: str | None = None,
    ) -> str:
        created = await services.entitlements.create_access_request(
            Actor(person.object_id, tenant_id),
            AccessRequestCreate(
                resource=EntitlementResource.model_validate(
                    {"kind": kind, "id": resource_id, "scopeId": scope_id}
                ),
                justification=justification,
            ),
        )
        return created.id

    approved = await request_access(
        PORTAL_USER,
        "modelApi",
        gpt4o,
        "Summarizing customer interviews for the churn analysis project.",
    )
    await services.entitlements.approve_access_request(
        admin,
        approved,
        AccessRequestApproval(
            note="Approved for the churn analysis project.",
            enforcement=EntitlementEnforcement(
                tokens=_tokens(per_minute=20_000, quota=2_000_000, period="Monthly")
            ),
        ),
    )
    denied = await request_access(
        PORTAL_USER,
        "mcpServer",
        service_desk_mcp,
        "Want to open tickets from my notebook.",
    )
    await services.entitlements.decide_access_request(
        admin,
        denied,
        state=AccessRequestState.DENIED,
        note="Limited to the AI Platform Engineers group. Use the service desk portal instead.",
    )
    # Megan builds against the development gateway first, then asks for production.
    prototype = await request_access(
        PORTAL_USER,
        "modelApi",
        estate.dev_model_apis["foundry-inference"],
        "Prototyping claims summarization with Phi-4 and Mistral Large.",
    )
    await services.entitlements.approve_access_request(
        admin,
        prototype,
        AccessRequestApproval(
            note="Approved for development. Ask for production when the prototype is ready.",
            enforcement=EntitlementEnforcement(tokens=_tokens(per_minute=10_000)),
        ),
    )
    await request_access(
        PORTAL_USER,
        "modelApi",
        estate.model_apis["foundry-inference"],
        "Taking the claims summarization prototype to production.",
    )
    await request_access(ISAIAH, "modelApi", gpt4o_mini, "Prototype for customer email triage.")
    await request_access(
        LIDIA, "modelApi", gpt4o, "Quarterly forecast narratives for the finance review."
    )
    await request_access(
        PRADEEP, "mcpServer", orders_mcp, "Agent that answers order-status questions in Teams."
    )
    _date_history(
        services.entitlement_repository,
        {approved: timedelta(days=52), denied: timedelta(days=33), prototype: timedelta(days=16)},
    )
    # Contoso began granting Claude five weeks ago. Megan's two grants share one key under CI-204,
    # the market research agent calls Opus with its own token, every agent builder may call
    # Sonnet with theirs, and the Docs Search server's tools draft longer answers with Sonnet.
    await grant(PORTAL_USER, "poolModel", sonnet, scope_id=claude_pool.id)
    await grant(
        PORTAL_USER,
        "poolModel",
        opus,
        EntitlementEnforcement(tokens=_tokens(per_minute=10_000)),
        "Long-form drafting for the churn analysis.",
        scope_id=claude_pool.id,
    )
    await grant(
        MARKET_RESEARCH_AGENT,
        "poolModel",
        opus,
        EntitlementEnforcement(tokens=_tokens(per_minute=30_000)),
        "Long-form competitor research reports.",
        scope_id=claude_pool.id,
    )
    await grant(AGENT_BUILDERS, "poolModel", sonnet, scope_id=claude_pool.id)
    await grant(
        DOCS_SEARCH_SERVICE,
        "poolModel",
        sonnet,
        EntitlementEnforcement(tokens=_tokens(per_minute=40_000)),
        "The Docs Search MCP server's answer tool drafts longer answers.",
        cost_center="general",
        scope_id=claude_pool.id,
    )
    claude_granted_at = utc_now() - timedelta(days=35)
    repository = services.entitlement_repository
    for entitlement_id, entitlement in list(repository.entitlements.items()):
        if entitlement.resource.kind == "poolModel":
            repository.entitlements[entitlement_id] = entitlement.model_copy(
                update={"created_at": claude_granted_at}
            )
    await request_access(
        ISAIAH,
        "poolModel",
        opus,
        "Comparing Claude and GPT-4o on long contract summaries.",
        scope_id=claude_pool.id,
    )

    # Apply the governed publications so their grants reach the gateway, then add one grant
    # afterwards so the console also shows a change still waiting to be applied. The Docs Search
    # server calls models as its own managed identity, so its model calls count for the people
    # they served once it's applied.
    await services.mcp_publishing.set_model_caller(
        admin,
        published_docs.id,
        McpModelCallerUpdate(principal_id=estate.principals[DOCS_SEARCH_SERVICE.label]),
    )
    await _publish_mcp(services, admin, published_docs.id, published_docs.display_name)
    for display_name in ("GPT-4o", "GPT-4o mini", "Phi-4"):
        await _publish(services, admin, estate.publications[display_name], display_name)
    await _publish_pool(services, admin, claude_pool.id, claude_pool.display_name)
    # Keys exist only where someone asked for one: the support copilot's, the key Megan's
    # notebook uses, and the one she calls Claude with, which covers both her Claude models. Her
    # General grant has none yet.
    for subject, resource_id, code in (
        (SUPPORT_COPILOT, gpt4o_mini, "CS-110"),
        (PORTAL_USER, gpt4o, "CI-204"),
        (PORTAL_USER, sonnet, "CI-204"),
    ):
        held = _held_grant(services, estate, subject, resource_id, code)
        await services.portal_access.create_key(admin, held.id, administrator=True)
    await grant(NESTOR, "modelApi", gpt4o)

    _require_synced(await services.gateways.sync_now(admin, gateway.id), gateway.name)
    return estate


# -- Gateway traffic ------------------------------------------------------------------------------

# Microsoft's public clients, which people sign in through to call the gateway. MOSAIC names them.
AZURE_CLI = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
VS_CODE = "aebc6443-996d-45c2-90f0-388ff96faa56"
# The most rollup cycles the seed waits for while the job reads back the quarter of logs.
MAX_ROLLUP_CYCLES = 30


def chat(model: str, prompt: int, completion: int, latency_ms: int) -> dict[str, Any]:
    """A model call's shape: the deployment it reaches, its usual tokens, and its usual latency."""

    return {
        "deployment": model,
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "latency_ms": latency_ms,
    }


async def traffic_streams(
    services: DemoServices, estate: Estate, tenant_id: str
) -> list[TrafficStream]:
    """A quarter of Contoso's gateway traffic, workload by workload.

    Calls to a governed publication carry MOSAIC's trace of the grant its policy matched: a direct
    grant beats a group grant, and a caller in several granted groups gets the most generous one.
    The rest is what a real estate also has: refusals, and calls to APIs MOSAIC only adopted, which
    it can count but not tie to anyone.
    """

    admin = Actor(ADMIN.object_id, tenant_id)
    today = utc_now().date()
    published = {
        label: await services.publishing.get_publication(admin, estate.publications[label])
        for label in ("GPT-4o", "GPT-4o mini", "Phi-4", "Text embeddings (large)")
    }
    embeddings = published["Text embeddings (large)"]
    gpt4o = estate.model_apis["GPT-4o"]
    gpt4o_mini = estate.model_apis["GPT-4o mini"]
    phi4 = estate.model_apis["Phi-4"]
    docs = estate.mcp_servers[estate.docs_mcp_api]
    api_names = {
        gpt4o: published["GPT-4o"].api_name,
        gpt4o_mini: published["GPT-4o mini"].api_name,
        phi4: published["Phi-4"].api_name,
        docs: estate.docs_mcp_api,
    }
    # Every Claude model is called through the pool's one API, and each call lands on whichever
    # region the pool picks for it. North Central US is short of Opus capacity, so it throttles
    # some calls and the pool sends them on. The pool's policy traces every attempt, which is what
    # the pool's health reads.
    claude = await services.pools.get_pool(admin, estate.pools["Anthropic Claude"])
    claude_models = {model.display_name: model.id for model in claude.models}
    opus = claude_models["Claude Opus 4.5"]
    sonnet = claude_models["Claude Sonnet 4.5"]
    keyed = {
        member.backend_name
        for model in (await services.pools.detail(admin, claude.id)).models
        for member in model.members
        if member.api_key
    }
    hosts = {
        endpoint.id: member_host(claude.api_shape, endpoint)
        for endpoint in await services.endpoints.list_endpoints(admin)
    }
    throttle_rates = {(opus, estate.foundry_north_central_endpoint_id): 0.08}
    members: dict[str, tuple[PoolMemberTraffic, ...]] = {}
    for pool_model in claude.models:
        api_names[pool_model.id] = claude.api_name
        members[pool_model.id] = tuple(
            PoolMemberTraffic(
                backend=member.backend_name,
                weight=member.weight,
                # An attempt through the model's backend pool names the backend pool. Only a
                # member reached with an API key is tried on a backend of its own.
                traced_backend=(
                    pool_model.backend_pool_name
                    if uses_backend_pools(claude.pool_type) and member.backend_name not in keyed
                    else member.backend_name
                ),
                host=hosts.get(member.model_endpoint_id, ""),
                path="/anthropic/v1/messages",
                throttle_rate=throttle_rates.get((pool_model.id, member.model_endpoint_id), 0.0),
            )
            for member in pool_model.active_members()
        )

    def granted(
        name: str,
        subject: Person,
        resource_id: str,
        *,
        caller: Person | None = None,
        client_app: str = "",
        key: bool = False,
        cost_center: str | None = None,
        **shape: Any,
    ) -> TrafficStream:
        """Calls under an applied grant, from the day it was made. ``caller`` is a group member.

        ``cost_center`` picks, by code, among a subject's grants on the same resource, as the
        ``x-mosaic-cost-center`` header does.
        """

        subject_id = estate.principals[subject.label]
        wanted = estate.cost_centers[cost_center] if cost_center else None
        entitlement = next(
            (
                item
                for item in services.entitlement_repository.entitlements.values()
                if item.enabled
                and item.subject.id == subject_id
                and item.resource.id == resource_id
                and (wanted is None or item.cost_center_id == wanted)
                and item.binding is not None
                and item.binding.attribution_key
            ),
            None,
        )
        if entitlement is None or entitlement.binding is None:
            raise SeedError(f"{subject.label} has no applied grant on {resource_id}")
        binding = entitlement.binding
        return TrafficStream(
            name=name,
            resource_id=GATEWAY_RESOURCE_ID,
            api=api_names[resource_id],
            grant=binding.attribution_key or "",
            member=caller.object_id if caller else "",
            # A key call names its subscription; only a validated token names a client app.
            client_app="" if key else client_app,
            subscription=(binding.apim_subscription_name or "") if key else "",
            since=entitlement.created_at.date(),
            **shape,
        )

    def refused(
        name: str, api: str, denial: str, *, caller: Person | None = None, **shape: Any
    ) -> TrafficStream:
        return TrafficStream(
            name=name,
            resource_id=GATEWAY_RESOURCE_ID,
            api=api,
            denial=denial,
            member=caller.object_id if caller else "",
            **shape,
        )

    def adopted(name: str, api: str, subscription: str, **shape: Any) -> TrafficStream:
        shape.setdefault("resource_id", GATEWAY_RESOURCE_ID)
        return TrafficStream(name=name, api=api, subscription=subscription, **shape)

    # The model calls the Docs Search server's tools make as its managed identity, passing on each
    # MCP call's reference, so MOSAIC counts them for the person who called. Most summarize with
    # GPT-4o mini, and one in four drafts a longer answer with Claude Sonnet, through the pool. See
    # ADR 0025.
    served = {
        "model_caller": DOCS_SEARCH_SERVICE.object_id,
        "model_calls": (
            (
                granted(
                    "docs-search-model",
                    DOCS_SEARCH_SERVICE,
                    gpt4o_mini,
                    client_app=DOCS_SEARCH_SERVICE.object_id,
                    per_day=0,
                    **chat("gpt-4o-mini", 1700, 260, 1100),
                ),
                3,
            ),
            (
                granted(
                    "docs-search-claude",
                    DOCS_SEARCH_SERVICE,
                    sonnet,
                    client_app=DOCS_SEARCH_SERVICE.object_id,
                    per_day=0,
                    members=members[sonnet],
                    pool_model=sonnet,
                    **chat("claude-sonnet-4-5", 2400, 700, 3600),
                ),
                1,
            ),
        ),
    }

    return [
        granted(
            "support-copilot",
            SUPPORT_COPILOT,
            gpt4o_mini,
            key=True,
            per_day=1300,
            rhythm="always",
            weekend=0.6,
            tokens_per_minute=80_000,
            burst=2,
            **chat("gpt-4o-mini", 950, 230, 700),
        ),
        granted(
            "claims-gpt-4o",
            CLAIMS_TRIAGE,
            gpt4o,
            client_app=CLAIMS_TRIAGE.object_id,
            per_day=70,
            rhythm="always",
            weekend=0.4,
            tokens_per_minute=20_000,
            burst=3,
            backend_throttle_rate=0.004,
            **chat("gpt-4o", 1900, 420, 2400),
        ),
        granted(
            "claims-phi-4",
            CLAIMS_TRIAGE,
            phi4,
            client_app=CLAIMS_TRIAGE.object_id,
            per_day=160,
            rhythm="always",
            weekend=0.4,
            tokens_per_minute=30_000,
            burst=4,
            **chat("Phi-4", 650, 110, 650),
        ),
        granted(
            "alex",
            ALEX,
            gpt4o,
            client_app=VS_CODE,
            per_day=24,
            tokens_per_minute=50_000,
            **chat("gpt-4o", 1400, 520, 2600),
        ),
        granted(
            "market-research",
            MARKET_RESEARCH_AGENT,
            gpt4o,
            client_app=MARKET_RESEARCH_AGENT.object_id,
            per_day=230,
            rhythm="always",
            weekend=0.85,
            tokens_per_minute=30_000,
            burst=10,
            backend_throttle_rate=0.004,
            **chat("gpt-4o", 2100, 480, 3200),
        ),
        # Megan's grant from the request approved seven weeks ago, charged to Customer Insights:
        # a notebook that uses her key. Her scripts sign in as her and name General with the
        # cost-center header, so they charge her own grant there.
        granted(
            "megan-key",
            MEGAN,
            gpt4o,
            key=True,
            cost_center="CI-204",
            per_day=30,
            tokens_per_minute=20_000,
            **chat("gpt-4o", 1100, 380, 2200),
        ),
        granted(
            "megan-token",
            MEGAN,
            gpt4o,
            client_app=AZURE_CLI,
            cost_center="general",
            per_day=14,
            tokens_per_minute=5_000,
            **chat("gpt-4o", 1300, 450, 2400),
        ),
        granted(
            "invoices",
            INVOICE_RECONCILIATION_AGENT,
            phi4,
            client_app=INVOICE_RECONCILIATION_AGENT.object_id,
            per_day=270,
            rhythm="always",
            weekend=0.3,
            tokens_per_minute=15_000,
            burst=8,
            **chat("Phi-4", 880, 160, 700),
        ),
        granted(
            "scheduling",
            SCHEDULING_ASSISTANT,
            phi4,
            client_app=SCHEDULING_ASSISTANT_AGENT.object_id,
            per_day=80,
            tokens_per_minute=8_000,
            **chat("Phi-4", 520, 90, 500),
        ),
        # AI Model Users' members call under the group's grant, unless they have their own. Lidia
        # is in Finance AI Pilot too: naming no cost center, she gets the more generous grant.
        granted(
            "isaiah",
            AI_MODEL_USERS,
            gpt4o,
            caller=ISAIAH,
            client_app=VS_CODE,
            per_day=28,
            tokens_per_minute=10_000,
            **chat("gpt-4o", 1200, 400, 2400),
        ),
        granted(
            "lidia",
            AI_MODEL_USERS,
            gpt4o,
            caller=LIDIA,
            client_app=AZURE_CLI,
            per_day=17,
            tokens_per_minute=10_000,
            **chat("gpt-4o", 1600, 500, 2600),
        ),
        granted(
            "diego",
            FINANCE_AI_PILOT,
            gpt4o,
            caller=DIEGO,
            client_app=AZURE_CLI,
            per_day=9,
            tokens_per_minute=5_000,
            burst=2,
            **chat("gpt-4o", 1500, 450, 2500),
        ),
        granted(
            "megan-docs",
            MEGAN,
            docs,
            client_app=VS_CODE,
            per_day=26,
            latency_ms=450,
            calls_per_minute=60,
            burst=3,
            model_call_share=0.6,
            **served,
        ),
        granted(
            "market-research-docs",
            MARKET_RESEARCH_AGENT,
            docs,
            client_app=MARKET_RESEARCH_AGENT.object_id,
            per_day=140,
            rhythm="always",
            weekend=0.85,
            latency_ms=380,
            calls_per_minute=120,
            burst=6,
        ),
        granted(
            "isaiah-docs",
            AI_MODEL_USERS,
            docs,
            caller=ISAIAH,
            client_app=VS_CODE,
            per_day=17,
            latency_ms=450,
            model_call_share=0.5,
            **served,
        ),
        granted(
            "lidia-docs",
            AI_MODEL_USERS,
            docs,
            caller=LIDIA,
            client_app=VS_CODE,
            per_day=8,
            latency_ms=450,
            model_call_share=0.7,
            **served,
        ),
        # The Docs Search server's own model use: refreshing its summaries each night.
        granted(
            "docs-search-own",
            DOCS_SEARCH_SERVICE,
            gpt4o_mini,
            client_app=DOCS_SEARCH_SERVICE.object_id,
            per_day=12,
            rhythm="nightly",
            weekend=1.0,
            **chat("gpt-4o-mini", 2400, 380, 1400),
        ),
        # Claude, through the pool. Megan's one key covers both her Claude models, and the agent
        # builders' grant serves each agent in the group with its own token.
        granted(
            "megan-claude-sonnet",
            MEGAN,
            sonnet,
            key=True,
            cost_center="CI-204",
            per_day=22,
            tokens_per_minute=20_000,
            members=members[sonnet],
            pool_model=sonnet,
            **chat("claude-sonnet-4-5", 1500, 520, 2800),
        ),
        granted(
            "megan-claude-opus",
            MEGAN,
            opus,
            key=True,
            cost_center="CI-204",
            per_day=8,
            tokens_per_minute=10_000,
            members=members[opus],
            pool_model=opus,
            **chat("claude-opus-4-5", 2600, 900, 5200),
        ),
        granted(
            "market-research-claude-opus",
            MARKET_RESEARCH_AGENT,
            opus,
            client_app=MARKET_RESEARCH_AGENT.object_id,
            per_day=90,
            rhythm="always",
            weekend=0.5,
            tokens_per_minute=30_000,
            burst=4,
            members=members[opus],
            pool_model=opus,
            **chat("claude-opus-4-5", 3200, 1100, 6100),
        ),
        granted(
            "invoices-claude-sonnet",
            AGENT_BUILDERS,
            sonnet,
            caller=INVOICE_RECONCILIATION_AGENT,
            client_app=INVOICE_RECONCILIATION_AGENT.object_id,
            per_day=40,
            rhythm="always",
            weekend=0.3,
            burst=2,
            members=members[sonnet],
            pool_model=sonnet,
            **chat("claude-sonnet-4-5", 1800, 360, 2600),
        ),
        refused(
            "johanna-claude",
            claude.api_name,
            "no-grant",
            caller=JOHANNA,
            client_app=VS_CODE,
            per_day=2,
            since=today - timedelta(days=12),
        ),
        # Nestor's grant isn't applied yet, so the gateway still refuses him.
        refused(
            "nestor",
            api_names[gpt4o],
            "no-grant",
            caller=NESTOR,
            client_app=AZURE_CLI,
            per_day=5,
            since=today - timedelta(days=1),
        ),
        refused(
            "patti",
            api_names[gpt4o],
            "no-grant",
            caller=PATTI,
            client_app=VS_CODE,
            per_day=2,
            since=today - timedelta(days=20),
        ),
        refused(
            "mini-tokens",
            api_names[gpt4o_mini],
            "tokens-off",
            per_day=3,
            since=today - timedelta(days=9),
        ),
        refused(
            "mini-unknown-key",
            api_names[gpt4o_mini],
            "key-unknown",
            per_day=4,
            rhythm="always",
            weekend=1.0,
            since=today - timedelta(days=30),
        ),
        refused(
            "no-credential",
            "azure-openai",
            "unauthenticated",
            per_day=6,
            rhythm="always",
            weekend=1.0,
        ),
        refused("bad-token", api_names[phi4], "token-invalid", per_day=3),
        adopted(
            "claims-openai",
            "azure-openai",
            "claims-triage",
            per_day=120,
            rhythm="always",
            weekend=0.4,
            **chat("gpt-4o-mini", 700, 150, 800),
        ),
        adopted(
            "support-openai",
            "azure-openai",
            "support-copilot-prod",
            per_day=30,
            rhythm="always",
            weekend=0.6,
            until=today - timedelta(days=20),
            **chat("gpt-4o", 1200, 300, 2300),
        ),
        adopted(
            "support-mistral",
            "foundry-inference",
            "support-copilot-prod",
            per_day=18,
            rhythm="always",
            weekend=0.6,
            **chat("Mistral-Large-2411", 1500, 350, 1800),
        ),
        adopted(
            "partner-orders",
            "orders-mcp",
            "fulfilment-partner",
            per_day=70,
            rhythm="always",
            weekend=0.5,
            latency_ms=300,
        ),
        # Text embeddings was published without governed access, so its calls trace no grant.
        adopted(
            "docs-indexer",
            embeddings.api_name,
            embeddings.subscription_name,
            per_day=300,
            rhythm="nightly",
            weekend=1.0,
            **chat("text-embedding-3-large", 2600, 0, 450),
        ),
        adopted(
            "dev-claims",
            "azure-openai",
            "claims-triage",
            resource_id=DEV_GATEWAY_RESOURCE_ID,
            per_day=40,
            **chat("gpt-4o-mini", 700, 150, 900),
        ),
        adopted(
            "dev-foundry",
            "foundry-inference",
            "support-copilot-prod",
            resource_id=DEV_GATEWAY_RESOURCE_ID,
            per_day=22,
            **chat("Phi-4", 600, 120, 750),
        ),
    ]


async def seed_usage(services: DemoServices, estate: Estate, tenant_id: str) -> None:
    """Turn on telemetry for the managed gateway, then roll up a quarter of both gateways' logs."""

    await services.telemetry.enable(Actor(ADMIN.object_id, tenant_id), estate.gateway_id)
    services.logs.streams = await traffic_streams(services, estate, tenant_id)
    for _ in range(MAX_ROLLUP_CYCLES):
        states = await services.usage_rollups.run_cycle()
        failed = next((state for state in states if state.last_error), None)
        if failed is not None:
            raise SeedError(f"Rolling up {failed.gateway_id} failed: {failed.last_error}")
        if not any(state.backfill_status == "running" for state in states):
            return
    raise SeedError(f"Rolling up the demo's usage took more than {MAX_ROLLUP_CYCLES} cycles")


def _budget_amount(spent: float, used: float) -> float:
    """The amount, to two significant figures, of which ``spent`` is the share ``used``."""

    exact = spent / used
    return round(exact, 1 - math.floor(math.log10(exact)))


async def seed_budgets(services: DemoServices, estate: Estate, tenant_id: str) -> None:
    """Turn budget email on, then set each budget from this month's spend so far."""

    admin = Actor(ADMIN.object_id, tenant_id)
    await services.budgets.save_email_settings(
        admin,
        EmailSettingsUpdate(enabled=True, endpoint=DEMO_EMAIL_ENDPOINT, sender=DEMO_EMAIL_SENDER),
    )
    await services.budgets.send_test_email(admin, EmailTest(to=_directory_detail(ADMIN) or ""))
    spend = await services.analytics.budget_spend(
        tenant_id,
        [estate.cost_centers[item.code] for item in BUDGETS if item.code],
        organization=True,
    )
    if spend is None:
        raise SeedError("The demo's usage has no prices, so no budget can be judged")
    for item in BUDGETS:
        cost_center_id = estate.cost_centers[item.code] if item.code else None
        figures = (
            spend.organization if cost_center_id is None else spend.cost_centers.get(cost_center_id)
        )
        spent = figures.month_to_date if figures is not None else None
        if not spent:
            # Only in the first minutes of a UTC month, before the demo's traffic adds up.
            print(f"No spend yet this month for {item.code or 'the organization'}", flush=True)
            continue
        # A budget past its limit crossed 80% first, so its 80% email went before the block.
        steps = [0.85, item.used] if item.used >= 1 else [item.used]
        for used in steps:
            await services.budgets.set_budget(
                admin,
                cost_center_id,
                BudgetUpdate(
                    amount=_budget_amount(spent, used),
                    recipients=item.recipients,
                    notify_owners=cost_center_id is not None,
                    action=item.action,
                ),
            )
    checked = await services.budgets.run_check()
    if checked is None:
        raise SeedError("Another budget check held the lease")
    overview = await services.budgets.overview(admin)
    unjudged = [view.id for view in overview.cost_centers if view.status.error]
    if unjudged:
        raise SeedError(f"Budgets {unjudged} could not be judged")
    unenforced = [
        gateway.name
        for view in overview.cost_centers
        if view.status.blocked
        for gateway in view.status.gateways
        if not gateway.enforcing
    ]
    if unenforced:
        raise SeedError(f"Gateways {unenforced} were not told about the blocked cost centers")


def build_settings(cors_origins: list[str]) -> Settings:
    return Settings(
        _env_file=None,
        environment=Environment.LOCAL,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT_ID,
        api_client_id=None,
        model_runtime_client_id=MODEL_RUNTIME_CLIENT_ID,
        model_client_id=MODEL_CLIENT_ID,
        managed_identity_principal_id=MOSAIC_PRINCIPAL_ID,
        entra_group_claims=True,
        applicationinsights_connection_string=None,
        apim_subscription_id=None,
        apim_resource_group=None,
        apim_service_name=None,
        cors_origins=cors_origins,
        log_level="WARNING",
        # Usage comes from rollups of the demo's gateway logs. The demo seeds them itself and
        # runs no rollup loop, so every capture shows the same figures.
        usage_source=UsageSourceMode.ROLLUPS,
        usage_rollup_enabled=False,
        # The demo judges budgets itself, with the gateway and email doubles.
        budgets_enabled=False,
    )


def origins_for(port: int) -> list[str]:
    return [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]


def build_demo_app(console_port: int, portal_port: int) -> FastAPI:
    portal_origins = origins_for(portal_port)
    app = create_app(build_settings([*origins_for(console_port), *portal_origins]))
    production_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def demo_lifespan(demo_app: FastAPI) -> AsyncIterator[None]:
        async with production_lifespan(demo_app):
            services = install_demo_services(demo_app, portal_origins)
            try:
                estate = await seed_estate(services, TENANT_ID)
                await seed_usage(services, estate, TENANT_ID)
                await seed_budgets(services, estate, TENANT_ID)
                demo_app.state.demo_estate = estate
                print(
                    "MOSAIC demo estate ready: "
                    f"{len(estate.principals)} principals, {len(estate.groups)} groups, "
                    f"{len(estate.publications)} publications, "
                    f"{len(estate.pools)} model pools, "
                    f"{len(estate.model_apis)} model APIs, {len(estate.mcp_servers)} MCP servers, "
                    f"{len(services.logs.streams)} traffic streams",
                    flush=True,
                )
                yield
            finally:
                await services.aclose()

    app.router.lifespan_context = demo_lifespan
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_API_PORT)
    parser.add_argument("--console-port", type=int, default=DEFAULT_CONSOLE_PORT)
    parser.add_argument("--portal-port", type=int, default=DEFAULT_PORTAL_PORT)
    args = parser.parse_args(argv)
    app = build_demo_app(args.console_port, args.portal_port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
