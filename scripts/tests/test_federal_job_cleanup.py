import hashlib
import json
import unittest
from unittest.mock import Mock, call, patch

from scripts.federal_demo.jobs import verify_wrong_client
from scripts.federal_demo.operator import OperationError, Operator


class WrongClientCleanupTests(unittest.TestCase):
    def setUp(self):
        self.operator = Mock(spec=Operator)
        bindings = [
            {"url": f"https://backend-{index}.example.test", "audience": f"api://backend-{index}"}
            for index in range(24)
        ]
        self.binding_hash = hashlib.sha256(
            json.dumps(bindings, sort_keys=True).encode()
        ).hexdigest()
        targets = sorted(bindings, key=lambda item: (item["url"], item["audience"]))
        self.targets_hash = hashlib.sha256(
            json.dumps(targets, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.operator.target = {
            "mcpBindings": bindings,
            "jobBindings": [{"key": "nasa-brief", "principalId": "job-principal"}],
        }
        self.operator.state = {
            "objects": {
                "backendVerification": {"bindingsSha256": self.binding_hash},
                "execution:nasa-brief": {"name": "probe-execution"},
            }
        }
        for env in ("dev", "stg", "prod"):
            self.operator.state["objects"][f"audience:{env}"] = {
                "principalId": f"{env}-principal",
                "roleId": f"{env}-role",
            }
        self.operator.items.return_value = []
        self.operator.remember.side_effect = self.operator.state["objects"].__setitem__
        self.ensure_role = self.enterContext(
            patch(
                "scripts.federal_demo.jobs.ensure_role",
                side_effect=["dev-assignment", "stg-assignment", "prod-assignment"],
            )
        )
        self.run = self.enterContext(
            patch(
                "scripts.federal_demo.jobs.run",
                return_value={
                    "targets_sha256": self.targets_hash,
                    "target_count": 24,
                    "status_counts": {"403": 24},
                },
            )
        )
        self.deletions = [
            call(
                "DELETE",
                f"/servicePrincipals/job-principal/appRoleAssignments/{env}-assignment",
            )
            for env in ("prod", "stg", "dev")
        ]

    def test_multiple_deletion_failures_attempt_all_removals_without_success_proof(self):
        self.operator.graph.side_effect = [
            OperationError("Production HTTP 403"),
            OperationError("Staging HTTP 503"),
            None,
        ]

        with self.assertRaises(OperationError) as raised:
            verify_wrong_client(self.operator)

        self.assertEqual(self.operator.graph.call_args_list, self.deletions)
        self.assertIn("prod-assignment: Production HTTP 403", str(raised.exception))
        self.assertIn("stg-assignment: Staging HTTP 503", str(raised.exception))
        self.assertNotIn("wrongClientVerification", self.operator.state["objects"])
        self.assertEqual(self.operator.remember.call_count, 3)

    def test_successful_full_cleanup_records_proof_only_after_all_removals(self):
        verify_wrong_client(self.operator)

        self.assertEqual(self.operator.graph.call_args_list, self.deletions)
        proof = {
            "bindingsSha256": self.binding_hash,
            "targetsSha256": self.targets_hash,
            "denied": 24,
            "execution": "probe-execution",
            "temporaryPermissionsRemoved": True,
        }
        self.assertEqual(self.operator.state["objects"]["wrongClientVerification"], proof)
        self.assertEqual(
            self.operator.mock_calls[-4:],
            [
                call.graph(*deletion.args)
                for deletion in self.deletions
            ] + [call.remember("wrongClientVerification", proof)],
        )

    def test_failed_probe_still_removes_every_role_without_success_proof(self):
        self.run.side_effect = OperationError("Probe execution failed")

        with self.assertRaisesRegex(OperationError, "Probe execution failed"):
            verify_wrong_client(self.operator)

        self.assertEqual(self.operator.graph.call_args_list, self.deletions)
        self.assertNotIn("wrongClientVerification", self.operator.state["objects"])

    def test_failed_probe_and_cleanup_report_all_failed_removals(self):
        self.run.side_effect = OperationError("Probe execution failed")
        self.operator.graph.side_effect = [
            OperationError("Production HTTP 403"),
            None,
            OperationError("Development HTTP 503"),
        ]

        with self.assertRaises(OperationError) as raised:
            verify_wrong_client(self.operator)

        self.assertEqual(self.operator.graph.call_args_list, self.deletions)
        self.assertIn("prod-assignment: Production HTTP 403", str(raised.exception))
        self.assertIn("dev-assignment: Development HTTP 503", str(raised.exception))
        self.assertEqual(str(raised.exception.__context__), "Probe execution failed")
        self.assertNotIn("wrongClientVerification", self.operator.state["objects"])


if __name__ == "__main__":
    unittest.main()
