"""Azure Resource Manager reads for Azure AI (Cognitive Services) accounts.

Every method here is read-only by construction, exactly as ``integrations.apim.client.ApimClient``
is. MOSAIC enumerates model deployments so an administrator can govern them; it never creates,
updates, or deletes a deployment, and it never calls ``listKeys``.

The transport is the shared :class:`~mosaic_api.integrations.apim.client.ArmClient`, so token
acquisition, retry, paging, and error mapping behave identically to gateway onboarding.
"""

from mosaic_api.domain import (
    AUTHORIZATION_API_VERSION,
    COGNITIVE_SERVICES_API_VERSION,
    SUBSCRIPTIONS_API_VERSION,
    CognitiveServicesResourceId,
)
from mosaic_api.errors import UpstreamAuthorizationError, UpstreamError
from mosaic_api.integrations.apim.client import ArmClient, JsonObject


class CognitiveServicesClient:
    """Reads one Azure AI account. Deployments are always read at the account scope.

    A Foundry project is not a deployment container: ``accounts/{account}/projects/{project}``
    carries only descriptive properties. When an administrator registers a project, this client
    still enumerates models at the owning account.
    """

    def __init__(self, arm: ArmClient, resource: CognitiveServicesResourceId) -> None:
        self._arm = arm
        self._resource = resource
        self._account = resource.account_scope
        self._params = {"api-version": COGNITIVE_SERVICES_API_VERSION}

    @property
    def resource(self) -> CognitiveServicesResourceId:
        return self._resource

    async def get_account(self) -> JsonObject | None:
        return await self._arm.get(self._account, params=self._params, allow_not_found=True)

    async def list_deployments(self) -> list[JsonObject]:
        return await self._arm.list(
            f"{self._account}/deployments", params=self._params, allow_not_found=True
        )

    async def list_models(self) -> list[JsonObject]:
        return await self._arm.list(
            f"{self._account}/models", params=self._params, allow_not_found=True
        )

    async def list_projects(self) -> list[JsonObject]:
        return await self._arm.list(
            f"{self._account}/projects", params=self._params, allow_not_found=True
        )

    async def effective_permissions(self) -> list[JsonObject] | None:
        """What MOSAIC's own identity may do here, or ``None`` if that cannot be evaluated."""

        url = f"{self._resource.canonical}/providers/Microsoft.Authorization/permissions"
        try:
            return await self._arm.list(
                url, params={"api-version": AUTHORIZATION_API_VERSION}, allow_not_found=True
            )
        except (UpstreamAuthorizationError, UpstreamError):
            return None

    async def role_assignments_for_principal(self, principal_id: str) -> list[JsonObject] | None:
        """Role assignments held by another principal that are visible at this scope.

        ``$filter=principalId eq`` returns assignments at, **above, and below** the scope, so the
        caller must inspect ``properties.scope`` before claiming an assignment is direct. Returns
        ``None`` when MOSAIC lacks ``Microsoft.Authorization/roleAssignments/read``, which must be
        reported as "not evaluated" rather than as "no access".
        """

        url = f"{self._resource.canonical}/providers/Microsoft.Authorization/roleAssignments"
        try:
            return await self._arm.list(
                url,
                params={
                    "api-version": AUTHORIZATION_API_VERSION,
                    "$filter": f"principalId eq '{principal_id}'",
                },
                allow_not_found=True,
            )
        except (UpstreamAuthorizationError, UpstreamError):
            return None


class SubscriptionScanner:
    """Enumerates Azure AI accounts across the subscriptions MOSAIC can see.

    Each subscription is read independently: one inaccessible subscription records what to grant
    and is skipped, rather than failing the whole scan.
    """

    def __init__(self, arm: ArmClient) -> None:
        self._arm = arm

    async def list_subscriptions(self) -> list[JsonObject]:
        return await self._arm.list(
            "/subscriptions",
            params={"api-version": SUBSCRIPTIONS_API_VERSION},
            allow_not_found=True,
        )

    async def list_accounts(self, subscription_id: str) -> list[JsonObject]:
        return await self._arm.list(
            f"/subscriptions/{subscription_id}/providers/Microsoft.CognitiveServices/accounts",
            params={"api-version": COGNITIVE_SERVICES_API_VERSION},
            allow_not_found=True,
        )
