"""An in-process stand-in for the Azure Resource Manager Cognitive Services surface.

Tests drive the real ``ArmClient``/``CognitiveServicesClient``/``ModelInventoryCollector`` against
this transport, so paging, error mapping, RBAC evaluation, and the runtime access check are all
exercised end to end rather than mocked away.
"""

import re
from typing import Any

import httpx
from mosaic_api.domain import (
    AZURE_OPENAI_USER_ROLE_ID,
    COGNITIVE_SERVICES_USER_ROLE_ID,
    FOUNDRY_USER_ROLE_ID,
    READER_ROLE_ID,
)
from role_definitions import BUILT_IN_ROLE_DEFINITIONS

AI_SUBSCRIPTION_ID = "00000000-0000-0000-0000-000000000000"
AI_RESOURCE_GROUP = "rg-contoso-ai"
AI_ACCOUNT_NAME = "contoso-aoai"
AI_RESOURCE_ID = (
    f"/subscriptions/{AI_SUBSCRIPTION_ID}"
    f"/resourceGroups/{AI_RESOURCE_GROUP}"
    f"/providers/Microsoft.CognitiveServices/accounts/{AI_ACCOUNT_NAME}"
)
AI_PROJECT_NAME = "team-a"
AI_PROJECT_ID = f"{AI_RESOURCE_ID}/projects/{AI_PROJECT_NAME}"
AI_ENDPOINT = f"https://{AI_ACCOUNT_NAME}.openai.azure.com/"

# Reader: */read with no dataActions. This is what MOSAIC asks operators to grant.
READER_PERMISSIONS: list[dict[str, Any]] = [{"actions": ["*/read"], "notActions": []}]
# A principal that can read the account but not its deployments.
PARTIAL_PERMISSIONS: list[dict[str, Any]] = [
    {"actions": ["Microsoft.CognitiveServices/accounts/read"], "notActions": []}
]

_AUTHORIZATION = "providers/Microsoft.Authorization"
# Permissions read at a subscription itself, rather than at the account or a project under it.
_SUBSCRIPTION_PERMISSIONS = re.compile(
    r"^/subscriptions/([^/]+)/providers/Microsoft\.Authorization/permissions$"
)


def role_assignment(
    role_definition_id: str,
    scope: str,
    principal_id: str,
    *,
    condition: str | None = None,
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "roleDefinitionId": (
            f"/subscriptions/{AI_SUBSCRIPTION_ID}/providers/Microsoft.Authorization"
            f"/roleDefinitions/{role_definition_id}"
        ),
        "principalId": principal_id,
        "principalType": "ServicePrincipal",
        "scope": scope,
    }
    if condition is not None:
        properties["condition"] = condition
        properties["conditionVersion"] = "2.0"
    return {
        "id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/assignment",
        "name": "assignment",
        "properties": properties,
    }


def role_definition_resource(
    role_definition_id: str,
    role_name: str,
    permissions: list[dict[str, Any]],
    *,
    role_type: str = "BuiltInRole",
) -> dict[str, Any]:
    """A role definition as ARM returns it. The preview API omits a ``null`` condition."""

    return {
        "id": (
            f"/subscriptions/{AI_SUBSCRIPTION_ID}/providers/Microsoft.Authorization"
            f"/roleDefinitions/{role_definition_id}"
        ),
        "name": role_definition_id,
        "type": "Microsoft.Authorization/roleDefinitions",
        "properties": {
            "roleName": role_name,
            "type": role_type,
            "permissions": [
                {key: value for key, value in block.items() if value is not None}
                for block in permissions
            ],
        },
    }


def deny_assignment(
    scope: str,
    *,
    data_actions: list[str],
    principals: list[dict[str, str]],
    exclude_principals: list[dict[str, str]] | None = None,
    not_data_actions: list[str] | None = None,
    do_not_apply_to_child_scopes: bool = False,
    condition: str | None = None,
    name: str = "deployment-stack-deny",
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "denyAssignmentName": name,
        "permissions": [
            {
                "actions": [],
                "notActions": [],
                "dataActions": data_actions,
                "notDataActions": not_data_actions or [],
            }
        ],
        "scope": scope,
        "doNotApplyToChildScopes": do_not_apply_to_child_scopes,
        "principals": principals,
        "excludePrincipals": exclude_principals or [],
        "isSystemProtected": True,
    }
    if condition is not None:
        properties["condition"] = condition
        properties["conditionVersion"] = "2.0"
    return {
        "id": f"{scope}/providers/Microsoft.Authorization/denyAssignments/{name}",
        "name": name,
        "type": "Microsoft.Authorization/denyAssignments",
        "properties": properties,
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
        role_definitions_status: int = 200,
        deny_assignments_status: int = 200,
        kind: str = "OpenAI",
        public_network_access: str = "Enabled",
        network_acls: dict[str, Any] | None = None,
    ) -> None:
        self.permissions = READER_PERMISSIONS if permissions is None else permissions
        self.account_status = account_status
        self.permissions_status = permissions_status
        self.role_assignments_status = role_assignments_status
        self.role_definitions_status = role_definitions_status
        self.deny_assignments_status = deny_assignments_status
        self.kind = kind
        self.public_network_access = public_network_access
        self.network_acls = network_acls
        self.requests: list[str] = []
        self.persistent_failures: dict[str, int] = {}
        self.role_assignments: list[dict[str, Any]] = []
        # Real built-in definitions, keyed by lowercase GUID, so the runtime check evaluates what
        # Azure actually returns rather than a hand-written approximation.
        self.role_definitions: dict[str, dict[str, Any]] = {
            guid.casefold(): role_definition_resource(guid, name, permissions)
            for guid, (name, permissions) in BUILT_IN_ROLE_DEFINITIONS.items()
        }
        self.deny_assignments: list[dict[str, Any]] = []
        self.deployments: list[dict[str, Any]] = list(_default_deployments())
        self.models: list[dict[str, Any]] = list(_default_models())
        # Subscription scan surface.
        self.subscriptions: list[dict[str, Any]] = [
            {"subscriptionId": AI_SUBSCRIPTION_ID, "displayName": "Contoso dev"},
        ]
        self.subscriptions_status = 200
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
        # What MOSAIC's identity holds at each subscription itself, which decides whether the
        # account list above is complete. Reader unless a test says otherwise.
        self.subscription_permissions: dict[str, list[dict[str, Any]]] = {}
        self.subscription_permissions_status = 200

    def fail_always(self, path_suffix: str, status_code: int) -> None:
        self.persistent_failures[path_suffix] = status_code

    def add_custom_role(
        self, role_definition_id: str, role_name: str, permissions: list[dict[str, Any]]
    ) -> None:
        self.role_definitions[role_definition_id.casefold()] = role_definition_resource(
            role_definition_id, role_name, permissions, role_type="CustomRole"
        )

    def role_definition_reads(self) -> int:
        return sum(1 for path in self.requests if "/roleDefinitions/" in path)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)

        if path == "/subscriptions":
            if self.subscriptions_status != 200:
                return httpx.Response(
                    self.subscriptions_status, json={"error": {"message": "denied"}}
                )
            return _collection(self.subscriptions)

        if path.endswith("/providers/Microsoft.CognitiveServices/accounts"):
            subscription_id = path.split("/")[2]
            if subscription_id in self.forbidden_subscriptions:
                return httpx.Response(403, json={"error": {"message": "denied"}})
            return _collection(self.accounts_by_subscription.get(subscription_id, []))

        match = _SUBSCRIPTION_PERMISSIONS.match(path)
        if match:
            if self.subscription_permissions_status != 200:
                return httpx.Response(
                    self.subscription_permissions_status, json={"error": {"message": "denied"}}
                )
            granted = self.subscription_permissions.get(match.group(1), READER_PERMISSIONS)
            return _collection(granted)

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
        if suffix == "providers/Microsoft.Authorization/permissions" or suffix == (
            f"projects/{AI_PROJECT_NAME}/{_AUTHORIZATION}/permissions"
        ):
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
        if suffix.startswith(f"{_AUTHORIZATION}/roleDefinitions/"):
            if self.role_definitions_status != 200:
                return httpx.Response(
                    self.role_definitions_status,
                    json={"error": {"code": "AuthorizationFailed", "message": "denied"}},
                )
            definition = self.role_definitions.get(suffix.rsplit("/", 1)[-1].casefold())
            if definition is None:
                return httpx.Response(
                    404,
                    json={
                        "error": {
                            "code": "RoleDefinitionDoesNotExist",
                            "message": "The specified role definition does not exist.",
                        }
                    },
                )
            return httpx.Response(200, json=definition)
        if suffix == f"{_AUTHORIZATION}/denyAssignments":
            if self.deny_assignments_status != 200:
                return httpx.Response(
                    self.deny_assignments_status, json={"error": {"message": "denied"}}
                )
            return _collection(self.deny_assignments)
        if suffix == "deployments":
            return _collection(self.deployments)
        if suffix == "models":
            return _collection(self.models)
        if suffix == "projects":
            return _collection([])
        return httpx.Response(404, json={"error": {"message": f"no route for {suffix}"}})

    def _account(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "provisioningState": "Succeeded",
            "endpoint": AI_ENDPOINT,
            "publicNetworkAccess": self.public_network_access,
            "disableLocalAuth": False,
        }
        if self.network_acls is not None:
            properties["networkAcls"] = self.network_acls
        return {
            "id": AI_RESOURCE_ID,
            "name": AI_ACCOUNT_NAME,
            "kind": self.kind,
            "location": "eastus2",
            "sku": {"name": "S0"},
            "properties": properties,
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
    "AI_PROJECT_ID",
    "AI_PROJECT_NAME",
    "AI_RESOURCE_GROUP",
    "AI_RESOURCE_ID",
    "AI_SUBSCRIPTION_ID",
    "AZURE_OPENAI_USER_ROLE_ID",
    "COGNITIVE_SERVICES_USER_ROLE_ID",
    "FOUNDRY_USER_ROLE_ID",
    "PARTIAL_PERMISSIONS",
    "READER_PERMISSIONS",
    "READER_ROLE_ID",
    "FakeCognitiveServices",
    "deny_assignment",
    "role_assignment",
    "role_definition_resource",
]
