import unittest
from unittest import mock

from scripts import mosaic_entra


class MosaicEntraTests(unittest.TestCase):
    def test_deterministic_ids_are_stable(self) -> None:
        self.assertEqual(
            mosaic_entra.api_scope_id(),
            mosaic_entra.api_scope_id(),
        )
        self.assertEqual(
            mosaic_entra.admin_role_id(),
            mosaic_entra.admin_role_id(),
        )
        self.assertEqual(
            mosaic_entra.portal_role_id(),
            mosaic_entra.portal_role_id(),
        )
        self.assertEqual(
            len({
                mosaic_entra.api_scope_id(),
                mosaic_entra.admin_role_id(),
                mosaic_entra.portal_role_id(),
            }),
            3,
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
