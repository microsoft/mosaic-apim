"""An in-process stand-in for the Azure Resource Manager Cognitive Services surface.

Tests drive the real ``ArmClient``/``CognitiveServicesClient``/``ModelInventoryCollector`` against
this transport, so paging, error mapping, RBAC evaluation, and the runtime access check are all
exercised end to end rather than mocked away.
"""

from typing import Any

import httpx
from mosaic_api.domain import (
    AZURE_OPENAI_USER_ROLE_ID,
    COGNITIVE_SERVICES_USER_ROLE_ID,
    READER_ROLE_ID,
)

AI_SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
AI_RESOURCE_GROUP = "rg-contoso-ai"
AI_ACCOUNT_NAME = "contoso-aoai"
AI_RESOURCE_ID = (
    f"/subscriptions/{AI_SUBSCRIPTION_ID}"
    f"/resourceGroups/{AI_RESOURCE_GROUP}"
    f"/providers/Microsoft.CognitiveServices/accounts/{AI_ACCOUNT_NAME}"
)
AI_ENDPOINT = f"https://{AI_ACCOUNT_NAME}.openai.azure.com/"

# Reader: */read with no dataActions. This is what MOSAIC asks operators to grant.
READER_PERMISSIONS: list[dict[str, Any]] = [{"actions": ["*/read"], "notActions": []}]
# A principal that can read the account but not its deployments.
PARTIAL_PERMISSIONS: list[dict[str, Any]] = [
    {"actions": ["Microsoft.CognitiveServices/accounts/read"], "notActions": []}
]


def role_assignment(role_definition_id: str, scope: str, principal_id: str) -> dict[str, Any]:
    return {
        "id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/assignment",
        "name": "assignment",
        "properties": {
            "roleDefinitionId": (
                f"/subscriptions/{AI_SUBSCRIPTION_ID}/providers/Microsoft.Authorization"
                f"/roleDefinitions/{role_definition_id}"
            ),
            "principalId": principal_id,
            "principalType": "ServicePrincipal",
            "scope": scope,
        },
    }


class FakeCognitiveServices:
    """A small but realistic Azure OpenAI account, plus a subscription to scan."""

    def __init__(
        self,
        *,
        permissions: list[dict[str, Any]] | None = None,
        account_status: int = 200,
        permissions_status: int = 200,
        role_assignments_status: int = 200,
        kind: str = "OpenAI",
    ) -> None:
        self.permissions = READER_PERMISSIONS if permissions is None else permissions
        self.account_status = account_status
        self.permissions_status = permissions_status
        self.role_assignments_status = role_assignments_status
        self.kind = kind
        self.requests: list[str] = []
        self.persistent_failures: dict[str, int] = {}
        self.role_assignments: list[dict[str, Any]] = []
        self.deployments: list[dict[str, Any]] = list(_default_deployments())
        self.models: list[dict[str, Any]] = list(_default_models())
        # Subscription scan surface.
        self.subscriptions: list[dict[str, Any]] = [
            {"subscriptionId": AI_SUBSCRIPTION_ID, "displayName": "Contoso dev"},
        ]
        self.accounts_by_subscription: dict[str, list[dict[str, Any]]] = {
            AI_SUBSCRIPTION_ID: [
                {
                    "id": AI_RESOURCE_ID,
                    "name": AI_ACCOUNT_NAME,
                    "kind": "OpenAI",
                    "location": "eastus2",
                    "properties": {"endpoint": AI_ENDPOINT},
                },
                {
                    "id": (
                        f"/subscriptions/{AI_SUBSCRIPTION_ID}/resourceGroups/{AI_RESOURCE_GROUP}"
                        "/providers/Microsoft.CognitiveServices/accounts/contoso-speech"
                    ),
                    "name": "contoso-speech",
                    "kind": "SpeechServices",
                    "location": "eastus2",
                    "properties": {},
                },
            ]
        }
        self.forbidden_subscriptions: set[str] = set()

    def fail_always(self, path_suffix: str, status_code: int) -> None:
        self.persistent_failures[path_suffix] = status_code

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)

        if path == "/subscriptions":
            return _collection(self.subscriptions)

        if path.endswith("/providers/Microsoft.CognitiveServices/accounts"):
            subscription_id = path.split("/")[2]
            if subscription_id in self.forbidden_subscriptions:
                return httpx.Response(403, json={"error": {"message": "denied"}})
            return _collection(self.accounts_by_subscription.get(subscription_id, []))

        if not path.startswith(AI_RESOURCE_ID):
            return httpx.Response(404, json={"error": {"message": "unknown resource"}})
        suffix = path[len(AI_RESOURCE_ID) :].strip("/")

        status_code = self.persistent_failures.get(suffix)
        if status_code is not None:
            return httpx.Response(
                status_code,
                json={"error": {"message": f"injected {status_code}"}},
                headers={"Retry-After": "0"} if status_code == 429 else {},
            )

        if suffix == "":
            if self.account_status != 200:
                return httpx.Response(
                    self.account_status, json={"error": {"message": "denied"}}
                )
            return httpx.Response(200, json=self._account())
        if suffix == "providers/Microsoft.Authorization/permissions":
            if self.permissions_status != 200:
                return httpx.Response(
                    self.permissions_status, json={"error": {"message": "denied"}}
                )
            return _collection(self.permissions)
        if suffix == "providers/Microsoft.Authorization/roleAssignments":
            if self.role_assignments_status != 200:
                return httpx.Response(
                    self.role_assignments_status, json={"error": {"message": "denied"}}
                )
            return _collection(self.role_assignments)
        if suffix == "deployments":
            return _collection(self.deployments)
        if suffix == "models":
            return _collection(self.models)
        if suffix == "projects":
            return _collection([])
        return httpx.Response(404, json={"error": {"message": f"no route for {suffix}"}})

    def _account(self) -> dict[str, Any]:
        return {
            "id": AI_RESOURCE_ID,
            "name": AI_ACCOUNT_NAME,
            "kind": self.kind,
            "location": "eastus2",
            "sku": {"name": "S0"},
            "properties": {
                "provisioningState": "Succeeded",
                "endpoint": AI_ENDPOINT,
                "publicNetworkAccess": "Enabled",
                "disableLocalAuth": False,
            },
        }


def _collection(values: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"value": values})


def _default_deployments() -> list[dict[str, Any]]:
    return [
        {
            "name": "gpt-4o-prod",
            "type": "Microsoft.CognitiveServices/accounts/deployments",
            "sku": {"name": "Standard", "capacity": 50},
            "properties": {
                "model": {
                    "format": "OpenAI",
                    "name": "gpt-4o",
                    "version": "2024-11-20",
                    "publisher": "OpenAI",
                },
                "provisioningState": "Succeeded",
                "raiPolicyName": "Microsoft.DefaultV2",
                "capabilities": {"chatCompletion": "true", "embeddings": "false"},
            },
        },
        {
            "name": "text-embedding-3-large",
            "sku": {"name": "Standard", "capacity": 10},
            "properties": {
                "model": {
                    "format": "OpenAI",
                    "name": "text-embedding-3-large",
                    "version": "1",
                },
                "provisioningState": "Succeeded",
                "capabilities": {"embeddings": "true"},
            },
        },
    ]


def _default_models() -> list[dict[str, Any]]:
    return [
        {
            "kind": "OpenAI",
            "skuName": "Standard",
            "model": {
                "name": "gpt-4o",
                "format": "OpenAI",
                "version": "2024-11-20",
                "lifecycleStatus": "GenerallyAvailable",
                "maxCapacity": 1000,
                "capabilities": {"chatCompletion": "true"},
                "deprecation": {"inference": "2027-01-01T00:00:00Z"},
            },
        },
        {
            "kind": "OpenAI",
            "model": {
                "name": "gpt-35-turbo",
                "format": "OpenAI",
                "version": "0613",
                "lifecycleStatus": "Deprecated",
                "capabilities": {"chatCompletion": "true"},
            },
        },
    ]


__all__ = [
    "AI_ACCOUNT_NAME",
    "AI_ENDPOINT",
    "AI_RESOURCE_GROUP",
    "AI_RESOURCE_ID",
    "AI_SUBSCRIPTION_ID",
    "AZURE_OPENAI_USER_ROLE_ID",
    "COGNITIVE_SERVICES_USER_ROLE_ID",
    "PARTIAL_PERMISSIONS",
    "READER_PERMISSIONS",
    "READER_ROLE_ID",
    "FakeCognitiveServices",
    "role_assignment",
]
