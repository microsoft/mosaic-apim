import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import NAMESPACE_URL, uuid4, uuid5

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.alias_generators import to_camel

APIM_API_VERSION = "2024-05-01"
# MCP servers are only visible on a preview contract. It is deliberately not the version the rest
# of the inventory uses: a preview API that changes or disappears must degrade MCP discovery alone,
# never the gateway sync that administrators depend on.
APIM_MCP_API_VERSION = "2025-09-01-preview"
AUTHORIZATION_API_VERSION = "2022-04-01"
APIM_PROVIDER_NAMESPACE = "Microsoft.ApiManagement"
APIM_RESOURCE_TYPE = "service"
APIM_READER_ROLE_NAME = "API Management Service Reader Role"
APIM_READER_ROLE_ID = "71522526-b88f-4d52-b57f-d31fc3546d0d"
APIM_CONTRIBUTOR_ROLE_NAME = "API Management Service Contributor"
APIM_CONTRIBUTOR_ROLE_ID = "312a565d-c81f-4fd8-895a-4e21e48d571c"

_APIM_RESOURCE_ID_PATTERN = re.compile(
    r"^/subscriptions/(?P<subscription>[0-9a-fA-F-]{36})"
    r"/resourceGroups/(?P<resourceGroup>[^/]{1,90})"
    r"/providers/Microsoft\.ApiManagement/service"
    r"/(?P<serviceName>[^/]{1,50})$",
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


class FoundryConnection(Entity):
    entity_type: Literal["foundryConnection"] = "foundryConnection"
    name: str
    endpoint: AnyHttpUrl
    azure_resource_id: str
    credential_reference_id: str


class CatalogModel(Entity):
    entity_type: Literal["catalogModel"] = "catalogModel"
    provider: str
    model_name: str
    model_version: str | None = None


class ModelDeployment(Entity):
    entity_type: Literal["modelDeployment"] = "modelDeployment"
    foundry_connection_id: str
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


class McpEndpoint(MosaicModel):
    name: str
    uri_template: str


class McpTool(MosaicModel):
    name: str
    display_name: str
    description: str | None = None
    backing_api_name: str | None = None
    backing_operation_name: str | None = None


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
    endpoints: list[McpEndpoint] = Field(default_factory=list)
    tools: list[McpTool] = Field(default_factory=list)
    tool_count: int = 0
    subscription_required: bool = True
    product_names: list[str] = Field(default_factory=list)
    selection: ImportSelection = ImportSelection.DETECTED
    imported_from_snapshot_id: str
    imported_at: datetime = Field(default_factory=utc_now)
    imported_by: str | None = None


def model_api_id(tenant_id: str, gateway_id: str, api_name: str) -> str:
    """Deterministic so re-importing an API updates the record instead of duplicating it."""

    return deterministic_id("modelApi", tenant_id, gateway_id, api_name)


def mcp_server_id(tenant_id: str, gateway_id: str, api_name: str) -> str:
    return deterministic_id("mcpServer", tenant_id, gateway_id, api_name)


class TokenEnforcement(MosaicModel):
    counter_key_expression: str
    tokens_per_minute: int | None = Field(default=None, ge=1)
    token_quota: int | None = Field(default=None, ge=1)
    token_quota_period: Literal["Hourly", "Daily", "Weekly", "Monthly", "Yearly"] | None = None
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


class Entitlement(Entity):
    entity_type: Literal["entitlement"] = "entitlement"
    group_id: str
    model_deployment_id: str
    enabled: bool = True
    enforcement: TokenEnforcement


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


class GatewaySuggestion(MosaicModel):
    azure_resource_id: str
    service_name: str
    resource_group: str
    subscription_id: str
    already_registered: bool
    gateway_id: str | None = None
    reason: str


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
