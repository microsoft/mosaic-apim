from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
DEFAULT_LOCATION = "eastus2"
DEFAULT_LOCALHOST_REDIRECTS = (
    "http://localhost:3000",
    "http://localhost:5173",
)
API_SCOPE_VALUE = "access_as_user"
APP_ROLE_VALUE = "Admin"
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


def application_redirect_uris(application: dict[str, Any]) -> list[str]:
    spa = application.get("spa")
    if not isinstance(spa, dict):
        return []
    redirect_uris = spa.get("redirectUris")
    if not isinstance(redirect_uris, list):
        return []
    return [uri for uri in redirect_uris if isinstance(uri, str)]


def api_scope_id() -> str:
    return deterministic_guid("entra/api/scope/access-as-user")


def admin_role_id() -> str:
    return deterministic_guid("entra/api/role/admin")


def build_api_app_payload(
    display_name: str,
    identifier_uri: str,
    tags: list[str],
    preauthorized_spa_client_id: str | None = None,
) -> dict[str, Any]:
    preauthorized_applications = (
        [
            {
                "appId": preauthorized_spa_client_id,
                "delegatedPermissionIds": [api_scope_id()],
            }
        ]
        if preauthorized_spa_client_id
        else []
    )
    return {
        "displayName": display_name,
        "identifierUris": [identifier_uri],
        "notes": "MOSAIC API application registration managed by azd hooks.",
        "signInAudience": "AzureADMyOrg",
        "tags": tags,
        "api": {
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
                "origin": "Application",
                "value": APP_ROLE_VALUE,
            }
        ],
    }


def build_spa_app_payload(
    display_name: str,
    api_application_client_id: str,
    redirect_uris: list[str],
    tags: list[str],
) -> dict[str, Any]:
    return {
        "displayName": display_name,
        "isFallbackPublicClient": False,
        "notes": "MOSAIC SPA application registration managed by azd hooks.",
        "requiredResourceAccess": [
            {
                "resourceAppId": api_application_client_id,
                "resourceAccess": [
                    {
                        "id": api_scope_id(),
                        "type": "Scope",
                    }
                ],
            }
        ],
        "signInAudience": "AzureADMyOrg",
        "spa": {
            "redirectUris": redirect_uris,
        },
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
        patch_builder=lambda _: build_spa_app_payload(
            display_name=context.spa_display_name,
            api_application_client_id=api_app["appId"],
            redirect_uris=normalize_redirect_uris(
                [
                    *context.localhost_redirects,
                    *application_redirect_uris(_),
                ]
            ),
            tags=context.app_tags,
        ),
        operation_name="create-or-update SPA application registration",
    )
    spa_sp = ensure_service_principal(
        runner,
        app_id=spa_app["appId"],
        tags=context.app_tags,
        operation_name="create-or-update SPA service principal",
    )
    graph_request(
        runner,
        "PATCH",
        f"/applications/{api_app['id']}",
        body=build_api_app_payload(
            display_name=context.api_display_name,
            identifier_uri=f"api://{api_app['appId']}",
            tags=context.app_tags,
            preauthorized_spa_client_id=spa_app["appId"],
        ),
        operation_name="pre-authorize SPA delegated access to API",
    )

    ensure_user_admin_role_assignment(
        runner=runner,
        user_object_id=context.deployer_object_id,
        api_service_principal_object_id=api_sp["id"],
        operation_name="assign deploying user Admin app role",
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
    set_azd_env(runner, "MOSAIC_SPA_APP_OBJECT_ID", spa_app["id"])
    set_azd_env(runner, "MOSAIC_SPA_CLIENT_ID", spa_app["appId"])
    set_azd_env(runner, "MOSAIC_SPA_SERVICE_PRINCIPAL_OBJECT_ID", spa_sp["id"])
    set_azd_env(runner, "MOSAIC_SPA_LOCALHOST_REDIRECT_URIS", ",".join(context.localhost_redirects))
    set_azd_env(runner, "MOSAIC_DEPLOYER_OBJECT_ID", context.deployer_object_id)
    set_azd_env(runner, "MOSAIC_APIM_PUBLISHER_NAME", context.deployer_display_name)
    set_azd_env(runner, "MOSAIC_APIM_PUBLISHER_EMAIL", context.deployer_email)


def postprovision(
    runner: CliRunner,
    environment_name_override: str | None,
    deployed_web_url_override: str | None,
) -> None:
    context = build_context(runner, environment_name_override)
    deployed_web_url = deployed_web_url_override or os.environ.get("WEB_APP_URL")
    if not deployed_web_url:
        raise OperationFailed(
            "Operation patch deployed SPA redirect URI failed: WEB_APP_URL was not "
            "available from the azd environment. "
            "Remediation: ensure infra/main.bicep outputs WEB_APP_URL and rerun `azd provision`."
        )

    spa_app = find_application(
        runner, context.spa_display_name, context.app_tags, "read SPA application registration"
    )
    if spa_app is None:
        raise OperationFailed(
            "Operation patch deployed SPA redirect URI failed: the MOSAIC SPA application "
            "registration does not exist. Remediation: run the preprovision hook or rerun "
            "`azd provision` so the app registrations are created first."
        )

    redirect_uris = normalize_redirect_uris(
        [
            *context.localhost_redirects,
            *application_redirect_uris(spa_app),
            deployed_web_url,
        ]
    )
    payload = build_spa_app_payload(
        display_name=context.spa_display_name,
        api_application_client_id=read_env_value("MOSAIC_API_CLIENT_ID", required=True),
        redirect_uris=redirect_uris,
        tags=context.app_tags,
    )
    graph_request(
        runner,
        "PATCH",
        f"/applications/{spa_app['id']}",
        body=payload,
        operation_name="patch deployed SPA redirect URI",
    )
    set_azd_env(runner, "MOSAIC_SPA_DEPLOYED_REDIRECT_URI", deployed_web_url)


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
    return EntraContext(
        environment_name=environment_name,
        tenant_id=account["tenantId"],
        location=os.environ.get("AZURE_LOCATION", DEFAULT_LOCATION),
        deployer_object_id=me["id"],
        deployer_display_name=me.get("displayName") or deployer_email,
        deployer_email=deployer_email,
        localhost_redirects=tuple(normalize_redirect_uris(redirect_values)),
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
        f"/applications/{app['id']}?$select=id,appId,displayName,tags,spa",
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


def find_application(
    runner: CliRunner,
    display_name: str,
    tags: list[str],
    operation_name: str,
) -> dict[str, Any] | None:
    query = urllib.parse.urlencode(
        {
            "$filter": f"displayName eq '{odata_quote(display_name)}'",
            "$select": "id,appId,displayName,tags,spa",
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
            if "graph.microsoft.com" in command_text:
                if "/me" in command_text:
                    return {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "displayName": "MOSAIC Dry Run",
                        "userPrincipalName": "mosaic@example.com",
                    }
                if "/applications" in command_text:
                    if "--method POST" in command_text or "--method PATCH" in command_text:
                        return {
                            "id": "11111111-1111-1111-1111-111111111111",
                            "appId": "22222222-2222-2222-2222-222222222222",
                            "displayName": "mosaic-dryrun-api",
                            "tags": ["product:MOSAIC"],
                        }
                    if "/applications/" in command_text:
                        return {
                            "id": "11111111-1111-1111-1111-111111111111",
                            "appId": "22222222-2222-2222-2222-222222222222",
                            "displayName": "mosaic-dryrun-api",
                            "tags": ["product:MOSAIC"],
                        }
                    return {
                        "value": [
                            {
                                "id": "11111111-1111-1111-1111-111111111111",
                                "appId": "22222222-2222-2222-2222-222222222222",
                                "displayName": "mosaic-dryrun-api",
                                "tags": ["product:MOSAIC", "managed-by:azd", "azd-env:mosaic-dev"],
                            }
                        ]
                    }
                if "/servicePrincipals" in command_text:
                    if "--method POST" in command_text or "--method PATCH" in command_text:
                        return {
                            "id": "33333333-3333-3333-3333-333333333333",
                            "appId": "22222222-2222-2222-2222-222222222222",
                            "displayName": "mosaic-dryrun-api",
                            "tags": ["product:MOSAIC"],
                        }
                    if "/servicePrincipals/" in command_text:
                        return {
                            "id": "33333333-3333-3333-3333-333333333333",
                            "appId": "22222222-2222-2222-2222-222222222222",
                            "displayName": "mosaic-dryrun-api",
                            "tags": ["product:MOSAIC"],
                        }
                    return {
                        "value": [
                            {
                                "id": "33333333-3333-3333-3333-333333333333",
                                "appId": "22222222-2222-2222-2222-222222222222",
                                "displayName": "mosaic-dryrun-api",
                                "tags": ["product:MOSAIC", "managed-by:azd", "azd-env:mosaic-dev"],
                            }
                        ]
                    }
                if "/appRoleAssignments" in command_text:
                    return {"value": []}
                return {}
            return {}
        return run_json_command(["az", *args], operation_name)

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
