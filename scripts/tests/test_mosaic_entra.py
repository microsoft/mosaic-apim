import json
import os
import unittest
import urllib.parse
from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from scripts import mosaic_entra

GRANTS_URL = "https://graph.microsoft.com/v1.0/oauth2PermissionGrants"
MODEL_CLIENT_CONSENT = {
    "client_display_name": "mosaic-test-model-client",
    "client_app_id": "77777777-7777-7777-7777-777777777777",
    "client_service_principal_id": "88888888-8888-8888-8888-888888888888",
    "runtime_app_id": "11111111-1111-1111-1111-111111111111",
    "runtime_service_principal_id": "99999999-9999-9999-9999-999999999999",
}


class MosaicEntraTests(unittest.TestCase):
    def test_deterministic_ids_are_stable(self) -> None:
        self.assertEqual(
            mosaic_entra.api_scope_id(),
            "8c8bc19d-0257-5d31-9b59-6baecfb2bc0a",
        )
        self.assertEqual(
            mosaic_entra.admin_role_id(),
            "c7b19afd-b776-5110-a40b-d04cbb4231b1",
        )
        self.assertEqual(
            mosaic_entra.portal_role_id(),
            "627894b9-f336-5445-9e90-1f545bb82c80",
        )
        self.assertEqual(
            mosaic_entra.model_runtime_scope_id(),
            "756c179d-8fe1-5bd1-bb75-db23c49e1bae",
        )
        self.assertEqual(
            mosaic_entra.model_runtime_role_id(),
            "eff1a5fa-fabb-5594-8879-78b951ecb47d",
        )
        self.assertEqual(
            len(
                {
                    mosaic_entra.api_scope_id(),
                    mosaic_entra.admin_role_id(),
                    mosaic_entra.portal_role_id(),
                    mosaic_entra.model_runtime_scope_id(),
                    mosaic_entra.model_runtime_role_id(),
                }
            ),
            5,
        )

    def test_redirect_normalization_is_unique(self) -> None:
        self.assertEqual(
            mosaic_entra.normalize_redirect_uris(
                [
                    "http://localhost:3000/",
                    "http://localhost:3000",
                    "https://example.com/",
                    "https://example.com",
                ]
            ),
            ["http://localhost:3000", "https://example.com"],
        )

    def test_existing_spa_redirects_are_preserved(self) -> None:
        self.assertEqual(
            mosaic_entra.application_redirect_uris(
                {
                    "spa": {
                        "redirectUris": [
                            "https://mosaic.example",
                            "http://localhost:5173",
                        ]
                    }
                }
            ),
            ["https://mosaic.example", "http://localhost:5173"],
        )

    def test_api_payload_contains_expected_scope_and_roles(self) -> None:
        payload = mosaic_entra.build_api_app_payload(
            display_name="mosaic-test-api",
            identifier_uri="api://example",
            tags=["product:MOSAIC"],
        )
        scope = payload["api"]["oauth2PermissionScopes"][0]
        roles = {role["value"]: role for role in payload["appRoles"]}
        self.assertEqual(scope["value"], "access_as_user")
        self.assertEqual(set(roles), {"Admin", "User"})
        self.assertEqual(roles["Admin"]["id"], mosaic_entra.admin_role_id())
        self.assertEqual(roles["User"]["id"], mosaic_entra.portal_role_id())
        self.assertIn("User", roles["User"]["allowedMemberTypes"])
        self.assertEqual(payload["identifierUris"], ["api://example"])
        self.assertTrue(all("origin" not in role for role in payload["appRoles"]))

    def test_api_payload_pre_authorizes_spa_and_portal(self) -> None:
        payload = mosaic_entra.build_api_app_payload(
            display_name="mosaic-test-api",
            identifier_uri="api://example",
            tags=["product:MOSAIC"],
            preauthorized_client_ids=[
                "22222222-2222-2222-2222-222222222222",
                "33333333-3333-3333-3333-333333333333",
                "22222222-2222-2222-2222-222222222222",
            ],
        )

        self.assertEqual(
            payload["api"]["preAuthorizedApplications"],
            [
                {
                    "appId": "22222222-2222-2222-2222-222222222222",
                    "delegatedPermissionIds": [mosaic_entra.api_scope_id()],
                },
                {
                    "appId": "33333333-3333-3333-3333-333333333333",
                    "delegatedPermissionIds": [mosaic_entra.api_scope_id()],
                },
            ],
        )

    def test_api_payload_without_preauthorization_is_empty(self) -> None:
        payload = mosaic_entra.build_api_app_payload(
            display_name="mosaic-test-api",
            identifier_uri="api://example",
            tags=["product:MOSAIC"],
        )
        self.assertEqual(payload["api"]["preAuthorizedApplications"], [])

    def test_portal_display_name_is_distinct(self) -> None:
        context = mosaic_entra.EntraContext(
            environment_name="mosaic-dev",
            tenant_id="tenant",
            location="eastus2",
            deployer_object_id="deployer",
            deployer_display_name="Deployer",
            deployer_email="deployer@example.com",
            localhost_redirects=mosaic_entra.DEFAULT_LOCALHOST_REDIRECTS,
        )
        self.assertEqual(context.api_display_name, "mosaic-dev-api")
        self.assertEqual(context.spa_display_name, "mosaic-dev-spa")
        self.assertEqual(context.portal_display_name, "mosaic-dev-portal")
        self.assertEqual(context.model_runtime_display_name, "mosaic-dev-model-runtime")
        self.assertEqual(context.model_client_display_name, "mosaic-dev-model-client")
        self.assertEqual(
            len(
                {
                    context.api_display_name,
                    context.spa_display_name,
                    context.portal_display_name,
                    context.model_runtime_display_name,
                    context.model_client_display_name,
                }
            ),
            5,
        )
        self.assertNotEqual(
            set(context.localhost_redirects),
            set(context.portal_localhost_redirects),
        )

    def test_spa_payload_uses_api_scope(self) -> None:
        payload = mosaic_entra.build_spa_app_payload(
            display_name="mosaic-test-spa",
            api_application_client_id="11111111-1111-1111-1111-111111111111",
            redirect_uris=["http://localhost:3000"],
            tags=["product:MOSAIC"],
        )
        self.assertEqual(
            payload["requiredResourceAccess"][0]["resourceAppId"],
            "11111111-1111-1111-1111-111111111111",
        )
        self.assertEqual(payload["requiredResourceAccess"][0]["resourceAccess"][0]["type"], "Scope")
        self.assertEqual(payload["spa"]["redirectUris"], ["http://localhost:3000"])

    def test_spa_payload_preserves_permissions_explicitly_configured_by_operator(self) -> None:
        api_client_id = "11111111-1111-1111-1111-111111111111"
        existing_access = [
            {"resourceAppId": api_client_id, "resourceAccess": []},
            {
                "resourceAppId": "22222222-2222-2222-2222-222222222222",
                "resourceAccess": [{"id": mosaic_entra.model_runtime_scope_id(), "type": "Scope"}],
            },
        ]
        original = deepcopy(existing_access)
        payload = mosaic_entra.build_spa_app_payload(
            display_name="mosaic-test-spa",
            api_application_client_id=api_client_id,
            redirect_uris=["http://localhost:3000"],
            tags=["product:MOSAIC"],
            existing_required_resource_access=existing_access,
        )
        self.assertEqual(
            payload["requiredResourceAccess"],
            [
                {
                    "resourceAppId": api_client_id,
                    "resourceAccess": [{"id": mosaic_entra.api_scope_id(), "type": "Scope"}],
                },
                original[1],
            ],
        )
        self.assertEqual(existing_access, original)

    def test_model_runtime_payload_is_a_separate_single_tenant_resource(self) -> None:
        payload = mosaic_entra.build_model_runtime_app_payload(
            display_name="mosaic-test-model-runtime",
            identifier_uri="api://11111111-1111-1111-1111-111111111111",
            tags=["product:MOSAIC"],
        )
        self.assertEqual(payload["signInAudience"], "AzureADMyOrg")
        self.assertEqual(payload["api"]["requestedAccessTokenVersion"], 2)
        self.assertEqual(payload["identifierUris"], ["api://11111111-1111-1111-1111-111111111111"])
        self.assertEqual(len(payload["api"]["oauth2PermissionScopes"]), 1)
        scope = payload["api"]["oauth2PermissionScopes"][0]
        self.assertEqual(scope["id"], mosaic_entra.model_runtime_scope_id())
        self.assertEqual(scope["value"], "Models.Invoke")
        self.assertEqual(scope["type"], "Admin")
        self.assertTrue(scope["isEnabled"])
        self.assertEqual(len(payload["appRoles"]), 1)
        role = payload["appRoles"][0]
        self.assertEqual(role["id"], mosaic_entra.model_runtime_role_id())
        self.assertEqual(role["value"], "Models.Invoke.Application")
        self.assertNotEqual(role["value"].casefold(), scope["value"].casefold())
        self.assertNotIn("origin", role)
        self.assertEqual(role["allowedMemberTypes"], ["Application"])
        self.assertTrue(role["isEnabled"])
        self.assertNotIn("preAuthorizedApplications", payload["api"])
        self.assertNotIn("requiredResourceAccess", payload)
        self.assertNotIn("spa", payload)

    def test_model_runtime_preserves_operator_client_preauthorization(self) -> None:
        existing_api = {
            "preAuthorizedApplications": [
                {
                    "appId": "22222222-2222-2222-2222-222222222222",
                    "delegatedPermissionIds": [mosaic_entra.model_runtime_scope_id()],
                }
            ],
            "knownClientApplications": ["22222222-2222-2222-2222-222222222222"],
        }
        original = deepcopy(existing_api)
        payload = mosaic_entra.build_model_runtime_app_payload(
            display_name="mosaic-test-model-runtime",
            identifier_uri="api://11111111-1111-1111-1111-111111111111",
            tags=["product:MOSAIC"],
            existing_api=existing_api,
        )
        for key, value in original.items():
            self.assertEqual(payload["api"][key], value)
        self.assertEqual(existing_api, original)

    def test_model_client_payload_is_a_public_client_for_the_runtime_scope_only(self) -> None:
        runtime_client_id = "11111111-1111-1111-1111-111111111111"
        payload = mosaic_entra.build_model_client_app_payload(
            display_name="mosaic-test-model-client",
            model_runtime_client_id=runtime_client_id,
            tags=["product:MOSAIC"],
        )
        self.assertEqual(payload["displayName"], "mosaic-test-model-client")
        self.assertEqual(payload["signInAudience"], "AzureADMyOrg")
        self.assertIs(payload["isFallbackPublicClient"], True)
        self.assertEqual(payload["publicClient"], {"redirectUris": ["http://localhost"]})
        self.assertEqual(
            payload["requiredResourceAccess"],
            [
                {
                    "resourceAppId": runtime_client_id,
                    "resourceAccess": [
                        {"id": mosaic_entra.model_runtime_scope_id(), "type": "Scope"}
                    ],
                }
            ],
        )
        for absent in (
            "api",
            "appRoles",
            "identifierUris",
            "keyCredentials",
            "passwordCredentials",
            "spa",
            "web",
        ):
            self.assertNotIn(absent, payload)

    def test_model_client_payload_preserves_operator_redirects_and_permissions(self) -> None:
        runtime_client_id = "11111111-1111-1111-1111-111111111111"
        graph_access = {
            "resourceAppId": "00000003-0000-0000-c000-000000000000",
            "resourceAccess": [{"id": "e1fe6dd8-ba31-4d61-89e7-88639da4683d", "type": "Scope"}],
        }
        existing = {
            "publicClient": {
                "redirectUris": [
                    "http://localhost",
                    "https://login.microsoftonline.com/common/oauth2/nativeclient",
                ]
            },
            "requiredResourceAccess": [
                graph_access,
                {
                    "resourceAppId": runtime_client_id,
                    "resourceAccess": [
                        {"id": mosaic_entra.model_runtime_scope_id(), "type": "Scope"}
                    ],
                },
            ],
        }
        original = deepcopy(existing)
        payload = mosaic_entra.build_model_client_app_payload(
            display_name="mosaic-test-model-client",
            model_runtime_client_id=runtime_client_id,
            tags=["product:MOSAIC"],
            existing_redirect_uris=mosaic_entra.application_redirect_uris(
                existing, "publicClient"
            ),
            existing_required_resource_access=existing["requiredResourceAccess"],
        )
        self.assertEqual(
            payload["publicClient"]["redirectUris"],
            [
                "http://localhost",
                "https://login.microsoftonline.com/common/oauth2/nativeclient",
            ],
        )
        self.assertEqual(payload["requiredResourceAccess"], original["requiredResourceAccess"])
        self.assertEqual(existing, original)

    def test_model_client_toggle_defaults_on_and_rejects_unknown_values(self) -> None:
        for value, expected in (("", True), (" TRUE ", True), ("true", True), ("False", False)):
            with mock.patch.dict(os.environ, {"MOSAIC_ENTRA_MODEL_CLIENT": value}):
                self.assertIs(mosaic_entra.model_client_enabled(), expected)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIs(mosaic_entra.model_client_enabled(), True)
        runner = mock.Mock()
        with (
            mock.patch.dict(os.environ, {"MOSAIC_ENTRA_MODEL_CLIENT": "yes"}, clear=True),
            self.assertRaisesRegex(mosaic_entra.OperationFailed, "MOSAIC_ENTRA_MODEL_CLIENT"),
        ):
            mosaic_entra.preprovision(runner, "mosaic-test")
        runner.az_json.assert_not_called()
        runner.azd.assert_not_called()

    def test_control_plane_preserves_operator_preauthorization_without_duplicates(self) -> None:
        client_id = "22222222-2222-2222-2222-222222222222"
        existing_api = {
            "preAuthorizedApplications": [
                {"appId": client_id, "delegatedPermissionIds": [mosaic_entra.api_scope_id()]}
            ],
            "knownClientApplications": [client_id],
        }
        original = deepcopy(existing_api)
        payload = mosaic_entra.build_api_app_payload(
            display_name="mosaic-test-api",
            identifier_uri="api://11111111-1111-1111-1111-111111111111",
            tags=["product:MOSAIC"],
            preauthorized_client_ids=[client_id, "33333333-3333-3333-3333-333333333333"],
            existing_api=existing_api,
        )
        self.assertEqual(
            payload["api"]["preAuthorizedApplications"],
            [
                *existing_api["preAuthorizedApplications"],
                {
                    "appId": "33333333-3333-3333-3333-333333333333",
                    "delegatedPermissionIds": [mosaic_entra.api_scope_id()],
                },
            ],
        )
        self.assertEqual(payload["api"]["knownClientApplications"], [client_id])
        self.assertEqual(existing_api, original)

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_preprovision_creates_distinct_registrations_and_consents_only_the_model_client(
        self,
    ) -> None:
        runner = mosaic_entra.CliRunner(dry_run=True)
        with (
            mock.patch.object(runner, "az_json", wraps=runner.az_json) as az_json,
            mock.patch("scripts.mosaic_entra.run_json_command") as real_cli,
            redirect_stderr(StringIO()),
        ):
            mosaic_entra.preprovision(runner, "mosaic-test")
        real_cli.assert_not_called()
        apps = {app["displayName"]: app for app in runner._dry_run_objects["applications"].values()}
        self.assertEqual(
            set(apps),
            {
                "mosaic-test-api",
                "mosaic-test-spa",
                "mosaic-test-portal",
                "mosaic-test-model-runtime",
                "mosaic-test-model-client",
            },
        )
        self.assertEqual(len({app["appId"] for app in apps.values()}), 5)
        self.assertEqual(len({app["id"] for app in apps.values()}), 5)
        service_principals = runner._dry_run_objects["servicePrincipals"]
        self.assertEqual(len(service_principals), 5)
        api = apps["mosaic-test-api"]
        runtime = apps["mosaic-test-model-runtime"]
        self.assertEqual(os.environ["MOSAIC_API_CLIENT_ID"], api["appId"])
        self.assertEqual(os.environ["MOSAIC_MODEL_RUNTIME_CLIENT_ID"], runtime["appId"])
        self.assertEqual(os.environ["MOSAIC_MODEL_RUNTIME_APP_OBJECT_ID"], runtime["id"])
        runtime_sp = service_principals[
            os.environ["MOSAIC_MODEL_RUNTIME_SERVICE_PRINCIPAL_OBJECT_ID"]
        ]
        self.assertEqual(runtime_sp["appId"], runtime["appId"])
        self.assertEqual(
            os.environ["MOSAIC_MODEL_RUNTIME_APPLICATION_ID_URI"], f"api://{runtime['appId']}"
        )
        self.assertEqual(
            os.environ["MOSAIC_MODEL_RUNTIME_SCOPE"], f"api://{runtime['appId']}/Models.Invoke"
        )
        self.assertEqual(
            os.environ["MOSAIC_MODEL_RUNTIME_SCOPE_ID"], mosaic_entra.model_runtime_scope_id()
        )
        self.assertEqual(
            os.environ["MOSAIC_MODEL_RUNTIME_ROLE_ID"], mosaic_entra.model_runtime_role_id()
        )
        self.assertEqual({role["value"] for role in api["appRoles"]}, {"Admin", "User"})
        self.assertEqual(api["api"]["oauth2PermissionScopes"][0]["id"], mosaic_entra.api_scope_id())
        for name in ("spa", "portal"):
            app = apps[f"mosaic-test-{name}"]
            self.assertEqual(
                app["requiredResourceAccess"],
                [
                    {
                        "resourceAppId": api["appId"],
                        "resourceAccess": [{"id": mosaic_entra.api_scope_id(), "type": "Scope"}],
                    }
                ],
            )
            self.assertEqual(os.environ[f"MOSAIC_{name.upper()}_CLIENT_ID"], app["appId"])
        self.assertNotIn("preAuthorizedApplications", runtime["api"])
        self.assertTrue(all("requiredResourceAccess" not in app for app in (api, runtime)))
        model_client = apps["mosaic-test-model-client"]
        self.assertIs(model_client["isFallbackPublicClient"], True)
        self.assertEqual(model_client["publicClient"], {"redirectUris": ["http://localhost"]})
        self.assertEqual(
            model_client["requiredResourceAccess"],
            [
                {
                    "resourceAppId": runtime["appId"],
                    "resourceAccess": [
                        {"id": mosaic_entra.model_runtime_scope_id(), "type": "Scope"}
                    ],
                }
            ],
        )
        for absent in ("api", "appRoles", "keyCredentials", "passwordCredentials", "spa", "web"):
            self.assertNotIn(absent, model_client)
        self.assertEqual(os.environ["MOSAIC_MODEL_CLIENT_ID"], model_client["appId"])
        self.assertEqual(os.environ["MOSAIC_MODEL_CLIENT_APP_OBJECT_ID"], model_client["id"])
        model_client_sp = service_principals[
            os.environ["MOSAIC_MODEL_CLIENT_SERVICE_PRINCIPAL_OBJECT_ID"]
        ]
        self.assertEqual(model_client_sp["appId"], model_client["appId"])
        grants = list(runner._dry_run_objects["oauth2PermissionGrants"].values())
        self.assertEqual(
            [{key: grant[key] for key in grant if key != "id"} for grant in grants],
            [
                {
                    "clientId": model_client_sp["id"],
                    "consentType": "AllPrincipals",
                    "resourceId": runtime_sp["id"],
                    "scope": "Models.Invoke",
                }
            ],
        )
        posts = [
            call.args[0]
            for call in az_json.call_args_list
            if call.args[0][:3] == ["rest", "--method", "POST"]
        ]
        paths = [urllib.parse.urlparse(args[4]).path for args in posts]
        self.assertEqual(paths.count("/v1.0/applications"), 5)
        self.assertEqual(paths.count("/v1.0/servicePrincipals"), 5)
        self.assertEqual(paths.count("/v1.0/oauth2PermissionGrants"), 1)
        assignments = [
            json.loads(args[args.index("--body") + 1])
            for args in posts
            if args[4].endswith("/appRoleAssignments")
        ]
        self.assertEqual(
            assignments,
            [
                {
                    "appRoleId": mosaic_entra.admin_role_id(),
                    "principalId": os.environ["MOSAIC_DEPLOYER_OBJECT_ID"],
                    "resourceId": os.environ["MOSAIC_API_SERVICE_PRINCIPAL_OBJECT_ID"],
                }
            ],
        )
        self.assertEqual(len(posts), 12)

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_preprovision_rerun_preserves_ids_redirects_and_operator_consent(self) -> None:
        runner = mosaic_entra.CliRunner(dry_run=True)
        with redirect_stderr(StringIO()):
            mosaic_entra.preprovision(runner, "mosaic-test")
        apps = {app["displayName"]: app for app in runner._dry_run_objects["applications"].values()}
        client_id = "44444444-4444-4444-4444-444444444444"
        for name, scope_id in (
            ("api", mosaic_entra.api_scope_id()),
            ("model-runtime", mosaic_entra.model_runtime_scope_id()),
        ):
            api = apps[f"mosaic-test-{name}"]["api"]
            api.setdefault("preAuthorizedApplications", []).append(
                {"appId": client_id, "delegatedPermissionIds": [scope_id]}
            )
            api["knownClientApplications"] = [client_id]
        for name in ("spa", "portal"):
            apps[f"mosaic-test-{name}"]["spa"]["redirectUris"].append(
                f"https://mosaic-{name}.example.com"
            )
            apps[f"mosaic-test-{name}"]["requiredResourceAccess"].append(
                {
                    "resourceAppId": apps["mosaic-test-model-runtime"]["appId"],
                    "resourceAccess": [
                        {"id": mosaic_entra.model_runtime_scope_id(), "type": "Scope"}
                    ],
                }
            )
        model_client = apps["mosaic-test-model-client"]
        model_client["publicClient"]["redirectUris"].append(
            "https://login.microsoftonline.com/common/oauth2/nativeclient"
        )
        model_client["requiredResourceAccess"].append(
            {
                "resourceAppId": "00000003-0000-0000-c000-000000000000",
                "resourceAccess": [{"id": "e1fe6dd8-ba31-4d61-89e7-88639da4683d", "type": "Scope"}],
            }
        )
        original_objects = deepcopy(runner._dry_run_objects)
        original_assignments = deepcopy(runner._dry_run_role_assignments)
        original_environment = dict(os.environ)
        with (
            mock.patch.object(runner, "az_json", wraps=runner.az_json) as az_json,
            redirect_stderr(StringIO()),
        ):
            mosaic_entra.preprovision(runner, "mosaic-test")
        self.assertEqual(runner._dry_run_objects, original_objects)
        self.assertEqual(runner._dry_run_role_assignments, original_assignments)
        self.assertEqual(dict(os.environ), original_environment)
        self.assertFalse(
            any(call.args[0][:3] == ["rest", "--method", "POST"] for call in az_json.call_args_list)
        )
        grant_writes = [
            args
            for args in (call.args[0] for call in az_json.call_args_list)
            if args[0] == "rest" and args[2] != "GET" and "oauth2PermissionGrants" in args[4]
        ]
        self.assertEqual(grant_writes, [])
        application_reads = [
            call.args[0][4]
            for call in az_json.call_args_list
            if call.args[0][:3] == ["rest", "--method", "GET"]
            and "/applications" in call.args[0][4]
        ]
        self.assertTrue(application_reads)
        for url in application_reads:
            selection = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["$select"][0]
            self.assertIn("api", selection.split(","))
            self.assertIn("requiredResourceAccess", selection.split(","))
            self.assertIn("publicClient", selection.split(","))
        os.environ["PORTAL_APP_URL"] = "https://mosaic-portal.example.com"
        with redirect_stderr(StringIO()):
            mosaic_entra.postprovision(runner, "mosaic-test", "https://mosaic-spa.example.com")
        self.assertEqual(runner._dry_run_objects, original_objects)
        self.assertEqual(runner._dry_run_role_assignments, original_assignments)

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_dry_run_commands_support_arbitrary_environment_and_deployed_redirects(self) -> None:
        output = StringIO()
        with (
            mock.patch("scripts.mosaic_entra.run_json_command") as real_cli,
            redirect_stderr(output),
        ):
            self.assertEqual(
                mosaic_entra.main(["preprovision", "--dry-run", "--environment-name", "other"]),
                0,
            )
            os.environ["PORTAL_APP_URL"] = "https://mosaic-portal.example.com"
            self.assertEqual(
                mosaic_entra.main(
                    [
                        "postprovision",
                        "--dry-run",
                        "--environment-name",
                        "other",
                        "--deployed-web-url",
                        "https://mosaic-web.example.com",
                    ]
                ),
                0,
            )
        real_cli.assert_not_called()
        self.assertIn("mosaic-other-model-runtime", output.getvalue())
        self.assertIn("mosaic-other-model-client", output.getvalue())
        self.assertNotEqual(
            os.environ["MOSAIC_MODEL_RUNTIME_CLIENT_ID"], os.environ["MOSAIC_API_CLIENT_ID"]
        )
        self.assertEqual(
            len(
                {
                    os.environ["MOSAIC_MODEL_CLIENT_ID"],
                    os.environ["MOSAIC_MODEL_RUNTIME_CLIENT_ID"],
                    os.environ["MOSAIC_API_CLIENT_ID"],
                }
            ),
            3,
        )
        self.assertEqual(
            os.environ["MOSAIC_SPA_DEPLOYED_REDIRECT_URI"], "https://mosaic-web.example.com"
        )
        self.assertEqual(
            os.environ["MOSAIC_PORTAL_DEPLOYED_REDIRECT_URI"], "https://mosaic-portal.example.com"
        )

    @mock.patch.dict(os.environ, {"MOSAIC_ENTRA_MODEL_CLIENT": "false"}, clear=True)
    def test_disabled_model_client_is_not_created(self) -> None:
        runner = mosaic_entra.CliRunner(dry_run=True)
        output = StringIO()
        with redirect_stderr(output):
            mosaic_entra.preprovision(runner, "mosaic-test")
        names = {app["displayName"] for app in runner._dry_run_objects["applications"].values()}
        self.assertEqual(len(names), 4)
        self.assertNotIn("mosaic-test-model-client", names)
        self.assertEqual(len(runner._dry_run_objects["servicePrincipals"]), 4)
        self.assertEqual(runner._dry_run_objects["oauth2PermissionGrants"], {})
        self.assertIn("MOSAIC_MODEL_RUNTIME_CLIENT_ID", os.environ)
        self.assertNotIn("MOSAIC_MODEL_CLIENT_ID", os.environ)
        self.assertIn("MOSAIC_ENTRA_MODEL_CLIENT is false", output.getvalue())

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_disabling_model_client_leaves_existing_registration_and_setting_untouched(
        self,
    ) -> None:
        runner = mosaic_entra.CliRunner(dry_run=True)
        with redirect_stderr(StringIO()):
            mosaic_entra.preprovision(runner, "mosaic-test")
        bring_your_own_client_id = "55555555-5555-5555-5555-555555555555"
        os.environ["MOSAIC_ENTRA_MODEL_CLIENT"] = "FALSE"
        os.environ["MOSAIC_MODEL_CLIENT_ID"] = bring_your_own_client_id
        original_objects = deepcopy(runner._dry_run_objects)
        with (
            mock.patch.object(runner, "az_json", wraps=runner.az_json) as az_json,
            mock.patch.object(runner, "azd", wraps=runner.azd) as azd,
            redirect_stderr(StringIO()),
        ):
            mosaic_entra.preprovision(runner, "mosaic-test")
        self.assertEqual(runner._dry_run_objects, original_objects)
        self.assertEqual(os.environ["MOSAIC_MODEL_CLIENT_ID"], bring_your_own_client_id)
        commands = [" ".join(call.args[0]) for call in az_json.call_args_list]
        self.assertFalse(
            any(
                "model-client" in command or "oauth2PermissionGrants" in command
                for command in commands
            )
        )
        written = [call.args[0][2] for call in azd.call_args_list]
        self.assertIn("MOSAIC_MODEL_RUNTIME_CLIENT_ID", written)
        self.assertFalse(any(key.startswith("MOSAIC_MODEL_CLIENT") for key in written))

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_model_client_consent_extends_an_existing_tenant_grant_without_duplicates(
        self,
    ) -> None:
        runner = mosaic_entra.CliRunner(dry_run=True)
        with redirect_stderr(StringIO()):
            mosaic_entra.preprovision(runner, "mosaic-test")
        grants = runner._dry_run_objects["oauth2PermissionGrants"]
        (tenant_grant,) = grants.values()
        tenant_grant["scope"] = "Other.Scope"
        grants["user-grant"] = {
            "id": "user-grant",
            "clientId": tenant_grant["clientId"],
            "consentType": "Principal",
            "principalId": "66666666-6666-6666-6666-666666666666",
            "resourceId": tenant_grant["resourceId"],
            "scope": "Models.Invoke",
        }
        with (
            mock.patch.object(runner, "az_json", wraps=runner.az_json) as az_json,
            redirect_stderr(StringIO()),
        ):
            mosaic_entra.preprovision(runner, "mosaic-test")
            mosaic_entra.preprovision(runner, "mosaic-test")
        grant_writes = [
            (
                args[2],
                urllib.parse.urlparse(args[4]).path,
                json.loads(args[args.index("--body") + 1]),
            )
            for args in (call.args[0] for call in az_json.call_args_list)
            if args[0] == "rest" and args[2] != "GET" and "oauth2PermissionGrants" in args[4]
        ]
        self.assertEqual(
            grant_writes,
            [
                (
                    "PATCH",
                    f"/v1.0/oauth2PermissionGrants/{tenant_grant['id']}",
                    {"scope": "Other.Scope Models.Invoke"},
                )
            ],
        )
        self.assertEqual(tenant_grant["scope"], "Other.Scope Models.Invoke")
        self.assertEqual(
            [grant["consentType"] for grant in grants.values()].count("AllPrincipals"), 1
        )
        self.assertEqual(grants["user-grant"]["scope"], "Models.Invoke")

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_denied_model_client_consent_warns_with_admin_command_and_continues(self) -> None:
        runner = mosaic_entra.CliRunner(dry_run=True)
        dry_run_graph_request = runner._dry_run_graph_request

        def deny_consent(args: list[str]) -> Any:
            if args[2] == "POST" and args[4].endswith("/oauth2PermissionGrants"):
                raise mosaic_entra.OperationFailed(
                    'Operation grant failed: Forbidden({"error":{"code":'
                    '"Authorization_RequestDenied","message":"Insufficient privileges to '
                    'complete the operation."}})'
                )
            return dry_run_graph_request(args)

        output = StringIO()
        with (
            mock.patch.object(runner, "_dry_run_graph_request", side_effect=deny_consent),
            mock.patch("scripts.mosaic_entra.time.sleep") as sleep,
            redirect_stderr(output),
        ):
            mosaic_entra.preprovision(runner, "mosaic-test")
        sleep.assert_not_called()
        self.assertEqual(runner._dry_run_objects["oauth2PermissionGrants"], {})
        client_id = os.environ["MOSAIC_MODEL_CLIENT_ID"]
        runtime_id = os.environ["MOSAIC_MODEL_RUNTIME_CLIENT_ID"]
        self.assertIn("WARNING", output.getvalue())
        self.assertIn(
            f"az ad app permission grant --id {client_id} --api {runtime_id} "
            "--scope Models.Invoke",
            output.getvalue(),
        )
        self.assertIn("Grant admin consent", output.getvalue())

    def test_model_client_consent_retries_directory_replication_lag(self) -> None:
        lag = mosaic_entra.OperationFailed(
            'Operation grant failed: Bad Request({"error":{"code":"Request_BadRequest",'
            "\"message\":\"Invalid value specified for property 'clientId' of resource "
            "'OAuth2PermissionGrant'.\"}})"
        )
        runner = mock.Mock()
        runner.az_json.side_effect = [{"value": []}, lag, {"value": []}, {"id": "grant"}]
        with (
            mock.patch("scripts.mosaic_entra.time.sleep") as sleep,
            redirect_stderr(StringIO()),
        ):
            self.assertTrue(
                mosaic_entra.ensure_model_client_consent(runner, **MODEL_CLIENT_CONSENT)
            )
        sleep.assert_called_once_with(2)
        post = runner.az_json.call_args_list[-1].args[0]
        self.assertEqual(post[:5], ["rest", "--method", "POST", "--url", GRANTS_URL])
        self.assertEqual(
            json.loads(post[post.index("--body") + 1]),
            {
                "clientId": MODEL_CLIENT_CONSENT["client_service_principal_id"],
                "consentType": "AllPrincipals",
                "resourceId": MODEL_CLIENT_CONSENT["runtime_service_principal_id"],
                "scope": "Models.Invoke",
            },
        )

    def test_model_client_consent_retries_are_bounded(self) -> None:
        def always_lagging(args: list[str], operation_name: str) -> Any:
            if args[2] == "GET":
                return {"value": []}
            raise mosaic_entra.OperationFailed(
                "Operation grant failed: Not Found(Request_ResourceNotFound: Resource "
                "'88888888-8888-8888-8888-888888888888' does not exist.)"
            )

        runner = mock.Mock()
        runner.az_json.side_effect = always_lagging
        with (
            mock.patch("scripts.mosaic_entra.time.sleep") as sleep,
            redirect_stderr(StringIO()),
            self.assertRaisesRegex(mosaic_entra.OperationFailed, "Request_ResourceNotFound"),
        ):
            mosaic_entra.ensure_model_client_consent(runner, **MODEL_CLIENT_CONSENT)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 8, 16])

    def test_model_client_consent_does_not_retry_other_failures(self) -> None:
        runner = mock.Mock()
        runner.az_json.side_effect = [
            {"value": []},
            mosaic_entra.OperationFailed("Operation grant failed: Request_BadRequest: bad scope"),
        ]
        with (
            mock.patch("scripts.mosaic_entra.time.sleep") as sleep,
            redirect_stderr(StringIO()),
            self.assertRaisesRegex(mosaic_entra.OperationFailed, "bad scope"),
        ):
            mosaic_entra.ensure_model_client_consent(runner, **MODEL_CLIENT_CONSENT)
        sleep.assert_not_called()

    def test_model_client_infrastructure_wiring_is_optional(self) -> None:
        root = Path(__file__).resolve().parents[2]
        parameters = json.loads(root.joinpath("infra", "main.parameters.json").read_text())
        self.assertEqual(
            parameters["parameters"]["modelClientId"]["value"], "${MOSAIC_MODEL_CLIENT_ID}"
        )
        template = root.joinpath("infra", "main.bicep").read_text()
        self.assertIn("param modelClientId string = ''", template)
        self.assertIn("name: 'MOSAIC_MODEL_CLIENT_ID'\n    value: modelClientId", template)
        self.assertIn("output MOSAIC_MODEL_CLIENT_ID string = modelClientId", template)
        example = root.joinpath("apps", "api", ".env.example").read_text()
        self.assertIn("# MOSAIC_MODEL_CLIENT_ID=", example)

    def test_runtime_audience_infrastructure_wiring_is_separate_and_optional(self) -> None:
        root = Path(__file__).resolve().parents[2]
        parameters = json.loads(root.joinpath("infra", "main.parameters.json").read_text())
        self.assertEqual(
            parameters["parameters"]["modelRuntimeClientId"]["value"],
            "${MOSAIC_MODEL_RUNTIME_CLIENT_ID}",
        )
        self.assertEqual(
            parameters["parameters"]["apiAppClientId"]["value"], "${MOSAIC_API_CLIENT_ID}"
        )
        template = root.joinpath("infra", "main.bicep").read_text()
        self.assertIn("param modelRuntimeClientId string = ''", template)
        self.assertIn(
            "name: 'MOSAIC_MODEL_RUNTIME_CLIENT_ID'\n    value: modelRuntimeClientId", template
        )
        self.assertIn("name: 'MOSAIC_API_CLIENT_ID'\n    value: apiAppClientId", template)
        self.assertIn(
            "output MOSAIC_MODEL_RUNTIME_CLIENT_ID string = modelRuntimeClientId", template
        )
        example = root.joinpath("apps", "api", ".env.example").read_text()
        self.assertIn("# MOSAIC_MODEL_RUNTIME_CLIENT_ID=", example)

    def test_unowned_name_collision_is_rejected(self) -> None:
        runner = mock.Mock()
        runner.az_json.return_value = {
            "value": [
                {
                    "id": "unrelated-id",
                    "appId": "unrelated-client-id",
                    "displayName": "mosaic-test-api",
                    "tags": [],
                }
            ]
        }

        with self.assertRaisesRegex(mosaic_entra.OperationFailed, "not tagged"):
            mosaic_entra.find_application(
                runner,
                "mosaic-test-api",
                ["product:MOSAIC", "azd-env:mosaic-test", "managed-by:azd"],
                "find API application",
            )


if __name__ == "__main__":
    unittest.main()
