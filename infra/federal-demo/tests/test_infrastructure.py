"""Offline ARM contract checks; compiles in memory and never calls Azure deployment APIs.

Run: python -m unittest discover -s infra\\federal-demo\\tests -v
"""

import json
import shutil
import subprocess
import unittest
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENVIRONMENTS = ("dev", "stg", "prod")
READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
OPENAI_USER = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"


@lru_cache
def compile_template(relative_path):
    az = shutil.which("az")
    if az is None:
        raise RuntimeError("Azure CLI with Bicep is required for offline infrastructure tests")
    result = subprocess.run(
        [az, "bicep", "build", "--file", str(ROOT / relative_path), "--stdout"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise AssertionError(f"Bicep compilation failed:\n{result.stderr}")
    return json.loads(result.stdout)


def resources(template):
    found = template["resources"]
    return list(found.values()) if isinstance(found, dict) else found


def one(template, resource_type, contains=""):
    found = [
        resource
        for resource in resources(template)
        if resource["type"] == resource_type
        and contains in resource["name"]
        and not resource.get("existing", False)
    ]
    if len(found) != 1:
        raise AssertionError(f"Expected one {resource_type}/{contains}, found {len(found)}")
    return found[0]


def inner(module):
    return module["properties"]["template"]


def created_resources(template):
    for resource in resources(template):
        if resource.get("existing"):
            continue
        if resource["type"] == "Microsoft.Resources/deployments":
            yield from created_resources(inner(resource))
        else:
            yield resource


class FederalInfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.estate = compile_template(Path("infra") / "federal-demo" / "main.bicep")
        cls.core = inner(cls.estate["resources"]["core"])

    def env(self):
        return inner(self.estate["resources"]["environmentStacks"])

    def test_exactly_three_gateways_and_no_fourth_core_gateway(self):
        environments = self.estate["variables"]["environments"]
        self.assertEqual([env["key"] for env in environments], list(ENVIRONMENTS))
        stack = self.estate["resources"]["environmentStacks"]
        self.assertEqual(stack["copy"]["count"], "[length(variables('environments'))]")
        core = self.estate["resources"]["core"]
        self.assertIs(core["properties"]["parameters"]["deployApim"]["value"], False)
        apim = one(self.core, "Microsoft.Resources/deployments", "apim")
        self.assertEqual(apim["condition"], "[parameters('deployApim')]")
        gateway = self.env()["resources"]["gateway"]
        self.assertEqual(gateway["properties"]["parameters"]["skuName"]["value"], "Developer")
        self.assertEqual(gateway["properties"]["parameters"]["skuCapacity"]["value"], 1)

    def test_first_create_apim_reads_and_scoped_writes_wait_for_the_creating_module(self):
        env = self.env()
        alias = env["resources"]["gatewayResource"]
        self.assertTrue(alias["existing"])
        self.assertEqual(alias["dependsOn"], ["gateway"])
        for symbol in ("apiGatewayContributor", "apiGatewayMonitoring", "gatewayDiagnostics"):
            self.assertIn("gateway", env["resources"][symbol]["dependsOn"])
        gateway_output = env["outputs"]["gateway"]["value"]
        self.assertEqual(
            gateway_output["principalId"], "[reference('gateway').outputs.principalId.value]"
        )
        self.assertNotIn("gatewayResource", json.dumps(gateway_output))
        creating_module = inner(env["resources"]["gateway"])
        self.assertIn("identity.principalId", creating_module["outputs"]["principalId"]["value"])
        service = one(creating_module, "Microsoft.ApiManagement/service")
        self.assertNotIn("existing", service)
        self.assertEqual(service["identity"]["type"], "SystemAssigned")
        self.assertIn(
            ".outputs.principalId.value", self.core["outputs"]["APIM_PRINCIPAL_ID"]["value"]
        )
        [backend_access] = [
            resource
            for resource in resources(self.core)
            if resource["type"] == "Microsoft.Authorization/roleAssignments"
            and "variables('apimName')" in resource["name"]
            and "KeyVaultSecretsUser" in resource["name"]
        ]
        self.assertIn(".outputs.principalId.value", backend_access["properties"]["principalId"])
        self.assertTrue(any("'apim'" in dependency for dependency in backend_access["dependsOn"]))

    def test_fixed_group_placement_and_region(self):
        groups = [r for r in resources(self.estate) if r["type"].endswith("/resourceGroups")]
        self.assertEqual(len(groups), 2)
        self.assertEqual(self.estate["resources"]["coreGroup"]["name"], "rg-mosaic-federal-core")
        self.assertEqual(self.estate["parameters"]["location"]["allowedValues"], ["eastus2"])
        self.assertEqual(
            self.estate["resources"]["environmentStacks"]["resourceGroup"],
            "[format('rg-mosaic-federal-{0}', variables('environments')[copyIndex()].key)]",
        )

    def test_only_the_app_service_plan_and_three_sites_use_the_hosting_region_override(self):
        for template in (self.estate, self.core):
            self.assertEqual(
                template["parameters"]["appServiceLocation"]["defaultValue"],
                "[parameters('location')]",
            )
            self.assertEqual(template["parameters"]["appServicePlanSku"]["defaultValue"], "B1")
        self.assertEqual(
            self.estate["parameters"]["appServiceLocation"]["allowedValues"],
            ["eastus2", "centralus"],
        )
        self.assertEqual(
            self.estate["resources"]["core"]["properties"]["parameters"]["appServiceLocation"],
            {"value": "[parameters('appServiceLocation')]"},
        )
        hosting_modules = {"appServicePlan", "apiApp", "webApp", "portalApp"}
        seen_hosting_modules = set()
        for module in resources(self.core):
            if module["type"] != "Microsoft.Resources/deployments":
                continue
            supplied = module["properties"]["parameters"]
            if "location" not in supplied:
                continue
            is_hosting = module["name"] in hosting_modules
            if is_hosting:
                seen_hosting_modules.add(module["name"])
            expected = "appServiceLocation" if is_hosting else "location"
            self.assertEqual(supplied["location"]["value"], f"[parameters('{expected}')]")
        self.assertEqual(seen_hosting_modules, hosting_modules)
        for symbol in ("environmentStacks", "onboarding"):
            self.assertEqual(
                self.estate["resources"][symbol]["properties"]["parameters"]["location"]["value"],
                "[parameters('location')]",
            )

    def test_core_external_gateway_is_bootstrap_only(self):
        self.assertEqual(
            self.estate["parameters"]["environmentName"]["defaultValue"], "mosaic-federal-demo"
        )
        self.assertEqual(
            self.estate["resources"]["core"]["properties"]["parameters"]["environmentName"],
            {"value": "[parameters('environmentName')]"},
        )
        for resource in resources(self.core):
            scope = str(resource.get("scope", ""))
            if "apimResource" in scope or "ApiManagement/service" in scope:
                self.assertEqual(resource.get("condition"), "[parameters('deployApim')]")
        self.assertEqual(self.core["parameters"]["deployApim"]["defaultValue"], True)
        self.assertIn("externalBootstrapApimResourceId", self.core["parameters"])
        self.assertEqual(self.core["outputs"]["APIM_PRINCIPAL_ID"]["value"][-6:], ", '')]")

    def test_only_demo_discovery_timeout_is_extended_for_cold_starts(self):
        timeout = self.core["parameters"]["mcpDiscoveryTimeoutSeconds"]
        self.assertEqual(timeout["defaultValue"], 20)
        self.assertEqual((timeout["minValue"], timeout["maxValue"]), (1, 120))
        supplied = self.estate["resources"]["core"]["properties"]["parameters"]
        self.assertEqual(supplied["mcpDiscoveryTimeoutSeconds"]["value"], 60)
        api_module = one(self.core, "Microsoft.Resources/deployments", "apiApp")
        settings = api_module["properties"]["parameters"]["appSettings"]["value"]
        discovery = next(
            item for item in settings if item["name"] == "MOSAIC_MCP_DISCOVERY_TIMEOUT_SECONDS"
        )
        self.assertEqual(discovery["value"], "[string(parameters('mcpDiscoveryTimeoutSeconds'))]")

    def test_reviewed_core_images_are_forwarded_on_every_staged_redeployment(self):
        for module_name, parameter_name, bootstrap_default in (
            (
                "apiApp",
                "apiPlaceholderImage",
                "mcr.microsoft.com/azuredocs/aci-helloworld:latest",
            ),
            ("webApp", "webPlaceholderImage", "mcr.microsoft.com/appsvc/staticsite:latest"),
            ("portalApp", "portalPlaceholderImage", "mcr.microsoft.com/appsvc/staticsite:latest"),
        ):
            for template in (self.estate, self.core):
                self.assertEqual(
                    template["parameters"][parameter_name]["defaultValue"], bootstrap_default
                )
            root_parameters = self.estate["resources"]["core"]["properties"]["parameters"]
            self.assertEqual(
                root_parameters[parameter_name],
                {"value": f"[parameters('{parameter_name}')]"},
            )
            site_module = one(self.core, "Microsoft.Resources/deployments", module_name)
            self.assertEqual(
                site_module["properties"]["parameters"]["containerImage"],
                {"value": f"[parameters('{parameter_name}')]"},
            )
            site = one(inner(site_module), "Microsoft.Web/sites")
            self.assertIn(
                "parameters('containerImage')", site["properties"]["siteConfig"]["linuxFxVersion"]
            )

    def test_json_array_settings_are_serialized_only_at_the_final_site_resource(self):
        api_module = one(self.core, "Microsoft.Resources/deployments", "apiApp")
        settings = {
            setting["name"]: setting["value"]
            for setting in api_module["properties"]["parameters"]["appSettings"]["value"]
        }
        self.assertEqual(
            settings["MOSAIC_TEAMS_WORKFLOW_ALLOWED_HOSTS"],
            "[json(parameters('teamsWorkflowAllowedHosts'))]",
        )
        self.assertEqual(settings["MOSAIC_CORS_ORIGINS"], "[variables('apiCorsAllowedOrigins')]")
        self.assertNotIn("copy", inner(api_module)["variables"])
        site = one(inner(api_module), "Microsoft.Web/sites")
        serialized = site["properties"]["siteConfig"]["appSettings"]
        self.assertTrue(serialized.startswith("[map("))
        self.assertIn("string(lambdaVariables('setting').value)", serialized)
        self.assertEqual(
            self.core["parameters"]["teamsWorkflowAllowedHosts"]["defaultValue"], "[[]"
        )

    def test_create_time_json_serialization_has_no_variable_copy_for_any_core_site(self):
        for module_name in ("apiApp", "webApp", "portalApp"):
            site_module = inner(one(self.core, "Microsoft.Resources/deployments", module_name))
            self.assertNotIn("copy", site_module["variables"])
            self.assertNotIn("normalizedAppSettings", site_module["variables"])
            self.assertIn("parameters('appSettings')", site_module["variables"]["siteAppSettings"])
            site = one(site_module, "Microsoft.Web/sites")
            expression = site["properties"]["siteConfig"]["appSettings"]
            self.assertIn("map(variables('siteAppSettings')", expression)
            self.assertIn("string(lambdaVariables('setting').value)", expression)

    def test_new_models_versions_skus_capacity_and_local_auth(self):
        env = self.env()
        account = env["resources"]["account"]
        self.assertTrue(account["properties"]["disableLocalAuth"])
        deployment = env["resources"]["modelDeployment"]
        self.assertEqual(deployment["sku"]["capacity"], 10)
        self.assertEqual(
            deployment["sku"]["name"],
            "[if(equals(parameters('environmentKey'), 'prod'), 'DataZoneStandard', "
            "'GlobalStandard')]",
        )
        self.assertEqual(
            env["variables"]["modelName"],
            "[if(equals(parameters('environmentKey'), 'prod'), 'gpt-5.1', 'gpt-5.6-luna')]",
        )
        self.assertEqual(
            deployment["properties"]["model"]["version"],
            "[if(equals(parameters('environmentKey'), 'prod'), '2025-11-13', '2026-07-09')]",
        )
        self.assertEqual(deployment["properties"]["versionUpgradeOption"], "NoAutoUpgrade")

    def test_core_backing_tags_preserve_supplied_values_only_on_four_owned_resources(self):
        for template in (self.estate, self.core):
            self.assertEqual(template["parameters"]["coreBackingResourceTags"]["defaultValue"], {})
        self.assertEqual(
            self.estate["resources"]["core"]["properties"]["parameters"]["coreBackingResourceTags"],
            {"value": "[parameters('coreBackingResourceTags')]"},
        )
        self.assertEqual(
            self.core["variables"]["backingResourceTags"],
            "[union(variables('sharedTags'), parameters('coreBackingResourceTags'))]",
        )
        backing_modules = {"cosmos", "keyVault", "managementKeyVault"}
        seen = set()
        for module in resources(self.core):
            if module["type"] != "Microsoft.Resources/deployments":
                continue
            supplied = module["properties"]["parameters"]
            if "tags" not in supplied:
                continue
            if module["name"] in backing_modules:
                self.assertEqual(supplied["tags"]["value"], "[variables('backingResourceTags')]")
                seen.add(module["name"])
            else:
                self.assertEqual(supplied["tags"]["value"], "[variables('sharedTags')]")
        self.assertEqual(seen, backing_modules)
        onboarding_tags = str(
            self.estate["resources"]["onboarding"]["properties"]["parameters"]["tags"]
        )
        self.assertIn("parameters('coreBackingResourceTags')", onboarding_tags)
        for symbol in ("coreGroup", "environmentGroups"):
            self.assertNotIn(
                "coreBackingResourceTags", str(self.estate["resources"][symbol]["tags"])
            )
        for symbol in ("core", "environmentStacks"):
            supplied = self.estate["resources"][symbol]["properties"]["parameters"]
            tag_key = "additionalTags" if symbol == "core" else "tags"
            self.assertNotIn("coreBackingResourceTags", str(supplied[tag_key]))
        for expression in (onboarding_tags, self.core["variables"]["backingResourceTags"]):
            self.assertNotIn("utcNow", expression)
            self.assertNotIn("dateTimeAdd", expression)

    def test_owned_production_luna_is_opt_in_prod_only_and_uses_reviewed_sku(self):
        self.assertFalse(self.estate["parameters"]["provisionProductionLuna"]["defaultValue"])
        self.assertEqual(
            self.estate["parameters"]["productionLunaSkuName"]["defaultValue"],
            "DataZoneStandard",
        )
        self.assertEqual(
            self.estate["parameters"]["productionLunaSkuName"]["allowedValues"],
            ["DataZoneStandard", "GlobalStandard"],
        )
        env = self.env()
        self.assertIn("provisionProductionLuna", env["variables"]["createProductionLuna"])
        self.assertIn("'prod'", env["variables"]["createProductionLuna"])
        account = env["resources"]["productionLuna"]
        deployment = env["resources"]["productionLunaDeployment"]
        for resource in (account, deployment):
            self.assertEqual(resource["condition"], "[variables('createProductionLuna')]")
        self.assertIn("ai-mosaic-fed-prod-luna-", account["name"])
        self.assertTrue(account["properties"]["disableLocalAuth"])
        self.assertEqual(account["properties"]["publicNetworkAccess"], "Enabled")
        self.assertEqual(account["tags"], "[parameters('tags')]")
        self.assertEqual(deployment["sku"]["name"], "[parameters('productionLunaSkuName')]")
        self.assertEqual(deployment["sku"]["capacity"], 10)
        self.assertEqual(
            deployment["properties"]["model"],
            {"format": "OpenAI", "name": "gpt-5.6-luna", "version": "2026-07-09"},
        )
        self.assertEqual(deployment["properties"]["versionUpgradeOption"], "NoAutoUpgrade")
        self.assertEqual(account["location"], "[parameters('location')]")
        self.assertIn("if(variables('createProductionLuna')", str(env["outputs"]["productionLuna"]))

    def test_effective_luna_iam_waits_for_new_account_and_preserves_role_contract(self):
        module = self.estate["resources"]["existingLunaAccess"]
        account_id = str(module["properties"]["parameters"]["accountResourceId"])
        self.assertIn("provisionProductionLuna", account_id)
        self.assertIn("productionLuna.value.id", account_id)
        self.assertIn("parameters('reusedLunaResourceId')", account_id)
        self.assertTrue(any("environmentStacks" in dep for dep in module["dependsOn"]))
        for name in ("EFFECTIVE_LUNA_RESOURCE_ID", "PRODUCTION_LUNA"):
            self.assertIn(name, self.estate["outputs"])
        self.assertIn(
            "resourceId", self.estate["outputs"]["ROLE_ASSIGNMENTS"]["value"]["reusedLuna"]
        )
        enabled = str(module["properties"]["parameters"]["enableInference"])
        for gate in ("enableGatewayInference", "deployRuntime", "backendAuthVerified"):
            self.assertIn(gate, enabled)

    def test_reused_resources_have_no_writable_account_or_deployment(self):
        for symbol, parameter in [
            ("existingLunaAccess", "reusedLunaResourceId"),
            ("existingTerraAccess", "reusedTerraResourceId"),
        ]:
            module = self.estate["resources"][symbol]
            self.assertNotIn("resourceGroup", module)
            account_id = str(module["properties"]["parameters"]["accountResourceId"])
            self.assertIn(f"parameters('{parameter}')", account_id)
            self.assertNotIn("defaultValue", self.estate["parameters"][parameter])
            gateway_principal = module["properties"]["parameters"]["gatewayPrincipalId"]["value"]
            self.assertIn("format('environmentStacks[{0}]', 2)", gateway_principal)
            wrapper = inner(module)
            assignment = one(wrapper, "Microsoft.Resources/deployments")
            self.assertEqual(
                assignment["resourceGroup"], "[variables('suppliedResourceGroupName')]"
            )
            access = inner(assignment)
            self.assertEqual(
                {r["type"] for r in resources(access) if not r.get("existing")},
                {"Microsoft.Authorization/roleAssignments"},
            )
            text = json.dumps(access)
            self.assertIn(READER, text)
            self.assertIn(OPENAI_USER, text)

    def test_reused_account_ids_fail_closed_for_wrong_shape_type_or_subscription(self):
        wrapper = inner(self.estate["resources"]["existingLunaAccess"])
        variables = wrapper["variables"]
        self.assertIn("9", variables["validShape"])
        self.assertIn("providers", variables["validProvider"])
        self.assertIn("microsoft.cognitiveservices", variables["validProvider"])
        self.assertIn("accounts", variables["validProvider"])
        self.assertIn("subscription().subscriptionId", variables["validScope"])
        self.assertIn("resourcegroups", variables["validScope"])
        self.assertIn("64", variables["validNames"])
        self.assertIn("90", variables["validNames"])
        for validation in ("validShape", "validProvider", "validScope", "validNames"):
            self.assertIn(validation, variables["validAccountResourceId"])
        assignment = one(wrapper, "Microsoft.Resources/deployments")
        account_name = str(assignment["properties"]["parameters"]["accountName"])
        self.assertIn("validAccountResourceId", account_name)
        self.assertIn("''", account_name)
        self.assertEqual(inner(assignment)["parameters"]["accountName"]["minLength"], 2)

    def test_inference_is_opt_in_after_auth_and_no_runtime_gets_direct_account_access(self):
        self.assertFalse(self.estate["parameters"]["enableGatewayInference"]["defaultValue"])
        self.assertFalse(self.estate["parameters"]["backendAuthVerified"]["defaultValue"])
        module = self.estate["resources"]["modelAccess"]
        enabled = str(module["properties"]["parameters"]["enableInference"]["value"])
        for flag in ("deployRuntime", "backendAuthVerified", "enableGatewayInference"):
            self.assertIn(flag, enabled)
        access = inner(module)
        [inference] = [
            r for r in resources(access) if OPENAI_USER in r["properties"]["roleDefinitionId"]
        ]
        self.assertEqual(inference["condition"], "[parameters('enableInference')]")
        self.assertIn("gatewayPrincipalId", inference["properties"]["principalId"])
        self.assertNotIn("runtimeIdentity", json.dumps(access))
        self.assertIn("environmentStacks", str(module["dependsOn"]))

    def test_one_consumption_environment_and_eight_protected_apps_per_group(self):
        env = self.env()
        managed = one(env, "Microsoft.App/managedEnvironments")
        self.assertEqual(
            managed["properties"]["workloadProfiles"],
            [{"name": "Consumption", "workloadProfileType": "Consumption"}],
        )
        self.assertEqual(
            managed["properties"]["appLogsConfiguration"]["destination"], "azure-monitor"
        )
        self.assertEqual(len(env["variables"]["mcpProfiles"]), 8)
        mcp = env["resources"]["mcpApps"]
        self.assertEqual(mcp["condition"], "[parameters('deployRuntime')]")
        self.assertEqual(mcp["copy"]["count"], "[length(variables('mcpProfiles'))]")
        self.assertFalse(self.estate["parameters"]["deployRuntime"]["defaultValue"])
        self.assertTrue(self.estate["parameters"]["runtimeConfigurations"]["nullable"])
        self.assertNotIn("parameters('runtimeConfiguration').", json.dumps(env))

    def test_each_backend_enforces_tenant_audience_two_callers_and_https(self):
        env = self.env()
        mcp = env["resources"]["mcpApps"]
        auth = one(inner(mcp), "Microsoft.App/containerApps/authConfigs")
        settings = auth["properties"]
        self.assertTrue(settings["platform"]["enabled"])
        self.assertEqual(settings["globalValidation"], {"unauthenticatedClientAction": "Return401"})
        self.assertEqual(settings["httpSettings"], {"requireHttps": True})
        entra = settings["identityProviders"]["azureActiveDirectory"]
        self.assertIn("tenantId", entra["registration"]["openIdIssuer"])
        self.assertEqual(entra["registration"]["clientId"], "[parameters('audienceClientId')]")
        self.assertEqual(len(entra["validation"]["allowedAudiences"]), 2)
        self.assertEqual(
            entra["validation"]["defaultAuthorizationPolicy"],
            {"allowedApplications": "[parameters('allowedClientIds')]"},
        )
        callers = mcp["properties"]["parameters"]["allowedClientIds"]["value"]
        self.assertEqual(len(callers), 2)
        self.assertIn("gatewayClientId", callers[0])
        self.assertIn("mosaicApiClientId", callers[1])
        self.assertFalse(settings["login"]["tokenStore"]["enabled"])
        self.assertIn(
            "backendAudiencesDistinct",
            str(mcp["properties"]["parameters"]["audienceClientId"]),
        )

    def test_backend_auth_live_verification_is_a_separate_model_helper_gate(self):
        settings = self.env()["resources"]["mcpApps"]["properties"]["parameters"]["env"]["value"]
        [gate] = [
            setting for setting in settings if setting["name"] == "FEDERAL_BACKEND_AUTH_VERIFIED"
        ]
        self.assertIn("backendAuthVerified", gate["value"])
        self.assertFalse(self.estate["parameters"]["backendAuthVerified"]["defaultValue"])
        self.assertIn("backendAudiencesDistinct", self.estate["variables"])

    def test_gateway_body_logging_is_disabled(self):
        gateway = inner(self.env()["resources"]["gateway"])
        diagnostic = one(gateway, "Microsoft.ApiManagement/service/diagnostics")
        for side in ("backend", "frontend"):
            for direction in ("request", "response"):
                self.assertEqual(
                    diagnostic["properties"][side][direction],
                    {"body": {"bytes": 0}, "headers": []},
                )

    def test_scale_to_zero_identity_image_pulls_and_no_placeholder_images(self):
        app = one(inner(self.env()["resources"]["mcpApps"]), "Microsoft.App/containerApps")
        config = app["properties"]["configuration"]
        self.assertEqual(app["identity"]["type"], "UserAssigned")
        self.assertEqual(
            config["registries"],
            [
                {
                    "server": "[parameters('registryServer')]",
                    "identity": "[parameters('pullIdentityId')]",
                }
            ],
        )
        self.assertNotIn("secrets", config)
        self.assertFalse(config["ingress"]["allowInsecure"])
        self.assertEqual(
            app["properties"]["template"]["scale"], {"minReplicas": 0, "maxReplicas": 1}
        )
        self.assertEqual(
            app["properties"]["template"]["containers"][0]["image"], "[parameters('image')]"
        )
        self.assertNotIn("aci-helloworld", json.dumps(inner(self.env()["resources"]["mcpApps"])))
        self.assertIn("runtimeImageValid", self.env()["variables"])
        self.assertIn("registryServer", self.env()["variables"]["runtimeImageValid"])
        self.assertIn(":latest", self.env()["variables"]["runtimeImageValid"])
        self.assertIn("gatewayRoutePrefix", self.env()["variables"]["runtimeModelRouteValid"])
        job_image = str(self.env()["resources"]["jobs"]["properties"]["parameters"]["image"])
        self.assertIn("modelEndpoint", job_image)
        self.assertIn("mcpGatewayUrl", job_image)
        self.assertIn("gatewayRoutePrefix", job_image)

    def test_prod_jobs_are_manual_and_bounded_with_distinct_identities(self):
        env = self.env()
        profiles = env["variables"]["workloadProfiles"]
        self.assertIn("environmentKey", str(profiles))
        self.assertIn("'prod'", str(profiles))
        job = one(inner(env["resources"]["jobs"]), "Microsoft.App/jobs")
        config = job["properties"]["configuration"]
        self.assertEqual(config["triggerType"], "Manual")
        self.assertEqual(config["replicaRetryLimit"], 0)
        self.assertEqual(config["replicaTimeout"], 300)
        self.assertEqual(
            config["manualTriggerConfig"], {"parallelism": 1, "replicaCompletionCount": 1}
        )
        self.assertNotIn("scheduleTriggerConfig", config)
        identity = env["resources"]["workloadIdentities"]
        self.assertEqual(identity["copy"]["count"], "[length(variables('workloadProfiles'))]")
        self.assertEqual(
            job["properties"]["template"]["containers"][0]["command"],
            ["python", "-m", "federal_runtime.workload"],
        )

    def test_mcp_and_jobs_select_their_own_user_assigned_identity_client_ids(self):
        for symbol, identity_collection, resource_type in (
            ("mcpApps", "mcpIdentities", "Microsoft.App/containerApps"),
            ("jobs", "workloadIdentities", "Microsoft.App/jobs"),
        ):
            module = self.env()["resources"][symbol]
            client_id = module["properties"]["parameters"]["runtimeIdentityClientId"]["value"]
            self.assertIn(identity_collection, client_id)
            self.assertIn(".clientId", client_id)
            resource = one(inner(module), resource_type)
            container = resource["properties"]["template"]["containers"][0]
            self.assertIn("AZURE_CLIENT_ID", str(container["env"]))
            self.assertIn("runtimeIdentityClientId", str(container["env"]))
            if symbol == "mcpApps":
                self.assertNotIn("command", container)

    def test_onboarding_vault_has_only_operator_data_permissions(self):
        vault = inner(self.estate["resources"]["onboarding"])
        roles = [
            r for r in resources(vault) if r["type"] == "Microsoft.Authorization/roleAssignments"
        ]
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0]["properties"]["principalId"], "[parameters('operatorObjectId')]")
        self.assertNotIn("gatewayPrincipalId", json.dumps(vault))
        self.assertNotIn("apiPrincipalId", json.dumps(vault))

    def test_new_vaults_and_cosmos_explicitly_request_public_identity_protected_access(self):
        vault_templates = [
            inner(one(self.core, "Microsoft.Resources/deployments", module_name))
            for module_name in ("keyVault", "managementKeyVault")
        ]
        onboarding = inner(self.estate["resources"]["onboarding"])
        vault_templates.append(
            inner(one(onboarding, "Microsoft.Resources/deployments", "onboarding-vault"))
        )
        self.assertEqual(len(vault_templates), 3)
        for template in vault_templates:
            vault = one(template, "Microsoft.KeyVault/vaults")
            properties = vault["properties"]
            self.assertEqual(properties["publicNetworkAccess"], "Enabled")
            self.assertTrue(properties["enableRbacAuthorization"])
            self.assertTrue(properties["enablePurgeProtection"])
        cosmos = inner(one(self.core, "Microsoft.Resources/deployments", "cosmos"))
        account = one(cosmos, "Microsoft.DocumentDB/databaseAccounts")
        self.assertEqual(account["properties"]["publicNetworkAccess"], "Enabled")
        self.assertTrue(account["properties"]["disableLocalAuth"])
        self.assertEqual(account["properties"]["minimalTlsVersion"], "Tls12")

    def test_azure_host_and_job_name_lengths(self):
        for env in ENVIRONMENTS:
            for profile in self.env()["variables"]["mcpProfiles"]:
                name = f"mcp-mf-{env}-{profile['shortKey']}"
                self.assertLessEqual(len(name), 32, name)
        for key in ("nasa-brief", "faa-airspace", "faa-standards", "doe-research", "usda-field"):
            self.assertLessEqual(len(f"job-mf-prod-{key}-abcdef"), 32)
        self.assertLessEqual(len("kv-mf-onboard-abcdef"), 24)

    def test_outputs_support_identity_bootstrap_and_governance(self):
        outputs = self.estate["outputs"]
        for name in (
            "CORE",
            "ENVIRONMENTS",
            "RUNTIME_IDENTITIES",
            "ROLE_ASSIGNMENTS",
            "ONBOARDING_KEY_VAULT_URI",
        ):
            self.assertIn(name, outputs)
        self.assertEqual(
            outputs["ENVIRONMENTS"]["copy"]["count"], "[length(variables('environments'))]"
        )
        for name in ("API_APP_NAME", "WEB_APP_URL", "PORTAL_APP_URL", "APIM_RESOURCE_ID"):
            self.assertIn(name, outputs)

    def test_default_single_gateway_entrypoint_still_compiles_and_retains_defaults(self):
        normal = compile_template(Path("infra") / "main.bicep")
        self.assertEqual(normal["parameters"]["deployApim"]["defaultValue"], True)
        self.assertEqual(normal["parameters"]["apimSkuName"]["defaultValue"], "Developer_1")
        self.assertEqual(normal["parameters"]["location"]["defaultValue"], "eastus2")
        for name in ("api", "web", "portal"):
            self.assertIn(f"{name.upper()}_APP_URL", normal["outputs"])


class SurgicalStagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = compile_template(Path("infra") / "federal-demo" / "runtime-only.bicep")
        cls.inference = compile_template(Path("infra") / "federal-demo" / "inference-only.bicep")
        cls.inference_access = one(cls.inference, "Microsoft.Resources/deployments")
        cls.runtime_env = inner(cls.runtime["resources"]["runtimes"])
        root = compile_template(Path("infra") / "federal-demo" / "main.bicep")
        cls.full_env = inner(root["resources"]["environmentStacks"])
        cls.full_access = inner(root["resources"]["modelAccess"])

    def test_runtime_stage_writes_only_the_same_24_mcp_hosts_auth_and_five_manual_jobs(self):
        self.assertEqual(
            {r["type"] for r in created_resources(self.runtime)},
            {
                "Microsoft.App/containerApps",
                "Microsoft.App/containerApps/authConfigs",
                "Microsoft.App/jobs",
            },
        )
        self.assertEqual(
            [env["key"] for env in self.runtime["variables"]["estateEnvironments"]],
            ["dev", "stg", "prod"],
        )
        loop = self.runtime["resources"]["runtimes"]
        self.assertEqual(loop["copy"]["count"], "[length(variables('estateEnvironments'))]")
        self.assertIn("rg-mosaic-federal-", loop["resourceGroup"])
        self.assertEqual(len(self.runtime_env["variables"]["mcpProfiles"]), 8)
        self.assertEqual(
            self.runtime_env["resources"]["mcpApps"]["copy"]["count"],
            "[length(variables('mcpProfiles'))]",
        )
        self.assertEqual(
            self.runtime_env["resources"]["jobs"]["copy"]["count"],
            "[length(variables('workloadProfiles'))]",
        )
        self.assertEqual(
            self.runtime_env["variables"]["workloadProfiles"],
            self.full_env["variables"]["workloadProfiles"],
        )

    def test_runtime_stage_reuses_the_existing_protected_host_and_manual_job_templates(self):
        self.assertEqual(
            self.runtime_env["variables"]["mcpProfiles"],
            self.full_env["variables"]["mcpProfiles"],
        )
        for symbol in ("mcpApps", "jobs"):
            self.assertEqual(
                resources(inner(self.runtime_env["resources"][symbol])),
                resources(inner(self.full_env["resources"][symbol])),
            )
            self.assertEqual(
                self.runtime_env["resources"][symbol]["properties"]["parameters"]["name"],
                self.full_env["resources"][symbol]["properties"]["parameters"]["name"],
            )

    def test_runtime_stage_references_existing_registry_cae_and_runtime_identities_only(self):
        supplied = self.runtime["resources"]["runtimes"]["properties"]["parameters"]
        self.assertIn(
            "parameters('core').registry.loginServer", supplied["registryServer"]["value"]
        )
        self.assertIn("containerAppsEnvironment.id", supplied["environmentId"]["value"])
        self.assertIn("parameters('runtimeIdentities')", supplied["identities"]["value"])
        self.assertIn("referencesValidated", self.runtime_env["parameters"])
        for name in ("validMcpKeys", "validWorkloadKeys", "validMcpScopes", "validWorkloadScopes"):
            self.assertIn(name, self.runtime_env["variables"]["referencesValid"])
        self.assertIn("coreReferenceValid", supplied["referencesValidated"]["value"])
        self.assertIn("backendAudiencesDistinct", self.runtime["variables"])
        for symbol, collection in (("mcpApps", "mcpIdentities"), ("jobs", "workloadIdentities")):
            supplied = self.runtime_env["resources"][symbol]["properties"]["parameters"]
            self.assertIn(collection, supplied["runtimeIdentityClientId"]["value"])
            self.assertIn(".clientId", supplied["runtimeIdentityClientId"]["value"])
            self.assertIn("identities').pull.id", supplied["pullIdentityId"]["value"])

    def test_runtime_stage_keeps_auth_gate_and_real_acr_image_checks(self):
        self.assertFalse(self.runtime["parameters"]["backendAuthVerified"]["defaultValue"])
        mcp = self.runtime_env["resources"]["mcpApps"]
        settings = mcp["properties"]["parameters"]["env"]["value"]
        [gate] = [entry for entry in settings if entry["name"] == "FEDERAL_BACKEND_AUTH_VERIFIED"]
        self.assertIn("backendAuthVerified", gate["value"])
        self.assertIn("runtimeImageValid", str(mcp["properties"]["parameters"]["image"]))
        self.assertIn("referencesValid", self.runtime_env["variables"]["runtimeImageValid"])
        self.assertIn(":latest", self.runtime_env["variables"]["runtimeImageValid"])
        self.assertIn("registryServer", self.runtime_env["variables"]["runtimeImageValid"])
        auth = one(inner(mcp), "Microsoft.App/containerApps/authConfigs")["properties"]
        self.assertEqual(auth["globalValidation"], {"unauthenticatedClientAction": "Return401"})

    def test_runtime_stage_returns_urls_without_any_core_images_tags_or_reprovisioning_inputs(self):
        parameters = self.runtime["parameters"]
        for name in (
            "coreBackingResourceTags",
            "apiPlaceholderImage",
            "webPlaceholderImage",
            "portalPlaceholderImage",
            "provisionProductionLuna",
        ):
            self.assertNotIn(name, parameters)
        for name in ("RUNTIMES", "ENVIRONMENTS", "BACKEND_AUTH_VERIFIED"):
            self.assertIn(name, self.runtime["outputs"])
        merged = str(self.runtime["outputs"]["ENVIRONMENTS"])
        self.assertIn("parameters('environments')", merged)
        self.assertIn("mcpApps", merged)
        self.assertIn("jobs", merged)
        self.assertEqual(self.runtime["parameters"]["location"]["allowedValues"], ["eastus2"])

    def test_inference_stage_has_exactly_the_five_fixed_account_gateway_pairs(self):
        bindings = self.inference["variables"]["bindings"]
        self.assertEqual(
            [binding["key"] for binding in bindings],
            ["dev-luna", "stg-luna", "prod-research", "prod-luna", "prod-terra"],
        )
        for binding, index in zip(bindings, (0, 1, 2, 2, 2), strict=True):
            self.assertEqual(
                binding["gatewayPrincipalId"],
                f"[parameters('environments')[{index}].gateway.principalId]",
            )
        self.assertEqual(
            bindings[3]["accountResourceId"], "[parameters('effectiveLunaResourceId')]"
        )
        self.assertEqual(bindings[4]["accountResourceId"], "[parameters('reusedTerraResourceId')]")
        self.assertEqual(
            self.inference_access["copy"]["count"],
            "[length(variables('bindings'))]",
        )
        self.assertEqual(
            {r["type"] for r in created_resources(self.inference)},
            {"Microsoft.Authorization/roleAssignments"},
        )

    def test_inference_stage_requires_verified_auth_and_rejects_duplicate_or_wrong_scope_refs(self):
        guard = self.inference["parameters"]["backendAuthVerified"]
        self.assertEqual(guard["allowedValues"], [True])
        self.assertNotIn("defaultValue", guard)
        supplied = self.inference_access["properties"]["parameters"]
        self.assertEqual(
            supplied["enableInference"]["value"], "[parameters('backendAuthVerified')]"
        )
        for name in (
            "distinctAccounts",
            "referenceOrderValid",
            "ownedAccountScopesValid",
            "gatewayReferencesValid",
        ):
            self.assertIn(name, str(supplied["accountResourceId"]))
        self.assertIn("5", self.inference["variables"]["distinctAccounts"])
        self.assertEqual(
            supplied["apiPrincipalId"]["value"], "[parameters('core').api.principalId]"
        )

    def test_inference_stage_reuses_identical_scoped_role_ids_and_has_no_network_or_policy_writes(
        self,
    ):
        wrapper = inner(self.inference_access)
        access = inner(one(wrapper, "Microsoft.Resources/deployments"))
        self.assertEqual(resources(access), resources(self.full_access))
        roles = list(created_resources(self.inference))
        self.assertEqual(len(roles), 2)
        self.assertIn(READER, json.dumps(roles))
        self.assertIn(OPENAI_USER, json.dumps(roles))
        self.assertIn("ROLE_ASSIGNMENTS", self.inference["outputs"])


if __name__ == "__main__":
    unittest.main()
