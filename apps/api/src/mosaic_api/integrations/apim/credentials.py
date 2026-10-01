"""Explicit, authorized credential reads and key changes; inventory never uses these clients."""

from typing import Any, Literal
from urllib.parse import quote

from pydantic import SecretStr

from mosaic_api.domain import APIM_API_VERSION, ApimResourceId
from mosaic_api.errors import ConflictError, UpstreamAuthorizationError, UpstreamError
from mosaic_api.integrations.apim.client import ApimClient, ArmClient
from mosaic_api.integrations.apim.writer import ApimWriter


class ApimCredentialClient:
    def __init__(self, arm: ArmClient, resource: ApimResourceId) -> None:
        self._arm = arm
        self._resource = resource
        self._metadata = ApimClient(arm, resource)

    async def read_key(
        self, subscription_name: str, api_name: str, slot: Literal["primary", "secondary"]
    ) -> SecretStr:
        expected_scopes = {
            f"{self._resource.canonical}/apis/{api_name}".casefold(),
            f"/apis/{api_name}".casefold(),
        }
        subscription = await self._metadata.get_subscription(quote(subscription_name, safe=""))
        properties = subscription.get("properties") if subscription is not None else None
        if not isinstance(properties, dict):
            raise ConflictError("The managed subscription is no longer available in API Management")
        scope = properties.get("scope")
        if (
            properties.get("state") != "active"
            or not isinstance(scope, str)
            or scope.casefold() not in expected_scopes
        ):
            raise ConflictError(
                "The subscription is not active at the expected model API scope. Re-plan access."
            )

        path = (
            f"{self._resource.canonical}/subscriptions/"
            f"{quote(subscription_name, safe='')}/listSecrets"
        )
        try:
            payload = await self._arm.post_sensitive(
                path, params={"api-version": APIM_API_VERSION}
            )
        except UpstreamAuthorizationError:
            raise UpstreamAuthorizationError(
                "MOSAIC can only reveal keys when its identity has subscription key-read "
                "permission on this gateway. Write permission alone is not sufficient.",
                details={
                    "missingAction": (
                        "Microsoft.ApiManagement/service/subscriptions/listSecrets/action"
                    ),
                    "scope": self._resource.canonical,
                },
            ) from None
        value = payload.get(f"{slot}Key")
        if not isinstance(value, str) or not value or len(value) > 256:
            raise UpstreamError("API Management returned an invalid subscription key response")
        return SecretStr(value)


class ApimKeyManager:
    """Creates, rotates and deletes a grant's key on an explicit request. It never reads a key.

    A grant's key is an API-scoped subscription whose name the grant fixes, so the gateway's
    policy recognizes it the moment it exists.
    """

    def __init__(self, arm: ArmClient, resource: ApimResourceId) -> None:
        self._metadata = ApimClient(arm, resource)
        self._writer = ApimWriter(arm, resource)

    async def get_subscription(self, name: str) -> dict[str, Any] | None:
        return await self._metadata.get_subscription(quote(name, safe=""))

    async def create(self, name: str, *, display_name: str, api_name: str) -> None:
        await self._writer.put_api_subscription(
            name, display_name=display_name, api_name=api_name, state="active"
        )

    async def regenerate(self, name: str, slot: Literal["primary", "secondary"]) -> None:
        await self._writer.regenerate_subscription_key(name, slot)

    async def delete(self, name: str) -> None:
        await self._writer.delete_subscription(name)
