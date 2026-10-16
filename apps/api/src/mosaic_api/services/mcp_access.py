"""Shared, secret-free MCP access state and publication mutation guards."""

from mosaic_api.domain import (
    BindingSource,
    Entitlement,
    EntitlementRuntime,
    McpAccessGrant,
    McpPublication,
    McpServer,
    Principal,
    PublicationStatus,
    PublishedResourceKind,
    model_access_subscription_name,
)
from mosaic_api.repositories import GatewayRepository
from mosaic_api.services.model_access import CostCenterIntent, entitlement_intent_digest


async def entitlement_mcp_publication(
    repository: GatewayRepository, entitlement: Entitlement
) -> McpPublication | None:
    if entitlement.resource.kind != "mcpServer":
        return None
    server = await repository.get_mcp_server(entitlement.tenant_id, entitlement.resource.id)
    if server is None or server.publication_id is None:
        return None
    publication = await repository.get_mcp_publication(entitlement.tenant_id, server.publication_id)
    if publication is None or publication.mcp_server_id != server.id:
        return None
    if publication.gateway_id != server.gateway_id or publication.api_name != server.api_name:
        return None
    return publication


def mcp_server_offered(server: McpServer, publication: McpPublication | None) -> bool:
    """Whether end users may be offered this MCP server, to request or to connect to.

    An MCP server imported from a gateway is, as it always was. One MOSAIC publishes is offered
    only while its publication, ``publication`` (the record ``server.publication_id`` names, if
    any), holds its MCP API in API Management: not before the first apply, and not once unpublished.
    """

    if server.publication_id is None:
        return True
    return (
        publication is not None
        and publication.id == server.publication_id
        and publication.mcp_server_id == server.id
        and publication.gateway_id == server.gateway_id
        and publication.api_name == server.api_name
        and publication.has_applied_api()
    )


def applied_mcp_grant(publication: McpPublication, entitlement_id: str) -> McpAccessGrant | None:
    if publication.applied_access is None:
        return None
    return next(
        (
            grant
            for grant in publication.applied_access.grants
            if grant.entitlement_id == entitlement_id
        ),
        None,
    )


def mcp_grant_needs_retention(publication: McpPublication, entitlement_id: str) -> bool:
    grant = applied_mcp_grant(publication, entitlement_id)
    return (
        any(
            item.kind == PublishedResourceKind.SUBSCRIPTION
            and item.name
            == model_access_subscription_name(publication.tenant_id, publication.id, entitlement_id)
            and item.created_by_mosaic
            for item in publication.resources
        )
        or (grant is not None and grant.enabled)
        or publication.status == PublicationStatus.APPLYING
        or publication.access_state in {"applying", "unknown"}
    )


def decorate_mcp_entitlement(
    entitlement: Entitlement,
    publication: McpPublication | None,
    principal: Principal | None,
    *,
    locked: bool = False,
    cost_center: CostCenterIntent | None = None,
) -> Entitlement:
    """Derive MCP runtime state from trusted publication state."""

    if (
        publication is None
        or entitlement.subject.kind == "group"
        or entitlement.resource.kind != "mcpServer"
        or publication.mcp_server_id != entitlement.resource.id
    ):
        return entitlement.model_copy(update={"runtime": None})

    snapshot = publication.applied_access
    grant = applied_mcp_grant(publication, entitlement.id)
    methods = snapshot.settings if snapshot else None
    status: str
    if publication.access_state == "unknown":
        status = "unknown"
    elif locked or publication.access_state == "applying":
        status = "applying"
    elif publication.access_state == "failed":
        status = "failed"
    elif snapshot is None:
        status = "pending"
    elif publication.status == PublicationStatus.DRAFT and not publication.created_resources():
        status = "revoked"
    elif not entitlement.enabled and grant is not None and grant.enabled:
        status = "revocationPending"
    elif (
        grant is None
        or principal is None
        or grant.intent_digest != entitlement_intent_digest(entitlement, principal, cost_center)
    ):
        status = "pending"
    elif not grant.enabled:
        status = "revoked"
    else:
        status = "applied"

    runtime = EntitlementRuntime(
        publication_id=publication.id,
        status=status,
        applied_methods=methods,
        subscription_name=grant.subscription_name if grant else None,
        key_exists=bool(
            grant
            and grant.subscription_name
            and publication.key_lifecycle.get(grant.subscription_name) == "ready"
            and any(
                item.kind == PublishedResourceKind.SUBSCRIPTION
                and item.name == grant.subscription_name
                and item.created_by_mosaic
                for item in publication.resources
            )
        ),
        key_state=(
            (
                "ineligible"
                if not entitlement.enabled
                or cost_center is None
                or not cost_center.keys_allowed
                or (grant is not None and (grant.is_group_grant or not grant.keys_allowed))
                else (
                    "pending"
                    if grant is None
                    else publication.key_lifecycle.get(grant.subscription_name or "", "pending")
                )
            )
            if publication.azure_cloud == "government"
            else None
        ),
        key_error=(
            "Key setup needs an administrator retry; no key has been revealed."
            if grant is not None
            and publication.key_lifecycle.get(grant.subscription_name or "") == "failed"
            else None
        ),
        applied_at=publication.last_applied_at,
        error=publication.last_error,
    )
    if publication.azure_cloud == "government":
        runtime.key_exists = runtime.key_exists and runtime.key_state == "ready"
    binding = entitlement.binding
    if binding and binding.source == BindingSource.ORCHESTRATED and grant is None:
        binding = None
    return entitlement.model_copy(update={"runtime": runtime, "binding": binding})
