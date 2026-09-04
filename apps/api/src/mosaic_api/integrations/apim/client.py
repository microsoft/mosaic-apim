"""Azure Resource Manager client for API Management.

MOSAIC talks to ARM over REST rather than shelling out to the Azure CLI: the API container stays
free of the CLI, calls stay async inside request handling, and the App Service managed identity is
the only credential path.
"""

import asyncio
from types import TracebackType
from typing import Any, Self

import httpx
import jwt
import structlog
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError

from mosaic_api.domain import (
    APIM_API_VERSION,
    APIM_MCP_API_VERSION,
    AUTHORIZATION_API_VERSION,
    ApimResourceId,
)
from mosaic_api.errors import (
    UpstreamAuthorizationError,
    UpstreamConflictError,
    UpstreamError,
    UpstreamNotFoundError,
    UpstreamUnsupportedError,
)

logger = structlog.get_logger()

ARM_SCOPE = "https://management.azure.com/.default"
ARM_BASE_URL = "https://management.azure.com"
MAX_ATTEMPTS = 4
MAX_RETRY_DELAY_SECONDS = 20.0
MAX_POLL_ATTEMPTS = 60
DEFAULT_POLL_DELAY_SECONDS = 2.0

_TERMINAL_OPERATION_STATES: frozenset[str] = frozenset({"succeeded"})
_FAILED_OPERATION_STATES: frozenset[str] = frozenset({"failed", "canceled", "cancelled"})

# ARM signals "this service does not speak that contract" through a small set of error codes. The
# code is matched first because it is stable; the message is only a fallback for services that
# return a bare description.
_UNSUPPORTED_VERSION_CODES: frozenset[str] = frozenset(
    {
        "invalidapiversionparameter",
        "noregisteredproviderfound",
        "unsupportedapiversion",
        "invalidresourcetype",
    }
)
_UNSUPPORTED_VERSION_MARKERS: tuple[str, ...] = ("api-version", "api version")

JsonObject = dict[str, Any]


class ArmClient:
    """Thin ARM transport: token acquisition, retry, paging, and typed error mapping."""

    def __init__(
        self,
        credential: AsyncTokenCredential,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = ARM_BASE_URL,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._credential = credential
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._owns_client = client is None
        self._sleep = sleep

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def caller_object_id(self) -> str | None:
        """The object ID of the identity MOSAIC calls Azure with.

        Read from the ``oid`` claim of MOSAIC's own access token. This cannot come from Bicep: the
        API's app settings are an input to the web app that owns the identity, so injecting the
        principal ID there would be circular. The token is not verified because MOSAIC issued the
        request for it and only uses the value to build a role-assignment command for an operator.
        """

        try:
            token = await self._credential.get_token(ARM_SCOPE)
            claims = jwt.decode(token.token, options={"verify_signature": False})
        except (ClientAuthenticationError, jwt.PyJWTError, ValueError):
            return None
        object_id = claims.get("oid")
        return object_id if isinstance(object_id, str) and object_id else None

    async def _authorization_header(self) -> str:
        try:
            token = await self._credential.get_token(ARM_SCOPE)
        except ClientAuthenticationError as error:
            raise UpstreamAuthorizationError(
                "MOSAIC could not acquire an Azure Resource Manager token",
                details={"reason": str(error)},
            ) from error
        return f"Bearer {token.token}"

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return min(float(header), MAX_RETRY_DELAY_SECONDS)
            except ValueError:
                pass
        return min(2.0**attempt, MAX_RETRY_DELAY_SECONDS)

    @staticmethod
    def _upstream_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.reason_phrase or f"HTTP {response.status_code}"
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        return response.reason_phrase or f"HTTP {response.status_code}"

    @staticmethod
    def _upstream_code(response: httpx.Response) -> str | None:
        """The machine-readable ARM error code, which is far steadier than its message text."""

        try:
            payload = response.json()
        except ValueError:
            return None
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, str) and code:
                return code
        return None

    async def request(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> httpx.Response | None:
        return await self._send("GET", url, params=params, allow_not_found=allow_not_found)

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json: JsonObject | None = None,
        if_match: str | None = None,
        allow_not_found: bool = False,
    ) -> httpx.Response | None:
        target = url if url.startswith("http") else f"{self._base_url}{url}"
        last_error: str = "unknown error"
        for attempt in range(MAX_ATTEMPTS):
            headers = {
                "Authorization": await self._authorization_header(),
                "Accept": "application/json",
            }
            if if_match is not None:
                headers["If-Match"] = if_match
            try:
                response = await self._client.request(
                    method, target, params=params, json=json, headers=headers
                )
            except httpx.HTTPError as error:
                last_error = str(error)
                if attempt == MAX_ATTEMPTS - 1:
                    raise UpstreamError(
                        "MOSAIC could not reach Azure Resource Manager",
                        details={"url": target, "reason": last_error},
                    ) from error
                await self._sleep(min(2.0**attempt, MAX_RETRY_DELAY_SECONDS))
                continue

            if response.status_code in {401, 403}:
                raise UpstreamAuthorizationError(
                    "MOSAIC's identity is not authorized for this Azure resource",
                    details={"url": target, "reason": self._upstream_detail(response)},
                )
            if response.status_code == 404:
                if allow_not_found:
                    return None
                raise UpstreamNotFoundError(
                    "The Azure resource was not found",
                    details={"url": target, "reason": self._upstream_detail(response)},
                )
            if response.status_code == 412:
                raise UpstreamConflictError(
                    "The Azure resource changed since MOSAIC last read it",
                    details={"url": target, "reason": self._upstream_detail(response)},
                )
            if response.status_code == 429 or response.status_code >= 500:
                last_error = self._upstream_detail(response)
                if attempt == MAX_ATTEMPTS - 1:
                    break
                delay = self._retry_delay(response, attempt)
                logger.warning(
                    "arm_request_retry",
                    url=target,
                    method=method,
                    status_code=response.status_code,
                    delay_seconds=delay,
                )
                await self._sleep(delay)
                continue
            if response.status_code >= 400:
                raise UpstreamError(
                    "Azure Resource Manager rejected the request",
                    details={
                        "url": target,
                        "statusCode": response.status_code,
                        "code": self._upstream_code(response),
                        "reason": self._upstream_detail(response),
                    },
                )
            return response

        raise UpstreamError(
            "Azure Resource Manager did not return a usable response",
            details={"url": target, "reason": last_error},
        )

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> JsonObject | None:
        response = await self.request(url, params=params, allow_not_found=allow_not_found)
        if response is None:
            return None
        try:
            payload = response.json()
        except ValueError as error:
            raise UpstreamError(
                "Azure Resource Manager returned a response MOSAIC could not parse",
                details={"url": url},
            ) from error
        return payload if isinstance(payload, dict) else {"value": payload}

    async def list(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        allow_not_found: bool = False,
        max_pages: int = 50,
    ) -> list[JsonObject]:
        items: list[JsonObject] = []
        next_url: str | None = url
        next_params = params
        pages = 0
        while next_url and pages < max_pages:
            payload = await self.get(
                next_url, params=next_params, allow_not_found=allow_not_found
            )
            if payload is None:
                return items
            values = payload.get("value")
            if isinstance(values, list):
                items.extend(item for item in values if isinstance(item, dict))
            link = payload.get("nextLink")
            next_url = link if isinstance(link, str) and link else None
            next_params = None
            pages += 1
        if next_url:
            logger.warning("arm_paging_truncated", url=url, pages=pages)
        return items

    async def _await_operation(self, response: httpx.Response) -> None:
        """Poll an Azure long-running operation to completion.

        A write that returns 202 has not happened yet. Treating it as done would make the *next*
        plan step fail against a resource that does not exist, and the reported cause would be the
        wrong step entirely.

        The two polling styles are handled together. An ``Azure-AsyncOperation`` endpoint answers
        200 with a ``status`` body throughout, so completion is decided by that status and never by
        the response code. A ``Location`` endpoint answers 202 while running and then returns the
        resource itself, which carries no operation status at all.
        """

        poll_url = response.headers.get("Azure-AsyncOperation") or response.headers.get("Location")
        if not poll_url:
            return
        delay = self._poll_delay(response, DEFAULT_POLL_DELAY_SECONDS)
        for _ in range(MAX_POLL_ATTEMPTS):
            await self._sleep(delay)
            polled = await self._send("GET", poll_url, allow_not_found=True)
            if polled is None:
                return
            state = self._operation_state(polled)
            if state in _FAILED_OPERATION_STATES:
                raise UpstreamError(
                    "The Azure operation did not succeed",
                    details={"url": poll_url, "status": state},
                )
            if state in _TERMINAL_OPERATION_STATES:
                return
            if not state and polled.status_code != 202:
                return
            delay = self._poll_delay(polled, delay)

        raise UpstreamError(
            "The Azure operation did not complete in time",
            details={"url": poll_url},
        )

    @staticmethod
    def _poll_delay(response: httpx.Response, fallback: float) -> float:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return min(float(header), MAX_RETRY_DELAY_SECONDS)
            except ValueError:
                pass
        return min(fallback, MAX_RETRY_DELAY_SECONDS)

    @staticmethod
    def _operation_state(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        state = payload.get("status") or payload.get("properties", {}).get("provisioningState")
        return str(state).casefold() if isinstance(state, str) else ""

    async def put(
        self,
        url: str,
        payload: JsonObject,
        *,
        params: dict[str, str] | None = None,
        if_match: str | None = None,
    ) -> JsonObject | None:
        """Create or replace a resource. Idempotent, so the shared retry policy is safe."""

        response = await self._send("PUT", url, params=params, json=payload, if_match=if_match)
        if response is None:
            return None
        if response.status_code in {201, 202}:
            await self._await_operation(response)
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    async def delete(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        if_match: str | None = None,
    ) -> bool:
        """Remove a resource. Returns whether it was there to remove.

        A 404 is success, not an error: rollback and unpublish both need deleting something already
        gone to be a no-op rather than a failure that masks the real one.
        """

        response = await self._send(
            "DELETE", url, params=params, if_match=if_match, allow_not_found=True
        )
        if response is None:
            return False
        if response.status_code == 202:
            await self._await_operation(response)
        return response.status_code != 204


class ApimClient:
    """API Management reads. Every method on this class is read-only by construction."""

    def __init__(self, arm: ArmClient, resource: ApimResourceId) -> None:
        self._arm = arm
        self._resource = resource
        self._base = resource.canonical
        self._params = {"api-version": APIM_API_VERSION}
        self._mcp_params = {"api-version": APIM_MCP_API_VERSION}

    @property
    def resource(self) -> ApimResourceId:
        return self._resource

    async def get_service(self) -> JsonObject | None:
        return await self._arm.get(self._base, params=self._params, allow_not_found=True)

    async def effective_permissions(self) -> list[JsonObject] | None:
        url = f"{self._base}/providers/Microsoft.Authorization/permissions"
        try:
            return await self._arm.list(
                url,
                params={"api-version": AUTHORIZATION_API_VERSION},
                allow_not_found=True,
            )
        except (UpstreamAuthorizationError, UpstreamError):
            return None

    async def _collection(self, segment: str, **extra: str) -> list[JsonObject]:
        return await self._arm.list(
            f"{self._base}/{segment}",
            params={**self._params, **extra},
            allow_not_found=True,
        )

    async def list_apis(self) -> list[JsonObject]:
        return await self._collection("apis")

    async def list_operations(self, api_name: str) -> list[JsonObject]:
        return await self._collection(f"apis/{api_name}/operations")

    async def list_api_products(self, api_name: str) -> list[JsonObject]:
        return await self._collection(f"apis/{api_name}/products")

    async def list_products(self) -> list[JsonObject]:
        return await self._collection("products")

    async def list_product_apis(self, product_name: str) -> list[JsonObject]:
        return await self._collection(f"products/{product_name}/apis")

    async def list_subscriptions(self) -> list[JsonObject]:
        return await self._collection("subscriptions")

    async def list_users(self) -> list[JsonObject]:
        return await self._collection("users")

    async def list_user_groups(self, user_name: str) -> list[JsonObject]:
        return await self._collection(f"users/{user_name}/groups")

    async def list_groups(self) -> list[JsonObject]:
        return await self._collection("groups")

    async def list_group_users(self, group_name: str) -> list[JsonObject]:
        return await self._collection(f"groups/{group_name}/users")

    async def list_backends(self) -> list[JsonObject]:
        return await self._collection("backends")

    async def list_named_values(self) -> list[JsonObject]:
        return await self._collection("namedValues")

    @staticmethod
    def _is_unsupported_version(error: UpstreamError) -> bool:
        if error.details.get("statusCode") not in {400, 404}:
            return False
        code = str(error.details.get("code") or "").casefold()
        if code in _UNSUPPORTED_VERSION_CODES:
            return True
        reason = str(error.details.get("reason") or "").casefold()
        return any(marker in reason for marker in _UNSUPPORTED_VERSION_MARKERS)

    async def _mcp_collection(self, segment: str, **extra: str) -> list[JsonObject]:
        """Read an MCP sub-resource on the preview contract.

        A service that does not implement the preview version raises
        ``UpstreamUnsupportedError`` so the caller can record an absent capability. Every other
        failure propagates unchanged, because "MOSAIC could not read this" and "this gateway has no
        MCP servers" must never collapse into the same answer.
        """

        try:
            return await self._arm.list(
                f"{self._base}/{segment}",
                params={**self._mcp_params, **extra},
                allow_not_found=True,
            )
        except UpstreamError as error:
            if self._is_unsupported_version(error):
                raise UpstreamUnsupportedError(
                    "This API Management service does not support MCP servers",
                    details={
                        "apiVersion": APIM_MCP_API_VERSION,
                        "reason": error.details.get("reason"),
                    },
                ) from error
            raise

    async def list_mcp_servers(self) -> list[JsonObject]:
        return await self._mcp_collection("apis", **{"$filter": "type eq 'mcp'"})

    async def list_mcp_tools(self, mcp_server_name: str) -> list[JsonObject]:
        return await self._mcp_collection(f"apis/{mcp_server_name}/tools")

    async def list_policy_fragments(self) -> list[JsonObject]:
        return await self._collection("policyFragments")

    async def _policy_value(self, path: str) -> str | None:
        payload = await self._arm.get(
            path,
            params={**self._params, "format": "rawxml"},
            allow_not_found=True,
        )
        if payload is None:
            return None
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            return None
        value = properties.get("value")
        return value if isinstance(value, str) else None

    async def get_service_policy(self) -> str | None:
        return await self._policy_value(f"{self._base}/policies/policy")

    async def get_api_policy(self, api_name: str) -> str | None:
        return await self._policy_value(f"{self._base}/apis/{api_name}/policies/policy")

    async def get_product_policy(self, product_name: str) -> str | None:
        return await self._policy_value(f"{self._base}/products/{product_name}/policies/policy")

    async def get_operation_policy(self, api_name: str, operation_name: str) -> str | None:
        return await self._policy_value(
            f"{self._base}/apis/{api_name}/operations/{operation_name}/policies/policy"
        )

    async def get_policy_fragment(self, name: str) -> str | None:
        return await self._policy_value(f"{self._base}/policyFragments/{name}")

    async def _sub_resource(self, segment: str) -> JsonObject | None:
        return await self._arm.get(
            f"{self._base}/{segment}", params=self._params, allow_not_found=True
        )

    async def get_api(self, name: str) -> JsonObject | None:
        return await self._sub_resource(f"apis/{name}")

    async def get_api_operation(self, api_name: str, name: str) -> JsonObject | None:
        return await self._sub_resource(f"apis/{api_name}/operations/{name}")

    async def get_backend(self, name: str) -> JsonObject | None:
        return await self._sub_resource(f"backends/{name}")

    async def get_product(self, name: str) -> JsonObject | None:
        return await self._sub_resource(f"products/{name}")

    async def get_subscription(self, name: str) -> JsonObject | None:
        return await self._sub_resource(f"subscriptions/{name}")

    async def get_policy_fragment_resource(self, name: str) -> JsonObject | None:
        return await self._sub_resource(f"policyFragments/{name}")
