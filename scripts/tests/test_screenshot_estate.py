import ast
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from scripts.screenshots.demo_api import COST_CENTER_MEMBERS, build_demo_app

PORTAL_HEADERS = {"Origin": "http://127.0.0.1:15174"}


class ScreenshotEstateTests(unittest.TestCase):
    client: TestClient

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = cls.enterClassContext(TestClient(build_demo_app(15173, 15174)))

    def test_fictional_center_uses_shared_directory_membership(self) -> None:
        response = self.client.get("/api/v1/cost-centers")
        response.raise_for_status()
        center = next(item for item in response.json() if item["code"] == "CI-204")
        self.assertEqual(center["setupState"], "ready")

        response = self.client.get(f"/api/v1/cost-centers/{center['id']}/directory-members")
        response.raise_for_status()
        members = response.json()
        self.assertEqual(members["apiVersion"], "beta")
        self.assertIsNone(members["nextCursor"])
        self.assertEqual(
            {item["objectId"] for item in members["members"]},
            {person.object_id for person in COST_CENTER_MEMBERS["CI-204"]},
        )

    def test_fictional_automatic_access_has_one_group_grant_per_offering(self) -> None:
        response = self.client.get("/api/v1/entitlements")
        response.raise_for_status()
        automatic = [item for item in response.json() if item["managedBy"] == "costCenterAutomatic"]
        self.assertEqual(len(automatic), 2)
        self.assertEqual(
            {item["resource"]["kind"] for item in automatic}, {"modelApi", "mcpServer"}
        )
        self.assertEqual({item["subject"]["kind"] for item in automatic}, {"securityGroup"})
        self.assertEqual(len({item["subject"]["id"] for item in automatic}), 1)
        self.assertEqual(len({item["costCenterId"] for item in automatic}), 1)
        self.assertTrue(
            all(item["enabled"] and item["binding"]["attributionKey"] for item in automatic)
        )

    def test_fictional_direct_connection_grants_are_applied(self) -> None:
        response = self.client.get("/api/v1/portal/entitlements", headers=PORTAL_HEADERS)
        response.raise_for_status()
        grants = [
            item for item in response.json() if (item.get("model") or {}).get("name") == "gpt-4o"
        ]
        self.assertEqual(len(grants), 2)
        self.assertEqual(
            {item["costCenter"]["name"] for item in grants}, {"Customer Insights", "General"}
        )
        self.assertTrue(all(item["via"] == "direct" for item in grants))
        self.assertTrue(all(item["accessOrigin"] == "approvedIndividual" for item in grants))
        self.assertTrue(
            all(item["entitlement"]["runtime"]["status"] == "applied" for item in grants)
        )

    def test_unapplied_model_review_is_captured_last(self) -> None:
        capture = Path(__file__).parents[1] / "screenshots" / "capture.py"
        source = ast.parse(capture.read_text(encoding="utf-8"))
        shots = next(
            node.value
            for node in source.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "SHOTS"
        )
        assert isinstance(shots, ast.List)
        last = shots.elts[-1]
        assert isinstance(last, ast.Call)
        assert isinstance(last.func, ast.Name) and last.func.id == "Shot"
        assert isinstance(last.args[0], ast.Constant)
        self.assertEqual(last.args[0].value, "console-model-change-review")

    def test_fictional_creation_requests_keep_incomplete_approval_honest(self) -> None:
        response = self.client.get("/api/v1/portal/cost-center-requests", headers=PORTAL_HEADERS)
        response.raise_for_status()
        proposals = response.json()
        self.assertEqual({item["state"] for item in proposals}, {"pending", "approved"})
        pending = next(item for item in proposals if item["state"] == "pending")
        self.assertEqual(pending["name"], "Responsible AI evaluation")
        self.assertTrue(pending["suggestedGroupObjectId"])
        approved = next(item for item in proposals if item["state"] == "approved")
        self.assertEqual(approved["name"], "Knowledge Engineering")
        self.assertEqual(approved["setupState"], "setupRequired")
        self.assertTrue(approved["requesterMembershipRequested"])
        self.assertEqual(approved["requesterMembershipState"], "pending")
        self.assertTrue(approved["needsAdministratorAttention"])

        response = self.client.get("/api/v1/entitlements")
        response.raise_for_status()
        self.assertFalse(
            any(item["costCenterId"] == approved["costCenter"]["id"] for item in response.json())
        )
        response = self.client.get("/api/v1/portal/request-counts", headers=PORTAL_HEADERS)
        response.raise_for_status()
        self.assertEqual(
            response.json(),
            {"pendingResourceCount": 1, "pendingCostCenterCount": 1, "pendingCount": 2},
        )
