"""Verify protected owned backends before enabling runtime application roles."""

import hashlib
import json
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from scripts.federal_demo.directory import ensure_role
from scripts.federal_demo.estate import ENVIRONMENTS, SERVICES
from scripts.federal_demo.operator import NoRedirect, OperationError, Operator

GRAPH_APP = "00000003-0000-0000-c000-000000000000"


def expect_refusal(url: str, *, token=None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with build_opener(NoRedirect()).open(Request(url, headers=headers), timeout=90):
            raise OperationError("Protected backend accepted an unexpected caller")
    except HTTPError as error:
        permitted = (401, 403) if token else (401,)
        if error.code not in permitted:
            raise OperationError(f"Backend refusal check returned HTTP {error.code}") from None
        return error.code
    except URLError:
        raise OperationError(
            "Backend unreachable; this is not evidence of access refusal"
        ) from None


def verify_backends(operator: Operator) -> None:
    operator.verify_target()
    target = operator.target
    bindings = target.get("mcpBindings", [])
    expected = {(env, service.key) for env in ENVIRONMENTS for service in SERVICES}
    if len(bindings) != 24 or {(b["environment"], b["serviceKey"]) for b in bindings} != expected:
        raise OperationError("Runtime authorization requires all 24 exact backend bindings")
    bootstrap = operator.state["objects"]["bootstrap"]
    jobs = target.get("jobBindings", [])
    if len(jobs) != 5 or {job["key"] for job in jobs} != {
        "nasa-brief",
        "faa-airspace",
        "faa-standards",
        "doe-research",
        "usda-field",
    }:
        raise OperationError("Exactly five distinct approved job bindings are required")
    api_client = operator.graph(
        "GET", f"/servicePrincipals/{target['apiPrincipalId']}?$select=appId"
    )
    wrong_audience_token = operator.token(f"api://{target['apiAppClientId']}")
    verified = []
    for binding in bindings:
        env = binding["environment"]
        prefix = (
            f"/subscriptions/{target['subscriptionId']}/resourceGroups/"
            f"rg-mosaic-federal-{env}/providers/Microsoft.App/containerApps/"
        )
        if not binding["resourceId"].casefold().startswith(prefix.casefold()):
            raise OperationError("Backend is outside its approved environment resource group")
        app = operator.request(
            "GET",
            "https://management.azure.com" + binding["resourceId"] + "?api-version=2025-01-01",
            "https://management.azure.com/",
        )
        identity = app["identity"]["userAssignedIdentities"]
        if not any(value["principalId"] == binding["principalId"] for value in identity.values()):
            raise OperationError("Backend identity does not match ARM")
        host = app["properties"]["configuration"]["ingress"]["fqdn"]
        if binding["url"] != f"https://{host}/mcp":
            raise OperationError("Backend URL does not match the owned ARM ingress")
        auth = operator.request(
            "GET",
            "https://management.azure.com"
            + binding["resourceId"]
            + "/authConfigs/current?api-version=2025-01-01",
            "https://management.azure.com/",
        )["properties"]
        audience = operator.state["objects"][f"audience:{env}"]
        gateway_id = target["gatewayPrincipals"][env]
        gateway_client = operator.graph("GET", f"/servicePrincipals/{gateway_id}?$select=appId")
        provider = auth["identityProviders"]["azureActiveDirectory"]
        allowed = provider["validation"]["defaultAuthorizationPolicy"]["allowedApplications"]
        if (
            not auth["platform"]["enabled"]
            or auth["globalValidation"]["unauthenticatedClientAction"] != "Return401"
            or provider["registration"]["clientId"] != audience["clientId"]
            or set(allowed) != {api_client["appId"], gateway_client["appId"]}
        ):
            raise OperationError(
                "Hosted backend protection differs from the approved caller policy"
            )
        if audience["clientId"] in (target["apiAppClientId"], bootstrap["runtimeClientId"]):
            raise OperationError("Backend and caller/control-plane audiences must be separate")
        verified.append(
            {
                "environment": env,
                "serviceKey": binding["serviceKey"],
                "anonymousStatus": expect_refusal(binding["url"]),
                "wrongAudienceStatus": expect_refusal(binding["url"], token=wrong_audience_token),
            }
        )
    operator.remember(
        "backendVerification",
        {
            "checkedAt": datetime.now(UTC).isoformat(),
            "checks": verified,
            "bindingsSha256": hashlib.sha256(
                json.dumps(bindings, sort_keys=True).encode()
            ).hexdigest(),
            "wrongClientTokenProof": "tracked separately in wrongClientVerification",
        },
    )


def authorize(operator: Operator) -> None:
    verify_backends(operator)
    target = operator.target
    bindings = target["mcpBindings"]
    jobs = target["jobBindings"]
    bootstrap = operator.state["objects"]["bootstrap"]
    proof = operator.state["objects"].get("wrongClientVerification")
    binding_hash = hashlib.sha256(json.dumps(bindings, sort_keys=True).encode()).hexdigest()
    if (
        not proof
        or proof.get("bindingsSha256") != binding_hash
        or proof.get("denied") != 24
        or proof.get("temporaryPermissionsRemoved") is not True
    ):
        raise OperationError(
            "Valid-token wrong-client verification is incomplete; no model roles enabled"
        )
    runtime = operator.graph(
        "GET", (f"/servicePrincipals/{bootstrap['runtimePrincipalId']}?$select=appRoles")
    )
    roles = {role["value"]: role["id"] for role in runtime["appRoles"] if role["isEnabled"]}
    for binding in bindings:
        service = next(item for item in SERVICES if item.key == binding["serviceKey"])
        if service.calls_model:
            ensure_role(
                operator,
                binding["principalId"],
                bootstrap["runtimePrincipalId"],
                roles["Models.Invoke.Application"],
            )
    for job in jobs:
        for role in ("Models.Invoke.Application", "Mcp.Invoke.Application"):
            ensure_role(operator, job["principalId"], bootstrap["runtimePrincipalId"], roles[role])
    graph = operator.items(
        "/servicePrincipals?" + urlencode({"$filter": f"appId eq '{GRAPH_APP}'"})
    )
    if len(graph) != 1:
        raise OperationError("Microsoft Graph service principal is ambiguous")
    role = next(
        role["id"]
        for role in graph[0]["appRoles"]
        if role["value"] == "Application.Read.All" and role["isEnabled"]
    )
    ensure_role(operator, target["apiPrincipalId"], graph[0]["id"], role)
