"""API Management writes.

Deliberately a separate class from :class:`~mosaic_api.integrations.apim.client.ApimClient`, whose
contract is that every method on it is read-only by construction. Mixing writes into that class
would quietly retire a property the rest of the codebase relies on when reasoning about blast
radius, so the two stay apart and a caller has to hold a writer on purpose.

Every method here is idempotent: a plan step may be re-applied after a partial failure, and a
delete of something already gone is a no-op rather than an error that masks the real one.
"""

from typing import Any, Literal

from mosaic_api.domain import APIM_API_VERSION, APIM_MCP_API_VERSION, ApimResourceId
from mosaic_api.integrations.apim.client import ArmClient, JsonObject

# API Management requires an If-Match header on deletes. MOSAIC sends "*" rather than a captured
# ETag: rollback must remove what this apply created even if something touched it since, and
# refusing to clean up after itself would be the worse failure.
DELETE_IF_MATCH = "*"
DEFAULT_SUBSCRIPTION_KEY_NAMES = {
    "header": "Ocp-Apim-Subscription-Key",
    "query": "subscription-key",
}


class ApimWriter:
    """Create, replace, and delete the API Management resources a publication owns."""

    def __init__(self, arm: ArmClient, resource: ApimResourceId) -> None:
        self._arm = arm
        self._resource = resource
        self._base = resource.canonical
        self._params = {"api-version": APIM_API_VERSION}
        self._mcp_params = {"api-version": APIM_MCP_API_VERSION}

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
        use_default_subscription_key_names: bool = False,
    ) -> JsonObject | None:
        """Create the API with no ``serviceUrl``.

        Routing lives entirely in the MOSAIC fragment's ``set-backend-service``. An API that also
        carried its own service URL would keep forwarding traffic if the fragment include were
        removed, which is a governance control that fails open. This one fails closed.
        """

        properties: JsonObject = {
            "displayName": display_name,
            "description": description,
            "path": path,
            "protocols": ["https"],
            "subscriptionRequired": subscription_required,
        }
        if use_default_subscription_key_names:
            properties["subscriptionKeyParameterNames"] = dict(DEFAULT_SUBSCRIPTION_KEY_NAMES)
        return await self._put(f"apis/{name}", {"properties": properties})

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

    async def put_mcp_api(
        self,
        name: str,
        *,
        display_name: str,
        path: str,
        backend_name: str,
        subscription_required: bool,
        description: str,
    ) -> JsonObject | None:
        """Create or replace a passthrough MCP server that forwards to ``backend_name``.

        Written on the preview contract, the only one that knows the ``mcp`` API type. The
        ``endpoints`` map is left out so API Management serves the streamable message endpoint at
        its default, ``/mcp``: the published schema and the live service disagree about its shape.
        """

        return await self._arm.put(
            self.resource_id(f"apis/{name}"),
            {
                "properties": {
                    "type": "mcp",
                    "displayName": display_name,
                    "description": description,
                    "path": path,
                    "protocols": ["https"],
                    "backendId": backend_name,
                    "subscriptionRequired": subscription_required,
                    "mcpProperties": {"transportType": "streamable"},
                }
            },
            params=self._mcp_params,
        )

    async def delete_mcp_api(self, name: str) -> bool:
        return await self._arm.delete(
            self.resource_id(f"apis/{name}"),
            params=self._mcp_params,
            if_match=DELETE_IF_MATCH,
        )

    async def put_mcp_api_policy(self, api_name: str, value: str) -> JsonObject | None:
        return await self._arm.put(
            self.resource_id(f"apis/{api_name}/policies/policy"),
            {"properties": {"format": "rawxml", "value": value}},
            params=self._mcp_params,
        )

    async def delete_mcp_api_policy(self, api_name: str) -> bool:
        return await self._arm.delete(
            self.resource_id(f"apis/{api_name}/policies/policy"),
            params=self._mcp_params,
            if_match=DELETE_IF_MATCH,
        )

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
        self,
        name: str,
        *,
        display_name: str,
        product_name: str,
        state: Literal["active", "suspended"] = "active",
    ) -> JsonObject | None:
        """Upsert a legacy product subscription without reading or rotating its keys."""

        return await self._put(
            f"subscriptions/{name}",
            {
                "properties": {
                    "displayName": display_name,
                    "scope": self.resource_id(f"products/{product_name}"),
                    "state": state,
                    "allowTracing": False,
                }
            },
        )

    async def put_api_subscription(
        self,
        name: str,
        *,
        display_name: str,
        api_name: str,
        state: Literal["active", "suspended"] = "suspended",
    ) -> JsonObject | None:
        """Upsert a grant's API-scoped subscription, preserving existing primary/secondary keys."""

        return await self._put(
            f"subscriptions/{name}",
            {
                "properties": {
                    "displayName": display_name,
                    "scope": self.resource_id(f"apis/{api_name}"),
                    "state": state,
                    "allowTracing": False,
                }
            },
        )

    async def delete_subscription(self, name: str) -> bool:
        return await self._delete(f"subscriptions/{name}")
