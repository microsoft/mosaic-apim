"""Use the existing identity hook and build private parameters for the opt-in profile."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from scripts.federal_demo.estate import SERVICES
from scripts.federal_demo.operator import OperationError, Operator, command
from scripts.federal_demo.workloads import JOBS
from scripts.mosaic_entra import CliRunner, postprovision, preprovision

PARAMETERS = {
    "tenantId": "MOSAIC_TENANT_ID",
    "apiAppClientId": "MOSAIC_API_CLIENT_ID",
    "apiServicePrincipalObjectId": "MOSAIC_API_SERVICE_PRINCIPAL_OBJECT_ID",
    "spaAppClientId": "MOSAIC_SPA_CLIENT_ID",
    "portalAppClientId": "MOSAIC_PORTAL_CLIENT_ID",
    "apiApplicationIdUri": "MOSAIC_API_APPLICATION_ID_URI",
    "apiScope": "MOSAIC_API_SCOPE",
    "modelRuntimeClientId": "MOSAIC_MODEL_RUNTIME_CLIENT_ID",
    "modelClientId": "MOSAIC_MODEL_CLIENT_ID",
    "apimPublisherName": "MOSAIC_APIM_PUBLISHER_NAME",
    "apimPublisherEmail": "MOSAIC_APIM_PUBLISHER_EMAIL",
    "onboardingOperatorObjectId": "MOSAIC_DEPLOYER_OBJECT_ID",
}


def prepare(operator: Operator, destination: Path) -> None:
    operator.verify_target()
    environment = f"mosaic-{operator.target['estate']}"
    prefix = f"{environment}-"
    apps = operator.items(
        "/applications?"
        + urlencode(
            {
                "$filter": f"startswith(displayName,'{prefix}')",
                "$select": "id,displayName,tags",
            }
        )
    )
    expected_names = {
        prefix + kind for kind in ("api", "spa", "portal", "model-runtime", "model-client")
    }
    for app in apps:
        if app["displayName"] in expected_names and f"azd-env:{environment}" not in app.get(
            "tags", []
        ):
            raise OperationError(
                "Unowned registration collision; refusing existing bootstrap writes"
            )
    os.environ["AZURE_DEV_USER_AGENT"] = "microsoft_foundry_skill"
    os.environ["AZURE_LOCATION"] = operator.target.get("location", "eastus2")
    os.environ["AZURE_ENV_NAME"] = environment
    preprovision(CliRunner(), environment)
    values = command(["azd", "env", "get-values", "--environment", environment, "--output", "json"])
    if (
        not isinstance(values, dict)
        or values.get("MOSAIC_TENANT_ID") != operator.target["tenantId"]
    ):
        raise OperationError("Bootstrap outputs do not match the approved tenant")
    parameters = {key: {"value": values[name]} for key, name in PARAMETERS.items()}
    parameters["environmentName"] = {"value": environment}
    if "appServiceLocation" in operator.target:
        parameters["appServiceLocation"] = {"value": operator.target["appServiceLocation"]}
    for key in ("reusedLunaResourceId", "reusedTerraResourceId"):
        parameters[key] = {"value": operator.target[key]}
    for service in ("api", "web", "portal"):
        if service in operator.target.get("images", {}):
            parameters[f"{service}PlaceholderImage"] = {"value": operator.target["images"][service]}
    for name in ("deployRuntime", "backendAuthVerified", "enableGatewayInference"):
        parameters[name] = {"value": False}
    destination.write_text(
        json.dumps(
            {
                "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                "contentVersion": "1.0.0.0",
                "parameters": parameters,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    operator.remember(
        "bootstrap",
        {
            "environment": environment,
            "apiClientId": values["MOSAIC_API_CLIENT_ID"],
            "apiServicePrincipalId": values["MOSAIC_API_SERVICE_PRINCIPAL_OBJECT_ID"],
            "userRoleId": values["MOSAIC_API_USER_ROLE_ID"],
            "runtimeClientId": values["MOSAIC_MODEL_RUNTIME_CLIENT_ID"],
            "runtimePrincipalId": values["MOSAIC_MODEL_RUNTIME_SERVICE_PRINCIPAL_OBJECT_ID"],
            "operatorId": values["MOSAIC_DEPLOYER_OBJECT_ID"],
        },
    )


def bind(operator: Operator, target_path: Path) -> None:
    operator.verify_target()
    result = command(
        [
            "az",
            "deployment",
            "sub",
            "show",
            "--subscription",
            operator.target["subscriptionId"],
            "--name",
            f"mosaic-{operator.target['estate']}",
            "--query",
            "properties.outputs",
            "--output",
            "json",
        ]
    )
    if not isinstance(result, dict) or not result:
        raise OperationError("Estate deployment outputs are not ready")
    outputs = {key.upper(): item["value"] for key, item in result.items()}
    core = outputs["CORE"]
    environments = outputs["ENVIRONMENTS"]
    if {item["key"] for item in environments} != {"dev", "stg", "prod"}:
        raise OperationError("ARM deployment has unexpected environment outputs")
    bootstrap = operator.state["objects"]["bootstrap"]
    target = operator.target | {
        "onboardingVault": outputs["ONBOARDING_KEY_VAULT_NAME"],
        "apiAppClientId": bootstrap["apiClientId"],
        "apiServicePrincipalId": bootstrap["apiServicePrincipalId"],
        "userRoleId": bootstrap["userRoleId"],
        "apiPrincipalId": core["api"]["principalId"],
        "mosaicApiUrl": core["api"]["url"],
        "gateways": {item["key"]: item["gateway"]["id"] for item in environments},
        "gatewayPrincipals": {item["key"]: item["gateway"]["principalId"] for item in environments},
        "coreOutputs": core,
        "environmentOutputs": environments,
        "runtimeIdentities": outputs["RUNTIME_IDENTITIES"],
    }
    new_endpoints = [
        {
            "environment": item["key"],
            "name": f"Federal {item['name']} AI",
            "resourceId": item["model"]["id"],
            "deploymentName": item["model"]["deployment"],
        }
        for item in environments
    ]
    new_endpoints.extend(
        [
            {
                "environment": "prod",
                "name": "Federal Production Luna",
                "resourceId": target["reusedLunaResourceId"],
                "deploymentName": "gpt-5.6-luna",
            },
            {
                "environment": "prod",
                "name": "Federal Production Terra",
                "resourceId": target["reusedTerraResourceId"],
                "deploymentName": "gpt-5.6-terra",
            },
        ]
    )
    target["endpoints"] = new_endpoints
    runtime_bindings(operator, target, environments, outputs["RUNTIME_IDENTITIES"])
    target_path.write_text(json.dumps(target, indent=2) + "\n", encoding="utf-8")
    operator.remember("deploymentOutputs", outputs)
    environment = f"mosaic-{operator.target['estate']}"
    os.environ["AZURE_DEV_USER_AGENT"] = "microsoft_foundry_skill"
    values = command(["azd", "env", "get-values", "--environment", environment, "--output", "json"])
    os.environ.update({key: str(value) for key, value in values.items()})
    for key, value in outputs.items():
        if isinstance(value, str):
            command(["azd", "env", "set", key, value, "--environment", environment])
            os.environ[key] = value
    command(
        [
            "azd",
            "env",
            "set",
            "AZURE_RESOURCE_GROUP",
            core["resourceGroupName"],
            "--environment",
            environment,
        ]
    )
    postprovision(CliRunner(), environment, core["web"]["url"])


def bind_core(operator: Operator, target_path: Path) -> None:
    operator.verify_target()
    result = command(
        [
            "az",
            "deployment",
            "group",
            "show",
            "--subscription",
            operator.target["subscriptionId"],
            "--resource-group",
            "rg-mosaic-federal-core",
            "--name",
            "mosaic-federal-core",
            "--query",
            "properties.outputs",
            "--output",
            "json",
        ]
    )
    if not result:
        raise OperationError("Core deployment outputs are not ready")
    values = {key.upper(): item["value"] for key, item in result.items()}
    bootstrap = operator.state["objects"]["bootstrap"]
    target = operator.target | {
        "apiAppClientId": values["MOSAIC_API_CLIENT_ID"],
        "apiServicePrincipalId": values["MOSAIC_API_SERVICE_PRINCIPAL_OBJECT_ID"],
        "userRoleId": bootstrap["userRoleId"],
        "apiPrincipalId": values["API_APP_PRINCIPAL_ID"],
        "mosaicApiUrl": values["API_APP_URL"],
    }
    # Resolve the vault by estate ownership rather than guessing its deterministic suffix.
    vaults = command(
        [
            "az",
            "keyvault",
            "list",
            "--subscription",
            target["subscriptionId"],
            "--resource-group",
            "rg-mosaic-federal-core",
            "--output",
            "json",
        ]
    )
    owned = [
        vault
        for vault in vaults
        if vault["name"].startswith("kv-mf-onboard-")
        and vault.get("tags", {}).get("mosaic-estate") == target["estate"]
    ]
    if len(owned) != 1:
        raise OperationError("Exactly one owned onboarding vault must exist")
    target["onboardingVault"] = owned[0]["name"]
    target_path.write_text(json.dumps(target, indent=2) + "\n", encoding="utf-8")
    environment = f"mosaic-{target['estate']}"
    os.environ["AZURE_DEV_USER_AGENT"] = "microsoft_foundry_skill"
    settings = command(
        [
            "azd",
            "env",
            "get-values",
            "--environment",
            environment,
            "--output",
            "json",
        ]
    )
    os.environ.update({key: str(value) for key, value in settings.items()})
    os.environ.update({key: value for key, value in values.items() if isinstance(value, str)})
    postprovision(CliRunner(), environment, values["WEB_APP_URL"])
    operator.remember("coreOutputs", values)


def plan(operator: Operator, parameter_path: Path, report_path: Path) -> dict:
    operator.verify_target()
    document = json.loads(parameter_path.read_text())
    temporary_tags = document["parameters"].get("coreBackingResourceTags", {}).get("value", {})
    if temporary_tags.get("SecurityControl") == "Ignore":
        evidence = operator.state["objects"].get("documentedTemporaryException")
        if (
            not evidence
            or temporary_tags.get("MOSAICExceptionExpiresUtc") != evidence["expiresUtc"]
            or datetime.now(UTC) >= datetime.fromisoformat(evidence["expiresUtc"])
        ):
            raise OperationError("Temporary exception is absent, changed, or expired; no renewal")
    result = command(
        [
            "az",
            "deployment",
            "sub",
            "what-if",
            "--subscription",
            operator.target["subscriptionId"],
            "--name",
            f"mosaic-{operator.target['estate']}",
            "--location",
            "eastus2",
            "--template-file",
            r"infra\federal-demo\main.bicep",
            "--parameters",
            f"@{parameter_path}",
            "--no-pretty-print",
            "--output",
            "json",
        ]
    )
    report_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    subscription = f"/subscriptions/{operator.target['subscriptionId']}".casefold()
    groups = [
        f"{subscription}/resourcegroups/rg-mosaic-federal-{name}"
        for name in ("core", "dev", "stg", "prod")
    ]
    role_scopes = [
        operator.target[name].casefold() + "/providers/microsoft.authorization/"
        for name in ("reusedLunaResourceId", "reusedTerraResourceId")
    ]
    counts = {}
    for change in result.get("changes", []):
        kind = change["changeType"]
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "Delete":
            raise OperationError("ARM proposed deletion; deployment stopped for review")
        resource = change["resourceId"].casefold()
        if (
            kind in ("Create", "Modify")
            and not any(
                resource == prefix or resource.startswith(prefix + "/") for prefix in groups
            )
            and not any(resource.startswith(prefix + "roleassignments/") for prefix in role_scopes)
        ):
            raise OperationError("ARM proposed changes outside the approved estate/scoped roles")
    if result.get("status") != "Succeeded" or not result.get("changes"):
        raise OperationError("ARM what-if did not produce a successful nonempty change report")
    operator.remember("armPlan", {"report": str(report_path), "counts": counts})
    return counts


def configure_runtime(operator: Operator, parameter_path: Path) -> None:
    operator.verify_target()
    outputs = operator.state["objects"]["deploymentOutputs"]
    api_id = outputs["CORE"]["api"]["principalId"]
    api_client = operator.graph("GET", f"/servicePrincipals/{api_id}?$select=appId")["appId"]
    configs = {}
    for env in outputs["ENVIRONMENTS"]:
        key = env["key"]
        gateway = env["gateway"]
        gateway_client = operator.graph(
            "GET", f"/servicePrincipals/{gateway['principalId']}?$select=appId"
        )["appId"]
        configs[key] = {
            "image": operator.target["images"]["runtime"],
            "backendAudienceClientId": operator.state["objects"][f"audience:{key}"]["clientId"],
            "gatewayClientId": gateway_client,
            "mosaicApiClientId": api_client,
            "modelEndpoint": gateway["url"].rstrip("/") + "/mosaic/federal/gpt-5-6-luna",
            "modelDeployment": "gpt-5.6-luna",
        }
        if key == "prod":
            configs[key]["workloads"] = {
                name: {
                    "modelEndpoint": gateway["url"].rstrip("/")
                    + "/mosaic/federal/"
                    + definition[2].replace(".", "-"),
                    "modelDeployment": definition[2],
                    "mcpGatewayUrl": gateway["url"].rstrip("/")
                    + "/mosaic/federal/mcp/"
                    + definition[3]
                    + "/mcp",
                }
                for name, definition in JOBS.items()
            }
    document = json.loads(parameter_path.read_text())
    document["parameters"]["runtimeConfigurations"] = {"value": configs}
    document["parameters"]["deployRuntime"] = {"value": True}
    parameter_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    operator.remember(
        "runtimeConfiguration", {"profiles": 24, "jobs": 5, "inferenceEnabled": False}
    )


def runtime_bindings(
    operator: Operator, target: dict, environments: list, identities: list
) -> None:
    bindings = []
    jobs = []
    for env in environments:
        identity = next(item for item in identities if item["environment"] == env["key"])
        for app in env["mcpApps"]:
            service = next(item for item in SERVICES if item.key == app["key"])
            if not app["deployed"]:
                continue
            caller = next(item for item in identity["mcp"] if item["key"] == service.key)
            audience = operator.state["objects"][f"audience:{env['key']}"]
            bindings.append(
                {
                    "environment": env["key"],
                    "serviceKey": service.key,
                    "url": app["url"],
                    "audience": audience["audience"],
                    "principalId": caller["principalId"],
                    "resourceId": (
                        f"/subscriptions/{target['subscriptionId']}/resourceGroups/"
                        f"{env['resourceGroupName']}/providers/Microsoft.App/containerApps/{app['name']}"
                    ),
                }
            )
        for job in env["jobs"]:
            if not job["deployed"]:
                continue
            caller = next(item for item in identity["workloads"] if item["key"] == job["key"])
            jobs.append(
                {
                    "key": job["key"],
                    "resourceId": job["id"],
                    "principalId": caller["principalId"],
                    "clientId": caller["clientId"],
                    "name": job["name"],
                }
            )
    target["mcpBindings"] = bindings
    target["jobBindings"] = jobs


def stage_parameters(
    operator: Operator, target_path: Path, *, inference=False, verified=False
) -> Path:
    operator.verify_target()
    outputs = operator.state["objects"]["deploymentOutputs"]
    if verified or inference:
        proof = operator.state["objects"].get("wrongClientVerification", {})
        if proof.get("denied") != 24 or proof.get("temporaryPermissionsRemoved") is not True:
            raise OperationError("Valid-token wrong-client backend proof is required")
    values = {"core": outputs["CORE"], "environments": outputs["ENVIRONMENTS"]}
    if inference:
        values |= {
            "effectiveLunaResourceId": outputs["EFFECTIVE_LUNA_RESOURCE_ID"],
            "reusedTerraResourceId": operator.target["reusedTerraResourceId"],
            "backendAuthVerified": True,
        }
        name = "federal-inference-parameters.local.json"
    else:
        root = json.loads(target_path.with_name("federal-parameters.local.json").read_text())
        values |= {
            "runtimeIdentities": outputs["RUNTIME_IDENTITIES"],
            "runtimeConfigurations": root["parameters"]["runtimeConfigurations"]["value"],
            "modelRuntimeClientId": operator.state["objects"]["bootstrap"]["runtimeClientId"],
            "tenantId": operator.target["tenantId"],
            "backendAuthVerified": verified,
        }
        name = "federal-runtime-parameters.local.json"
    destination = target_path.with_name(name)
    destination.write_text(
        json.dumps(
            {
                "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                "contentVersion": "1.0.0.0",
                "parameters": {key: {"value": value} for key, value in values.items()},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def bind_runtime(operator: Operator, target_path: Path) -> None:
    operator.verify_target()
    result = command(
        [
            "az",
            "deployment",
            "sub",
            "show",
            "--subscription",
            operator.target["subscriptionId"],
            "--name",
            f"mosaic-{operator.target['estate']}-runtime",
            "--query",
            "properties.outputs",
            "--output",
            "json",
        ]
    )
    if not result:
        raise OperationError("Runtime deployment outputs are not ready")
    outputs = {key.upper(): value["value"] for key, value in result.items()}
    target = dict(operator.target)
    target["environmentOutputs"] = outputs["ENVIRONMENTS"]
    runtime_bindings(operator, target, outputs["ENVIRONMENTS"], target["runtimeIdentities"])
    if len(target["mcpBindings"]) != 24 or len(target["jobBindings"]) != 5:
        raise OperationError("Runtime outputs did not provide the full approved inventory")
    target_path.write_text(json.dumps(target, indent=2) + "\n", encoding="utf-8")
    operator.remember("runtimeOutputs", outputs)
