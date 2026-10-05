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


def test_easy_auth_admits_only_tokens_for_the_audience_from_the_two_allowed_clients(
    template: Resource,
) -> None:
    function_app = main_module(template, "function-app")
    # Deployed with the function app, every time: only deploy's first stage leaves both out.
    assert function_app["condition"] == "[parameters('deployServers')]"
    easy_auth = one(inner(function_app), "Microsoft.Web/sites/config", "authsettingsV2")
    assert "condition" not in easy_auth
    settings = easy_auth["properties"]

    assert settings["platform"]["enabled"] is True
    assert settings["globalValidation"] == {
        "requireAuthentication": True,
        "unauthenticatedClientAction": "Return401",
    }
    entra = settings["identityProviders"]["azureActiveDirectory"]
    assert entra["enabled"] is True
    assert entra["registration"] == {
        "openIdIssuer": (
            "[format('{0}{1}/v2.0', environment().authentication.loginEndpoint, "
            "parameters('tenantId'))]"
        ),
        "clientId": "[parameters('audienceClientId')]",
    }
    # The gateway's token for api://<audience-app-id> is v2, so its aud is the client ID.
    assert entra["validation"]["allowedAudiences"] == [
        "[parameters('audienceClientId')]",
        "[parameters('audienceAppIdUri')]",
    ]
    assert entra["validation"]["defaultAuthorizationPolicy"] == {
        "allowedApplications": "[parameters('allowedClientIds')]"
    }
    assert function_app["properties"]["parameters"]["allowedClientIds"]["value"] == [
        "[parameters('gatewayClientId')]",
        "[parameters('mosaicApiClientId')]",
    ]
    assert settings["login"]["tokenStore"]["enabled"] is False


def test_m_protected_accepts_tokens_only_from_its_tenant(template: Resource) -> None:
    settings = one(
        inner(main_module(template, "function-app")), "Microsoft.Web/sites/config", "appsettings"
    )
    assert settings["properties"]["WEBSITE_AUTH_AAD_ALLOWED_TENANTS"] == "[parameters('tenantId')]"


def test_m_protected_runs_python_3_13_on_the_smallest_flex_consumption_instance(
    template: Resource,
) -> None:
    resources = inner(main_module(template, "function-app"))
    plan = one(resources, "Microsoft.Web/serverfarms")
    assert plan["sku"] == {"tier": "FlexConsumption", "name": "FC1"}
    site = one(resources, "Microsoft.Web/sites")
    assert site["identity"] == {"type": "SystemAssigned"}
    assert site["properties"]["httpsOnly"] is True
    config = site["properties"]["functionAppConfig"]
    assert config["runtime"] == {"name": "python", "version": "3.13"}
    assert config["scaleAndConcurrency"] == {"instanceMemoryMB": 512, "maximumInstanceCount": 1}
    assert config["deployment"]["storage"]["authentication"] == {"type": "SystemAssignedIdentity"}


def test_m_protected_s_storage_takes_no_shared_key_or_public_access(template: Resource) -> None:
    storage = one(inner(main_module(template, "function-app")), "Microsoft.Storage/storageAccounts")
    properties = storage["properties"]
    assert properties["allowSharedKeyAccess"] is False
    assert properties["allowBlobPublicAccess"] is False
    assert properties["minimumTlsVersion"] == "TLS1_2"


def test_container_apps_pull_with_the_shared_identity_and_never_a_password(
    template: Resource,
) -> None:
    container_apps = main_module(template, "container-apps")
    servers = [r for r in inner(container_apps) if r["type"] == "Microsoft.Resources/deployments"]
    assert sorted(server["name"] for server in servers) == ["m-agent", "m-tools", "m-tools-sse"]
    for server in servers:
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
    servers = {
        r["name"]: r["properties"]["parameters"]
        for r in inner(main_module(template, "container-apps"))
        if r["type"] == "Microsoft.Resources/deployments"
    }
    assert servers["m-tools"]["image"] == servers["m-tools-sse"]["image"]
    assert servers["m-tools"]["env"]["value"] == [
        {"name": "MCP_TRANSPORT", "value": "streamable-http"}
    ]
    assert servers["m-tools-sse"]["env"]["value"] == [{"name": "MCP_TRANSPORT", "value": "sse"}]
    assert "systemAssignedIdentity" not in servers["m-tools"]
    assert servers["m-agent"]["systemAssignedIdentity"]["value"] is True


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
    assert categories == ["ContainerAppConsoleLogs", "ContainerAppSystemLogs", "FunctionAppLogs"]
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
    for key in ("acrPullRoleAssignmentId", "functionAppName", "resourceGroupName"):
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
