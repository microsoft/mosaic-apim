"""Explicit creation of owned demo users/groups; never changes existing passwords."""

import secrets
from urllib.parse import quote, urlencode

from scripts.federal_demo.estate import PEOPLE, TEAMS
from scripts.federal_demo.operator import OperationError, Operator


def ensure_role(operator: Operator, principal: str, resource: str, role: str) -> str:
    assignments = operator.items(
        f"/servicePrincipals/{principal}/appRoleAssignments?$select=id,resourceId,appRoleId"
    )
    existing = [
        item for item in assignments if item["resourceId"] == resource and item["appRoleId"] == role
    ]
    if len(existing) > 1:
        raise OperationError("Ambiguous application-role assignment")
    if existing:
        return existing[0]["id"]
    result = operator.graph(
        "POST",
        f"/servicePrincipals/{principal}/appRoleAssignments",
        {
            "principalId": principal,
            "resourceId": resource,
            "appRoleId": role,
        },
    )
    if not isinstance(result, dict) or not result.get("id"):
        raise OperationError("Application-role write outcome uncertain; inspect before retrying")
    return result["id"]


def provision(operator: Operator) -> None:
    operator.verify_target()
    target = operator.target
    vault = target.get("onboardingVault")
    if not vault or not target.get("apiServicePrincipalId") or not target.get("userRoleId"):
        raise OperationError(
            "Directory setup requires the core onboarding vault and API role outputs"
        )
    marker = f"MOSAIC demo:{target['estate']}"
    groups = {}
    # Check every collision before creating the first object.
    found_users = {}
    for person in PEOPLE:
        upn = f"{person.local_part}@{target['domain']}"
        query = urlencode(
            {
                "$filter": f"userPrincipalName eq '{upn}'",
                "$select": "id,userPrincipalName,companyName,accountEnabled",
            }
        )
        found = operator.items(f"/users?{query}")
        if found and (len(found) != 1 or found[0].get("companyName") != marker):
            raise OperationError("UPN collision with an unowned account; no accounts were adopted")
        found_users[upn] = found[0] if found else None
    found_groups = {}
    for team in TEAMS:
        name = f"sg-mosaic-demo-{team.group}"
        query = urlencode({"$filter": f"displayName eq '{name}'"})
        found = operator.items(f"/groups?{query}")
        if found and (len(found) != 1 or found[0].get("description") != marker):
            raise OperationError("Group-name collision with an unowned object")
        found_groups[team.code] = found
    for team in TEAMS:
        name = f"sg-mosaic-demo-{team.group}"
        found = found_groups[team.code]
        group = (
            found[0]
            if found
            else operator.graph(
                "POST",
                "/groups",
                {
                    "displayName": name,
                    "description": marker,
                    "mailEnabled": False,
                    "mailNickname": name,
                    "securityEnabled": True,
                    "groupTypes": [],
                },
            )
        )
        operator.remember(f"group:{team.code}", {"id": group["id"], "name": name})
        groups[team.code] = group["id"]
    for person in PEOPLE:
        upn = f"{person.local_part}@{target['domain']}"
        secret_name = person.local_part.replace(".", "-")
        secret_url = f"https://{vault}.vault.azure.net/secrets/{secret_name}?api-version=7.4"
        user = found_users[upn]
        if user is None:
            password = "Aa1!" + secrets.token_urlsafe(32)
            operator.request(
                "PUT",
                secret_url,
                "https://vault.azure.net",
                {
                    "value": password,
                    "tags": {"estate": target["estate"], "purpose": "initial-only"},
                },
            )
            user = operator.graph(
                "POST",
                "/users",
                {
                    "accountEnabled": True,
                    "displayName": f"{person.name} (MOSAIC demo - {person.agency})",
                    "mailNickname": secret_name,
                    "userPrincipalName": upn,
                    "companyName": marker,
                    "department": f"MOSAIC demo / {person.agency}",
                    "passwordProfile": {
                        "forceChangePasswordNextSignIn": True,
                        "password": password,
                    },
                },
            )
            del password
        else:
            # A resumed run must not reset a used password, or hide lost onboarding credentials.
            operator.request("GET", secret_url, "https://vault.azure.net")
            if not user.get("accountEnabled"):
                raise OperationError(
                    "An existing demo account is disabled; operator review required"
                )
        operator.remember(
            f"user:{person.local_part}",
            {
                "id": user["id"],
                "upn": upn,
                "initialSecretName": secret_name,
            },
        )
        for center in person.centers:
            members = operator.items(f"/groups/{groups[center]}/members?$select=id")
            if not any(member["id"] == user["id"] for member in members):
                operator.graph(
                    "POST",
                    f"/groups/{groups[center]}/members/$ref",
                    {
                        "@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{user['id']}",
                    },
                )
        assignments = operator.items(f"/users/{user['id']}/appRoleAssignments")
        if not any(
            item["resourceId"] == target["apiServicePrincipalId"]
            and item["appRoleId"] == target["userRoleId"]
            for item in assignments
        ):
            operator.graph(
                "POST",
                f"/users/{quote(user['id'])}/appRoleAssignments",
                {
                    "principalId": user["id"],
                    "resourceId": target["apiServicePrincipalId"],
                    "appRoleId": target["userRoleId"],
                },
            )
