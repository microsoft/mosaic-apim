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
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, cast

import httpx
import uvicorn
from azure.core.credentials_async import AsyncTokenCredential
from fastapi import FastAPI, Request
from mosaic_api.auth import AuthContext
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    AccessRequestApproval,
    AccessRequestCreate,
    AccessRequestState,
    CatalogEntryUpdate,
    DirectoryObject,
    EntitlementCreate,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementSubject,
    GatewayCreate,
    GatewayUpdate,
    GroupCreate,
    ImportRequest,
    McpEndpointCreate,
    McpPublicationCreate,
    ModelAccessSettings,
    ModelEndpointCreate,
    PrincipalCreate,
    PrincipalKind,
    PublicationCreate,
    PublishRunStatus,
    RequestEnforcement,
    TokenEnforcement,
    mcp_server_id,
    model_api_id,
    subject_kind_for,
    utc_now,
)
from mosaic_api.environments import EnvironmentColor, EnvironmentCreate
from mosaic_api.integrations.aoai import CognitiveServicesClient
from mosaic_api.integrations.aoai.client import SubscriptionScanner
from mosaic_api.integrations.apim import ApimClient, ApimWriter, ArmClient
from mosaic_api.integrations.apim.credentials import ApimCredentialClient
from mosaic_api.integrations.graph.fake import FakeDirectoryLookup
from mosaic_api.main import create_app
from mosaic_api.repositories import InMemoryEntitlementRepository
from mosaic_api.services import (
    DirectoryService,
    EntitlementService,
    EnvironmentService,
    GatewayService,
    McpEndpointService,
    ModelEndpointService,
    PublishingService,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.mcp_endpoints import build_mcp_client_factory
from mosaic_api.services.mcp_publishing import McpPublishingService
from mosaic_api.services.portal_access import PortalAccessService

from scripts.screenshots.demo_fakes import (
    AI_RESOURCE_ID,
    DEV_GATEWAY_RESOURCE_ID,
    FOUNDRY_RESOURCE_ID,
    GATEWAY_RESOURCE_ID,
    PARTNER_GATEWAY_RESOURCE_ID,
    DemoApim,
    FakeCredential,
    build_cognitive_accounts,
    build_mcp_servers,
    cognitive_handler,
    gateway_handler,
    mcp_handler,
)

TENANT_ID = "5f2d7c1e-8a3b-4c9d-b0e1-f2a3b4c5d6e7"
MOSAIC_PRINCIPAL_ID = "6e3f8d2a-9b4c-4d0e-a1f2-a3b4c5d6e7f8"
MODEL_RUNTIME_CLIENT_ID = "7f4a9e3b-0c5d-4e1f-b2a3-b4c5d6e7f809"
MODEL_CLIENT_ID = "8a5b0f4c-1d6e-4f2a-83b4-c5d6e7f8091a"
SUBSCRIPTION_COUNTER = "@(context.Subscription.Id)"

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
    clients: list[httpx.AsyncClient] = field(default_factory=list)

    async def aclose(self) -> None:
        await self.gateways.aclose()
        await self.endpoints.aclose()
        await self.publishing.aclose()
        await self.mcp_publishing.aclose()
        await self.mcp_endpoints.aclose()
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
    gateway_arm, gateway_http = _arm(gateway_handler(apim, dev_apim))
    ai_arm, ai_http = _arm(cognitive_handler(build_cognitive_accounts()))
    mcp_http = httpx.AsyncClient(transport=httpx.MockTransport(mcp_handler(build_mcp_servers())))
    lookup = build_directory_lookup()
    directory = DirectoryService(
        state.repository,
        gateway_repository=state.gateway_repository,
        entitlement_repository=state.entitlement_repository,
        directory_lookup=lookup,
        group_claims_enabled=settings.entra_group_claims,
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
    state.portal_access_service = PortalAccessService(
        state.entitlement_service,
        repository=state.entitlement_repository,
        directory_repository=state.repository,
        gateway_repository=state.gateway_repository,
        credential_factory=lambda resource: ApimCredentialClient(gateway_arm, resource),
        model_runtime_client_id=settings.model_runtime_client_id,
        model_client_id=settings.model_client_id,
    )
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
        clients=[gateway_http, ai_http, mcp_http],
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
    publications: dict[str, str] = field(default_factory=dict)
    model_apis: dict[str, str] = field(default_factory=dict)
    dev_model_apis: dict[str, str] = field(default_factory=dict)
    mcp_endpoints: dict[str, str] = field(default_factory=dict)
    mcp_servers: dict[str, str] = field(default_factory=dict)
    principals: dict[str, str] = field(default_factory=dict)
    groups: dict[str, str] = field(default_factory=dict)


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


# How long ago the estate's grants were made, other than those that came from a request.
GRANT_AGE = timedelta(days=120)


def _date_history(
    repository: InMemoryEntitlementRepository, decisions: dict[str, timedelta]
) -> None:
    """Move the grants and decisions seeded so far into the past.

    MOSAIC stamps each record with the time it's written, and the usage report simulates a grant's
    traffic only from the day it was made, so an estate seeded a moment ago would chart a month of
    nothing and then one busy day. ``decisions`` says how long ago each decided request was
    decided; the grant it produced dates from then, and every other grant from ``GRANT_AGE`` ago.
    Pending requests keep today's date.
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


async def seed_estate(services: DemoServices, tenant_id: str) -> Estate:
    """Build the Contoso estate through MOSAIC's own services, in the order an operator would."""

    admin = Actor(ADMIN.object_id, tenant_id)
    estate = Estate()

    for person in PEOPLE:
        principal = await services.directory.create_principal(
            admin,
            PrincipalCreate.model_validate(
                {"object_id": person.object_id, "kind": person.kind, "label": person.label}
            ),
        )
        estate.principals[person.label] = principal.id
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
    for endpoint in (aoai, foundry):
        _require_synced(await services.endpoints.sync_now(admin, endpoint.id), endpoint.name)

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

    async def grant(
        subject: Person | str,
        resource_kind: str,
        resource_id: str,
        enforcement: EntitlementEnforcement | None = None,
        notes: str | None = None,
    ) -> None:
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
                    {"kind": resource_kind, "id": resource_id}
                ),
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
    await grant(
        AI_MODEL_USERS,
        "modelApi",
        gpt4o,
        EntitlementEnforcement(tokens=_tokens(per_minute=10_000)),
        "Per-member access for the broad AI model user population.",
    )
    await grant(
        FINANCE_AI_PILOT,
        "modelApi",
        gpt4o,
        EntitlementEnforcement(tokens=_tokens(per_minute=5_000)),
        "Finance pilot users share a smaller per-member allowance.",
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
        person: Person, kind: str, resource_id: str, justification: str
    ) -> str:
        created = await services.entitlements.create_access_request(
            Actor(person.object_id, tenant_id),
            AccessRequestCreate(
                resource=EntitlementResource.model_validate({"kind": kind, "id": resource_id}),
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

    # Apply the governed publications so their grants reach the gateway, then add one grant
    # afterwards so the console also shows a change still waiting to be applied.
    await _publish_mcp(services, admin, published_docs.id, published_docs.display_name)
    for display_name in ("GPT-4o", "GPT-4o mini", "Phi-4"):
        await _publish(services, admin, estate.publications[display_name], display_name)
    await grant(NESTOR, "modelApi", gpt4o)

    _require_synced(await services.gateways.sync_now(admin, gateway.id), gateway.name)
    return estate


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
                demo_app.state.demo_estate = estate
                print(
                    "MOSAIC demo estate ready: "
                    f"{len(estate.principals)} principals, {len(estate.groups)} groups, "
                    f"{len(estate.publications)} publications, "
                    f"{len(estate.model_apis)} model APIs, {len(estate.mcp_servers)} MCP servers",
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
