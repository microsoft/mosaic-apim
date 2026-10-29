"""Authorize the existing operator's Azure CLI session for this owned MOSAIC API only."""

from urllib.parse import urlencode

from scripts.federal_demo.operator import OperationError, Operator

AZURE_CLI_CLIENT = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"


def provision(operator: Operator, *, for_model_verification=False) -> None:
    operator.verify_target()
    target = operator.target
    app_id = target["apiAppClientId"]
    apps = operator.items("/applications?" + urlencode({"$filter": f"appId eq '{app_id}'"}))
    expected_tag = f"azd-env:mosaic-{target['estate']}"
    if len(apps) != 1 or expected_tag not in apps[0].get("tags", []):
        raise OperationError("Operator consent is restricted to this estate's owned API app")
    app = apps[0]
    principal_id = target["apiServicePrincipalId"]
    me = operator.graph("GET", "/me?$select=id")
    admin_roles = {role["id"] for role in app["appRoles"] if role["value"] == "Admin"}
    assignments = operator.items(f"/servicePrincipals/{principal_id}/appRoleAssignedTo")
    if not any(
        row["principalId"] == me["id"] and row["appRoleId"] in admin_roles for row in assignments
    ):
        raise OperationError("Current operator lacks an explicit MOSAIC Admin assignment")
    scope_value = "access_as_user"
    state_key = "operatorConsent"
    if for_model_verification:
        bootstrap = operator.state["objects"]["bootstrap"]
        app_id = bootstrap["runtimeClientId"]
        principal_id = bootstrap["runtimePrincipalId"]
        apps = operator.items("/applications?" + urlencode({"$filter": f"appId eq '{app_id}'"}))
        if len(apps) != 1 or expected_tag not in apps[0].get("tags", []):
            raise OperationError("Model verification audience is not owned by this estate")
        app = apps[0]
        scope_value = "Models.Invoke"
        state_key = "operatorModelConsent"
    client = operator.items(
        "/servicePrincipals?"
        + urlencode(
            {
                "$filter": f"appId eq '{AZURE_CLI_CLIENT}'",
            }
        )
    )
    if not client:
        principal = operator.graph("POST", "/servicePrincipals", {"appId": AZURE_CLI_CLIENT})
        operator.remember(
            "operatorCliPrincipal",
            {
                "id": principal["id"],
                "sharedFirstPartyApplication": True,
                "cleanup": "Never delete this shared enterprise application automatically.",
            },
        )
        client = [principal]
    if len(client) != 1:
        raise OperationError("Azure CLI enterprise application is ambiguous")
    api = app["api"]
    scopes = [
        scope["id"]
        for scope in api["oauth2PermissionScopes"]
        if scope["value"] == scope_value and scope["isEnabled"]
    ]
    if len(scopes) != 1:
        raise OperationError("The owned API's delegated operator scope is unavailable")
    authorized = list(api.get("preAuthorizedApplications", []))
    entry = next((item for item in authorized if item["appId"] == AZURE_CLI_CLIENT), None)
    if for_model_verification and state_key not in operator.state["objects"]:
        operator.remember(
            state_key,
            {
                "apiClientId": app_id,
                "applicationObjectId": app["id"],
                "resourceId": principal_id,
                "operatorId": me["id"],
                "clientId": client[0]["id"],
                "scopeId": scopes[0],
                "preauthorizedBefore": bool(entry and scopes[0] in entry["delegatedPermissionIds"]),
                "temporary": True,
            },
        )
    if entry is None:
        authorized.append({"appId": AZURE_CLI_CLIENT, "delegatedPermissionIds": scopes})
    else:
        entry["delegatedPermissionIds"] = sorted(set(entry["delegatedPermissionIds"]) | set(scopes))
    operator.graph(
        "PATCH",
        f"/applications/{app['id']}",
        {
            "api": api | {"preAuthorizedApplications": authorized},
        },
    )
    grants = operator.items(
        "/oauth2PermissionGrants?"
        + urlencode(
            {
                "$filter": f"clientId eq '{client[0]['id']}'",
            }
        )
    )
    existing = next(
        (
            row
            for row in grants
            if row["resourceId"] == principal_id
            and row.get("principalId") == me["id"]
            and row["consentType"] == "Principal"
        ),
        None,
    )
    if for_model_verification:
        recorded = operator.state["objects"][state_key]
        if "grantExistedBefore" not in recorded:
            operator.remember(
                state_key,
                recorded
                | {
                    "grantExistedBefore": existing is not None,
                    "grantScopeBefore": existing["scope"] if existing else None,
                },
            )
    if existing is None:
        created = operator.graph(
            "POST",
            "/oauth2PermissionGrants",
            {
                "clientId": client[0]["id"],
                "resourceId": principal_id,
                "principalId": me["id"],
                "consentType": "Principal",
                "scope": scope_value,
            },
        )
        if for_model_verification:
            operator.remember(
                state_key,
                operator.state["objects"][state_key]
                | {
                    "grantId": created["id"],
                    "createdGrant": True,
                },
            )
    elif scope_value not in existing["scope"].split():
        raise OperationError("Existing delegated consent differs; review before changing it")
    if not for_model_verification:
        operator.remember(
            state_key,
            {
                "apiClientId": app_id,
                "operatorId": me["id"],
                "clientId": AZURE_CLI_CLIENT,
            },
        )


def cleanup_model_verification(operator: Operator) -> None:
    operator.verify_target()
    state = operator.state["objects"].get("operatorModelConsent")
    if not state or state.get("cleanedUp"):
        return
    app = operator.graph("GET", f"/applications/{state['applicationObjectId']}?$select=api")
    api = app["api"]
    authorized = list(api.get("preAuthorizedApplications", []))
    if not state["preauthorizedBefore"]:
        for item in authorized:
            if item["appId"] == AZURE_CLI_CLIENT:
                item["delegatedPermissionIds"] = [
                    identifier
                    for identifier in item["delegatedPermissionIds"]
                    if identifier != state["scopeId"]
                ]
        authorized = [item for item in authorized if item["delegatedPermissionIds"]]
        operator.graph(
            "PATCH",
            f"/applications/{state['applicationObjectId']}",
            {
                "api": api | {"preAuthorizedApplications": authorized},
            },
        )
    if state.get("createdGrant"):
        grant = operator.graph("GET", f"/oauth2PermissionGrants/{state['grantId']}")
        if (
            grant["resourceId"] != state["resourceId"]
            or grant["principalId"] != state["operatorId"]
        ):
            raise OperationError("Temporary consent ownership changed; cleanup stopped")
        remaining = [scope for scope in grant["scope"].split() if scope != "Models.Invoke"]
        if remaining:
            operator.graph(
                "PATCH",
                f"/oauth2PermissionGrants/{state['grantId']}",
                {
                    "scope": " ".join(remaining),
                },
            )
        else:
            operator.graph("DELETE", f"/oauth2PermissionGrants/{state['grantId']}")
    operator.remember("operatorModelConsent", state | {"cleanedUp": True})
