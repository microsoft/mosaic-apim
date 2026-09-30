from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
APPLICATION_SELECT = (
    "id,appId,displayName,tags,spa,publicClient,isFallbackPublicClient,api,requiredResourceAccess,"
    "appRoles,groupMembershipClaims"
)
DEFAULT_LOCATION = "eastus2"
DEFAULT_LOCALHOST_REDIRECTS = (
    "http://localhost:3000",
    "http://localhost:5173",
)
DEFAULT_PORTAL_LOCALHOST_REDIRECTS = (
    "http://localhost:3001",
    "http://localhost:5174",
)
API_SCOPE_VALUE = "access_as_user"
APP_ROLE_VALUE = "Admin"
PORTAL_ROLE_VALUE = "User"
MODEL_RUNTIME_SCOPE_VALUE = "Models.Invoke"
MODEL_RUNTIME_ROLE_VALUE = "Models.Invoke.Application"
MCP_RUNTIME_SCOPE_VALUE = "Mcp.Invoke"
MCP_RUNTIME_ROLE_VALUE = "Mcp.Invoke.Application"
MODEL_RUNTIME_SCOPE_VALUES = (MODEL_RUNTIME_SCOPE_VALUE, MCP_RUNTIME_SCOPE_VALUE)
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
GRAPH_APPLICATION_PERMISSION_VALUES = (
    "User.ReadBasic.All",
    "GroupMember.Read.All",
    "AgentIdentity.Read.All",
)
PORTAL_APP_NOTES = "MOSAIC end-user portal application registration managed by azd hooks."
MODEL_CLIENT_NOTES = (
    "MOSAIC public client application registration managed by azd hooks. People sign in with "
    "it to get delegated model-runtime tokens."
)
MODEL_CLIENT_TOGGLE = "MOSAIC_ENTRA_MODEL_CLIENT"
DIRECTORY_LOOKUP_TOGGLE = "MOSAIC_ENTRA_DIRECTORY_LOOKUP"
GROUP_CLAIMS_TOGGLE = "MOSAIC_ENTRA_GROUP_CLAIMS"
# Loopback redirect; Entra accepts any port for http://localhost on public clients.
MODEL_CLIENT_REDIRECT_URI = "http://localhost"
CONSENT_RETRY_DELAYS_SECONDS = (2, 4, 8, 16)
REPLICATION_LAG_MARKERS = (
    "Request_ResourceNotFound",
    "does not exist",
    "Invalid value specified for property 'clientId'",
    "Invalid value specified for property 'resourceId'",
)
DIRECTORY_DENIAL_MARKERS = (
    "Authorization_RequestDenied",
    "Insufficient privileges",
    "Permission being requested requires admin consent",
    "does not have authorization to perform action",
    "Forbidden",
)


class OperationFailed(RuntimeError):
    pass


class DirectoryPermissionDenied(OperationFailed):
    pass


@dataclass(frozen=True)
class EntraContext:
    environment_name: str
    tenant_id: str
    location: str
    deployer_object_id: str
    deployer_display_name: str
    deployer_email: str
    localhost_redirects: tuple[str, ...]
    portal_localhost_redirects: tuple[str, ...] = DEFAULT_PORTAL_LOCALHOST_REDIRECTS

    @property
    def environment_label(self) -> str:
        return (
            self.environment_name[7:]
            if self.environment_name.startswith("mosaic-")
            else self.environment_name
        )

    @property
    def api_display_name(self) -> str:
        return f"mosaic-{self.environment_label}-api"

    @property
    def spa_display_name(self) -> str:
        return f"mosaic-{self.environment_label}-spa"

    @property
    def portal_display_name(self) -> str:
        return f"mosaic-{self.environment_label}-portal"

    @property
    def model_runtime_display_name(self) -> str:
        return f"mosaic-{self.environment_label}-model-runtime"

    @property
    def model_client_display_name(self) -> str:
        return f"mosaic-{self.environment_label}-model-client"

    @property
    def app_tags(self) -> list[str]:
        return [
            "product:MOSAIC",
            f"azd-env:{self.environment_name}",
            "managed-by:azd",
        ]


def deterministic_guid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"https://mosaic.example/{name}"))


def normalize_redirect_uris(redirect_uris: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for value in redirect_uris:
        uri = value.strip()
        if not uri:
            continue
        if uri.endswith("/") and uri != "http://localhost/" and uri != "https://localhost/":
            uri = uri.rstrip("/")
        if uri not in normalized:
            normalized.append(uri)
    return normalized


def application_redirect_uris(application: dict[str, Any], platform: str = "spa") -> list[str]:
    settings = application.get(platform)
    if not isinstance(settings, dict):
        return []
    redirect_uris = settings.get("redirectUris")
    if not isinstance(redirect_uris, list):
        return []
    return [uri for uri in redirect_uris if isinstance(uri, str)]


def application_api_settings(application: dict[str, Any]) -> dict[str, Any]:
    api = application.get("api")
    return api if isinstance(api, dict) else {}


def api_scope_id() -> str:
    return deterministic_guid("entra/api/scope/access-as-user")


def admin_role_id() -> str:
    return deterministic_guid("entra/api/role/admin")


def portal_role_id() -> str:
    return deterministic_guid("entra/api/role/user")


def model_runtime_scope_id() -> str:
    return deterministic_guid("entra/model-runtime/scope/models-invoke")


def model_runtime_role_id() -> str:
    return deterministic_guid("entra/model-runtime/role/models-invoke")


def mcp_runtime_scope_id() -> str:
    return deterministic_guid("entra/model-runtime/scope/mcp-invoke")


def mcp_runtime_role_id() -> str:
    return deterministic_guid("entra/model-runtime/role/mcp-invoke")


def model_runtime_scope_definitions() -> list[dict[str, Any]]:
    return [
        {
            "adminConsentDescription": (
                "Allow the application to invoke MOSAIC-published models on behalf of "
                "the signed-in user, subject to model access grants."
            ),
            "adminConsentDisplayName": "Invoke MOSAIC-published models",
            "id": model_runtime_scope_id(),
            "isEnabled": True,
            "type": "Admin",
            "value": MODEL_RUNTIME_SCOPE_VALUE,
        },
        {
            "adminConsentDescription": (
                "Allow the application to call MCP servers published through MOSAIC on behalf "
                "of the signed-in user."
            ),
            "adminConsentDisplayName": "Call MCP servers published through MOSAIC",
            "id": mcp_runtime_scope_id(),
            "isEnabled": True,
            "type": "Admin",
            "userConsentDescription": "Call MCP servers published through MOSAIC.",
            "userConsentDisplayName": "Call MCP servers published through MOSAIC",
            "value": MCP_RUNTIME_SCOPE_VALUE,
        },
    ]


def model_runtime_role_definitions() -> list[dict[str, Any]]:
    return [
        {
            "allowedMemberTypes": ["Application"],
            "description": (
                "Invoke MOSAIC-published models as an application, subject to model "
                "access grants."
            ),
            "displayName": MODEL_RUNTIME_ROLE_VALUE,
            "id": model_runtime_role_id(),
            "isEnabled": True,
            "value": MODEL_RUNTIME_ROLE_VALUE,
        },
        {
            "allowedMemberTypes": ["Application"],
            "description": "Call MCP servers published through MOSAIC as an application.",
            "displayName": MCP_RUNTIME_ROLE_VALUE,
            "id": mcp_runtime_role_id(),
            "isEnabled": True,
            "value": MCP_RUNTIME_ROLE_VALUE,
        },
    ]


def merge_named_items(
    existing_items: Iterable[dict[str, Any]] | None, required_items: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged = [dict(item) for item in existing_items or ()]
    for required in required_items:
        existing = next(
            (
                item
                for item in merged
                if item.get("id") == required["id"] or item.get("value") == required["value"]
            ),
            None,
        )
        if existing is None:
            merged.append(dict(required))
            continue
        preserved_id = existing.get("id")
        existing.update(required)
        if preserved_id:
            existing["id"] = preserved_id
    return merged


def parse_default_true_toggle(name: str) -> bool:
    raw_value = os.environ.get(name, "")
    value = raw_value.strip().casefold()
    if value in ("", "true"):
        return True
    if value == "false":
        return False
    raise OperationFailed(
        f"Operation read {name} failed: expected true or false but got '{raw_value}'. "
        f"Remediation: run `azd env set {name} true` to enable it (the default), or "
        f"`azd env set {name} false` to skip it, then rerun."
    )


def model_client_enabled() -> bool:
    return parse_default_true_toggle(MODEL_CLIENT_TOGGLE)


def directory_lookup_enabled() -> bool:
    return parse_default_true_toggle(DIRECTORY_LOOKUP_TOGGLE)


def group_claims_enabled() -> bool:
    return parse_default_true_toggle(GROUP_CLAIMS_TOGGLE)


def group_membership_claims_patch(
    existing_value: Any, *, display_name: str, operation_name: str
) -> str | None:
    if not group_claims_enabled():
        return None
    if existing_value is None:
        return "SecurityGroup"
    value = str(existing_value).strip()
    if value == "" or value.casefold() == "none":
        return "SecurityGroup"
    values = {part.strip().casefold() for part in value.split(",") if part.strip()}
    if value.casefold() == "all" or "securitygroup" in values:
        return value
    print(
        f"WARNING: {display_name} has groupMembershipClaims set to '{value}'. Microsoft Learn "
        "documents groupMembershipClaims as a single value: None, SecurityGroup, "
        "ApplicationGroup, DirectoryRole, or All. MOSAIC did not change it. Set it to "
        "'SecurityGroup' or 'All' if this application should emit security-group object IDs "
        "in access tokens.",
        file=sys.stderr,
    )
    return None


def build_api_app_payload(
    display_name: str,
    identifier_uri: str,
    tags: list[str],
    preauthorized_client_ids: Iterable[str] | None = None,
    existing_api: dict[str, Any] | None = None,
    existing_group_membership_claims: Any = None,
) -> dict[str, Any]:
    preauthorized_applications = [
        {
            **application,
            "delegatedPermissionIds": list(application.get("delegatedPermissionIds", [])),
        }
        for application in (existing_api or {}).get("preAuthorizedApplications", [])
    ]
    for client_id in normalize_client_ids(preauthorized_client_ids):
        existing = next(
            (item for item in preauthorized_applications if item["appId"] == client_id),
            None,
        )
        if existing is None:
            preauthorized_applications.append(
                {"appId": client_id, "delegatedPermissionIds": [api_scope_id()]}
            )
        elif api_scope_id() not in existing["delegatedPermissionIds"]:
            existing["delegatedPermissionIds"].append(api_scope_id())
    payload: dict[str, Any] = {
        "displayName": display_name,
        "identifierUris": [identifier_uri],
        "notes": "MOSAIC API application registration managed by azd hooks.",
        "signInAudience": "AzureADMyOrg",
        "tags": tags,
        "api": {
            **(existing_api or {}),
            "requestedAccessTokenVersion": 2,
            "preAuthorizedApplications": preauthorized_applications,
            "oauth2PermissionScopes": [
                {
                    "adminConsentDescription": (
                        "Allow the MOSAIC SPA to access the MOSAIC API on behalf of "
                        "the signed-in user."
                    ),
                    "adminConsentDisplayName": "Access MOSAIC API",
                    "id": api_scope_id(),
                    "isEnabled": True,
                    "type": "User",
                    "userConsentDescription": "Allow the application to access MOSAIC as you.",
                    "userConsentDisplayName": "Access MOSAIC on your behalf",
                    "value": API_SCOPE_VALUE,
                }
            ],
        },
        "appRoles": [
            {
                "allowedMemberTypes": ["User", "Application"],
                "description": "MOSAIC tenant administrator.",
                "displayName": APP_ROLE_VALUE,
                "id": admin_role_id(),
                "isEnabled": True,
                "value": APP_ROLE_VALUE,
            },
            {
                "allowedMemberTypes": ["User", "Application"],
                "description": (
                    "MOSAIC end user. Grants the portal, not the administrator console. "
                    "Assign it to an Entra group to onboard users in bulk."
                ),
                "displayName": PORTAL_ROLE_VALUE,
                "id": portal_role_id(),
                "isEnabled": True,
                "value": PORTAL_ROLE_VALUE,
            },
        ],
    }
    group_claims = group_membership_claims_patch(
        existing_group_membership_claims,
        display_name=display_name,
        operation_name="configure MOSAIC API group claims",
    )
    if group_claims is not None:
        payload["groupMembershipClaims"] = group_claims
    return payload


def build_model_runtime_app_payload(
    display_name: str,
    identifier_uri: str,
    tags: list[str],
    existing_api: dict[str, Any] | None = None,
    existing_app_roles: Iterable[dict[str, Any]] | None = None,
    existing_group_membership_claims: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "displayName": display_name,
        "identifierUris": [identifier_uri],
        "notes": "MOSAIC model-runtime API application registration managed by azd hooks.",
        "signInAudience": "AzureADMyOrg",
        "tags": tags,
        "api": {
            # Pre-authorization stays operator-maintained. The optional model client gets a
            # separate, revocable tenant-wide grant instead (see ensure_model_client_consent).
            **(existing_api or {}),
            "requestedAccessTokenVersion": 2,
            "oauth2PermissionScopes": merge_named_items(
                (existing_api or {}).get("oauth2PermissionScopes"),
                model_runtime_scope_definitions(),
            ),
        },
        "appRoles": merge_named_items(existing_app_roles, model_runtime_role_definitions()),
    }
    group_claims = group_membership_claims_patch(
        existing_group_membership_claims,
        display_name=display_name,
        operation_name="configure model-runtime group claims",
    )
    if group_claims is not None:
        payload["groupMembershipClaims"] = group_claims
    return payload


def normalize_client_ids(client_ids: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    for client_id in client_ids or ():
        value = client_id.strip()
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def merge_required_scope(
    existing_required_resource_access: Iterable[dict[str, Any]] | None,
    resource_app_id: str,
    scope_id: str,
) -> list[dict[str, Any]]:
    return merge_required_scopes(
        existing_required_resource_access,
        resource_app_id=resource_app_id,
        scope_ids=[scope_id],
    )


def merge_required_scopes(
    existing_required_resource_access: Iterable[dict[str, Any]] | None,
    resource_app_id: str,
    scope_ids: Iterable[str],
) -> list[dict[str, Any]]:
    required_resource_access = [
        {**resource, "resourceAccess": list(resource.get("resourceAccess", []))}
        for resource in existing_required_resource_access or ()
    ]
    resource_access = next(
        (
            resource
            for resource in required_resource_access
            if resource["resourceAppId"] == resource_app_id
        ),
        None,
    )
    scopes = [{"id": scope_id, "type": "Scope"} for scope_id in scope_ids]
    if resource_access is None:
        required_resource_access.append(
            {"resourceAppId": resource_app_id, "resourceAccess": scopes}
        )
    else:
        for scope in scopes:
            if scope not in resource_access["resourceAccess"]:
                resource_access["resourceAccess"].append(scope)
    return required_resource_access


def build_spa_app_payload(
    display_name: str,
    api_application_client_id: str,
    redirect_uris: list[str],
    tags: list[str],
    notes: str = "MOSAIC SPA application registration managed by azd hooks.",
    existing_required_resource_access: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "displayName": display_name,
        "isFallbackPublicClient": False,
        "notes": notes,
        "requiredResourceAccess": merge_required_scope(
            existing_required_resource_access,
            resource_app_id=api_application_client_id,
            scope_id=api_scope_id(),
        ),
        "signInAudience": "AzureADMyOrg",
        "spa": {
            "redirectUris": redirect_uris,
        },
        "tags": tags,
    }


def build_model_client_app_payload(
    display_name: str,
    model_runtime_client_id: str,
    tags: list[str],
    existing_redirect_uris: Iterable[str] | None = None,
    existing_required_resource_access: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # A public client: no secrets, certificates, app roles or exposed API. Operator-added
    # redirects and permissions are kept, as for the SPA registrations.
    return {
        "displayName": display_name,
        "isFallbackPublicClient": True,
        "notes": MODEL_CLIENT_NOTES,
        "publicClient": {
            "redirectUris": normalize_redirect_uris(
                [MODEL_CLIENT_REDIRECT_URI, *(existing_redirect_uris or ())]
            ),
        },
        "requiredResourceAccess": merge_required_scopes(
            existing_required_resource_access,
            resource_app_id=model_runtime_client_id,
            scope_ids=[model_runtime_scope_id(), mcp_runtime_scope_id()],
        ),
        "signInAudience": "AzureADMyOrg",
        "tags": tags,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bootstrap MOSAIC Entra applications for azd.")
    parser.add_argument("command", choices=("preprovision", "postprovision"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print intended changes without mutating Entra or azd env state.",
    )
    parser.add_argument("--environment-name", help="Override the azd environment name.")
    parser.add_argument(
        "--deployed-web-url", help="Override the deployed MOSAIC web URL for postprovision."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = parse_args(argv or sys.argv[1:])
    runner = CliRunner(dry_run=options.dry_run)
    if options.command == "preprovision":
        preprovision(runner, options.environment_name)
    else:
        postprovision(runner, options.environment_name, options.deployed_web_url)
    return 0


def preprovision(runner: CliRunner, environment_name_override: str | None) -> None:
    manage_model_client = model_client_enabled()
    ensure_location_seeded(runner)
    context = build_context(runner, environment_name_override)

    api_app = ensure_application(
        runner=runner,
        display_name=context.api_display_name,
        create_payload={
            "displayName": context.api_display_name,
            "signInAudience": "AzureADMyOrg",
            "tags": context.app_tags,
        },
        patch_builder=lambda existing: build_api_app_payload(
            display_name=context.api_display_name,
            identifier_uri=f"api://{existing['appId']}",
            tags=context.app_tags,
            existing_api=application_api_settings(existing),
            existing_group_membership_claims=existing.get("groupMembershipClaims"),
        ),
        operation_name="create-or-update API application registration",
    )
    api_sp = ensure_service_principal(
        runner,
        app_id=api_app["appId"],
        tags=context.app_tags,
        operation_name="create-or-update API service principal",
    )

    spa_app = ensure_application(
        runner=runner,
        display_name=context.spa_display_name,
        create_payload={
            "displayName": context.spa_display_name,
            "signInAudience": "AzureADMyOrg",
            "tags": context.app_tags,
        },
        patch_builder=lambda existing: build_spa_app_payload(
            display_name=context.spa_display_name,
            api_application_client_id=api_app["appId"],
            redirect_uris=normalize_redirect_uris(
                [
                    *context.localhost_redirects,
                    *application_redirect_uris(existing),
                ]
            ),
            tags=context.app_tags,
            existing_required_resource_access=existing.get("requiredResourceAccess"),
        ),
        operation_name="create-or-update SPA application registration",
    )
    spa_sp = ensure_service_principal(
        runner,
        app_id=spa_app["appId"],
        tags=context.app_tags,
        operation_name="create-or-update SPA service principal",
    )

    portal_app = ensure_application(
        runner=runner,
        display_name=context.portal_display_name,
        create_payload={
            "displayName": context.portal_display_name,
            "signInAudience": "AzureADMyOrg",
            "tags": context.app_tags,
        },
        patch_builder=lambda existing: build_spa_app_payload(
            display_name=context.portal_display_name,
            api_application_client_id=api_app["appId"],
            redirect_uris=normalize_redirect_uris(
                [
                    *context.portal_localhost_redirects,
                    *application_redirect_uris(existing),
                ]
            ),
            tags=context.app_tags,
            notes=PORTAL_APP_NOTES,
            existing_required_resource_access=existing.get("requiredResourceAccess"),
        ),
        operation_name="create-or-update portal application registration",
    )
    portal_sp = ensure_service_principal(
        runner,
        app_id=portal_app["appId"],
        tags=context.app_tags,
        operation_name="create-or-update portal service principal",
    )

    graph_request(
        runner,
        "PATCH",
        f"/applications/{api_app['id']}",
        body=build_api_app_payload(
            display_name=context.api_display_name,
            identifier_uri=f"api://{api_app['appId']}",
            tags=context.app_tags,
            preauthorized_client_ids=[spa_app["appId"], portal_app["appId"]],
            existing_api=application_api_settings(api_app),
            existing_group_membership_claims=api_app.get("groupMembershipClaims"),
        ),
        operation_name="pre-authorize SPA and portal delegated access to API",
    )

    model_runtime_app = ensure_application(
        runner=runner,
        display_name=context.model_runtime_display_name,
        create_payload={
            "displayName": context.model_runtime_display_name,
            "signInAudience": "AzureADMyOrg",
            "tags": context.app_tags,
        },
        patch_builder=lambda existing: build_model_runtime_app_payload(
            display_name=context.model_runtime_display_name,
            identifier_uri=f"api://{existing['appId']}",
            tags=context.app_tags,
            existing_api=application_api_settings(existing),
            existing_app_roles=existing.get("appRoles"),
            existing_group_membership_claims=existing.get("groupMembershipClaims"),
        ),
        operation_name="create-or-update model-runtime API application registration",
    )
    model_runtime_sp = ensure_service_principal(
        runner,
        app_id=model_runtime_app["appId"],
        tags=context.app_tags,
        operation_name="create-or-update model-runtime API service principal",
    )

    model_client_app: dict[str, Any] | None = None
    model_client_sp: dict[str, Any] | None = None
    if manage_model_client:
        model_client_app = ensure_application(
            runner=runner,
            display_name=context.model_client_display_name,
            create_payload={
                "displayName": context.model_client_display_name,
                "signInAudience": "AzureADMyOrg",
                "tags": context.app_tags,
            },
            patch_builder=lambda existing: build_model_client_app_payload(
                display_name=context.model_client_display_name,
                model_runtime_client_id=model_runtime_app["appId"],
                tags=context.app_tags,
                existing_redirect_uris=application_redirect_uris(existing, "publicClient"),
                existing_required_resource_access=existing.get("requiredResourceAccess"),
            ),
            operation_name="create-or-update model client application registration",
        )
        model_client_sp = ensure_service_principal(
            runner,
            app_id=model_client_app["appId"],
            tags=context.app_tags,
            operation_name="create-or-update model client service principal",
        )
    else:
        configured_client_id = os.environ.get("MOSAIC_MODEL_CLIENT_ID") or "not set"
        print(
            f"{MODEL_CLIENT_TOGGLE} is false; skipping the {context.model_client_display_name} "
            "registration and its consent. Existing registrations, grants and "
            f"MOSAIC_MODEL_CLIENT_ID ({configured_client_id}) are left unchanged.",
            file=sys.stderr,
        )

    ensure_user_admin_role_assignment(
        runner=runner,
        user_object_id=context.deployer_object_id,
        api_service_principal_object_id=api_sp["id"],
        operation_name="assign deploying user Admin app role",
    )

    if model_client_app is not None and model_client_sp is not None:
        ensure_model_client_consent(
            runner,
            client_display_name=context.model_client_display_name,
            client_app_id=model_client_app["appId"],
            client_service_principal_id=model_client_sp["id"],
            runtime_app_id=model_runtime_app["appId"],
            runtime_service_principal_id=model_runtime_sp["id"],
        )

    set_azd_env(runner, "AZURE_LOCATION", context.location)
    set_azd_env(runner, "MOSAIC_TENANT_ID", context.tenant_id)
    set_azd_env(runner, "MOSAIC_API_APP_OBJECT_ID", api_app["id"])
    set_azd_env(runner, "MOSAIC_API_CLIENT_ID", api_app["appId"])
    set_azd_env(runner, "MOSAIC_API_SERVICE_PRINCIPAL_OBJECT_ID", api_sp["id"])
    set_azd_env(runner, "MOSAIC_API_APPLICATION_ID_URI", f"api://{api_app['appId']}")
    set_azd_env(runner, "MOSAIC_API_SCOPE", f"api://{api_app['appId']}/{API_SCOPE_VALUE}")
    set_azd_env(runner, "MOSAIC_API_SCOPE_ID", api_scope_id())
    set_azd_env(runner, "MOSAIC_API_ADMIN_ROLE_ID", admin_role_id())
    set_azd_env(runner, "MOSAIC_API_USER_ROLE_ID", portal_role_id())
    set_azd_env(runner, "MOSAIC_SPA_APP_OBJECT_ID", spa_app["id"])
    set_azd_env(runner, "MOSAIC_SPA_CLIENT_ID", spa_app["appId"])
    set_azd_env(runner, "MOSAIC_SPA_SERVICE_PRINCIPAL_OBJECT_ID", spa_sp["id"])
    set_azd_env(runner, "MOSAIC_SPA_LOCALHOST_REDIRECT_URIS", ",".join(context.localhost_redirects))
    set_azd_env(runner, "MOSAIC_PORTAL_APP_OBJECT_ID", portal_app["id"])
    set_azd_env(runner, "MOSAIC_PORTAL_CLIENT_ID", portal_app["appId"])
    set_azd_env(runner, "MOSAIC_PORTAL_SERVICE_PRINCIPAL_OBJECT_ID", portal_sp["id"])
    set_azd_env(
        runner,
        "MOSAIC_PORTAL_LOCALHOST_REDIRECT_URIS",
        ",".join(context.portal_localhost_redirects),
    )
    set_azd_env(runner, "MOSAIC_MODEL_RUNTIME_APP_OBJECT_ID", model_runtime_app["id"])
    set_azd_env(runner, "MOSAIC_MODEL_RUNTIME_CLIENT_ID", model_runtime_app["appId"])
    set_azd_env(runner, "MOSAIC_MODEL_RUNTIME_SERVICE_PRINCIPAL_OBJECT_ID", model_runtime_sp["id"])
    set_azd_env(
        runner, "MOSAIC_MODEL_RUNTIME_APPLICATION_ID_URI", f"api://{model_runtime_app['appId']}"
    )
    set_azd_env(
        runner,
        "MOSAIC_MODEL_RUNTIME_SCOPE",
        f"api://{model_runtime_app['appId']}/{MODEL_RUNTIME_SCOPE_VALUE}",
    )
    set_azd_env(runner, "MOSAIC_MODEL_RUNTIME_SCOPE_ID", model_runtime_scope_id())
    set_azd_env(runner, "MOSAIC_MODEL_RUNTIME_ROLE_ID", model_runtime_role_id())
    set_azd_env(
        runner,
        "MOSAIC_MCP_RUNTIME_SCOPE",
        f"api://{model_runtime_app['appId']}/{MCP_RUNTIME_SCOPE_VALUE}",
    )
    set_azd_env(runner, "MOSAIC_MCP_RUNTIME_SCOPE_ID", mcp_runtime_scope_id())
    set_azd_env(runner, "MOSAIC_MCP_RUNTIME_ROLE_ID", mcp_runtime_role_id())
    if model_client_app is not None and model_client_sp is not None:
        set_azd_env(runner, "MOSAIC_MODEL_CLIENT_APP_OBJECT_ID", model_client_app["id"])
        set_azd_env(runner, "MOSAIC_MODEL_CLIENT_ID", model_client_app["appId"])
        set_azd_env(
            runner, "MOSAIC_MODEL_CLIENT_SERVICE_PRINCIPAL_OBJECT_ID", model_client_sp["id"]
        )
    set_azd_env(runner, "MOSAIC_DEPLOYER_OBJECT_ID", context.deployer_object_id)
    set_azd_env(runner, "MOSAIC_APIM_PUBLISHER_NAME", context.deployer_display_name)
    set_azd_env(runner, "MOSAIC_APIM_PUBLISHER_EMAIL", context.deployer_email)


def postprovision(
    runner: CliRunner,
    environment_name_override: str | None,
    deployed_web_url_override: str | None,
) -> None:
    context = build_context(runner, environment_name_override)
    if runner.dry_run:
        runner.seed_dry_run_applications(context)
    deployed_web_url = deployed_web_url_override or os.environ.get("WEB_APP_URL")
    if not deployed_web_url:
        raise OperationFailed(
            "Operation patch deployed SPA redirect URI failed: WEB_APP_URL was not "
            "available from the azd environment. "
            "Remediation: ensure infra/main.bicep outputs WEB_APP_URL and rerun `azd provision`."
        )

    patch_deployed_redirect(
        runner,
        context=context,
        display_name=context.spa_display_name,
        localhost_redirects=context.localhost_redirects,
        deployed_url=deployed_web_url,
        azd_env_key="MOSAIC_SPA_DEPLOYED_REDIRECT_URI",
        operation_label="SPA",
    )

    # The portal registration always exists, but its deployed redirect only exists once the portal
    # web app is deployed. Absent PORTAL_APP_URL there is no redirect to add, which is a different
    # thing from skipping identity setup.
    deployed_portal_url = os.environ.get("PORTAL_APP_URL")
    if deployed_portal_url:
        patch_deployed_redirect(
            runner,
            context=context,
            display_name=context.portal_display_name,
            localhost_redirects=context.portal_localhost_redirects,
            deployed_url=deployed_portal_url,
            azd_env_key="MOSAIC_PORTAL_DEPLOYED_REDIRECT_URI",
            operation_label="portal",
            notes=PORTAL_APP_NOTES,
        )
    else:
        print(
            "PORTAL_APP_URL was not set; the MOSAIC portal registration keeps only its localhost "
            "redirects until the portal web app is deployed.",
            file=sys.stderr,
        )

    if directory_lookup_enabled():
        api_managed_identity_principal_id = os.environ.get("API_APP_PRINCIPAL_ID")
        if runner.dry_run and not api_managed_identity_principal_id:
            api_managed_identity_principal_id = deterministic_guid(
                f"dry-run/api-managed-identity/{context.environment_name}"
            )
        if not api_managed_identity_principal_id:
            raise OperationFailed(
                "Operation grant Microsoft Graph application permissions failed: "
                "API_APP_PRINCIPAL_ID was not available from the azd environment. "
                "Remediation: ensure infra/main.bicep outputs API_APP_PRINCIPAL_ID and rerun "
                "`azd provision`."
            )
        ensure_api_managed_identity_graph_permissions(
            runner,
            managed_identity_principal_id=api_managed_identity_principal_id,
        )
    else:
        print(
            f"{DIRECTORY_LOOKUP_TOGGLE} is false; skipping Microsoft Graph application "
            "permission grants for the MOSAIC API managed identity.",
            file=sys.stderr,
        )


def patch_deployed_redirect(
    runner: CliRunner,
    *,
    context: EntraContext,
    display_name: str,
    localhost_redirects: tuple[str, ...],
    deployed_url: str,
    azd_env_key: str,
    operation_label: str,
    notes: str | None = None,
) -> None:
    operation_name = f"patch deployed {operation_label} redirect URI"
    application = find_application(
        runner,
        display_name,
        context.app_tags,
        f"read {operation_label} application registration",
    )
    if application is None:
        raise OperationFailed(
            f"Operation {operation_name} failed: the MOSAIC {operation_label} application "
            "registration does not exist. Remediation: run the preprovision hook or rerun "
            "`azd provision` so the app registrations are created first."
        )

    redirect_uris = normalize_redirect_uris(
        [
            *localhost_redirects,
            *application_redirect_uris(application),
            deployed_url,
        ]
    )
    payload_kwargs = {"notes": notes} if notes else {}
    payload = build_spa_app_payload(
        display_name=display_name,
        api_application_client_id=read_env_value("MOSAIC_API_CLIENT_ID", required=True),
        redirect_uris=redirect_uris,
        tags=context.app_tags,
        existing_required_resource_access=application.get("requiredResourceAccess"),
        **payload_kwargs,
    )
    graph_request(
        runner,
        "PATCH",
        f"/applications/{application['id']}",
        body=payload,
        operation_name=operation_name,
    )
    set_azd_env(runner, azd_env_key, deployed_url)


def build_context(runner: CliRunner, environment_name_override: str | None) -> EntraContext:
    account = runner.az_json(
        ["account", "show"],
        operation_name="read Azure account context",
    )
    environment_name = environment_name_override or os.environ.get("AZURE_ENV_NAME")
    if not environment_name:
        raise OperationFailed(
            "Operation resolve azd environment failed: AZURE_ENV_NAME was not set. "
            "Remediation: run this script through azd hooks or pass --environment-name explicitly."
        )
    if str(account.get("user", {}).get("type", "")).lower() != "user":
        raise OperationFailed(
            "Operation resolve deploying user failed: the current Azure CLI principal is not "
            "a user account. Remediation: sign in with `az login` as a user who can create "
            "app registrations and assign app roles, then rerun."
        )

    me = graph_request(
        runner,
        "GET",
        "/me?$select=id,displayName,userPrincipalName,mail",
        operation_name="read deploying user from Microsoft Graph",
    )
    deployer_email = me.get("mail") or me.get("userPrincipalName")
    if not deployer_email:
        raise OperationFailed(
            "Operation resolve deploying user email failed: Microsoft Graph returned no mail "
            "or userPrincipalName. "
            "Remediation: ensure the deploying user has a valid UPN and rerun."
        )

    extra_redirects = os.environ.get("MOSAIC_SPA_LOCALHOST_REDIRECT_URIS", "")
    redirect_values = [
        *DEFAULT_LOCALHOST_REDIRECTS,
        *(value for value in extra_redirects.split(",") if value),
    ]
    extra_portal_redirects = os.environ.get("MOSAIC_PORTAL_LOCALHOST_REDIRECT_URIS", "")
    portal_redirect_values = [
        *DEFAULT_PORTAL_LOCALHOST_REDIRECTS,
        *(value for value in extra_portal_redirects.split(",") if value),
    ]
    return EntraContext(
        environment_name=environment_name,
        tenant_id=account["tenantId"],
        location=os.environ.get("AZURE_LOCATION", DEFAULT_LOCATION),
        deployer_object_id=me["id"],
        deployer_display_name=me.get("displayName") or deployer_email,
        deployer_email=deployer_email,
        localhost_redirects=tuple(normalize_redirect_uris(redirect_values)),
        portal_localhost_redirects=tuple(normalize_redirect_uris(portal_redirect_values)),
    )


def ensure_location_seeded(runner: CliRunner) -> None:
    if not os.environ.get("AZURE_LOCATION"):
        set_azd_env(runner, "AZURE_LOCATION", DEFAULT_LOCATION)
        os.environ["AZURE_LOCATION"] = DEFAULT_LOCATION


def ensure_application(
    runner: CliRunner,
    display_name: str,
    create_payload: dict[str, Any],
    patch_builder: Any,
    operation_name: str,
) -> dict[str, Any]:
    app = find_application(runner, display_name, create_payload.get("tags", []), operation_name)
    if app is None:
        app = graph_request(
            runner,
            "POST",
            "/applications",
            body=create_payload,
            operation_name=operation_name,
        )
    graph_request(
        runner,
        "PATCH",
        f"/applications/{app['id']}",
        body=patch_builder(app),
        operation_name=operation_name,
    )
    result = graph_request(
        runner,
        "GET",
        f"/applications/{app['id']}?$select={APPLICATION_SELECT}",
        operation_name=f"read back {display_name}",
    )
    if not isinstance(result, dict):
        raise OperationFailed(f"Operation {operation_name} returned an invalid application.")
    return result


def ensure_service_principal(
    runner: CliRunner,
    app_id: str,
    tags: list[str],
    operation_name: str,
) -> dict[str, Any]:
    sp = find_service_principal(runner, app_id, operation_name)
    if sp is None:
        sp = graph_request(
            runner,
            "POST",
            "/servicePrincipals",
            body={"appId": app_id, "tags": tags},
            operation_name=operation_name,
        )
    else:
        graph_request(
            runner,
            "PATCH",
            f"/servicePrincipals/{sp['id']}",
            body={"tags": tags},
            operation_name=operation_name,
        )
    result = graph_request(
        runner,
        "GET",
        f"/servicePrincipals/{sp['id']}?$select=id,appId,displayName,tags",
        operation_name=f"read back service principal for {app_id}",
    )
    if not isinstance(result, dict):
        raise OperationFailed(f"Operation {operation_name} returned an invalid service principal.")
    return result


def ensure_user_admin_role_assignment(
    runner: CliRunner,
    user_object_id: str,
    api_service_principal_object_id: str,
    operation_name: str,
) -> None:
    assignments = graph_request(
        runner,
        "GET",
        f"/users/{user_object_id}/appRoleAssignments?$select=appRoleId,resourceId",
        operation_name=operation_name,
    ).get("value", [])
    if any(
        item.get("resourceId") == api_service_principal_object_id
        and item.get("appRoleId") == admin_role_id()
        for item in assignments
    ):
        return

    graph_request(
        runner,
        "POST",
        f"/users/{user_object_id}/appRoleAssignments",
        body={
            "appRoleId": admin_role_id(),
            "principalId": user_object_id,
            "resourceId": api_service_principal_object_id,
        },
        operation_name=operation_name,
    )


def ensure_api_managed_identity_graph_permissions(
    runner: CliRunner, *, managed_identity_principal_id: str
) -> None:
    operation_name = "grant Microsoft Graph application permissions to API managed identity"
    graph_sp = resolve_graph_service_principal(runner, operation_name)
    role_ids_by_value = {
        role["value"]: role["id"]
        for role in graph_sp.get("appRoles", [])
        if isinstance(role, dict)
        and role.get("value") in GRAPH_APPLICATION_PERMISSION_VALUES
        and "Application" in role.get("allowedMemberTypes", [])
        and role.get("isEnabled", True)
    }
    assignments = graph_items(
        graph_request(
            runner,
            "GET",
            f"/servicePrincipals/{managed_identity_principal_id}/appRoleAssignments"
            "?$select=appRoleId,resourceId",
            operation_name=operation_name,
        ),
        operation_name,
    )
    assigned_role_ids = {
        item.get("appRoleId")
        for item in assignments
        if item.get("resourceId") == graph_sp["id"] and isinstance(item.get("appRoleId"), str)
    }
    missing: list[tuple[str, str]] = []
    for role_value in GRAPH_APPLICATION_PERMISSION_VALUES:
        role_id = role_ids_by_value.get(role_value)
        if role_id is None:
            print(
                f"WARNING: Microsoft Graph in this tenant does not offer the {role_value} "
                "application permission. MOSAIC skipped that grant and deployment continues.",
                file=sys.stderr,
            )
            continue
        if role_id in assigned_role_ids:
            continue
        missing.append((role_value, role_id))
    for role_value, role_id in missing:
        try:
            graph_request(
                runner,
                "POST",
                f"/servicePrincipals/{managed_identity_principal_id}/appRoleAssignments",
                body={
                    "principalId": managed_identity_principal_id,
                    "resourceId": graph_sp["id"],
                    "appRoleId": role_id,
                },
                operation_name=operation_name,
            )
        except DirectoryPermissionDenied:
            print(
                graph_application_permission_warning(
                    managed_identity_principal_id,
                    graph_sp["id"],
                    [(value, app_role_id) for value, app_role_id in missing],
                ),
                file=sys.stderr,
            )
            return
        print(
            f"Granted Microsoft Graph application permission {role_value} to the MOSAIC API "
            "managed identity.",
            file=sys.stderr,
        )


def resolve_graph_service_principal(
    runner: CliRunner, operation_name: str
) -> dict[str, Any]:
    query = urllib.parse.urlencode(
        {
            "$filter": f"appId eq '{GRAPH_APP_ID}'",
            "$select": "id,appId,displayName,appRoles",
        }
    )
    items = graph_items(
        graph_request(
            runner,
            "GET",
            f"/servicePrincipals?{query}",
            operation_name=operation_name,
        ),
        operation_name,
    )
    if len(items) != 1:
        raise OperationFailed(
            "Operation grant Microsoft Graph application permissions failed: Microsoft Graph "
            f"service principal {GRAPH_APP_ID} was not found uniquely in this tenant."
        )
    return items[0]


def graph_application_permission_warning(
    managed_identity_principal_id: str,
    graph_service_principal_id: str,
    missing_permissions: Iterable[tuple[str, str]],
) -> str:
    permissions = list(missing_permissions)
    commands = "\n".join(
        "  az rest --method POST "
        f"--url {GRAPH_ROOT}/servicePrincipals/{managed_identity_principal_id}/appRoleAssignments "
        "--headers Content-Type=application/json "
        "--body "
        f"'{{\"principalId\":\"{managed_identity_principal_id}\","
        f"\"resourceId\":\"{graph_service_principal_id}\",\"appRoleId\":\"{role_id}\"}}'"
        for _, role_id in permissions
    )
    values = ", ".join(value for value, _ in permissions)
    return (
        "WARNING: Microsoft Graph denied application permission assignment for the MOSAIC API "
        f"managed identity. Missing permissions: {values}. Deployment continues, but directory "
        "lookup will fail until a Privileged Role Administrator or Global Administrator grants "
        "them. Run these commands:\n"
        f"{commands}"
    )


def ensure_model_client_consent(
    runner: CliRunner,
    *,
    client_display_name: str,
    client_app_id: str,
    client_service_principal_id: str,
    runtime_app_id: str,
    runtime_service_principal_id: str,
) -> bool:
    # A deploying user who can't grant consent gets the admin command instead of a failed hook.
    operation_name = (
        f"grant tenant-wide consent for {client_display_name} to request "
        f"{' and '.join(MODEL_RUNTIME_SCOPE_VALUES)}"
    )
    retry_delays = iter(CONSENT_RETRY_DELAYS_SECONDS)
    while True:
        try:
            change = ensure_all_principals_scope_grant(
                runner,
                client_service_principal_id=client_service_principal_id,
                resource_service_principal_id=runtime_service_principal_id,
                scopes=MODEL_RUNTIME_SCOPE_VALUES,
                operation_name=operation_name,
            )
        except DirectoryPermissionDenied:
            print(
                model_client_consent_warning(client_display_name, client_app_id, runtime_app_id),
                file=sys.stderr,
            )
            return False
        except OperationFailed as exc:
            delay = next(retry_delays, None)
            if delay is None or not any(marker in str(exc) for marker in REPLICATION_LAG_MARKERS):
                raise
            # New service principals can take a few seconds to replicate across the directory.
            print(
                "Microsoft Entra has not replicated the new service principals yet; retrying "
                f"consent for {client_display_name} in {delay} seconds.",
                file=sys.stderr,
            )
            time.sleep(delay)
            continue
        if change is not None:
            print(
                f"{change} tenant-wide admin consent for {client_display_name} to request "
                f"{' and '.join(MODEL_RUNTIME_SCOPE_VALUES)} from the model runtime.",
                file=sys.stderr,
            )
        return True


def ensure_all_principals_scope_grant(
    runner: CliRunner,
    *,
    client_service_principal_id: str,
    resource_service_principal_id: str,
    scopes: Iterable[str],
    operation_name: str,
) -> str | None:
    required_scopes = list(scopes)
    grants = graph_items(
        graph_request(
            runner,
            "GET",
            f"/servicePrincipals/{client_service_principal_id}/oauth2PermissionGrants",
            operation_name=operation_name,
        ),
        operation_name,
    )
    tenant_grant = next(
        (
            grant
            for grant in grants
            if grant.get("clientId") == client_service_principal_id
            and grant.get("resourceId") == resource_service_principal_id
            and grant.get("consentType") == "AllPrincipals"
        ),
        None,
    )
    if tenant_grant is None:
        graph_request(
            runner,
            "POST",
            "/oauth2PermissionGrants",
            body={
                "clientId": client_service_principal_id,
                "consentType": "AllPrincipals",
                "resourceId": resource_service_principal_id,
                "scope": " ".join(required_scopes),
            },
            operation_name=operation_name,
        )
        return "Granted"
    existing_scopes = str(tenant_grant.get("scope") or "").split()
    missing_scopes = [scope for scope in required_scopes if scope not in existing_scopes]
    if not missing_scopes:
        return None
    # PATCH replaces the scope list, so keep the scopes an administrator already granted.
    graph_request(
        runner,
        "PATCH",
        f"/oauth2PermissionGrants/{tenant_grant['id']}",
        body={"scope": " ".join([*existing_scopes, *missing_scopes])},
        operation_name=operation_name,
    )
    return "Extended"


def model_client_consent_warning(
    client_display_name: str, client_app_id: str, runtime_app_id: str
) -> str:
    return (
        f"WARNING: Microsoft Graph denied the tenant-wide consent for {client_display_name} to "
        f"request {' and '.join(MODEL_RUNTIME_SCOPE_VALUES)}. The registration exists, but "
        "people can't get "
        "model-runtime tokens with it until an administrator grants consent. Deployment "
        "continues.\n"
        "Remediation: a Privileged Role Administrator, Cloud Application Administrator or "
        "Application Administrator can run:\n"
        f"  az ad app permission grant --id {client_app_id} --api {runtime_app_id} "
        f"--scope \"{' '.join(MODEL_RUNTIME_SCOPE_VALUES)}\"\n"
        "or open Microsoft Entra admin center > App registrations > "
        f"{client_display_name} > API permissions > Grant admin consent. Rerunning "
        "`azd provision` as one of those roles also grants it."
    )


def find_application(
    runner: CliRunner,
    display_name: str,
    tags: list[str],
    operation_name: str,
) -> dict[str, Any] | None:
    query = urllib.parse.urlencode(
        {
            "$filter": f"displayName eq '{odata_quote(display_name)}'",
            "$select": APPLICATION_SELECT,
        }
    )
    response = graph_request(
        runner,
        "GET",
        f"/applications?{query}",
        operation_name=operation_name,
    )
    matches = graph_items(response, operation_name)
    tagged_matches = [item for item in matches if set(tags).issubset(set(item.get("tags", [])))]
    if len(tagged_matches) > 1:
        raise OperationFailed(
            f"Operation {operation_name} failed: multiple applications matched {display_name}. "
            f"Remediation: remove duplicate app registrations for {display_name} or narrow "
            "the tags before rerunning."
        )
    if tagged_matches:
        return tagged_matches[0]
    if matches:
        raise OperationFailed(
            f"Operation {operation_name} failed: an application named {display_name} exists "
            "but is not tagged as managed by this MOSAIC environment. Remediation: rename the "
            "unrelated application or add the expected ownership tags after verifying it is "
            "safe for MOSAIC to manage."
        )
    return None


def find_service_principal(
    runner: CliRunner, app_id: str, operation_name: str
) -> dict[str, Any] | None:
    query = urllib.parse.urlencode(
        {
            "$filter": f"appId eq '{odata_quote(app_id)}'",
            "$select": "id,appId,displayName,tags",
        }
    )
    response = graph_request(
        runner,
        "GET",
        f"/servicePrincipals?{query}",
        operation_name=operation_name,
    )
    items = graph_items(response, operation_name)
    if len(items) > 1:
        raise OperationFailed(
            f"Operation {operation_name} failed: multiple service principals matched appId "
            f"{app_id}. "
            "Remediation: remove duplicate service principals before rerunning."
        )
    return items[0] if items else None


def graph_request(
    runner: CliRunner,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    operation_name: str = "call Microsoft Graph",
) -> Any:
    url = f"{GRAPH_ROOT}{path}"
    args = ["rest", "--method", method, "--url", url]
    if body is not None:
        args.extend(
            [
                "--headers",
                "Content-Type=application/json",
                "--body",
                json.dumps(body, separators=(",", ":")),
            ]
        )
    try:
        return runner.az_json(args, operation_name=operation_name)
    except OperationFailed as exc:
        if any(marker.lower() in str(exc).lower() for marker in DIRECTORY_DENIAL_MARKERS):
            raise DirectoryPermissionDenied(
                f"Operation {operation_name} failed: Microsoft Graph denied directory access. "
                "Remediation: use a user account with Application Administrator or Cloud "
                "Application Administrator rights, "
                "ensure Azure CLI can call Microsoft Graph, then rerun `azd provision`."
            ) from exc
        raise


def graph_items(response: Any, operation_name: str) -> list[dict[str, Any]]:
    if not isinstance(response, dict):
        raise OperationFailed(
            f"Operation {operation_name} failed: Microsoft Graph returned an invalid collection."
        )
    items = response.get("value")
    if not isinstance(items, list):
        raise OperationFailed(
            f"Operation {operation_name} failed: Microsoft Graph returned an invalid collection."
        )
    if not all(isinstance(item, dict) for item in items):
        raise OperationFailed(
            f"Operation {operation_name} failed: Microsoft Graph returned an invalid item."
        )
    return items


def set_azd_env(runner: CliRunner, key: str, value: str) -> None:
    runner.azd(["env", "set", key, value], operation_name=f"set azd environment variable {key}")
    os.environ[key] = value


def odata_quote(value: str) -> str:
    return value.replace("'", "''")


def read_env_value(key: str, required: bool = False) -> str:
    value = os.environ.get(key, "")
    if required and not value:
        raise OperationFailed(
            f"Operation read environment variable {key} failed: no value was available. "
            "Remediation: rerun the preprovision hook so azd environment variables are populated."
        )
    return value


class CliRunner:
    def __init__(self, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._dry_run_objects: dict[str, dict[str, dict[str, Any]]] = {
            "applications": {},
            "servicePrincipals": {},
            "oauth2PermissionGrants": {},
        }
        graph_sp_id = deterministic_guid("dry-run/servicePrincipals/microsoft-graph")
        self._dry_run_objects["servicePrincipals"][graph_sp_id] = {
            "id": graph_sp_id,
            "appId": GRAPH_APP_ID,
            "displayName": "Microsoft Graph",
            "appRoles": [
                {
                    "id": deterministic_guid(f"dry-run/graph-app-role/{value}"),
                    "value": value,
                    "allowedMemberTypes": ["Application"],
                    "isEnabled": True,
                }
                for value in GRAPH_APPLICATION_PERMISSION_VALUES
            ],
        }
        self._dry_run_role_assignments: dict[str, list[dict[str, Any]]] = {}

    def az_json(self, args: list[str], operation_name: str) -> Any:
        if self.dry_run:
            command_text = " ".join(args)
            print(f"[dry-run] az {command_text}", file=sys.stderr)
            if args[:2] == ["account", "show"]:
                return {
                    "tenantId": os.environ.get(
                        "MOSAIC_TENANT_ID", "00000000-0000-0000-0000-000000000000"
                    ),
                    "user": {"type": "user"},
                }
            if args and args[0] == "rest":
                return self._dry_run_graph_request(args)
            return {}
        return run_json_command(["az", *args], operation_name)

    def _dry_run_create_object(self, collection: str, body: dict[str, Any]) -> dict[str, Any]:
        if collection == "applications":
            key = body["displayName"]
        elif collection == "oauth2PermissionGrants":
            key = f"{body['clientId']}/{body['consentType']}/{body['resourceId']}"
        else:
            key = body["appId"]
        object_id = deterministic_guid(f"dry-run/{collection}/{key}")
        item = {"id": object_id, **body}
        if collection == "applications":
            item["appId"] = deterministic_guid(f"dry-run/client/{key}")
        return self._dry_run_objects[collection].setdefault(object_id, item)

    def seed_dry_run_applications(self, context: EntraContext) -> None:
        # Postprovision can be previewed independently of preprovision's in-memory state.
        for display_name in (
            context.api_display_name,
            context.spa_display_name,
            context.portal_display_name,
            context.model_runtime_display_name,
        ):
            self._dry_run_create_object(
                "applications",
                {"displayName": display_name, "tags": context.app_tags},
            )

    def _dry_run_graph_request(self, args: list[str]) -> Any:
        method = args[args.index("--method") + 1]
        url = urllib.parse.urlparse(args[args.index("--url") + 1])
        path = url.path.removeprefix("/v1.0/")
        body = json.loads(args[args.index("--body") + 1]) if "--body" in args else {}
        if path == "me":
            return {
                "id": "00000000-0000-0000-0000-000000000001",
                "displayName": "MOSAIC Dry Run",
                "userPrincipalName": "mosaic@example.com",
            }
        if path.endswith("/appRoleAssignments"):
            assignments = self._dry_run_role_assignments.setdefault(path, [])
            if method == "POST":
                assignments.append(body)
                return body
            return {"value": assignments}
        if path.startswith("servicePrincipals/") and path.endswith("/oauth2PermissionGrants"):
            client_id = path.split("/")[1]
            grants = self._dry_run_objects["oauth2PermissionGrants"].values()
            return {"value": [grant for grant in grants if grant.get("clientId") == client_id]}
        collection, _, object_id = path.partition("/")
        if collection not in self._dry_run_objects:
            return {}
        objects = self._dry_run_objects[collection]
        if object_id:
            item = objects[object_id]
            if method == "PATCH":
                item.update(body)
            return item
        if method == "POST":
            return self._dry_run_create_object(collection, body)
        query = urllib.parse.parse_qs(url.query)
        field, _, value = query["$filter"][0].partition(" eq ")
        expected = value[1:-1].replace("''", "'")
        return {"value": [item for item in objects.values() if item.get(field) == expected]}

    def azd(self, args: list[str], operation_name: str) -> Any:
        if self.dry_run:
            print(f"[dry-run] azd {' '.join(args)}", file=sys.stderr)
            return None
        return run_json_command(["azd", *args], operation_name, allow_non_json=True)


def run_json_command(command: list[str], operation_name: str, allow_non_json: bool = False) -> Any:
    executable = shutil.which(command[0])
    if executable is None:
        raise OperationFailed(
            f"Operation {operation_name} failed: required executable '{command[0]}' "
            "was not found on PATH"
        )
    resolved_command: list[str] | str = [executable, *command[1:]]
    if os.name == "nt" and os.path.basename(executable).lower() == "az.cmd":
        azure_cli_python = os.path.abspath(
            os.path.join(os.path.dirname(executable), os.pardir, "python.exe")
        )
        resolved_command = [azure_cli_python, "-IBm", "azure.cli", *command[1:]]
    elif os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        command_line = subprocess.list2cmdline([executable, *command[1:]])
        command_processor = os.environ.get("COMSPEC", "cmd.exe")
        resolved_command = f'"{command_processor}" /d /s /c "{command_line}"'
    result = subprocess.run(resolved_command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise OperationFailed(f"Operation {operation_name} failed: {detail}")

    text = (result.stdout or "").strip()
    if not text:
        return {} if not allow_non_json else None
    if allow_non_json:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return json.loads(text)


if __name__ == "__main__":
    raise SystemExit(main())
