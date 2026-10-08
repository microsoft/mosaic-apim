"""main.bicep, compiled: what each server gets, and the token check that guards M-protected."""

from collections.abc import Callable
from typing import Any

import deploy
from support import kit

Resource = dict[str, Any]


def module(template: Resource, name: Callable[[str], bool]) -> Resource:
    [found] = [
        resource
        for resource in template["resources"]
        if resource["type"] == "Microsoft.Resources/deployments" and name(resource["name"])
    ]
    return found


def main_module(template: Resource, suffix: str) -> Resource:
    return module(template, lambda name: f"'{{0}}-{suffix}'" in name)


def inner(resource: Resource) -> list[Resource]:
    return resource["properties"]["template"]["resources"]


def one(resources: list[Resource], resource_type: str, name: str = "") -> Resource:
    [found] = [r for r in resources if r["type"] == resource_type and name in r["name"]]
    return found


def all_resources(template: Resource) -> list[Resource]:
    found = []
    for resource in template["resources"]:
        if resource["type"] == "Microsoft.Resources/deployments":
            found += all_resources(resource["properties"]["template"])
        else:
            found.append(resource)
    return found


def servers(template: Resource) -> dict[str, Resource]:
    """Each container app's module, by name: m-tools, m-tools-sse, m-protected and m-agent."""

    return {
        resource["name"]: resource
        for resource in inner(main_module(template, "container-apps"))
        if resource["type"] == "Microsoft.Resources/deployments"
    }


def protected_auth(template: Resource) -> Resource:
    return one(
        inner(main_module(template, "container-apps")), "Microsoft.App/containerApps/authConfigs"
    )


def test_m_protected_runs_m_tools_image_under_its_own_name_with_the_others(
    template: Resource,
) -> None:
    container_apps = main_module(template, "container-apps")
    # Deployed with the other servers, every time: only deploy's first stage leaves them out.
    assert container_apps["condition"] == "[parameters('deployServers')]"
    protected = servers(template)["m-protected"]["properties"]["parameters"]
    assert protected["image"] == {"value": "[parameters('toolsImage')]"}
    assert protected["name"] == {"value": "[variables('protectedName')]"}
    assert container_apps["properties"]["template"]["variables"]["protectedName"] == (
        "[format('{0}-protected', parameters('namePrefix'))]"
    )
    assert protected["env"]["value"] == [
        {"name": "MCP_TRANSPORT", "value": "streamable-http"},
        {"name": "MCP_SERVER_NAME", "value": "M-protected"},
    ]
    assert "systemAssignedIdentity" not in protected
    # MOSAIC publishes only an endpoint whose last segment is /mcp.
    assert container_apps["properties"]["template"]["outputs"]["protectedUrl"]["value"] == (
        "[format('https://{0}/mcp', reference(resourceId('Microsoft.Resources/deployments', "
        "'m-protected'), '2025-04-01').outputs.fqdn.value)]"
    )


def test_built_in_authentication_admits_only_tokens_for_the_audience_from_the_two_allowed_clients(
    template: Resource,
) -> None:
    auth = protected_auth(template)
    # M-protected's own, and applied as soon as its app exists: never another app's, never left out.
    assert auth["name"] == "[format('{0}/{1}', variables('protectedName'), 'current')]"
    assert auth["dependsOn"] == ["[resourceId('Microsoft.Resources/deployments', 'm-protected')]"]
    assert "condition" not in auth
    settings = auth["properties"]

    assert settings["platform"] == {"enabled": True}
    # 401 to any request without a valid token, on every path: no redirect, no excluded path.
    assert settings["globalValidation"] == {"unauthenticatedClientAction": "Return401"}
    assert settings["httpSettings"] == {"requireHttps": True}
    assert list(settings["identityProviders"]) == ["azureActiveDirectory"]
    entra = settings["identityProviders"]["azureActiveDirectory"]
    assert entra["enabled"] is True
    # The tenant's v2 issuer, and no client secret: the app only checks bearer tokens.
    assert entra["registration"] == {
        "openIdIssuer": (
            "[format('{0}{1}/v2.0', environment().authentication.loginEndpoint, "
            "parameters('tenantId'))]"
        ),
        "clientId": "[parameters('protectedAudienceClientId')]",
    }
    # The gateway's token for api://<audience-app-id> is v2, so its aud is the client ID.
    assert entra["validation"] == {
        "allowedAudiences": [
            "[parameters('protectedAudienceClientId')]",
            "[parameters('protectedAudienceAppIdUri')]",
        ],
        "defaultAuthorizationPolicy": {
            "allowedApplications": "[parameters('protectedAllowedClientIds')]"
        },
    }
    assert "login" not in entra
    assert main_module(template, "container-apps")["properties"]["parameters"][
        "protectedAllowedClientIds"
    ]["value"] == [
        "[parameters('gatewayClientId')]",
        "[parameters('mosaicApiClientId')]",
    ]
    assert settings["login"] == {"tokenStore": {"enabled": False}}


def test_m_protected_accepts_tokens_only_from_its_tenant_for_its_audience(
    template: Resource,
) -> None:
    passed = main_module(template, "container-apps")["properties"]["parameters"]
    assert passed["tenantId"] == {"value": "[parameters('tenantId')]"}
    assert passed["protectedAudienceClientId"] == {
        "value": "[parameters('protectedAudienceClientId')]"
    }
    assert passed["protectedAudienceAppIdUri"] == {
        "value": "[parameters('protectedAudienceAppIdUri')]"
    }
    assert template["parameters"]["protectedAudienceAppIdUri"]["defaultValue"] == (
        "[format('api://{0}', parameters('protectedAudienceClientId'))]"
    )


def test_nothing_of_the_functions_host_is_deployed_any_more(template: Resource) -> None:
    types = {resource["type"] for resource in all_resources(template)}
    assert not {t for t in types if t.startswith(("Microsoft.Web/", "Microsoft.Storage/"))}
    names = [
        r["name"] for r in template["resources"] if r["type"] == "Microsoft.Resources/deployments"
    ]
    assert not [name for name in names if "function-app" in name]
    assert "functionAppName" not in template["outputs"]


def test_container_apps_pull_with_the_shared_identity_and_never_a_password(
    template: Resource,
) -> None:
    found = servers(template)
    assert sorted(found) == ["m-agent", "m-protected", "m-tools", "m-tools-sse"]
    for server in found.values():
        [app] = inner(server)
        configuration = app["properties"]["configuration"]
        assert configuration["registries"] == [
            {
                "server": "[parameters('registryServer')]",
                "identity": "[parameters('pullIdentityId')]",
            }
        ]
        assert "secrets" not in configuration
        assert configuration["ingress"]["external"] is True
        assert configuration["ingress"]["targetPort"] == 8000
        assert configuration["ingress"]["allowInsecure"] is False
        [container] = app["properties"]["template"]["containers"]
        assert container["resources"] == {"cpu": "[json('0.25')]", "memory": "0.5Gi"}
        assert app["properties"]["template"]["scale"] == {
            "minReplicas": "[parameters('minReplicas')]",
            "maxReplicas": 1,
        }
    assert template["parameters"]["minReplicas"]["defaultValue"] == 1


def test_the_sse_only_variant_runs_the_m_tools_image_with_the_sse_transport(
    template: Resource,
) -> None:
    found = {name: server["properties"]["parameters"] for name, server in servers(template).items()}
    assert found["m-tools"]["image"] == found["m-tools-sse"]["image"]
    assert found["m-tools"]["env"]["value"] == [
        {"name": "MCP_TRANSPORT", "value": "streamable-http"}
    ]
    assert found["m-tools-sse"]["env"]["value"] == [{"name": "MCP_TRANSPORT", "value": "sse"}]
    assert "systemAssignedIdentity" not in found["m-tools"]
    assert found["m-agent"]["systemAssignedIdentity"]["value"] is True


def test_m_agent_is_configured_to_call_its_model_through_the_gateway(template: Resource) -> None:
    variables = main_module(template, "container-apps")["properties"]["template"]["variables"]
    scope = "[format('api://{0}/.default', parameters('modelRuntimeClientId'))]"
    assert variables["agentSettings"] == {
        "MOSAIC_MODEL_ENDPOINT": "[parameters('modelEndpoint')]",
        "MOSAIC_MODEL_DEPLOYMENT": "[parameters('modelDeployment')]",
        "MOSAIC_MODEL_API_VERSION": "[parameters('modelApiVersion')]",
        "MOSAIC_RUNTIME_SCOPE": scope,
        "MOSAIC_MODEL_TOKEN_PARAMETER": "[parameters('modelTokenParameter')]",
        "MOSAIC_MODEL_MAX_TOKENS": "[string(parameters('modelMaxTokens'))]",
    }
    assert template["parameters"]["modelMaxTokens"]["defaultValue"] == 16


def test_acr_pull_is_granted_on_the_registry_in_its_own_resource_group(template: Resource) -> None:
    registry_pull = main_module(template, "registry-pull")
    assert registry_pull["subscriptionId"] == "[variables('registry')[2]]"
    assert registry_pull["resourceGroup"] == "[variables('registry')[4]]"
    assignment = one(inner(registry_pull), "Microsoft.Authorization/roleAssignments")
    assert assignment["scope"] == (
        "[resourceId('Microsoft.ContainerRegistry/registries', parameters('registryName'))]"
    )
    assert registry_pull["properties"]["template"]["variables"]["acrPullRoleId"] == (
        "7f951dda-4ed3-4680-a7ca-43fe172d538d"
    )


def test_every_server_logs_to_the_existing_workspace(template: Resource) -> None:
    settings = [
        r for r in all_resources(template) if r["type"] == "Microsoft.Insights/diagnosticSettings"
    ]
    categories = sorted(log["category"] for s in settings for log in s["properties"]["logs"])
    assert categories == ["ContainerAppConsoleLogs", "ContainerAppSystemLogs"]
    for setting in settings:
        assert setting["properties"]["workspaceId"] == "[parameters('logAnalyticsWorkspaceId')]"


def test_the_resource_group_carries_the_tag_teardown_checks(template: Resource) -> None:
    group = one(template["resources"], "Microsoft.Resources/resourceGroups")
    assert group["tags"] == "[variables('tags')]"
    assert template["variables"]["tags"] == {deploy.KIT_TAG_NAME: deploy.KIT_TAG_VALUE}


def test_the_outputs_give_the_coordinator_everything_the_next_steps_need(
    template: Resource,
) -> None:
    for key, _, _ in deploy.OUTPUT_LINES:
        assert key in template["outputs"]
    for key in ("acrPullRoleAssignmentId", "resourceGroupName"):
        assert key in template["outputs"]
    for _, key in deploy.SMOKE_CHECKS:
        assert key in template["outputs"]


def test_deploy_py_passes_exactly_the_parameters_the_template_takes(template: Resource) -> None:
    passed = set(kit().arm_parameters("tag", deploy_servers=True)["parameters"])
    declared = template["parameters"]
    assert passed <= set(declared)
    assert {name for name, item in declared.items() if "defaultValue" not in item} <= passed


def test_the_plan_lists_every_kind_of_resource_the_template_creates(template: Resource) -> None:
    created = {r["type"] for r in all_resources(template)}
    planned = {resource_type for resource_type, _ in deploy.planned_resources(kit())}
    assert planned == created
