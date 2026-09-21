import json
import os
import unittest
import urllib.parse
from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts import mosaic_entra


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
        self.assertEqual(
            len(
                {
                    context.api_display_name,
                    context.spa_display_name,
                    context.portal_display_name,
                    context.model_runtime_display_name,
                }
            ),
            4,
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
    def test_preprovision_creates_four_distinct_registrations_without_runtime_grants(self) -> None:
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
            },
        )
        self.assertEqual(len({app["appId"] for app in apps.values()}), 4)
        self.assertEqual(len({app["id"] for app in apps.values()}), 4)
        service_principals = runner._dry_run_objects["servicePrincipals"]
        self.assertEqual(len(service_principals), 4)
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
        posts = [
            call.args[0]
            for call in az_json.call_args_list
            if call.args[0][:3] == ["rest", "--method", "POST"]
        ]
        paths = [urllib.parse.urlparse(args[4]).path for args in posts]
        self.assertEqual(paths.count("/v1.0/applications"), 4)
        self.assertEqual(paths.count("/v1.0/servicePrincipals"), 4)
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
        self.assertEqual(len(posts), 9)

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
        self.assertNotEqual(
            os.environ["MOSAIC_MODEL_RUNTIME_CLIENT_ID"], os.environ["MOSAIC_API_CLIENT_ID"]
        )
        self.assertEqual(
            os.environ["MOSAIC_SPA_DEPLOYED_REDIRECT_URI"], "https://mosaic-web.example.com"
        )
        self.assertEqual(
            os.environ["MOSAIC_PORTAL_DEPLOYED_REDIRECT_URI"], "https://mosaic-portal.example.com"
        )

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
