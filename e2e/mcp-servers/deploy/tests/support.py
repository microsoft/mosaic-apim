import copy
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import deploy

SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
VALUES: dict[str, Any] = {
    "subscriptionId": SUBSCRIPTION,
    "location": "eastus2",
    "resourceGroup": "rg-mcp-test-servers",
    "namePrefix": "mosaic-mcp",
    "logAnalyticsWorkspaceId": (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-mosaic"
        "/providers/Microsoft.OperationalInsights/workspaces/log-mosaic"
    ),
    "containerRegistryId": (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-mosaic"
        "/providers/Microsoft.ContainerRegistry/registries/crmosaic"
    ),
    "imageTag": "test-tag",
    "minReplicas": 1,
    "protected": {
        "tenantId": "22222222-2222-2222-2222-222222222222",
        "audienceClientId": "33333333-3333-3333-3333-333333333333",
        "audienceAppIdUri": "api://33333333-3333-3333-3333-333333333333",
        "gatewayClientId": "44444444-4444-4444-4444-444444444444",
        "mosaicApiClientId": "55555555-5555-5555-5555-555555555555",
    },
    "agent": {
        "modelEndpoint": "https://gateway.example.test/models/chat",
        "modelDeployment": "gpt-test",
        "modelApiVersion": "2024-10-21",
        "modelRuntimeClientId": "66666666-6666-6666-6666-666666666666",
        "costCenter": "research",
        "tokenParameter": "max_tokens",
        "maxTokens": 16,
    },
}
OUTPUTS = {
    "resourceGroupName": "rg-mcp-test-servers",
    "acrPullRoleAssignmentId": (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-mosaic/providers"
        "/Microsoft.ContainerRegistry/registries/crmosaic/providers"
        "/Microsoft.Authorization/roleAssignments/77777777-7777-7777-7777-777777777777"
    ),
    "toolsUrl": "https://mosaic-mcp-tools.example.test/mcp",
    "toolsSseUrl": "https://mosaic-mcp-tools-sse.example.test/sse",
    "agentUrl": "https://mosaic-mcp-agent.example.test/mcp",
    "agentPrincipalId": "88888888-8888-8888-8888-888888888888",
    "protectedUrl": "https://mosaic-mcp-protected.example.test/mcp",
    "protectedAudience": "api://33333333-3333-3333-3333-333333333333",
}
# Every az verb that changes something in Azure.
CHANGES = frozenset({"create", "delete", "build", "update", "set", "assign"})

# The kit's resource group, as M-protected's Functions host left it beside the new servers.
GROUP_ID = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-mcp-test-servers"
KIT_TAGS = {"mosaic-e2e-kit": "mcp-test-servers"}
# uniqueString(resourceGroup().id) for the old module: 13 lowercase letters and digits.
SUFFIX = "q2w3e4r5t6y7u"
SITE_PRINCIPAL = "12121212-1212-1212-1212-121212121212"
BLOB_OWNER = "b7e6dc6d-f1e8-4753-8033-0f276bb0955b"
QUEUE_CONTRIBUTOR = "974c5e8b-45b9-4653-ba55-5f855dd0fb88"


def resource(resource_type: str, name: str, kind: str = "", **tags: str) -> dict[str, Any]:
    return {
        "id": f"{GROUP_ID}/providers/{resource_type}/{name}",
        "name": name,
        "type": resource_type,
        "kind": kind,
        "tags": tags or dict(KIT_TAGS),
    }


OLD_SITE = resource("Microsoft.Web/sites", f"mosaic-mcp-protected-{SUFFIX}", "functionapp,linux")
OLD_PLAN = resource("Microsoft.Web/serverFarms", "mosaic-mcp-protected-plan", "functionapp")
OLD_STORAGE = resource("Microsoft.Storage/storageAccounts", f"stmosaicmcp{SUFFIX}", "StorageV2")
NEW_RESOURCES = [
    resource("Microsoft.ManagedIdentity/userAssignedIdentities", "mosaic-mcp-pull"),
    resource("Microsoft.App/managedEnvironments", "mosaic-mcp-env"),
    resource("Microsoft.App/containerApps", "mosaic-mcp-tools"),
    resource("Microsoft.App/containerApps", "mosaic-mcp-tools-sse"),
    resource("Microsoft.App/containerApps", "mosaic-mcp-protected"),
    resource("Microsoft.App/containerApps", "mosaic-mcp-agent"),
]


def assignment(
    number: int, role: str, principal: str = SITE_PRINCIPAL, scope: str = OLD_STORAGE["id"]
) -> dict[str, str]:
    return {
        "id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/{number:08d}-0000",
        "principalId": principal,
        "roleDefinitionId": (
            f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{role}"
        ),
        "scope": scope,
    }


OLD_GRANTS = [assignment(1, BLOB_OWNER), assignment(2, QUEUE_CONTRIBUTOR)]


def values(**changes: Any) -> dict[str, Any]:
    result = copy.deepcopy(VALUES)
    for key, value in changes.items():
        section, _, name = key.partition("__")
        if name:
            result[section][name] = value
        else:
            result[key] = value
    return result


def kit(**changes: Any) -> deploy.Kit:
    return deploy.Kit.from_values(values(**changes))


class FakeAz:
    """Answers the Azure CLI commands the kit runs, and records every one."""

    def __init__(
        self,
        *,
        group: str = "missing",
        outputs: dict[str, str] | None = None,
        records: bool = True,
        failures: int = 0,
        resources: list[dict[str, Any]] | None = None,
        principal: str | None = SITE_PRINCIPAL,
        diagnostic_settings: tuple[str, ...] = ("logs-to-workspace",),
        storage_assignments: list[dict[str, str]] | None = None,
        functions_record: bool = True,
    ) -> None:
        self.group = group
        self.outputs = outputs
        self.records = records
        self.failures = failures
        self.resources = resources or []
        self.principal = principal
        self.diagnostic_settings = diagnostic_settings
        self.storage_assignments = storage_assignments or []
        self.functions_record = functions_record
        self.calls: list[list[str]] = []
        self.deployed_servers: list[bool] = []

    def __call__(self, args: Sequence[str], capture: bool) -> Any:
        args = [str(arg) for arg in args]
        self.calls.append(args)
        verb = args[:3]
        if args[:2] == ["account", "show"]:
            return {"id": SUBSCRIPTION}
        if args[:2] == ["group", "exists"]:
            return self.group != "missing"
        if args[:2] == ["group", "show"]:
            return {"mosaic-e2e-kit": "mcp-test-servers"} if self.group == "kit" else {"other": "x"}
        if verb == ["deployment", "sub", "show"]:
            if self.outputs is None:
                raise deploy.KitError("DeploymentNotFound")
            return {
                name: {"type": "String", "value": value} for name, value in self.outputs.items()
            }
        if verb == ["deployment", "group", "show"]:
            name = args[args.index("--name") + 1]
            exists = self.functions_record if name.endswith("-function-app") else self.records
            if not exists:
                raise deploy.KitError("DeploymentNotFound")
            return name
        if verb == ["deployment", "sub", "create"]:
            parameters = json.loads(Path(args[args.index("--parameters") + 1][1:]).read_text())
            servers = parameters["parameters"]["deployServers"]["value"]
            if servers and self.failures:
                self.failures -= 1
                raise deploy.KitError("revision failed")
            self.deployed_servers.append(servers)
            if servers:
                self.outputs = dict(OUTPUTS)
            return None
        if verb == ["identity", "show", "--subscription"]:
            return "99999999-9999-9999-9999-999999999999"
        if verb == ["role", "assignment", "list"]:
            if args[args.index("--scope") + 1] == VALUES["containerRegistryId"]:
                return [OUTPUTS["acrPullRoleAssignmentId"]]
            return self.storage_assignments
        if args[:2] == ["resource", "list"]:
            return self.resources
        if args[:2] == ["resource", "show"]:
            return self.principal
        if verb == ["monitor", "diagnostic-settings", "list"]:
            return [{"name": name} for name in self.diagnostic_settings]
        return None

    @property
    def changes(self) -> list[list[str]]:
        return [call for call in self.calls if CHANGES & set(call[:4])]
