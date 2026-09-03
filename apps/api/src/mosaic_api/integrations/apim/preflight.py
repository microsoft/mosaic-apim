"""Verify that MOSAIC can actually read a gateway before claiming to manage it.

Onboarding fails loudly and specifically rather than silently producing an empty inventory. When
access is missing MOSAIC reports the exact role, scope, and command an operator needs, because the
role assignment must be granted out-of-band by someone with more privilege than MOSAIC has.
"""

import re
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
    "Microsoft.ApiManagement/service/policies/write",
    "Microsoft.ApiManagement/service/policyFragments/write",
    "Microsoft.ApiManagement/service/products/write",
    "Microsoft.ApiManagement/service/subscriptions/write",
)


@dataclass(frozen=True)
class PreflightResult:
    access: GatewayAccess
    capabilities: GatewayCapabilities
    status: GatewayStatus
    service_name: str | None = None


def _action_matches(pattern: str, action: str) -> bool:
    regex = re.escape(pattern).replace(r"\*", ".*")
    return re.fullmatch(regex, action, re.IGNORECASE) is not None


def _permits(permissions: list[JsonObject], action: str) -> bool:
    """Evaluate RBAC per assignment: notActions only subtract from their own actions."""

    for permission in permissions:
        actions = permission.get("actions")
        granted = isinstance(actions, list) and any(
            isinstance(pattern, str) and _action_matches(pattern, action) for pattern in actions
        )
        if not granted:
            continue
        not_actions = permission.get("notActions")
        excluded = isinstance(not_actions, list) and any(
            isinstance(pattern, str) and _action_matches(pattern, action)
            for pattern in not_actions
        )
        if not excluded:
            return True
    return False


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


def _capabilities(service: JsonObject | None) -> GatewayCapabilities:
    if not service:
        return GatewayCapabilities(
            notes=["MOSAIC could not read the service description."],
        )
    sku = service.get("sku") if isinstance(service.get("sku"), dict) else {}
    properties = service.get("properties") if isinstance(service.get("properties"), dict) else {}
    sku_name = sku.get("name") if isinstance(sku, dict) else None
    sku_capacity = sku.get("capacity") if isinstance(sku, dict) else None
    gateway_url = properties.get("gatewayUrl") if isinstance(properties, dict) else None
    provisioning_state = (
        properties.get("provisioningState") if isinstance(properties, dict) else None
    )
    notes = [
        "MOSAIC reports AI gateway policy support only once it observes those policies in use.",
    ]
    return GatewayCapabilities(
        sku_name=sku_name if isinstance(sku_name, str) else None,
        sku_capacity=sku_capacity if isinstance(sku_capacity, int) else None,
        provisioning_state=(
            provisioning_state if isinstance(provisioning_state, str) else None
        ),
        location=service.get("location") if isinstance(service.get("location"), str) else None,
        gateway_url=gateway_url if isinstance(gateway_url, str) else None,
        ai_gateway_policies=CapabilitySupport.UNKNOWN,
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
                "MOSAIC can read this gateway. Enrollment will later need write access, which is "
                "not granted."
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
