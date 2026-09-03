"""Observed gateway state.

Everything in this module describes what MOSAIC *saw* in a gateway, as opposed to the desired
state MOSAIC stores in ``domain``. Two rules apply to every model here:

1. No secret material. Subscription keys, named value secret values, and credential headers are
   never requested from Azure and never modelled.
2. No raw policy XML. Policy documents are reduced to a digest plus redacted semantic facets by
   ``integrations.apim.policy_semantics`` before they reach persistence.
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from mosaic_api.domain import (
    AiBackendKind,
    Entity,
    FacetConfidence,
    McpEndpoint,
    McpServerKind,
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
    endpoints: list[McpEndpoint] = Field(default_factory=list)
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
    "OBSERVED_ENTITY_TYPES",
    "AiBackendKind",
    "AnnotatedApi",
    "FacetConfidence",
    "GatewayPolicyView",
    "McpEndpoint",
    "McpServerKind",
    "McpTool",
    "McpTransportType",
    "ObservedApi",
    "ObservedApimGroup",
    "ObservedApimUser",
    "ObservedBackend",
    "ObservedEntity",
    "ObservedMcpServer",
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
