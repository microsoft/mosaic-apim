import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

APIM_API_VERSION = "2024-05-01"
# MCP servers are only visible on a preview contract. It is deliberately not the version the rest
# of the inventory uses: a preview API that changes or disappears must degrade MCP discovery alone,
# never the gateway sync that administrators depend on.
APIM_MCP_API_VERSION = "2025-09-01-preview"
AUTHORIZATION_API_VERSION = "2022-04-01"
SUBSCRIPTIONS_API_VERSION = "2022-12-01"
COGNITIVE_SERVICES_API_VERSION = "2024-10-01"
APIM_PROVIDER_NAMESPACE = "Microsoft.ApiManagement"
APIM_RESOURCE_TYPE = "service"
COGNITIVE_SERVICES_PROVIDER_NAMESPACE = "Microsoft.CognitiveServices"
COGNITIVE_SERVICES_RESOURCE_TYPE = "accounts"
APIM_READER_ROLE_NAME = "API Management Service Reader Role"
APIM_READER_ROLE_ID = "71522526-b88f-4d52-b57f-d31fc3546d0d"
APIM_CONTRIBUTOR_ROLE_NAME = "API Management Service Contributor"
APIM_CONTRIBUTOR_ROLE_ID = "312a565d-c81f-4fd8-895a-4e21e48d571c"

# MOSAIC enumerates model deployments with the built-in Reader role. Every "Cognitive Services *"
# and "Foundry *" role that grants control-plane deployment read also carries data-plane
# ``dataActions`` (usually ``Microsoft.CognitiveServices/*``, i.e. full inference) and frequently
# ``accounts/listkeys/action``. Reader is the only built-in that grants
# ``Microsoft.CognitiveServices/accounts/deployments/read`` with no data actions, no key access, and
# no write. It also grants ``Microsoft.Authorization/roleAssignments/read``, which is what verifying
# a gateway's runtime access requires, so one assignment covers both jobs.
READER_ROLE_NAME = "Reader"
READER_ROLE_ID = "acdd72a7-3385-48ef-bd42-f606fba81ae7"

# Runtime roles are reported for the gateway's managed identity and never granted by MOSAIC. They
# are keyed by role definition ID rather than name: the Foundry roles were renamed in 2026
# ("Azure AI User" became "Foundry User") and Microsoft advises binding to the GUID while the
# rename rolls out. The GUIDs are unchanged by the rename.
AZURE_OPENAI_USER_ROLE_NAME = "Cognitive Services OpenAI User"
AZURE_OPENAI_USER_ROLE_ID = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
COGNITIVE_SERVICES_USER_ROLE_NAME = "Cognitive Services User"
COGNITIVE_SERVICES_USER_ROLE_ID = "a97b65f3-24c7-4388-baec-2e87135dc908"
FOUNDRY_USER_ROLE_NAME = "Foundry User"
FOUNDRY_USER_ROLE_ID = "53ca6127-db72-4b80-b1b0-d745d6d5456d"

# MCP protocol revision MOSAIC offers when it connects to a registered MCP server.
#
# The current published revision, 2026-07-28, is a *stateless* protocol: it removed the
# ``initialize`` handshake, the session header, and the GET stream outright. Everything from
# 2025-11-25 back is the handshake era, and the handshake era is what API Management speaks.
# MOSAIC implements that era alone, offers the newest revision of it, and accepts a server's
# counter-offer from the set below. A server answering with anything else -- including a modern
# stateless server -- is recorded as an unsupported protocol, which is a capability and not a
# failure, exactly as an API Management service too old for MCP is.
MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_SUPPORTED_PROTOCOL_VERSIONS: frozenset[str] = frozenset(
    {"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"}
)
# The ``MCP-Protocol-Version`` header was introduced in 2025-06-18. A server that negotiated an
# earlier revision never defined it, so MOSAIC omits it rather than sending a header the server
# is entitled to reject with 400.
MCP_PROTOCOL_VERSION_HEADER_MINIMUM = "2025-06-18"
MCP_CLIENT_NAME = "mosaic"

_APIM_RESOURCE_ID_PATTERN = re.compile(
    r"^/subscriptions/(?P<subscription>[0-9a-fA-F-]{36})"
    r"/resourceGroups/(?P<resourceGroup>[^/]{1,90})"
    r"/providers/Microsoft\.ApiManagement/service"
    r"/(?P<serviceName>[^/]{1,50})$",
    re.IGNORECASE,
)

_COGNITIVE_SERVICES_RESOURCE_ID_PATTERN = re.compile(
    r"^/subscriptions/(?P<subscription>[0-9a-fA-F-]{36})"
    r"/resourceGroups/(?P<resourceGroup>[^/]{1,90})"
    r"/providers/Microsoft\.CognitiveServices/accounts"
    r"/(?P<accountName>[^/]{1,64})"
    r"(?:/projects/(?P<projectName>[^/]{1,64}))?$",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def deterministic_id(prefix: str, *parts: str) -> str:
    value = "|".join(part.casefold() for part in parts)
    return f"{prefix}_{uuid5(NAMESPACE_URL, value).hex}"


class ApimResourceId(BaseModel):
    model_config = ConfigDict(frozen=True)

    subscription_id: str
    resource_group: str
    service_name: str

    @classmethod
    def parse(cls, value: str) -> "ApimResourceId":
        candidate = value.strip().rstrip("/")
        if not candidate.startswith("/"):
            candidate = f"/{candidate}"
        match = _APIM_RESOURCE_ID_PATTERN.match(candidate)
        if not match:
            raise ValueError(
                "Expected an Azure API Management resource ID of the form /subscriptions/"
                "{subscriptionId}/resourceGroups/{resourceGroup}/providers/"
                "Microsoft.ApiManagement/service/{serviceName}"
            )
        return cls(
            subscription_id=match.group("subscription").lower(),
            resource_group=match.group("resourceGroup"),
            service_name=match.group("serviceName"),
        )

    @property
    def canonical(self) -> str:
        return (
            f"/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{self.resource_group}"
            f"/providers/{APIM_PROVIDER_NAMESPACE}/{APIM_RESOURCE_TYPE}/{self.service_name}"
        )

    @property
    def dedupe_key(self) -> str:
        return self.canonical.casefold()


class CognitiveServicesResourceId(BaseModel):
    """An Azure AI resource ID, optionally naming a Foundry project.

    Deployments are never children of a project: ``accounts/{account}/projects/{project}`` exposes
    only descriptive properties, and models are enumerated at the parent account. ``account_scope``
    therefore resolves upward, while ``canonical`` preserves whatever the administrator registered.
    """

    model_config = ConfigDict(frozen=True)

    subscription_id: str
    resource_group: str
    account_name: str
    project_name: str | None = None

    @classmethod
    def parse(cls, value: str) -> "CognitiveServicesResourceId":
        candidate = value.strip().rstrip("/")
        if not candidate.startswith("/"):
            candidate = f"/{candidate}"
        match = _COGNITIVE_SERVICES_RESOURCE_ID_PATTERN.match(candidate)
        if not match:
            raise ValueError(
                "Expected an Azure AI resource ID of the form /subscriptions/{subscriptionId}"
                "/resourceGroups/{resourceGroup}/providers/Microsoft.CognitiveServices/accounts"
                "/{accountName}, optionally followed by /projects/{projectName}"
            )
        return cls(
            subscription_id=match.group("subscription").lower(),
            resource_group=match.group("resourceGroup"),
            account_name=match.group("accountName"),
            project_name=match.group("projectName"),
        )

    @property
    def account_scope(self) -> str:
        """The account that owns the deployments, regardless of whether a project was registered."""

        return (
            f"/subscriptions/{self.subscription_id}"
            f"/resourceGroups/{self.resource_group}"
            f"/providers/{COGNITIVE_SERVICES_PROVIDER_NAMESPACE}"
            f"/{COGNITIVE_SERVICES_RESOURCE_TYPE}/{self.account_name}"
        )

    @property
    def canonical(self) -> str:
        if self.project_name:
            return f"{self.account_scope}/projects/{self.project_name}"
        return self.account_scope

    @property
    def dedupe_key(self) -> str:
        return self.canonical.casefold()


class MosaicModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
        use_enum_values=True,
    )


class Entity(MosaicModel):
    id: str
    tenant_id: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    etag: str | None = Field(default=None, exclude=True)


class PrincipalKind(StrEnum):
    USER = "user"
    SERVICE_PRINCIPAL = "servicePrincipal"
    MANAGED_IDENTITY = "managedIdentity"


class Principal(Entity):
    entity_type: Literal["principal"] = "principal"
    object_id: str
    kind: PrincipalKind
    label: str | None = None


class Group(Entity):
    entity_type: Literal["group"] = "group"
    name: str
    description: str | None = None


class GroupMembership(Entity):
    entity_type: Literal["groupMembership"] = "groupMembership"
    group_id: str
    principal_id: str


class GatewayProvider(StrEnum):
    APIM = "apim"


class ManagementMode(StrEnum):
    OBSERVE = "observe"
    MANAGE = "manage"


class GatewayStatus(StrEnum):
    PENDING = "pending"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    UNAUTHORIZED = "unauthorized"
    UNREACHABLE = "unreachable"


class CapabilitySupport(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class AccessEvaluation(StrEnum):
    EFFECTIVE_PERMISSIONS = "effectivePermissions"
    PROBE = "probe"
    NOT_EVALUATED = "notEvaluated"


class AccessRemediation(MosaicModel):
    role_name: str
    role_definition_id: str
    scope: str
    principal_id: str | None = None
    command: str
    custom_role_definition: dict[str, Any] | None = Field(
        default=None,
        description=(
            "A narrower custom role an operator may create instead of the built-in role. It is "
            "offered, never created: MOSAIC cannot define roles any more than it can assign them."
        ),
    )


class GatewayAccess(MosaicModel):
    can_read: bool = False
    can_write: bool = False
    evaluation: AccessEvaluation = AccessEvaluation.NOT_EVALUATED
    checked_at: datetime | None = None
    missing_actions: list[str] = Field(default_factory=list)
    remediation: AccessRemediation | None = None
    message: str | None = None


class AiBackendKind(StrEnum):
    """Which model provider an API or backend fronts.

    Lives here rather than in ``observed`` because adopted desired state records the classification
    that was true at import, and ``observed`` imports from this module rather than the reverse.
    """

    AZURE_OPENAI = "azureOpenAi"
    AZURE_AI_FOUNDRY = "azureAiFoundry"
    AZURE_AI_INFERENCE = "azureAiInference"
    OPEN_AI = "openAi"
    ANTHROPIC = "anthropic"
    GOOGLE_VERTEX = "googleVertex"
    AWS_BEDROCK = "awsBedrock"
    OTHER_LLM = "otherLlm"
    NONE = "none"


class GatewayCapabilities(MosaicModel):
    sku_name: str | None = None
    sku_capacity: int | None = None
    provisioning_state: str | None = None
    location: str | None = None
    gateway_url: AnyHttpUrl | None = None
    management_api_version: str = APIM_API_VERSION
    ai_gateway_policies: CapabilitySupport = CapabilitySupport.UNKNOWN
    mcp_servers: CapabilitySupport = CapabilitySupport.UNKNOWN
    principal_id: str | None = Field(
        default=None,
        description=(
            "Object ID of the gateway's managed identity. This is the principal that must hold a "
            "data-plane role on a model endpoint for the gateway to call it at runtime."
        ),
    )
    identity_observed: bool = Field(
        default=False,
        description=(
            "Whether MOSAIC has actually read this gateway's identity block. A missing "
            "``principal_id`` on a gateway that was never observed means 'not known yet', not "
            "'has no identity', and the two must not be reported the same way."
        ),
    )
    notes: list[str] = Field(default_factory=list)


class GatewayInventorySummary(MosaicModel):
    apis: int = 0
    ai_apis: int = 0
    mcp_servers: int = 0
    operations: int = 0
    products: int = 0
    subscriptions: int = 0
    users: int = 0
    groups: int = 0
    backends: int = 0
    named_values: int = 0
    policy_documents: int = 0
    policy_fragments: int = 0
    recognized_facets: int = 0
    unrecognized_facets: int = 0
    mosaic_managed_facets: int = 0


class Gateway(Entity):
    entity_type: Literal["gateway"] = "gateway"
    name: str
    provider: GatewayProvider = GatewayProvider.APIM
    azure_resource_id: str
    subscription_id: str
    resource_group: str
    service_name: str
    environment_label: str | None = None
    management_mode: ManagementMode = ManagementMode.OBSERVE
    status: GatewayStatus = GatewayStatus.PENDING
    access: GatewayAccess = Field(default_factory=GatewayAccess)
    capabilities: GatewayCapabilities = Field(default_factory=GatewayCapabilities)
    inventory: GatewayInventorySummary = Field(default_factory=GatewayInventorySummary)
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None


class GatewaySyncStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class GatewaySyncRun(Entity):
    entity_type: Literal["gatewaySyncRun"] = "gatewaySyncRun"
    gateway_id: str
    status: GatewaySyncStatus = GatewaySyncStatus.RUNNING
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = None
    counts: GatewayInventorySummary = Field(default_factory=GatewayInventorySummary)
    removed: int = 0
    errors: list[str] = Field(default_factory=list)
    actor_object_id: str | None = None


class ModelProvider(StrEnum):
    AZURE_OPENAI = "azureOpenAi"
    AZURE_AI_FOUNDRY = "azureAiFoundry"
    OPENAI_COMPATIBLE = "openAiCompatible"


class EndpointAuthMode(StrEnum):
    MANAGED_IDENTITY = "managedIdentity"
    API_KEY = "apiKey"


class ModelEndpointStatus(StrEnum):
    PENDING = "pending"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    UNAUTHORIZED = "unauthorized"
    UNREACHABLE = "unreachable"


class RuntimeAccessEvaluation(StrEnum):
    ROLE_ASSIGNMENTS = "roleAssignments"
    NO_GATEWAY_IDENTITY = "noGatewayIdentity"
    NOT_APPLICABLE = "notApplicable"
    NOT_EVALUATED = "notEvaluated"


class EndpointAccess(MosaicModel):
    """Whether MOSAIC's own identity can enumerate models on an endpoint."""

    can_read: bool = False
    evaluation: AccessEvaluation = AccessEvaluation.NOT_EVALUATED
    checked_at: datetime | None = None
    missing_actions: list[str] = Field(default_factory=list)
    remediation: AccessRemediation | None = None
    message: str | None = None


class GatewayRuntimeAccess(MosaicModel):
    """Whether one registered gateway's managed identity can call this endpoint at runtime.

    This is a different question from :class:`EndpointAccess`, asked of a different principal
    against a different plane. MOSAIC reads role assignments to answer it and never grants one.
    """

    gateway_id: str
    gateway_name: str
    apim_principal_id: str | None = None
    can_invoke: bool = False
    evaluation: RuntimeAccessEvaluation = RuntimeAccessEvaluation.NOT_EVALUATED
    checked_at: datetime | None = None
    required_role_name: str | None = None
    required_role_definition_id: str | None = None
    assignment_scope: str | None = None
    inherited: bool = False
    remediation: AccessRemediation | None = None
    message: str | None = None


class ModelEndpointCapabilities(MosaicModel):
    kind: str | None = None
    sku_name: str | None = None
    location: str | None = None
    provisioning_state: str | None = None
    public_network_access: str | None = None
    local_auth_disabled: bool | None = None
    management_api_version: str = COGNITIVE_SERVICES_API_VERSION
    notes: list[str] = Field(default_factory=list)


class ModelInventorySummary(MosaicModel):
    deployments: int = 0
    available_models: int = 0
    succeeded_deployments: int = 0
    deprecated_deployments: int = 0


class ModelEndpoint(Entity):
    """A registered provider endpoint MOSAIC reads models from.

    Azure endpoints are identified by resource ID and read with MOSAIC's managed identity.
    OpenAI-compatible endpoints are identified by URL and read with a key MOSAIC resolves from Key
    Vault at call time; only the secret URI is ever stored.
    """

    entity_type: Literal["modelEndpoint"] = "modelEndpoint"
    name: str
    provider: ModelProvider
    endpoint: AnyHttpUrl
    azure_resource_id: str | None = None
    subscription_id: str | None = None
    resource_group: str | None = None
    account_name: str | None = None
    project_name: str | None = None
    environment_label: str | None = None
    auth_mode: EndpointAuthMode = EndpointAuthMode.MANAGED_IDENTITY
    credential_reference_id: str | None = None
    status: ModelEndpointStatus = ModelEndpointStatus.PENDING
    access: EndpointAccess = Field(default_factory=EndpointAccess)
    runtime_access: list[GatewayRuntimeAccess] = Field(default_factory=list)
    capabilities: ModelEndpointCapabilities = Field(default_factory=ModelEndpointCapabilities)
    inventory: ModelInventorySummary = Field(default_factory=ModelInventorySummary)
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None


class ModelEndpointSyncRun(Entity):
    entity_type: Literal["modelEndpointSyncRun"] = "modelEndpointSyncRun"
    endpoint_id: str
    status: GatewaySyncStatus = GatewaySyncStatus.RUNNING
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = None
    counts: ModelInventorySummary = Field(default_factory=ModelInventorySummary)
    removed: int = 0
    errors: list[str] = Field(default_factory=list)
    actor_object_id: str | None = None


class CatalogModel(Entity):
    entity_type: Literal["catalogModel"] = "catalogModel"
    provider: str
    model_name: str
    model_version: str | None = None


class ModelDeployment(Entity):
    entity_type: Literal["modelDeployment"] = "modelDeployment"
    model_endpoint_id: str
    catalog_model_id: str | None = None
    deployment_name: str
    endpoint: AnyHttpUrl


class ImportSelection(StrEnum):
    """Whether MOSAIC recommended an import or the administrator chose it themselves."""

    DETECTED = "detected"
    MANUAL = "manual"


class McpTransportType(StrEnum):
    STREAMABLE = "streamable"
    SSE = "sse"
    UNKNOWN = "unknown"


class McpServerKind(StrEnum):
    REST_API_BACKED = "restApiBacked"
    PASSTHROUGH = "passthrough"


class McpServerRoute(MosaicModel):
    """One named URI template an API Management MCP server is reachable on.

    Named for what API Management calls it, not "endpoint": a streamable server declares a single
    ``message`` route and an SSE server declares ``sse`` and ``message``. The registered-endpoint
    entity is :class:`McpEndpoint`.
    """

    name: str
    uri_template: str


class McpTool(MosaicModel):
    name: str
    display_name: str
    description: str | None = None
    backing_api_name: str | None = None
    backing_operation_name: str | None = None


class CatalogVisibility(StrEnum):
    """Whether a governed resource is discoverable in the end-user portal.

    ``catalog`` means any portal user can see that it exists and request access; it does not grant
    use. ``private`` means only entitled users see it at all.
    """

    CATALOG = "catalog"
    PRIVATE = "private"


class ModelApi(Entity):
    """An API Management API an administrator adopted as a governed model endpoint.

    Adoption is a Cosmos write, never an Azure one. The record is desired state: it says MOSAIC
    governs this API, and it survives the sweep that rebuilds ``observed-state`` on every sync.
    """

    entity_type: Literal["modelApi"] = "modelApi"
    gateway_id: str
    api_name: str
    display_name: str
    path: str
    service_url: str | None = None
    protocols: list[str] = Field(default_factory=list)
    ai_kind: AiBackendKind = AiBackendKind.NONE
    ai_signals: list[str] = Field(default_factory=list)
    subscription_required: bool = True
    operation_count: int = 0
    product_names: list[str] = Field(default_factory=list)
    visibility: CatalogVisibility = CatalogVisibility.CATALOG
    summary: str | None = None
    selection: ImportSelection = ImportSelection.DETECTED
    imported_from_snapshot_id: str
    imported_at: datetime = Field(default_factory=utc_now)
    imported_by: str | None = None


class McpServer(Entity):
    """An API Management MCP server an administrator adopted."""

    entity_type: Literal["mcpServer"] = "mcpServer"
    gateway_id: str
    api_name: str
    display_name: str
    path: str
    service_url: str | None = None
    protocols: list[str] = Field(default_factory=list)
    kind: McpServerKind = McpServerKind.REST_API_BACKED
    transport_type: McpTransportType = McpTransportType.UNKNOWN
    endpoints: list[McpServerRoute] = Field(default_factory=list)
    tools: list[McpTool] = Field(default_factory=list)
    tool_count: int = 0
    subscription_required: bool = True
    product_names: list[str] = Field(default_factory=list)
    visibility: CatalogVisibility = CatalogVisibility.CATALOG
    summary: str | None = None
    selection: ImportSelection = ImportSelection.DETECTED
    imported_from_snapshot_id: str
    imported_at: datetime = Field(default_factory=utc_now)
    imported_by: str | None = None


class CatalogEntryUpdate(MosaicModel):
    """Administrator-authored catalog metadata, kept separate from what a sync discovers."""

    visibility: CatalogVisibility | None = None
    summary: str | None = None


def model_api_id(tenant_id: str, gateway_id: str, api_name: str) -> str:
    """Deterministic so re-importing an API updates the record instead of duplicating it."""

    return deterministic_id("modelApi", tenant_id, gateway_id, api_name)


def mcp_server_id(tenant_id: str, gateway_id: str, api_name: str) -> str:
    return deterministic_id("mcpServer", tenant_id, gateway_id, api_name)


class McpAuthMode(StrEnum):
    """How MOSAIC authenticates when it connects to a registered MCP server.

    Deliberately not :class:`EndpointAuthMode`. An MCP server may legitimately require no
    credential at all, and that must never become a valid way to register a model endpoint.
    """

    NONE = "none"
    API_KEY = "apiKey"
    MANAGED_IDENTITY = "managedIdentity"


class McpEndpointStatus(StrEnum):
    PENDING = "pending"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    UNAUTHORIZED = "unauthorized"
    UNREACHABLE = "unreachable"
    # The server answered, and the answer is that it speaks a protocol revision or a transport
    # MOSAIC does not. Neither is a fault an operator can clear by retrying, so both are held
    # apart from the failure states above.
    UNSUPPORTED_PROTOCOL = "unsupportedProtocol"
    UNSUPPORTED_TRANSPORT = "unsupportedTransport"


class McpDiscoveryEvaluation(StrEnum):
    HANDSHAKE = "handshake"
    AUTHORIZATION_REQUIRED = "authorizationRequired"
    NOT_EVALUATED = "notEvaluated"


class McpAuthChallenge(MosaicModel):
    """What a ``401`` asked for, parsed from ``WWW-Authenticate``.

    Recorded so that "this server wants credentials MOSAIC was not given" is never presented as
    "this server is unreachable".
    """

    scheme: str | None = None
    resource_metadata_url: str | None = None
    scope: str | None = None


class McpDiscoveryAccess(MosaicModel):
    """Whether MOSAIC can reach and read a registered MCP server.

    An MCP server has no control plane, so unlike :class:`EndpointAccess` this can only be
    answered by connecting. There is deliberately no second, gateway-runtime relationship here:
    API Management fronts an MCP server with a backend credential rather than a role assignment,
    and Entra app roles are not readable over ARM at all.
    """

    can_discover: bool = False
    evaluation: McpDiscoveryEvaluation = McpDiscoveryEvaluation.NOT_EVALUATED
    checked_at: datetime | None = None
    challenge: McpAuthChallenge | None = None
    message: str | None = None


class McpEndpointCapabilities(MosaicModel):
    protocol_version: str | None = None
    offered_protocol_version: str = MCP_PROTOCOL_VERSION
    transport_type: McpTransportType = McpTransportType.STREAMABLE
    server_name: str | None = None
    server_title: str | None = None
    server_version: str | None = None
    instructions: str | None = None
    supports_tools: CapabilitySupport = CapabilitySupport.UNKNOWN
    session_managed: bool = False
    notes: list[str] = Field(default_factory=list)


class McpInventorySummary(MosaicModel):
    """Counts only what the server actually stated.

    There is no "destructive tools" count on purpose. ``destructiveHint`` and ``openWorldHint``
    default to *true* when absent, so a count derived from the defaults would report tools as
    destructive that simply said nothing. ``unannotated_tools`` reports that silence directly.
    """

    tools: int = 0
    read_only_tools: int = 0
    unannotated_tools: int = 0


class McpEndpoint(Entity):
    """A registered MCP server MOSAIC reads tools from.

    The sibling of :class:`ModelEndpoint`: an administrator registers a server that exists
    somewhere, and MOSAIC connects to it as a read-only MCP client to record what it offers.
    MOSAIC never calls a tool, and creates nothing in Azure or API Management.
    """

    entity_type: Literal["mcpEndpoint"] = "mcpEndpoint"
    name: str
    endpoint: AnyHttpUrl
    environment_label: str | None = None
    auth_mode: McpAuthMode = McpAuthMode.NONE
    credential_reference_id: str | None = None
    resource_audience: str | None = Field(
        default=None,
        description=(
            "The Entra audience a managed-identity token is requested for. Required rather than "
            "inferred: a token is only ever attached when the operator named who it is for."
        ),
    )
    status: McpEndpointStatus = McpEndpointStatus.PENDING
    access: McpDiscoveryAccess = Field(default_factory=McpDiscoveryAccess)
    capabilities: McpEndpointCapabilities = Field(default_factory=McpEndpointCapabilities)
    inventory: McpInventorySummary = Field(default_factory=McpInventorySummary)
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None


class McpEndpointSyncRun(Entity):
    entity_type: Literal["mcpEndpointSyncRun"] = "mcpEndpointSyncRun"
    endpoint_id: str
    status: GatewaySyncStatus = GatewaySyncStatus.RUNNING
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = None
    counts: McpInventorySummary = Field(default_factory=McpInventorySummary)
    removed: int = 0
    errors: list[str] = Field(default_factory=list)
    actor_object_id: str | None = None


def canonical_mcp_url(value: str) -> str:
    """The canonical form of an MCP server URL, per the authorization spec's definition.

    Lowercase scheme and host, no fragment, and no trailing slash on a non-root path. Used both
    to key the record and as the RFC 8707 resource identifier, so the two can never disagree.
    """

    parts = urlsplit(value.strip())
    host = (parts.hostname or "").casefold()
    if not host:
        raise ValueError("An MCP server URL must include a host")
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.casefold(), netloc, path, parts.query, ""))


def mcp_endpoint_id(tenant_id: str, url: str) -> str:
    """Deterministic so re-registering the same server refreshes it instead of forking it."""

    return deterministic_id("mcpEndpoint", tenant_id, canonical_mcp_url(url))


QuotaPeriod = Literal["Hourly", "Daily", "Weekly", "Monthly", "Yearly"]


class TokenEnforcement(MosaicModel):
    counter_key_expression: str
    tokens_per_minute: int | None = Field(default=None, ge=1)
    token_quota: int | None = Field(default=None, ge=1)
    token_quota_period: QuotaPeriod | None = None
    estimate_prompt_tokens: bool = True

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if self.tokens_per_minute is None and self.token_quota is None:
            raise ValueError("At least one token rate or quota must be configured")
        if self.token_quota is not None and self.token_quota_period is None:
            raise ValueError("A token quota period is required when a quota is configured")
        if self.token_quota is None and self.token_quota_period is not None:
            raise ValueError("A token quota is required when a quota period is configured")
        return self


class RequestEnforcement(MosaicModel):
    """Call limits, which API Management enforces with different policies to token limits.

    ``calls`` over ``renewal_period_seconds`` is a short sliding window (``rate-limit-by-key``);
    ``call_quota`` over ``call_quota_period`` is a long accounting window (``quota-by-key``). They
    are independent, and a caller commonly has both.
    """

    counter_key_expression: str
    calls: int | None = Field(default=None, ge=1)
    renewal_period_seconds: int | None = Field(default=None, ge=1)
    call_quota: int | None = Field(default=None, ge=1)
    call_quota_period: QuotaPeriod | None = None

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        if self.calls is None and self.call_quota is None:
            raise ValueError("At least one call rate or quota must be configured")
        if (self.calls is None) != (self.renewal_period_seconds is None):
            raise ValueError("A call rate needs both a call count and a renewal period")
        if (self.call_quota is None) != (self.call_quota_period is None):
            raise ValueError("A call quota needs both a quota and a quota period")
        return self


class EntitlementEnforcement(MosaicModel):
    """What restricts a subject's use of a resource.

    Token limits keep the exact shape of :class:`TokenEnforcement` because the policy preview and
    the ``llm-token-limit`` renderer already speak it. An entitlement with no enforcement at all is
    a legitimate unrestricted grant, so this whole object is optional on the entitlement; when it
    is present it must actually restrict something.
    """

    tokens: TokenEnforcement | None = None
    requests: RequestEnforcement | None = None

    @model_validator(mode="after")
    def validate_present(self) -> Self:
        if self.tokens is None and self.requests is None:
            raise ValueError(
                "Enforcement must configure a token or request limit; omit it entirely for an "
                "unrestricted entitlement"
            )
        return self


class EntitlementSubjectKind(StrEnum):
    USER = "user"
    GROUP = "group"
    APPLICATION = "application"


class EntitlementResourceKind(StrEnum):
    MODEL_API = "modelApi"
    MCP_SERVER = "mcpServer"
    MODEL_DEPLOYMENT = "modelDeployment"
    PRODUCT = "product"


class EntitlementSubject(MosaicModel):
    """Who a grant is for.

    ``user`` and ``application`` name a :class:`Principal`; ``group`` names a :class:`Group`. The
    distinction between a user and an application is the principal's own kind, kept here so a
    reader of the entitlement does not have to dereference it.
    """

    kind: EntitlementSubjectKind
    id: str


class EntitlementResource(MosaicModel):
    """What a grant is over.

    ``modelApi`` and ``mcpServer`` name desired-state records that carry their own gateway.
    ``product`` and ``modelDeployment`` name observed records, which are scoped to the gateway or
    model endpoint MOSAIC read them from, so those require ``scope_id``.
    """

    kind: EntitlementResourceKind
    id: str
    scope_id: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.kind in {"product", "modelDeployment"} and not self.scope_id:
            raise ValueError(
                f"A {self.kind} entitlement needs scopeId naming the gateway or model endpoint "
                "it was observed on"
            )
        return self


class BindingSource(StrEnum):
    INFERRED = "inferred"
    MANUAL = "manual"
    ORCHESTRATED = "orchestrated"


class EntitlementBinding(MosaicModel):
    """The API Management object that realizes an entitlement at runtime.

    MOSAIC does not write to API Management (ADR 0001), so this records the product or subscription
    that an administrator identified or that MOSAIC inferred from observed state. It exists because
    gateway telemetry is keyed on the subscription: ``ApimSubscriptionId`` in
    ``ApiManagementGatewayLogs`` and ``ApiManagementGatewayLlmLog`` is the subscription's resource
    name, and without this binding a Cosmos entitlement cannot be joined to a usage row.

    When MOSAIC begins orchestrating the assignment it will populate the same field with
    ``source`` of ``orchestrated``; nothing downstream has to change.
    """

    gateway_id: str
    apim_product_name: str | None = None
    apim_subscription_name: str | None = None
    counter_key_expression: str | None = None
    source: BindingSource = BindingSource.MANUAL
    bound_at: datetime | None = None


class Entitlement(Entity):
    """A grant of a governed resource to a subject, and the limits that apply to it.

    Cosmos is the source of truth. An entitlement with no ``enforcement`` is unrestricted, which
    the portal reports as such rather than rendering a limit of zero.
    """

    entity_type: Literal["entitlement"] = "entitlement"
    subject: EntitlementSubject
    resource: EntitlementResource
    enabled: bool = True
    enforcement: EntitlementEnforcement | None = None
    binding: EntitlementBinding | None = None
    notes: str | None = None


class EntitlementCreate(MosaicModel):
    subject: EntitlementSubject
    resource: EntitlementResource
    enabled: bool = True
    enforcement: EntitlementEnforcement | None = None
    binding: EntitlementBinding | None = None
    notes: str | None = None


class EntitlementUpdate(MosaicModel):
    enabled: bool | None = None
    enforcement: EntitlementEnforcement | None = None
    binding: EntitlementBinding | None = None
    notes: str | None = None


def entitlement_id(
    tenant_id: str,
    subject: EntitlementSubject,
    resource: EntitlementResource,
) -> str:
    """Deterministic, so re-granting the same pair updates the record instead of duplicating it."""

    return deterministic_id(
        "entitlement",
        tenant_id,
        str(subject.kind),
        subject.id,
        str(resource.kind),
        resource.id,
        resource.scope_id or "",
    )


class GrantPath(StrEnum):
    DIRECT = "direct"
    GROUP = "group"


class ResolvedEntitlement(MosaicModel):
    """An entitlement that applies to a principal, and how it reached them."""

    entitlement: Entitlement
    via: GrantPath
    via_group_id: str | None = None
    via_group_name: str | None = None


class AccessRequestState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"


class AccessRequest(Entity):
    entity_type: Literal["accessRequest"] = "accessRequest"
    requester_object_id: str
    requester_principal_id: str | None = None
    resource: EntitlementResource
    justification: str | None = None
    state: AccessRequestState = AccessRequestState.PENDING
    decided_by_object_id: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    granted_entitlement_id: str | None = None


class AccessRequestCreate(MosaicModel):
    resource: EntitlementResource
    justification: str | None = None


class AccessRequestDecision(MosaicModel):
    note: str | None = None


MOSAIC_RESOURCE_PREFIX = "mosaic-"
_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


def apim_slug(value: str, *, max_length: int = 40) -> str:
    """Reduce a display name to something API Management accepts as a resource name."""

    slug = _SLUG_PATTERN.sub("-", value.casefold()).strip("-")
    return slug[:max_length].strip("-")


def publication_slug(endpoint_name: str, deployment_name: str) -> str:
    parts = [part for part in (apim_slug(endpoint_name), apim_slug(deployment_name)) if part]
    return "-".join(parts) or "model"


def publication_id(
    tenant_id: str, gateway_id: str, model_endpoint_id: str, deployment_name: str
) -> str:
    """Deterministic so publishing the same deployment twice refreshes intent, never forks it."""

    return deterministic_id(
        "publication", tenant_id, gateway_id, model_endpoint_id, deployment_name
    )


class PublicationStatus(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    APPLYING = "applying"
    PUBLISHED = "published"
    FAILED = "failed"
    ROLLED_BACK = "rolledBack"


class PublishedResourceKind(StrEnum):
    """The API Management resource types a publication creates, in dependency order."""

    POLICY_FRAGMENT = "policyFragment"
    BACKEND = "backend"
    API = "api"
    API_OPERATION = "apiOperation"
    API_POLICY = "apiPolicy"
    PRODUCT = "product"
    PRODUCT_API = "productApi"
    SUBSCRIPTION = "subscription"


class PublishedResource(MosaicModel):
    """One API Management resource a publication apply touched.

    ``created_by_mosaic`` is recorded at the moment of the write, never inferred afterwards from a
    name. A product that merely happens to match a MOSAIC name was not created by MOSAIC, and
    rollback and unpublish must never delete it.
    """

    kind: PublishedResourceKind
    name: str
    resource_id: str
    created_by_mosaic: bool = False
    applied_at: datetime = Field(default_factory=utc_now)


class Publication(Entity):
    """An administrator's intent to expose one model deployment through one gateway.

    Desired state. Saving it writes only to Cosmos; API Management is changed by an explicit apply
    against a specific plan, never as a side effect of recording the intent.
    """

    entity_type: Literal["publication"] = "publication"
    gateway_id: str
    model_endpoint_id: str
    deployment_name: str
    provider: ModelProvider
    display_name: str
    api_name: str
    api_path: str
    backend_name: str
    fragment_name: str
    product_name: str
    subscription_name: str
    subscription_required: bool = True
    enforcement: TokenEnforcement
    shape_version: str
    status: PublicationStatus = PublicationStatus.DRAFT
    resources: list[PublishedResource] = Field(default_factory=list)
    last_plan_id: str | None = None
    last_plan_digest: str | None = None
    last_run_id: str | None = None
    last_applied_at: datetime | None = None
    last_error: str | None = None

    def created_resources(self) -> list[PublishedResource]:
        """The subset rollback and unpublish are allowed to delete."""

        return [resource for resource in self.resources if resource.created_by_mosaic]


class CredentialReference(Entity):
    entity_type: Literal["credentialReference"] = "credentialReference"
    name: str
    secret_uri: AnyHttpUrl


class PolicyRevision(Entity):
    entity_type: Literal["policyRevision"] = "policyRevision"
    entitlement_id: str
    revision: int = Field(ge=1)
    content_sha256: str
    policy_xml: str


class SyncStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SyncOperation(Entity):
    entity_type: Literal["syncOperation"] = "syncOperation"
    status: SyncStatus
    desired_revision: str
    plan: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    completed_at: datetime | None = None


class AuditEvent(Entity):
    entity_type: Literal["auditEvent"] = "auditEvent"
    action: str
    resource_type: str
    resource_id: str
    actor_object_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class PrincipalCreate(MosaicModel):
    object_id: str = Field(min_length=1, max_length=128)
    kind: PrincipalKind
    label: str | None = Field(default=None, max_length=200)


class PrincipalUpdate(MosaicModel):
    kind: PrincipalKind | None = None
    label: str | None = Field(default=None, max_length=200)

    @field_validator("kind")
    @classmethod
    def kind_cannot_be_null(cls, value: PrincipalKind | None) -> PrincipalKind:
        if value is None:
            raise ValueError("kind cannot be null")
        return value


class GroupCreate(MosaicModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)


class GroupUpdate(MosaicModel):
    description: str | None = Field(default=None, max_length=1000)


class GatewayCreate(MosaicModel):
    azure_resource_id: str = Field(min_length=1, max_length=512)
    name: str | None = Field(default=None, max_length=120)
    environment_label: str | None = Field(default=None, max_length=60)
    provider: GatewayProvider = GatewayProvider.APIM

    @field_validator("azure_resource_id")
    @classmethod
    def validate_resource_id(cls, value: str) -> str:
        return ApimResourceId.parse(value).canonical


class GatewayUpdate(MosaicModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    environment_label: str | None = Field(default=None, max_length=60)
    management_mode: ManagementMode | None = None

    @field_validator("name")
    @classmethod
    def name_cannot_be_null(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("name cannot be null")
        return value


class GatewaySuggestion(MosaicModel):
    azure_resource_id: str
    service_name: str
    resource_group: str
    subscription_id: str
    already_registered: bool
    gateway_id: str | None = None
    reason: str


class ModelEndpointCreate(MosaicModel):
    """Register an Azure endpoint by resource ID, or any other endpoint by URL.

    ``credential_secret_uri`` is a Key Vault secret identifier, never a key. MOSAIC resolves it at
    discovery time with the Key Vault Secrets User role it already holds, and stores only the URI.
    """

    azure_resource_id: str | None = Field(default=None, max_length=512)
    endpoint: AnyHttpUrl | None = None
    name: str | None = Field(default=None, max_length=120)
    environment_label: str | None = Field(default=None, max_length=60)
    provider: ModelProvider | None = None
    credential_secret_uri: AnyHttpUrl | None = None

    @field_validator("azure_resource_id")
    @classmethod
    def validate_resource_id(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return CognitiveServicesResourceId.parse(value).canonical

    @model_validator(mode="after")
    def validate_identification(self) -> Self:
        if not self.azure_resource_id and not self.endpoint:
            raise ValueError(
                "Provide an Azure resource ID for an Azure AI endpoint, or a URL for an "
                "OpenAI-compatible endpoint"
            )
        if not self.azure_resource_id:
            if self.provider is None:
                self.provider = ModelProvider.OPENAI_COMPATIBLE
            if self.provider != ModelProvider.OPENAI_COMPATIBLE:
                raise ValueError(
                    "Azure OpenAI and Azure AI Foundry endpoints must be registered by resource ID "
                    "so MOSAIC can read their deployments with its managed identity"
                )
            if self.credential_secret_uri is None:
                raise ValueError(
                    "An OpenAI-compatible endpoint needs a Key Vault secret URI holding its API key"
                )
        return self


class ModelEndpointUpdate(MosaicModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    environment_label: str | None = Field(default=None, max_length=60)
    credential_secret_uri: AnyHttpUrl | None = None

    @field_validator("name")
    @classmethod
    def name_cannot_be_null(cls, value: str | None) -> str:
        # ``exclude_unset`` keeps an explicitly submitted null, which would then fail validation
        # against the required field on the entity and surface as a 500 rather than a 4xx.
        if value is None:
            raise ValueError("name cannot be null")
        return value


class McpEndpointCreate(MosaicModel):
    """Register an MCP server by URL.

    ``credential_secret_uri`` is a Key Vault secret identifier holding a bearer token, never the
    token itself. ``resource_audience`` names who a managed-identity token is for; MOSAIC will not
    infer it, because attaching a token to a host nobody named is how a managed identity leaks.
    """

    endpoint: AnyHttpUrl
    name: str | None = Field(default=None, max_length=120)
    environment_label: str | None = Field(default=None, max_length=60)
    auth_mode: McpAuthMode | None = None
    credential_secret_uri: AnyHttpUrl | None = None
    resource_audience: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_auth(self) -> Self:
        if self.auth_mode is None:
            if self.credential_secret_uri is not None:
                self.auth_mode = McpAuthMode.API_KEY
            elif self.resource_audience:
                self.auth_mode = McpAuthMode.MANAGED_IDENTITY
            else:
                self.auth_mode = McpAuthMode.NONE
        if self.auth_mode == McpAuthMode.API_KEY and self.credential_secret_uri is None:
            raise ValueError(
                "A key-authenticated MCP server needs a Key Vault secret URI holding its token"
            )
        if self.auth_mode == McpAuthMode.MANAGED_IDENTITY and not self.resource_audience:
            raise ValueError(
                "A managed-identity MCP server needs the audience its token should be issued for"
            )
        if self.auth_mode == McpAuthMode.NONE and (
            self.credential_secret_uri is not None or self.resource_audience
        ):
            raise ValueError(
                "An unauthenticated MCP server must not carry a secret URI or an audience"
            )
        return self


class McpEndpointUpdate(MosaicModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    environment_label: str | None = Field(default=None, max_length=60)
    credential_secret_uri: AnyHttpUrl | None = None
    resource_audience: str | None = Field(default=None, max_length=512)

    @field_validator("name")
    @classmethod
    def name_cannot_be_null(cls, value: str | None) -> str:
        # ``exclude_unset`` keeps an explicitly submitted null, which would then fail validation
        # against the required field on the entity and surface as a 500 rather than a 4xx.
        if value is None:
            raise ValueError("name cannot be null")
        return value


class SuggestionSource(StrEnum):
    BOOTSTRAP = "bootstrap"
    GATEWAY_BACKEND = "gatewayBackend"
    SUBSCRIPTION_SCAN = "subscriptionScan"


class ModelEndpointSuggestion(MosaicModel):
    """An endpoint MOSAIC believes is worth registering, and how it found it.

    ``azure_resource_id`` is absent when a gateway routes to a hostname MOSAIC cannot resolve to a
    resource; the hostname is still offered so an administrator can finish the identification.
    """

    source: SuggestionSource
    endpoint: AnyHttpUrl | None = None
    azure_resource_id: str | None = None
    account_name: str | None = None
    resource_group: str | None = None
    subscription_id: str | None = None
    kind: str | None = None
    location: str | None = None
    provider: ModelProvider | None = None
    already_registered: bool = False
    model_endpoint_id: str | None = None
    reason: str


class SubscriptionScanIssue(MosaicModel):
    """One subscription MOSAIC could not enumerate, and what would fix it."""

    subscription_id: str
    display_name: str | None = None
    message: str
    remediation: AccessRemediation | None = None


class ModelEndpointSuggestionView(MosaicModel):
    suggestions: list[ModelEndpointSuggestion] = Field(default_factory=list)
    scan_issues: list[SubscriptionScanIssue] = Field(default_factory=list)
    subscriptions_scanned: int = 0


class ImportRequest(MosaicModel):
    """The APIM API names an administrator chose to adopt.

    Detection decides which boxes start checked, not which imports are allowed. An administrator
    may adopt an API MOSAIC did not recognise, so no name is rejected for failing classification —
    only for being absent from the gateway's current snapshot.
    """

    api_names: list[str] = Field(min_length=1, max_length=500)

    @field_validator("api_names")
    @classmethod
    def clean_names(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for name in value:
            trimmed = name.strip()
            if not trimmed:
                raise ValueError("apiNames cannot contain blank entries")
            key = trimmed.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(trimmed)
        return cleaned


class ModelApiCandidate(MosaicModel):
    api_name: str
    display_name: str
    path: str
    service_url: str | None = None
    ai_kind: AiBackendKind = AiBackendKind.NONE
    ai_signals: list[str] = Field(default_factory=list)
    operation_count: int = 0
    product_names: list[str] = Field(default_factory=list)
    recommended: bool = False
    already_imported: bool = False


class McpServerCandidate(MosaicModel):
    api_name: str
    display_name: str
    path: str
    service_url: str | None = None
    kind: McpServerKind = McpServerKind.REST_API_BACKED
    transport_type: McpTransportType = McpTransportType.UNKNOWN
    tool_count: int = 0
    recommended: bool = True
    already_imported: bool = False


class ModelApiCandidateList(MosaicModel):
    gateway_id: str
    snapshot_id: str | None = None
    last_synced_at: datetime | None = None
    candidates: list[ModelApiCandidate] = Field(default_factory=list)


class McpServerCandidateList(MosaicModel):
    gateway_id: str
    snapshot_id: str | None = None
    last_synced_at: datetime | None = None
    support: CapabilitySupport = CapabilitySupport.UNKNOWN
    candidates: list[McpServerCandidate] = Field(default_factory=list)


class PolicyScope(StrEnum):
    GLOBAL = "global"
    PRODUCT = "product"
    API = "api"
    OPERATION = "operation"


class PolicySection(StrEnum):
    INBOUND = "inbound"
    BACKEND = "backend"
    OUTBOUND = "outbound"
    ON_ERROR = "onError"
    UNKNOWN = "unknown"


class PolicyFacetKind(StrEnum):
    RATE_LIMIT = "rateLimit"
    TOKEN_LIMIT = "tokenLimit"
    QUOTA = "quota"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    ROUTING = "routing"
    CACHING = "caching"
    CONTENT_SAFETY = "contentSafety"
    TRANSFORMATION = "transformation"
    OBSERVABILITY = "observability"
    NETWORK = "network"
    FRAGMENT_INCLUDE = "fragmentInclude"
    UNRECOGNIZED = "unrecognized"


class FacetConfidence(StrEnum):
    RECOGNIZED = "recognized"
    PARTIAL = "partial"
    UNRECOGNIZED = "unrecognized"


class PolicyFacet(MosaicModel):
    """One plain-language statement about a gateway policy element.

    ``summary`` is the only field intended for display. ``element`` is retained for diagnostics and
    drift reasoning but is an APIM element name, never markup.
    """

    kind: PolicyFacetKind
    element: str
    section: PolicySection = PolicySection.UNKNOWN
    summary: str
    details: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)
    confidence: FacetConfidence = FacetConfidence.RECOGNIZED
    managed_by_mosaic: bool = False


class PolicyPreviewRequest(MosaicModel):
    enforcement: TokenEnforcement
    backend_resource: str = "https://cognitiveservices.azure.com"


class PolicyPreview(MosaicModel):
    """A preview of the policy MOSAIC would author.

    ``policy_xml`` is retained in process because a later apply phase needs it, but it is excluded
    from serialisation: administrators read the plain-language facets, and markup never crosses the
    API boundary into a browser.
    """

    policy_xml: str = Field(exclude=True)
    content_sha256: str
    facets: list[PolicyFacet] = Field(default_factory=list)
    unrecognized_elements: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PublishAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    NO_CHANGE = "noChange"


class PublishStepStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROLLED_BACK = "rolledBack"
    ROLLBACK_FAILED = "rollbackFailed"


class PublishRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolledBack"
    ROLLBACK_FAILED = "rollbackFailed"


class PublishPlanStep(MosaicModel):
    """One API Management write the plan intends to perform.

    ``existed`` is what MOSAIC observed *before* applying. It is what makes rollback able to
    distinguish a resource it created from one it merely updated.
    """

    kind: PublishedResourceKind
    name: str
    action: PublishAction
    reason: str
    resource_id: str
    existed: bool = False


class PublishPlan(Entity):
    """A deterministic, persisted description of the writes an apply will perform.

    Apply runs against a specific plan and rejects one whose digest no longer matches the
    publication, so an administrator can never approve one set of changes and have another applied.
    """

    entity_type: Literal["publishPlan"] = "publishPlan"
    publication_id: str
    gateway_id: str
    digest: str
    steps: list[PublishPlanStep] = Field(default_factory=list)
    facets: list[PolicyFacet] = Field(default_factory=list)
    policy_content_sha256: str | None = None
    warnings: list[str] = Field(default_factory=list)
    actor_object_id: str | None = None


class PublishStepResult(MosaicModel):
    kind: PublishedResourceKind
    name: str
    action: PublishAction
    status: PublishStepStatus = PublishStepStatus.PENDING
    resource_id: str
    created_by_mosaic: bool = False
    error: str | None = None


class PublishRun(Entity):
    """The audited result of one apply, including what a rollback did or failed to do."""

    entity_type: Literal["publishRun"] = "publishRun"
    publication_id: str
    gateway_id: str
    plan_id: str
    plan_digest: str
    status: PublishRunStatus = PublishRunStatus.RUNNING
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    duration_ms: int | None = None
    steps: list[PublishStepResult] = Field(default_factory=list)
    rolled_back: bool = False
    orphaned_resources: list[PublishedResource] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    actor_object_id: str | None = None


class PublicationCreate(MosaicModel):
    gateway_id: str = Field(min_length=1, max_length=128)
    model_endpoint_id: str = Field(min_length=1, max_length=128)
    deployment_name: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    api_name: str | None = Field(default=None, min_length=1, max_length=80)
    api_path: str | None = Field(default=None, min_length=1, max_length=200)
    product_name: str | None = Field(default=None, min_length=1, max_length=80)
    subscription_required: bool = True
    enforcement: TokenEnforcement

    @field_validator("api_name", "product_name")
    @classmethod
    def validate_resource_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", candidate):
            raise ValueError(
                "API Management resource names must start with a letter or digit and contain "
                "only letters, digits, and hyphens"
            )
        return candidate

    @field_validator("api_path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip().strip("/")
        if not candidate or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-/]*", candidate):
            raise ValueError(
                "An API path may contain only letters, digits, hyphens, and forward slashes"
            )
        return candidate


class PublicationUpdate(MosaicModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    subscription_required: bool | None = None
    enforcement: TokenEnforcement | None = None


class PublishableModel(MosaicModel):
    """A deployment on a registered endpoint that could be published through a given gateway.

    ``runtime_access`` is carried through unchanged rather than collapsed into a boolean, because
    ADR 0006's distinction between "the gateway cannot call this" and "MOSAIC could not evaluate
    whether the gateway can call this" has to survive into the publish experience.
    """

    model_endpoint_id: str
    endpoint_name: str
    provider: ModelProvider
    deployment_name: str
    model_name: str | None = None
    model_version: str | None = None
    publication_id: str | None = None
    publication_status: PublicationStatus | None = None
    suggested_api_name: str = ""
    suggested_api_path: str = ""
    runtime_access: GatewayRuntimeAccess | None = None
