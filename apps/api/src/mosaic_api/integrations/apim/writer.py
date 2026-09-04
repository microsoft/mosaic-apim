"""API Management writes.

Deliberately a separate class from :class:`~mosaic_api.integrations.apim.client.ApimClient`, whose
contract is that every method on it is read-only by construction. Mixing writes into that class
would quietly retire a property the rest of the codebase relies on when reasoning about blast
radius, so the two stay apart and a caller has to hold a writer on purpose.

Every method here is idempotent: a plan step may be re-applied after a partial failure, and a
delete of something already gone is a no-op rather than an error that masks the real one.
"""

from typing import Any

from mosaic_api.domain import APIM_API_VERSION, ApimResourceId
from mosaic_api.integrations.apim.client import ArmClient, JsonObject

# API Management requires an If-Match header on deletes. MOSAIC sends "*" rather than a captured
# ETag: rollback must remove what this apply created even if something touched it since, and
# refusing to clean up after itself would be the worse failure.
DELETE_IF_MATCH = "*"


class ApimWriter:
    """Create, replace, and delete the API Management resources a publication owns."""

    def __init__(self, arm: ArmClient, resource: ApimResourceId) -> None:
        self._arm = arm
        self._resource = resource
        self._base = resource.canonical
        self._params = {"api-version": APIM_API_VERSION}

    @property
    def resource(self) -> ApimResourceId:
        return self._resource

    def resource_id(self, segment: str) -> str:
        return f"{self._base}/{segment}"

    async def _put(self, segment: str, payload: JsonObject) -> JsonObject | None:
        return await self._arm.put(self.resource_id(segment), payload, params=self._params)

    async def _delete(self, segment: str, **extra: str) -> bool:
        return await self._arm.delete(
            self.resource_id(segment),
            params={**self._params, **extra},
            if_match=DELETE_IF_MATCH,
        )

    async def put_policy_fragment(
        self, name: str, value: str, *, description: str
    ) -> JsonObject | None:
        return await self._put(
            f"policyFragments/{name}",
            {"properties": {"description": description, "format": "rawxml", "value": value}},
        )

    async def delete_policy_fragment(self, name: str) -> bool:
        return await self._delete(f"policyFragments/{name}")

    async def put_backend(self, name: str, *, url: str, title: str) -> JsonObject | None:
        return await self._put(
            f"backends/{name}",
            {"properties": {"title": title, "url": url, "protocol": "http"}},
        )

    async def delete_backend(self, name: str) -> bool:
        return await self._delete(f"backends/{name}")

    async def put_api(
        self,
        name: str,
        *,
        display_name: str,
        path: str,
        subscription_required: bool,
        description: str,
    ) -> JsonObject | None:
        """Create the API with no ``serviceUrl``.

        Routing lives entirely in the MOSAIC fragment's ``set-backend-service``. An API that also
        carried its own service URL would keep forwarding traffic if the fragment include were
        removed, which is a governance control that fails open. This one fails closed.
        """

        return await self._put(
            f"apis/{name}",
            {
                "properties": {
                    "displayName": display_name,
                    "description": description,
                    "path": path,
                    "protocols": ["https"],
                    "subscriptionRequired": subscription_required,
                }
            },
        )

    async def delete_api(self, name: str) -> bool:
        return await self._delete(f"apis/{name}")

    async def put_api_operation(
        self,
        api_name: str,
        name: str,
        *,
        display_name: str,
        method: str,
        url_template: str,
        description: str,
    ) -> JsonObject | None:
        return await self._put(
            f"apis/{api_name}/operations/{name}",
            {
                "properties": {
                    "displayName": display_name,
                    "method": method,
                    "urlTemplate": url_template,
                    "description": description,
                    "templateParameters": [],
                }
            },
        )

    async def delete_api_operation(self, api_name: str, name: str) -> bool:
        return await self._delete(f"apis/{api_name}/operations/{name}")

    async def put_api_policy(self, api_name: str, value: str) -> JsonObject | None:
        return await self._put(
            f"apis/{api_name}/policies/policy",
            {"properties": {"format": "rawxml", "value": value}},
        )

    async def delete_api_policy(self, api_name: str) -> bool:
        return await self._delete(f"apis/{api_name}/policies/policy")

    async def put_product(
        self,
        name: str,
        *,
        display_name: str,
        description: str,
        subscription_required: bool,
    ) -> JsonObject | None:
        properties: dict[str, Any] = {
            "displayName": display_name,
            "description": description,
            "state": "published",
            "subscriptionRequired": subscription_required,
        }
        if subscription_required:
            # API Management rejects approvalRequired outright when subscriptions are not required,
            # so it is only ever sent alongside the flag that makes it meaningful.
            properties["approvalRequired"] = False
        return await self._put(f"products/{name}", {"properties": properties})

    async def delete_product(self, name: str) -> bool:
        return await self._delete(f"products/{name}", deleteSubscriptions="true")

    async def put_product_api(self, product_name: str, api_name: str) -> JsonObject | None:
        return await self._put(f"products/{product_name}/apis/{api_name}", {})

    async def delete_product_api(self, product_name: str, api_name: str) -> bool:
        return await self._delete(f"products/{product_name}/apis/{api_name}")

    async def put_subscription(
        self, name: str, *, display_name: str, product_name: str
    ) -> JsonObject | None:
        """Create a subscription without ever reading its keys.

        ``API Management Service Contributor`` grants ``subscriptions/listSecrets/action``, so
        MOSAIC could read the primary key here. It does not: the operator retrieves it from Azure.
        See ADR 0010 — this is a product policy, not a permission boundary, and it is worth being
        precise about which of the two it is.
        """

        return await self._put(
            f"subscriptions/{name}",
            {
                "properties": {
                    "displayName": display_name,
                    "scope": self.resource_id(f"products/{product_name}"),
                    "state": "active",
                    "allowTracing": False,
                }
            },
        )

    async def delete_subscription(self, name: str) -> bool:
        return await self._delete(f"subscriptions/{name}")
