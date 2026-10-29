"""Owned backend audiences. No human, anonymous, or cross-environment backend permission."""

from urllib.parse import urlencode
from uuid import NAMESPACE_URL, uuid5

from scripts.federal_demo.directory import ensure_role
from scripts.federal_demo.estate import ENVIRONMENTS
from scripts.federal_demo.operator import OperationError, Operator


def provision(operator: Operator) -> None:
    operator.verify_target()
    marker = f"mosaic-estate:{operator.target['estate']}"
    for env in ENVIRONMENTS:
        name = f"mosaic-{operator.target['estate']}-{env}-mcp-backend"
        found = operator.items(
            "/applications?" + urlencode({"$filter": f"displayName eq '{name}'"})
        )
        if found and (len(found) != 1 or marker not in found[0].get("tags", [])):
            raise OperationError("Unowned or ambiguous backend application name")
        role_id = str(uuid5(NAMESPACE_URL, f"{marker}/{env}/backend-invoke"))
        app = (
            found[0]
            if found
            else operator.graph(
                "POST",
                "/applications",
                {
                    "displayName": name,
                    "signInAudience": "AzureADMyOrg",
                    "tags": [marker],
                    "api": {"requestedAccessTokenVersion": 2},
                    "appRoles": [
                        {
                            "id": role_id,
                            "allowedMemberTypes": ["Application"],
                            "isEnabled": True,
                            "value": "Backend.Invoke",
                            "displayName": "Invoke protected demo backend",
                            "description": "Matching gateway and MOSAIC discovery identity only.",
                        }
                    ],
                },
            )
        )
        identifier = f"api://{app['appId']}"
        if app.get("identifierUris") != [identifier]:
            operator.graph(
                "PATCH",
                f"/applications/{app['id']}",
                {
                    "identifierUris": [identifier],
                },
            )
        principals = operator.items(
            "/servicePrincipals?"
            + urlencode(
                {
                    "$filter": f"appId eq '{app['appId']}'",
                }
            )
        )
        principal = principals[0] if len(principals) == 1 else None
        if len(principals) > 1:
            raise OperationError("Backend application has ambiguous service principals")
        if principal is None:
            principal = operator.graph(
                "POST",
                "/servicePrincipals",
                {
                    "appId": app["appId"],
                    "appRoleAssignmentRequired": True,
                    "tags": [marker],
                },
            )
        elif not principal.get("appRoleAssignmentRequired"):
            raise OperationError("Existing backend audience does not require assignment")
        operator.remember(
            f"audience:{env}",
            {
                "id": app["id"],
                "clientId": app["appId"],
                "principalId": principal["id"],
                "roleId": role_id,
                "audience": identifier,
            },
        )
        api_principal = operator.target.get("apiPrincipalId")
        gateway_principal = operator.target.get("gatewayPrincipals", {}).get(env)
        if bool(api_principal) != bool(gateway_principal):
            raise OperationError(
                "Backend authorization requires both discovery and gateway identities"
            )
        if api_principal:
            for caller in (api_principal, gateway_principal):
                ensure_role(operator, caller, principal["id"], role_id)
