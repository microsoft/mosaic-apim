"""Verify that MOSAIC can enumerate models on an endpoint before claiming to govern it.

This answers one question about one identity: can *MOSAIC's* managed identity read this endpoint's
deployments over ARM? Whether a *gateway* can call those models at runtime is a different question
about a different principal on a different plane, and lives in ``runtime_access``.

Onboarding fails loudly and specifically rather than producing an empty catalog. MOSAIC cannot grant
itself a role, so when access is missing it reports the exact role, scope, and command.
"""

from dataclasses import dataclass
from typing import Any

from mosaic_api.domain import (
    READER_ROLE_ID,
    READER_ROLE_NAME,
    AccessEvaluation,
    AccessRemediation,
    CognitiveServicesResourceId,
    EndpointAccess,
    ModelEndpointCapabilities,
    ModelEndpointStatus,
    utc_now,
)
from mosaic_api.errors import UpstreamAuthorizationError, UpstreamError, UpstreamNotFoundError
from mosaic_api.integrations.aoai.client import CognitiveServicesClient
from mosaic_api.integrations.apim.client import JsonObject
from mosaic_api.integrations.rbac import permits

# Control-plane reads only. Note that `accounts/deployments/read` is an *action*; the similarly
# named `accounts/AIServices/deployments/read` is a dataAction and would grant inference, so it is
# deliberately absent.
MODEL_READ_ACTIONS: tuple[str, ...] = (
    "Microsoft.CognitiveServices/accounts/read",
    "Microsoft.CognitiveServices/accounts/deployments/read",
    "Microsoft.CognitiveServices/accounts/models/read",
    "Microsoft.CognitiveServices/accounts/projects/read",
)


@dataclass(frozen=True)
class EndpointPreflightResult:
    access: EndpointAccess
    capabilities: ModelEndpointCapabilities
    status: ModelEndpointStatus
    account_name: str | None = None
    endpoint_url: str | None = None


def least_privilege_role_definition(scope: str) -> dict[str, Any]:
    """A custom role granting only what MOSAIC needs, offered as an alternative to Reader.

    Reader is recommended because it is built-in and additionally grants
    ``Microsoft.Authorization/roleAssignments/read``, which the gateway runtime check needs. This
    definition is narrower and deliberately omits that action, so an operator who chooses it should
    expect runtime access to report as not evaluated.
    """

    return {
        "properties": {
            "roleName": "MOSAIC Model Deployment Reader",
            "description": (
                "Control-plane enumeration of model deployments only. No data-plane inference, "
                "no key access, no write."
            ),
            "assignableScopes": [scope],
            "permissions": [
                {
                    "actions": list(MODEL_READ_ACTIONS),
                    "notActions": [],
                    "dataActions": [],
                    "notDataActions": [],
                }
            ],
        }
    }


def build_endpoint_remediation(
    resource: CognitiveServicesResourceId, *, principal_id: str | None
) -> AccessRemediation:
    """Name the role, scope, and command an operator needs to run for MOSAIC.

    Reader is chosen over any ``Cognitive Services *`` or ``Foundry *`` role because every one of
    those that grants control-plane deployment read also carries data-plane inference rights, and
    most carry ``accounts/listkeys/action``. MOSAIC needs neither and should not hold either.
    """

    scope = resource.account_scope
    assignee = principal_id or "<mosaic-managed-identity-object-id>"
    command = (
        "az role assignment create"
        f' --assignee-object-id "{assignee}"'
        " --assignee-principal-type ServicePrincipal"
        f' --role "{READER_ROLE_NAME}"'
        f' --scope "{scope}"'
    )
    return AccessRemediation(
        role_name=READER_ROLE_NAME,
        role_definition_id=READER_ROLE_ID,
        scope=scope,
        principal_id=principal_id,
        command=command,
        custom_role_definition=least_privilege_role_definition(scope),
    )


def _capabilities(account: JsonObject | None) -> ModelEndpointCapabilities:
    if not account:
        return ModelEndpointCapabilities(
            notes=["MOSAIC could not read the account description."]
        )
    properties = account.get("properties") if isinstance(account.get("properties"), dict) else {}
    sku = account.get("sku") if isinstance(account.get("sku"), dict) else {}
    kind = account.get("kind")
    sku_name = sku.get("name") if isinstance(sku, dict) else None
    location = account.get("location")
    provisioning_state = properties.get("provisioningState") if properties else None
    public_network_access = properties.get("publicNetworkAccess") if properties else None
    disable_local_auth = properties.get("disableLocalAuth") if properties else None

    notes: list[str] = []
    if disable_local_auth is False:
        notes.append(
            "Key authentication is enabled on this endpoint. Disabling it forces callers, "
            "including the gateway, onto managed identity."
        )
    if isinstance(public_network_access, str) and public_network_access.casefold() == "disabled":
        notes.append(
            "Public network access is disabled. A gateway can only reach this endpoint over a "
            "private connection."
        )

    return ModelEndpointCapabilities(
        kind=kind if isinstance(kind, str) else None,
        sku_name=sku_name if isinstance(sku_name, str) else None,
        location=location if isinstance(location, str) else None,
        provisioning_state=(
            provisioning_state if isinstance(provisioning_state, str) else None
        ),
        public_network_access=(
            public_network_access if isinstance(public_network_access, str) else None
        ),
        local_auth_disabled=(
            disable_local_auth if isinstance(disable_local_auth, bool) else None
        ),
        notes=notes,
    )


def _endpoint_url(account: JsonObject | None) -> str | None:
    if not account:
        return None
    properties = account.get("properties") if isinstance(account.get("properties"), dict) else {}
    if not isinstance(properties, dict):
        return None
    endpoint = properties.get("endpoint")
    if isinstance(endpoint, str) and endpoint:
        return endpoint
    endpoints = properties.get("endpoints")
    if isinstance(endpoints, dict):
        for value in endpoints.values():
            if isinstance(value, str) and value.startswith("https://"):
                return value
    return None


async def run_endpoint_preflight(
    client: CognitiveServicesClient, *, principal_id: str | None = None
) -> EndpointPreflightResult:
    """Read the account, then confirm MOSAIC holds every control-plane action it needs."""

    resource = client.resource
    checked_at = utc_now()

    try:
        account = await client.get_account()
    except UpstreamAuthorizationError as error:
        return EndpointPreflightResult(
            access=EndpointAccess(
                can_read=False,
                evaluation=AccessEvaluation.PROBE,
                checked_at=checked_at,
                missing_actions=list(MODEL_READ_ACTIONS),
                remediation=build_endpoint_remediation(resource, principal_id=principal_id),
                message=(
                    "MOSAIC's managed identity cannot read this Azure AI resource. Grant it the "
                    "role shown below and try again."
                ),
            ),
            capabilities=ModelEndpointCapabilities(notes=[str(error.message)]),
            status=ModelEndpointStatus.UNAUTHORIZED,
        )
    except (UpstreamNotFoundError, UpstreamError) as error:
        return EndpointPreflightResult(
            access=EndpointAccess(
                can_read=False,
                evaluation=AccessEvaluation.PROBE,
                checked_at=checked_at,
                message=str(error.message),
            ),
            capabilities=ModelEndpointCapabilities(notes=[str(error.message)]),
            status=ModelEndpointStatus.UNREACHABLE,
        )

    if account is None:
        return EndpointPreflightResult(
            access=EndpointAccess(
                can_read=False,
                evaluation=AccessEvaluation.PROBE,
                checked_at=checked_at,
                message=(
                    "No Azure AI resource exists at this resource ID, or it is not visible to "
                    "MOSAIC's managed identity."
                ),
            ),
            capabilities=ModelEndpointCapabilities(),
            status=ModelEndpointStatus.UNREACHABLE,
        )

    capabilities = _capabilities(account)
    endpoint_url = _endpoint_url(account)
    permissions = await client.effective_permissions()

    if permissions is None:
        # The account read succeeded, so MOSAIC demonstrably has some access. Reporting that as a
        # probe result is honest about how it was established.
        return EndpointPreflightResult(
            access=EndpointAccess(
                can_read=True,
                evaluation=AccessEvaluation.PROBE,
                checked_at=checked_at,
                message=(
                    "MOSAIC can read this endpoint. It could not evaluate role assignments, so "
                    "the exact permissions it holds are unconfirmed."
                ),
            ),
            capabilities=capabilities,
            status=ModelEndpointStatus.CONNECTED,
            account_name=resource.account_name,
            endpoint_url=endpoint_url,
        )

    missing = [action for action in MODEL_READ_ACTIONS if not permits(permissions, action)]
    if missing:
        return EndpointPreflightResult(
            access=EndpointAccess(
                can_read=False,
                evaluation=AccessEvaluation.EFFECTIVE_PERMISSIONS,
                checked_at=checked_at,
                missing_actions=missing,
                remediation=build_endpoint_remediation(resource, principal_id=principal_id),
                message=(
                    "MOSAIC's managed identity is missing permissions needed to enumerate models "
                    "on this endpoint."
                ),
            ),
            capabilities=capabilities,
            status=ModelEndpointStatus.UNAUTHORIZED,
            account_name=resource.account_name,
            endpoint_url=endpoint_url,
        )

    return EndpointPreflightResult(
        access=EndpointAccess(
            can_read=True,
            evaluation=AccessEvaluation.EFFECTIVE_PERMISSIONS,
            checked_at=checked_at,
            message="MOSAIC can enumerate models on this endpoint.",
        ),
        capabilities=capabilities,
        status=ModelEndpointStatus.CONNECTED,
        account_name=resource.account_name,
        endpoint_url=endpoint_url,
    )
