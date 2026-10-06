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
    "protectedUrl": "https://mosaic-mcp-protected-x.example.test/runtime/webhooks/mcp",
    "protectedAudience": "api://33333333-3333-3333-3333-333333333333",
    "functionAppName": "mosaic-mcp-protected-x",
}
# Every az verb that changes something in Azure.
CHANGES = frozenset({"create", "delete", "build", "config-zip", "update", "set", "assign"})


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
    ) -> None:
        self.group = group
        self.outputs = outputs
        self.records = records
        self.failures = failures
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
            if not self.records:
                raise deploy.KitError("DeploymentNotFound")
            return "mosaic-mcp-registry-pull"
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
        if verb == ["functionapp", "function", "list"]:
            return [f"mosaic-mcp-protected-x/{name}" for name in deploy.FUNCTIONS]
        if verb == ["identity", "show", "--subscription"]:
            return "99999999-9999-9999-9999-999999999999"
        if verb == ["role", "assignment", "list"]:
            return [OUTPUTS["acrPullRoleAssignmentId"]]
        return None

    @property
    def changes(self) -> list[list[str]]:
        return [call for call in self.calls if CHANGES & set(call[:4])]
