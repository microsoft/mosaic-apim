"""An in-process stand-in for the Azure Resource Manager APIM surface.

Tests drive the real ``ArmClient``/``ApimClient``/``InventoryCollector`` against this transport so
paging, error mapping, policy parsing, and AI detection are all covered by the same fixture.
"""

import time
from typing import Any

import httpx
from azure.core.credentials import AccessToken

SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
RESOURCE_GROUP = "rg-contoso-dev"
SERVICE_NAME = "apim-contoso-dev"
RESOURCE_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}"
    f"/resourceGroups/{RESOURCE_GROUP}"
    f"/providers/Microsoft.ApiManagement/service/{SERVICE_NAME}"
)

READER_PERMISSIONS = [
    {
        "actions": [
            "Microsoft.ApiManagement/service/*/read",
            "Microsoft.ApiManagement/service/read",
        ],
        "notActions": ["Microsoft.ApiManagement/service/users/keys/read"],
    }
]
CONTRIBUTOR_PERMISSIONS = [
    {"actions": ["Microsoft.ApiManagement/service/*"], "notActions": []}
]

CHAT_API_POLICY = """
<policies>
  <inbound>
    <base />
    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
    <llm-token-limit counter-key="@(context.Subscription.Id)" tokens-per-minute="10000"
                     estimate-prompt-tokens="true" />
    <set-header name="Authorization" exists-action="override">
      <value>Bearer sk-live-not-a-real-key</value>
    </set-header>
    <set-backend-service
      base-url="https://contoso-fn.azurewebsites.net/api?code=FunctionKeySecret" />
    <acme-custom-guard mode="strict" />
  </inbound>
</policies>
"""

GLOBAL_POLICY = """
<policies>
  <inbound>
    <base />
    <rate-limit calls="600" renewal-period="60" />
  </inbound>
</policies>
"""

MOSAIC_FRAGMENT = """
<fragment>
  <llm-token-limit counter-key="@(context.Subscription.Id)" tokens-per-minute="2000" />
</fragment>
"""


class FakeCredential:
    def __init__(self) -> None:
        self.token_requests = 0

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.token_requests += 1
        return AccessToken("fake-token", int(time.time()) + 3600)

    async def close(self) -> None:
        return None


class FakeApim:
    """A small but realistic API Management instance."""

    def __init__(
        self,
        *,
        permissions: list[dict[str, Any]] | None = None,
        service_status: int = 200,
        permissions_status: int = 200,
    ) -> None:
        self.permissions = READER_PERMISSIONS if permissions is None else permissions
        self.service_status = service_status
        self.permissions_status = permissions_status
        self.requests: list[str] = []
        self.failures: dict[str, int] = {}
        self.persistent_failures: dict[str, int] = {}

    def fail_once(self, path_suffix: str, status_code: int) -> None:
        self.failures[path_suffix] = status_code

    def fail_always(self, path_suffix: str, status_code: int) -> None:
        self.persistent_failures[path_suffix] = status_code

    def _maybe_fail(self, suffix: str) -> httpx.Response | None:
        status_code = self.persistent_failures.get(suffix) or self.failures.pop(suffix, None)
        if status_code is None:
            return None
        headers = {"Retry-After": "0"} if status_code == 429 else {}
        return httpx.Response(
            status_code,
            json={"error": {"message": f"injected {status_code}"}},
            headers=headers,
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if not path.startswith(RESOURCE_ID):
            return httpx.Response(404, json={"error": {"message": "unknown resource"}})
        suffix = path[len(RESOURCE_ID) :].strip("/")
        injected = self._maybe_fail(suffix)
        if injected is not None:
            return injected
        page = request.url.params.get("page")
        return self._route(suffix, page)

    def _route(self, suffix: str, page: str | None) -> httpx.Response:
        if suffix == "":
            if self.service_status != 200:
                return httpx.Response(
                    self.service_status, json={"error": {"message": "denied"}}
                )
            return httpx.Response(200, json=self._service())
        if suffix == "providers/Microsoft.Authorization/permissions":
            if self.permissions_status != 200:
                return httpx.Response(
                    self.permissions_status, json={"error": {"message": "denied"}}
                )
            return httpx.Response(200, json={"value": self.permissions})
        if suffix == "apis":
            return self._paged_apis(page)

        routes = {
            "policies/policy": lambda: self._policy(GLOBAL_POLICY),
            "apis/chat-api/operations": lambda: self._collection(self._chat_operations()),
            "apis/chat-api/policies/policy": lambda: self._policy(CHAT_API_POLICY),
            "apis/echo-api/operations": lambda: self._collection(self._echo_operations()),
            "apis/echo-api/policies/policy": lambda: httpx.Response(
                404, json={"error": {"message": "policy not found"}}
            ),
            "products": lambda: self._collection(self._products()),
            "products/gold/apis": lambda: self._collection(
                [{"name": "chat-api", "properties": {}}]
            ),
            "products/gold/policies/policy": lambda: self._policy(GLOBAL_POLICY),
            "subscriptions": lambda: self._collection(self._subscriptions()),
            "users": lambda: self._collection(self._users()),
            "groups": lambda: self._collection(self._groups()),
            "groups/developers/users": lambda: self._collection(
                [{"name": "user-ada", "properties": {}}]
            ),
            "backends": lambda: self._collection(self._backends()),
            "namedValues": lambda: self._collection(self._named_values()),
            "policyFragments": lambda: self._collection(
                [{"name": "mosaic-rate-standard", "properties": {"description": "MOSAIC"}}]
            ),
            "policyFragments/mosaic-rate-standard": lambda: self._policy(MOSAIC_FRAGMENT),
        }
        route = routes.get(suffix)
        if route is None:
            return httpx.Response(404, json={"error": {"message": f"no route for {suffix}"}})
        return route()

    @staticmethod
    def _collection(values: list[dict[str, Any]]) -> httpx.Response:
        return httpx.Response(200, json={"value": values})

    @staticmethod
    def _policy(xml: str) -> httpx.Response:
        return httpx.Response(200, json={"properties": {"value": xml, "format": "rawxml"}})

    @staticmethod
    def _service() -> dict[str, Any]:
        return {
            "name": SERVICE_NAME,
            "location": "eastus2",
            "sku": {"name": "Developer", "capacity": 1},
            "properties": {
                "provisioningState": "Succeeded",
                "gatewayUrl": f"https://{SERVICE_NAME}.azure-api.net",
            },
        }

    def _paged_apis(self, page: str | None) -> httpx.Response:
        if page is None:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "name": "chat-api",
                            "properties": {
                                "displayName": "Chat completions",
                                "path": "openai",
                                "protocols": ["https"],
                                "serviceUrl": "https://contoso.openai.azure.com/openai",
                                "apiRevision": "1",
                                "isCurrent": True,
                                "subscriptionRequired": True,
                            },
                        }
                    ],
                    "nextLink": (
                        f"https://management.azure.com{RESOURCE_ID}/apis"
                        "?api-version=2024-05-01&page=2"
                    ),
                },
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "name": "echo-api",
                        "properties": {
                            "displayName": "Echo",
                            "path": "echo",
                            "protocols": ["https"],
                            "serviceUrl": "https://echo.contoso.com",
                            "apiRevision": "1",
                            "isCurrent": True,
                            "subscriptionRequired": True,
                        },
                    }
                ]
            },
        )

    @staticmethod
    def _chat_operations() -> list[dict[str, Any]]:
        return [
            {
                "name": "chat-completions",
                "properties": {
                    "displayName": "Create chat completion",
                    "method": "POST",
                    "urlTemplate": "/deployments/{deployment}/chat/completions",
                },
            }
        ]

    @staticmethod
    def _echo_operations() -> list[dict[str, Any]]:
        return [
            {
                "name": "get-echo",
                "properties": {
                    "displayName": "Echo it back",
                    "method": "GET",
                    "urlTemplate": "/resource",
                },
            }
        ]

    @staticmethod
    def _products() -> list[dict[str, Any]]:
        return [
            {
                "name": "gold",
                "properties": {
                    "displayName": "Gold tier",
                    "description": "High volume access",
                    "state": "published",
                    "subscriptionRequired": True,
                    "approvalRequired": True,
                    "subscriptionsLimit": 10,
                },
            }
        ]

    @staticmethod
    def _subscriptions() -> list[dict[str, Any]]:
        return [
            {
                "name": "sub-ada",
                "properties": {
                    "displayName": "Ada research",
                    "scope": f"{RESOURCE_ID}/products/gold",
                    "state": "active",
                    "ownerId": f"{RESOURCE_ID}/users/user-ada",
                    "createdDate": "2026-01-15T10:30:00Z",
                },
            }
        ]

    @staticmethod
    def _users() -> list[dict[str, Any]]:
        return [
            {
                "name": "user-ada",
                "properties": {
                    "firstName": "Ada",
                    "lastName": "Lovelace",
                    "email": "ada@contoso.com",
                    "state": "active",
                    "identities": [
                        {"provider": "Aad", "id": "11111111-2222-3333-4444-555555555555"}
                    ],
                },
            }
        ]

    @staticmethod
    def _groups() -> list[dict[str, Any]]:
        return [
            {
                "name": "developers",
                "properties": {
                    "displayName": "Developers",
                    "type": "system",
                    "builtIn": True,
                },
            }
        ]

    @staticmethod
    def _backends() -> list[dict[str, Any]]:
        return [
            {
                "name": "foundry-pool",
                "properties": {
                    "title": "Foundry pool",
                    "url": "https://contoso.openai.azure.com/openai?sig=SasTokenSecret",
                    "protocol": "http",
                },
            }
        ]

    @staticmethod
    def _named_values() -> list[dict[str, Any]]:
        return [
            {
                "name": "openai-primary-key",
                "properties": {
                    "displayName": "openai-primary-key",
                    "secret": True,
                    "value": "sk-live-should-never-be-stored",
                    "tags": ["ai"],
                    "keyVault": {
                        "secretIdentifier": "https://kv-contoso.vault.azure.net/secrets/openai"
                    },
                },
            }
        ]


def build_client(fake: FakeApim, **kwargs: Any) -> tuple[httpx.AsyncClient, FakeCredential]:
    transport = httpx.MockTransport(fake.handler)
    return httpx.AsyncClient(transport=transport, **kwargs), FakeCredential()
