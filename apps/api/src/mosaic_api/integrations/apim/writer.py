"""API Management writes.

Deliberately a separate class from :class:`~mosaic_api.integrations.apim.client.ApimClient`, whose
contract is that every method on it is read-only by construction. Mixing writes into that class
would quietly retire a property the rest of the codebase relies on when reasoning about blast
radius, so the two stay apart and a caller has to hold a writer on purpose.

Every method here is idempotent: a plan step may be re-applied after a partial failure, and a
delete of something already gone is a no-op rather than an error that masks the real one.
"""

from typing import Any, Literal

from mosaic_api.domain import (
    APIM_API_VERSION,
    APIM_LLM_DIAGNOSTIC_API_VERSION,
    APIM_MCP_API_VERSION,
    ApimResourceId,
)
from mosaic_api.errors import DomainError, UpstreamError
from mosaic_api.integrations.apim.client import ArmClient, JsonObject
from mosaic_api.integrations.apim.diagnostics import AZURE_MONITOR, azure_monitor_logger_payload
from mosaic_api.integrations.backend_keys import named_value_properties

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

    async def put_backend(
        self,
        name: str,
        *,
        url: str,
        title: str,
        circuit_breaker: JsonObject | None = None,
    ) -> JsonObject | None:
        properties: JsonObject = {"title": title, "url": url, "protocol": "http"}
        if circuit_breaker is not None:
            properties["circuitBreaker"] = circuit_breaker
        return await self._put(f"backends/{name}", {"properties": properties})

    async def put_backend_pool(
        self, name: str, *, title: str, services: list[tuple[str, int, int]]
    ) -> JsonObject | None:
        """Create or replace a load-balanced pool over member backends.

        ``services`` holds ``(backend name, priority, weight)``. API Management routes to the
        lowest priority with an available member, and between members of one priority by weight.
        A member whose circuit breaker has tripped is unavailable until it closes again. Deleting
        a pool is :meth:`delete_backend`, because a pool is a backend.
        """

        return await self._put(
            f"backends/{name}",
            {
                "properties": {
                    "title": title,
                    "type": "Pool",
                    "pool": {
                        "services": [
                            {
                                "id": self.resource_id(f"backends/{member}"),
                                "priority": priority,
                                "weight": weight,
                            }
                            for member, priority, weight in services
                        ]
                    },
                }
            },
        )

    async def delete_backend(self, name: str) -> bool:
        return await self._delete(f"backends/{name}")

    async def put_named_value(self, name: str, *, secret_identifier: str) -> JsonObject | None:
        """Create or replace a Key Vault-backed named value and confirm the gateway resolves it.

        API Management reads the secret itself, with its system-assigned managed identity. MOSAIC
        sends only the identifier, keeps it out of any error Azure echoes it in, and never reads the
        value back. A named value API Management couldn't resolve is removed again rather than left
        for a policy to reference: every call through it would fail.
        """

        reference = secret_identifier.split("://", 1)[-1]
        segment = f"namedValues/{name}"
        written = await self._arm.put(
            self.resource_id(segment),
            {"properties": named_value_properties(name, secret_identifier)},
            params=self._params,
            redact=(reference,),
        )
        current = await self._arm.get(
            self.resource_id(segment), params=self._params, allow_not_found=True
        )
        properties = (current or {}).get("properties")
        key_vault = properties.get("keyVault") if isinstance(properties, dict) else None
        status = key_vault.get("lastStatus") if isinstance(key_vault, dict) else None
        code = status.get("code") if isinstance(status, dict) else None
        if isinstance(code, str) and code.strip() and code.strip().casefold() != "success":
            try:
                await self._delete(segment)
                outcome = "MOSAIC removed it again."
            except DomainError:
                outcome = "MOSAIC couldn't remove it, so delete it before trying again."
            raise UpstreamError(
                f"API Management created named value {name} but couldn't read the key from Key "
                f"Vault ({code.strip()[:80]}). {outcome} Grant the gateway's managed identity Key "
                "Vault Secrets User on the vault, and make sure the vault's firewall lets API "
                "Management through.",
                details={"namedValue": name, "code": code.strip()[:80]},
            )
        return written

    async def delete_named_value(self, name: str) -> bool:
        return await self._delete(f"namedValues/{name}")

    async def put_plain_named_value(self, name: str, value: str) -> JsonObject | None:
        """Create or replace a named value that holds plain text, such as the blocked list.

        Never a secret: API Management shows a plain named value's text to anyone who may read
        the service, and MOSAIC reads it back to check what a gateway holds. See ADR 0023.
        """

        return await self._put(
            f"namedValues/{name}",
            {
                "properties": {
                    "displayName": name,
                    "value": value,
                    "secret": False,
                    "tags": ["mosaic"],
                }
            },
        )

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
        template_parameters: list[str] | None = None,
    ) -> JsonObject | None:
        """Create or replace an operation. Every ``{name}`` in ``url_template`` must be listed."""

        return await self._put(
            f"apis/{api_name}/operations/{name}",
            {
                "properties": {
                    "displayName": display_name,
                    "method": method,
                    "urlTemplate": url_template,
                    "description": description,
                    "templateParameters": [
                        {"name": parameter, "type": "string", "required": True}
                        for parameter in template_parameters or []
                    ],
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

    async def put_api_operation_policy(
        self, api_name: str, operation_name: str, value: str
    ) -> JsonObject | None:
        return await self._put(
            f"apis/{api_name}/operations/{operation_name}/policies/policy",
            {"properties": {"format": "rawxml", "value": value}},
        )

    async def delete_api_operation_policy(self, api_name: str, operation_name: str) -> bool:
        return await self._delete(f"apis/{api_name}/operations/{operation_name}/policies/policy")

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
        API Management then forwards each call to the backend's URL with ``/mcp`` added, as
        observed live, so the backend must point at the server's endpoint without its final
        ``/mcp``. :func:`~mosaic_api.domain.mcp_backend_url` derives that URL.
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

    async def regenerate_subscription_key(
        self, name: str, slot: Literal["primary", "secondary"]
    ) -> None:
        """Give a grant's key a new value in one slot. The other slot keeps working meanwhile."""

        action = "regeneratePrimaryKey" if slot == "primary" else "regenerateSecondaryKey"
        await self._arm.post_action(
            self.resource_id(f"subscriptions/{name}/{action}"), params=self._params
        )

    async def put_azure_monitor_logger(self) -> JsonObject | None:
        """Create the logger through which API diagnostics feed the service's resource logs."""

        return await self._put(f"loggers/{AZURE_MONITOR}", azure_monitor_logger_payload())

    async def put_api_diagnostic(self, api_name: str, payload: JsonObject) -> JsonObject | None:
        """Upsert an API's Azure Monitor diagnostic, on the contract that knows LLM logging."""

        return await self._arm.put(
            self.resource_id(f"apis/{api_name}/diagnostics/{AZURE_MONITOR}"),
            payload,
            params={"api-version": APIM_LLM_DIAGNOSTIC_API_VERSION},
        )
