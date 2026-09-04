"""Observed gateway state.

Everything in this module describes what MOSAIC *saw* in a gateway, as opposed to the desired
state MOSAIC stores in ``domain``. Two rules apply to every model here:

1. No secret material. Subscription keys, named value secret values, and credential headers are
   never requested from Azure and never modelled.
2. No raw policy XML. Policy documents are reduced to a digest plus redacted semantic facets by
   ``integrations.apim.policy_semantics`` before they reach persistence.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from mosaic_api.domain import (
    AiBackendKind,
    Entity,
    FacetConfidence,
    McpServerKind,
    McpServerRoute,
    McpTool,
    McpTransportType,
    MosaicModel,
    PolicyFacet,
    PolicyFacetKind,
    PolicyScope,
    PolicySection,
    utc_now,
)


class ObservedEntity(Entity):
    gateway_id: str
    snapshot_id: str
    observed_at: datetime = Field(default_factory=utc_now)


class ObservedApi(ObservedEntity):
    entity_type: Literal["observedApi"] = "observedApi"
    name: str
    display_name: str
    path: str
    protocols: list[str] = Field(default_factory=list)
    service_url: str | None = None
    api_type: str | None = None
    api_revision: str | None = None
    api_version: str | None = None
    is_current: bool = True
    subscription_required: bool = True
    ai_kind: AiBackendKind = AiBackendKind.NONE
    ai_signals: list[str] = Field(default_factory=list)
    operation_count: int = 0
    product_names: list[str] = Field(default_factory=list)


class ObservedOperation(ObservedEntity):
    entity_type: Literal["observedOperation"] = "observedOperation"
    api_name: str
    name: str
    display_name: str
    method: str
    url_template: str


class ObservedMcpServer(ObservedEntity):
    """An MCP server hosted by the gateway.

    API Management models these as APIs of type ``mcp``, but they are kept as their own observed
    type because they are a different product surface: tools rather than operations, and a
    transport rather than a set of HTTP verbs.
    """

    entity_type: Literal["observedMcpServer"] = "observedMcpServer"
    name: str
    display_name: str
    path: str
    protocols: list[str] = Field(default_factory=list)
    service_url: str | None = None
    kind: McpServerKind = McpServerKind.REST_API_BACKED
    transport_type: McpTransportType = McpTransportType.UNKNOWN
    endpoints: list[McpServerRoute] = Field(default_factory=list)
    tools: list[McpTool] = Field(default_factory=list)
    tool_count: int = 0
    subscription_required: bool = True
    product_names: list[str] = Field(default_factory=list)


class ObservedProduct(ObservedEntity):
    entity_type: Literal["observedProduct"] = "observedProduct"
    name: str
    display_name: str
    description: str | None = None
    state: str | None = None
    subscription_required: bool = True
    approval_required: bool = False
    subscriptions_limit: int | None = None
    api_names: list[str] = Field(default_factory=list)


class ObservedSubscription(ObservedEntity):
    """An APIM subscription. Primary and secondary keys are deliberately never requested."""

    entity_type: Literal["observedSubscription"] = "observedSubscription"
    name: str
    display_name: str | None = None
    scope: str
    scope_kind: Literal["allApis", "product", "api", "unknown"] = "unknown"
    scope_name: str | None = None
    state: str | None = None
    owner_id: str | None = None
    owner_label: str | None = None
    created_date: datetime | None = None


class ObservedApimUser(ObservedEntity):
    """A gateway-local developer identity.

    APIM users are not Entra principals. When the identity provider is AAD the Entra object ID is
    surfaced in ``entra_object_id`` so MOSAIC can correlate, but governance anchors on
    ``domain.Principal``.
    """

    entity_type: Literal["observedApimUser"] = "observedApimUser"
    name: str
    display_name: str | None = None
    email: str | None = None
    state: str | None = None
    identity_providers: list[str] = Field(default_factory=list)
    entra_object_id: str | None = None
    group_names: list[str] = Field(default_factory=list)


class ObservedApimGroup(ObservedEntity):
    entity_type: Literal["observedApimGroup"] = "observedApimGroup"
    name: str
    display_name: str
    description: str | None = None
    group_type: str | None = None
    built_in: bool = False


class ObservedBackend(ObservedEntity):
    entity_type: Literal["observedBackend"] = "observedBackend"
    name: str
    title: str | None = None
    url: str | None = None
    protocol: str | None = None
    ai_kind: AiBackendKind = AiBackendKind.NONE


class ObservedNamedValue(ObservedEntity):
    """Named value metadata only. ``value`` is never read, requested, or stored."""

    entity_type: Literal["observedNamedValue"] = "observedNamedValue"
    name: str
    display_name: str
    secret: bool = False
    tags: list[str] = Field(default_factory=list)
    key_vault_secret_identifier: str | None = None


class ObservedPolicyDocument(ObservedEntity):
    entity_type: Literal["observedPolicyDocument"] = "observedPolicyDocument"
    scope: PolicyScope
    scope_id: str
    scope_label: str
    content_sha256: str
    element_count: int = 0
    facets: list[PolicyFacet] = Field(default_factory=list)
    unrecognized_elements: list[str] = Field(default_factory=list)


class ObservedPolicyFragment(ObservedEntity):
    entity_type: Literal["observedPolicyFragment"] = "observedPolicyFragment"
    name: str
    description: str | None = None
    content_sha256: str
    managed_by_mosaic: bool = False
    facets: list[PolicyFacet] = Field(default_factory=list)
    unrecognized_elements: list[str] = Field(default_factory=list)


OBSERVED_ENTITY_TYPES: tuple[str, ...] = (
    "observedApi",
    "observedOperation",
    "observedMcpServer",
    "observedProduct",
    "observedSubscription",
    "observedApimUser",
    "observedApimGroup",
    "observedBackend",
    "observedNamedValue",
    "observedPolicyDocument",
    "observedPolicyFragment",
)


class ObservedEndpointEntity(Entity):
    """Base for state MOSAIC observed on a *registered endpoint* — a model endpoint or an MCP one.

    Deliberately a sibling of :class:`ObservedEntity` rather than a subclass. Observed gateway
    documents are keyed on ``gatewayId`` in Cosmos SQL; introducing a shared scope field would
    orphan every existing document, because a query on the new field cannot sweep documents written
    with only the old one. Two explicit shapes cost a little duplication and no migration.

    ADR 0006 named MCP tools as the third scope that should force a generalisation rather than a
    third copy. That generalisation is this rename: the sweep already keys on ``endpointId``, and
    an MCP endpoint is an endpoint, so tools live here and nothing had to be migrated.
    """

    endpoint_id: str
    snapshot_id: str
    observed_at: datetime = Field(default_factory=utc_now)


class ObservedModelDeployment(ObservedEndpointEntity):
    """A model deployment MOSAIC read from a provider endpoint.

    This is the callable unit an entitlement will later grant access to, so its ID is deterministic
    and stable across syncs.
    """

    entity_type: Literal["observedModelDeployment"] = "observedModelDeployment"
    deployment_name: str
    model_name: str | None = None
    model_version: str | None = None
    model_format: str | None = None
    model_publisher: str | None = None
    sku_name: str | None = None
    sku_capacity: int | None = None
    provisioning_state: str | None = None
    rai_policy_name: str | None = None
    capabilities: dict[str, str] = Field(default_factory=dict)
    request_paths: list[str] = Field(default_factory=list)


class ObservedAvailableModel(ObservedEndpointEntity):
    """A model the endpoint could host but has not deployed.

    Kept separate from :class:`ObservedModelDeployment` so the UI never implies something callable
    that is not actually deployed.
    """

    entity_type: Literal["observedAvailableModel"] = "observedAvailableModel"
    model_name: str
    model_format: str | None = None
    model_version: str | None = None
    lifecycle_status: str | None = None
    max_capacity: int | None = None
    capabilities: dict[str, str] = Field(default_factory=dict)
    deprecation_inference: str | None = None
    deprecation_fine_tune: str | None = None


class McpToolAnnotations(MosaicModel):
    """The server's own claims about what a tool does, stored exactly as it stated them.

    Every hint is tri-state on purpose. The MCP specification defines ``destructiveHint`` and
    ``openWorldHint`` as defaulting to **true**, and ``readOnlyHint``/``idempotentHint`` as
    defaulting to false, so collapsing an absent hint into its default would turn "the server
    said nothing" into a claim it never made — and in the safe direction for two of the four,
    which is the dangerous way round for a governance product.

    The specification is also explicit that clients "MUST consider tool annotations to be
    untrusted". These are assertions by the server, never verdicts by MOSAIC.
    """

    title: str | None = None
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
    idempotent_hint: bool | None = None
    open_world_hint: bool | None = None

    def stated_anything(self) -> bool:
        return any(
            value is not None
            for value in (
                self.read_only_hint,
                self.destructive_hint,
                self.idempotent_hint,
                self.open_world_hint,
            )
        )


class ObservedMcpTool(ObservedEndpointEntity):
    """A tool a registered MCP server declared.

    Richer than the :class:`~mosaic_api.domain.McpTool` read from API Management, which exposes
    only a name, display name, description, and backing operation. The schemas and annotations
    here exist nowhere in the management plane; they come from the server itself.
    """

    entity_type: Literal["observedMcpTool"] = "observedMcpTool"
    name: str
    display_name: str
    title: str | None = None
    description: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    annotations: McpToolAnnotations | None = None


OBSERVED_ENDPOINT_ENTITY_TYPES: tuple[str, ...] = (
    "observedModelDeployment",
    "observedAvailableModel",
    "observedMcpTool",
)


class GatewayPolicyView(MosaicModel):
    """The policy surface of a gateway, already reduced to plain language."""

    documents: list[ObservedPolicyDocument] = Field(default_factory=list)
    fragments: list[ObservedPolicyFragment] = Field(default_factory=list)
    recognized_count: int = 0
    unrecognized_count: int = 0
    mosaic_managed_count: int = 0


class ScopedPolicyView(MosaicModel):
    """A policy read on demand rather than during a full sync."""

    scope: PolicyScope
    scope_id: str
    scope_label: str
    exists: bool = False
    content_sha256: str | None = None
    facets: list[PolicyFacet] = Field(default_factory=list)
    unrecognized_elements: list[str] = Field(default_factory=list)


class AnnotatedApi(MosaicModel):
    api: ObservedApi
    operations: list[ObservedOperation] = Field(default_factory=list)


__all__ = [
    "OBSERVED_ENDPOINT_ENTITY_TYPES",
    "OBSERVED_ENTITY_TYPES",
    "AiBackendKind",
    "AnnotatedApi",
    "FacetConfidence",
    "GatewayPolicyView",
    "McpServerKind",
    "McpServerRoute",
    "McpTool",
    "McpToolAnnotations",
    "McpTransportType",
    "ObservedApi",
    "ObservedApimGroup",
    "ObservedApimUser",
    "ObservedAvailableModel",
    "ObservedBackend",
    "ObservedEndpointEntity",
    "ObservedEntity",
    "ObservedMcpServer",
    "ObservedMcpTool",
    "ObservedModelDeployment",
    "ObservedNamedValue",
    "ObservedOperation",
    "ObservedPolicyDocument",
    "ObservedPolicyFragment",
    "ObservedProduct",
    "ObservedSubscription",
    "PolicyFacet",
    "PolicyFacetKind",
    "PolicyScope",
    "PolicySection",
    "ScopedPolicyView",
]
