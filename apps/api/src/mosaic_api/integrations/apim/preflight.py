"""Verify that MOSAIC can actually read a gateway before claiming to manage it.

Onboarding fails loudly and specifically rather than silently producing an empty inventory. When
access is missing MOSAIC reports the exact role, scope, and command an operator needs, because the
role assignment must be granted out-of-band by someone with more privilege than MOSAIC has.
"""

from dataclasses import dataclass

from mosaic_api.domain import (
    APIM_CONTRIBUTOR_ROLE_ID,
    APIM_CONTRIBUTOR_ROLE_NAME,
    APIM_READER_ROLE_ID,
    APIM_READER_ROLE_NAME,
    AccessEvaluation,
    AccessRemediation,
    ApimResourceId,
    CapabilitySupport,
    GatewayAccess,
    GatewayCapabilities,
    GatewayStatus,
    utc_now,
)
from mosaic_api.errors import UpstreamAuthorizationError, UpstreamError, UpstreamNotFoundError
from mosaic_api.integrations.apim.client import ApimClient, JsonObject
from mosaic_api.integrations.rbac import permits as _permits

READ_ACTIONS: tuple[str, ...] = (
    "Microsoft.ApiManagement/service/read",
    "Microsoft.ApiManagement/service/apis/read",
    "Microsoft.ApiManagement/service/products/read",
    "Microsoft.ApiManagement/service/subscriptions/read",
    "Microsoft.ApiManagement/service/users/read",
    "Microsoft.ApiManagement/service/groups/read",
    "Microsoft.ApiManagement/service/backends/read",
    "Microsoft.ApiManagement/service/namedValues/read",
    "Microsoft.ApiManagement/service/policies/read",
    "Microsoft.ApiManagement/service/policyFragments/read",
)

WRITE_ACTIONS: tuple[str, ...] = (
    "Microsoft.ApiManagement/service/apis/write",
    "Microsoft.ApiManagement/service/apis/delete",
    "Microsoft.ApiManagement/service/apis/operations/write",
    "Microsoft.ApiManagement/service/apis/operations/delete",
    "Microsoft.ApiManagement/service/apis/policies/write",
    "Microsoft.ApiManagement/service/apis/policies/delete",
    "Microsoft.ApiManagement/service/backends/write",
    "Microsoft.ApiManagement/service/backends/delete",
    "Microsoft.ApiManagement/service/policies/write",
    "Microsoft.ApiManagement/service/policyFragments/write",
    "Microsoft.ApiManagement/service/policyFragments/delete",
    "Microsoft.ApiManagement/service/products/write",
    "Microsoft.ApiManagement/service/products/delete",
    "Microsoft.ApiManagement/service/products/apis/write",
    "Microsoft.ApiManagement/service/products/apis/delete",
    "Microsoft.ApiManagement/service/subscriptions/write",
    "Microsoft.ApiManagement/service/subscriptions/delete",
)


@dataclass(frozen=True)
class PreflightResult:
    access: GatewayAccess
    capabilities: GatewayCapabilities
    status: GatewayStatus
    service_name: str | None = None


def build_remediation(
    resource: ApimResourceId,
    *,
    principal_id: str | None,
    write: bool = False,
) -> AccessRemediation:
    role_name = APIM_CONTRIBUTOR_ROLE_NAME if write else APIM_READER_ROLE_NAME
    role_id = APIM_CONTRIBUTOR_ROLE_ID if write else APIM_READER_ROLE_ID
    assignee = principal_id or "<mosaic-managed-identity-object-id>"
    command = (
        "az role assignment create"
        f' --assignee-object-id "{assignee}"'
        " --assignee-principal-type ServicePrincipal"
        f' --role "{role_name}"'
        f' --scope "{resource.canonical}"'
    )
    return AccessRemediation(
        role_name=role_name,
        role_definition_id=role_id,
        scope=resource.canonical,
        principal_id=principal_id,
        command=command,
    )


def _identity_principal_id(identity: JsonObject) -> tuple[str | None, int]:
    """The principal a gateway authenticates as, and how many identities it has.

    ARM only populates the top-level ``principalId`` for a system-assigned identity. A service using
    user-assigned identities exposes each principal under ``userAssignedIdentities`` instead, and
    reading only the top level would report such a gateway as having no identity at all.
    """

    principal_id = identity.get("principalId")
    assigned = identity.get("userAssignedIdentities")
    user_assigned = [
        value.get("principalId")
        for value in (assigned.values() if isinstance(assigned, dict) else [])
        if isinstance(value, dict) and isinstance(value.get("principalId"), str)
    ]
    if isinstance(principal_id, str) and principal_id:
        return principal_id, 1 + len(user_assigned)
    if user_assigned:
        # Which one a policy actually uses depends on its client-id, which MOSAIC cannot know from
        # the service description. The count is reported so the UI can say so.
        return str(user_assigned[0]), len(user_assigned)
    return None, 0


def _capabilities(service: JsonObject | None) -> GatewayCapabilities:
    if not service:
        return GatewayCapabilities(
            notes=["MOSAIC could not read the service description."],
        )
    sku = service.get("sku") if isinstance(service.get("sku"), dict) else {}
    properties = service.get("properties") if isinstance(service.get("properties"), dict) else {}
    identity = service.get("identity") if isinstance(service.get("identity"), dict) else {}
    sku_name = sku.get("name") if isinstance(sku, dict) else None
    sku_capacity = sku.get("capacity") if isinstance(sku, dict) else None
    gateway_url = properties.get("gatewayUrl") if isinstance(properties, dict) else None
    provisioning_state = (
        properties.get("provisioningState") if isinstance(properties, dict) else None
    )
    # The gateway's own managed identity is the principal that must hold a data-plane role on a
    # model endpoint before the gateway can call it. Capturing it here means the endpoint runtime
    # check never has to re-read the API Management service.
    principal_id, identity_count = _identity_principal_id(
        identity if isinstance(identity, dict) else {}
    )
    notes = [
        "MOSAIC reports AI gateway policy support only once it observes those policies in use.",
    ]
    if principal_id is None:
        notes.append(
            "This gateway has no managed identity, so it cannot authenticate to model endpoints "
            "without a key."
        )
    elif identity_count > 1:
        notes.append(
            f"This gateway has {identity_count} managed identities. Which one it uses depends on "
            "the client ID in each policy, so confirm the role is assigned to the right one."
        )
    return GatewayCapabilities(
        sku_name=sku_name if isinstance(sku_name, str) else None,
        sku_capacity=sku_capacity if isinstance(sku_capacity, int) else None,
        provisioning_state=(
            provisioning_state if isinstance(provisioning_state, str) else None
        ),
        location=service.get("location") if isinstance(service.get("location"), str) else None,
        gateway_url=gateway_url if isinstance(gateway_url, str) else None,
        ai_gateway_policies=CapabilitySupport.UNKNOWN,
        principal_id=principal_id,
        identity_observed=True,
        notes=notes,
    )


async def run_preflight(
    client: ApimClient,
    *,
    principal_id: str | None = None,
) -> PreflightResult:
    """Check read access, probe write capability, and describe the service."""

    resource = client.resource
    checked_at = utc_now()

    try:
        service = await client.get_service()
    except UpstreamAuthorizationError as error:
        return PreflightResult(
            access=GatewayAccess(
                can_read=False,
                can_write=False,
                evaluation=AccessEvaluation.PROBE,
                checked_at=checked_at,
                missing_actions=list(READ_ACTIONS),
                remediation=build_remediation(resource, principal_id=principal_id),
                message=(
                    "MOSAIC's managed identity cannot read this API Management service. "
                    "Grant it the reader role shown below and try again."
                ),
            ),
            capabilities=GatewayCapabilities(notes=[str(error.message)]),
            status=GatewayStatus.UNAUTHORIZED,
        )
    except (UpstreamNotFoundError, UpstreamError) as error:
        return PreflightResult(
            access=GatewayAccess(
                can_read=False,
                evaluation=AccessEvaluation.PROBE,
                checked_at=checked_at,
                message=str(error.message),
            ),
            capabilities=GatewayCapabilities(notes=[str(error.message)]),
            status=GatewayStatus.UNREACHABLE,
        )

    if service is None:
        return PreflightResult(
            access=GatewayAccess(
                can_read=False,
                evaluation=AccessEvaluation.PROBE,
                checked_at=checked_at,
                message=(
                    "No API Management service exists at this resource ID, or it is not visible "
                    "to MOSAIC's managed identity."
                ),
            ),
            capabilities=GatewayCapabilities(),
            status=GatewayStatus.UNREACHABLE,
        )

    capabilities = _capabilities(service)
    permissions = await client.effective_permissions()

    if permissions is None:
        access = GatewayAccess(
            can_read=True,
            can_write=False,
            evaluation=AccessEvaluation.PROBE,
            checked_at=checked_at,
            remediation=build_remediation(resource, principal_id=principal_id, write=True),
            message=(
                "MOSAIC can read this gateway. It could not evaluate role assignments, so write "
                "capability is reported as unavailable until it is confirmed."
            ),
        )
        return PreflightResult(
            access=access,
            capabilities=capabilities,
            status=GatewayStatus.CONNECTED,
            service_name=resource.service_name,
        )

    missing_read = [action for action in READ_ACTIONS if not _permits(permissions, action)]
    missing_write = [action for action in WRITE_ACTIONS if not _permits(permissions, action)]
    can_read = not missing_read
    can_write = not missing_write

    if can_read:
        message = (
            "MOSAIC can read this gateway."
            if can_write
            else (
                "MOSAIC can read this gateway. Publishing models into it needs write access, "
                "which is not granted."
            )
        )
        remediation = (
            None
            if can_write
            else build_remediation(resource, principal_id=principal_id, write=True)
        )
        status = GatewayStatus.CONNECTED
        missing = missing_write
    else:
        message = (
            "MOSAIC's managed identity is missing read permissions on this API Management service."
        )
        remediation = build_remediation(resource, principal_id=principal_id)
        status = GatewayStatus.UNAUTHORIZED
        missing = missing_read

    return PreflightResult(
        access=GatewayAccess(
            can_read=can_read,
            can_write=can_write,
            evaluation=AccessEvaluation.EFFECTIVE_PERMISSIONS,
            checked_at=checked_at,
            missing_actions=missing,
            remediation=remediation,
            message=message,
        ),
        capabilities=capabilities,
        status=status,
        service_name=resource.service_name,
    )
