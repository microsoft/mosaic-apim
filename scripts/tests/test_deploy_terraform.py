import argparse
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from scripts import deploy_terraform as deploy
from scripts import mosaic_entra
from scripts.architecture import private_networking


class TerraformDeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        for name, value in {
            "DIRECTORY": directory,
            "VARIABLES": directory / "mosaic.auto.tfvars.json",
            "STATE": directory / ".mosaic-terraform" / "environment.json",
            "PLAN": directory / "mosaic.tfplan",
            "RECEIPT": directory / ".mosaic-terraform" / "plan.json",
        }.items():
            patcher = mock.patch.object(deploy, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_environment_sink_never_calls_azd(self) -> None:
        deploy.write_json(deploy.STATE, {"AZURE_ENV_NAME": "mosaic-test"})
        runner = deploy.EnvironmentRunner()
        with mock.patch.object(mosaic_entra, "run_json_command") as command:
            mosaic_entra.set_azd_env(runner, "MOSAIC_API_CLIENT_ID", "client-id")
        command.assert_not_called()
        self.assertEqual(deploy.read_json(deploy.STATE)["MOSAIC_API_CLIENT_ID"], "client-id")
        self.assertEqual(os.environ["MOSAIC_API_CLIENT_ID"], "client-id")
        with self.assertRaises(mosaic_entra.OperationFailed):
            runner.azd(["provision"], "unexpected invocation")

    def test_dry_run_does_not_persist_environment(self) -> None:
        runner = deploy.EnvironmentRunner(dry_run=True)
        runner.azd(["env", "set", "KEY", "value"], "persist")
        self.assertFalse(deploy.STATE.exists())

    def test_wrong_subscription_and_cloud_are_rejected(self) -> None:
        with mock.patch.object(deploy, "invoke", return_value={"id": "other"}):
            with self.assertRaisesRegex(mosaic_entra.OperationFailed, "subscription"):
                deploy.check_account("expected")
        with mock.patch.object(
            deploy,
            "invoke",
            return_value={
                "id": "expected",
                "tenantId": "tenant",
                "environmentName": "AzureUSGovernment",
            },
        ):
            with self.assertRaisesRegex(mosaic_entra.OperationFailed, "Commercial"):
                deploy.check_account("expected", "tenant")

    def test_bootstrap_refuses_another_environment_before_directory_changes(self) -> None:
        deploy.write_json(deploy.STATE, {"AZURE_ENV_NAME": "mosaic-existing"})
        options = argparse.Namespace(
            environment_name="mosaic-new", subscription_id="sub", location="eastus2"
        )
        with (
            mock.patch.object(deploy, "check_account"),
            mock.patch.object(deploy, "terraform"),
            mock.patch.object(mosaic_entra, "preprovision") as preprovision,
        ):
            with self.assertRaisesRegex(mosaic_entra.OperationFailed, "different deployment"):
                deploy.bootstrap(options)
        preprovision.assert_not_called()

    def test_changed_inputs_cannot_apply_a_stale_plan(self) -> None:
        deploy.write_json(deploy.VARIABLES, {"images": {"api": "old"}})
        (deploy.DIRECTORY / ".terraform.lock.hcl").write_text("lock", encoding="utf-8")
        deploy.PLAN.write_bytes(b"saved-plan")
        deploy.write_json(deploy.RECEIPT, {"fingerprint": deploy.fingerprint()})
        deploy.write_json(deploy.VARIABLES, {"images": {"api": "new"}})
        with (
            mock.patch.object(deploy, "load_environment"),
            mock.patch.object(deploy, "terraform") as terraform,
        ):
            with self.assertRaisesRegex(mosaic_entra.OperationFailed, "current reviewed plan"):
                deploy.apply_plan()
        terraform.assert_not_called()

    def test_plan_never_bootstraps_or_builds(self) -> None:
        with (
            mock.patch.object(deploy, "load_environment"),
            mock.patch.object(deploy, "fingerprint", return_value="fingerprint"),
            mock.patch.object(deploy, "terraform") as terraform,
            mock.patch.object(mosaic_entra, "preprovision") as preprovision,
            mock.patch.object(deploy, "build_images") as build,
        ):
            deploy.make_plan()
        self.assertEqual([call.args[0] for call in terraform.call_args_list], ["init", "plan"])
        preprovision.assert_not_called()
        build.assert_not_called()

    def test_build_persists_all_digest_images_without_deploying(self) -> None:
        deployment = {
            "AZURE_CONTAINER_REGISTRY_NAME": "registry",
            "AZURE_CONTAINER_REGISTRY_ENDPOINT": "registry.azurecr.io",
        }
        deploy.write_json(deploy.VARIABLES, {"publisher_name": "keep"})
        deploy.PLAN.write_bytes(b"stale")
        with (
            mock.patch.object(deploy, "load_environment"),
            mock.patch.object(deploy, "outputs", return_value=deployment),
            mock.patch.object(
                deploy, "invoke", return_value={"digest": "sha256:" + "a" * 64}
            ) as run,
        ):
            deploy.build_images("https://pypi.org/simple")
        values = deploy.read_json(deploy.VARIABLES)
        self.assertEqual(values["publisher_name"], "keep")
        self.assertEqual(set(values["images"]), set(deploy.APPS))
        self.assertTrue(all("@sha256:" in image for image in values["images"].values()))
        self.assertFalse(deploy.PLAN.exists())
        self.assertEqual(
            sum(call.args[0][:3] == ["az", "acr", "build"] for call in run.call_args_list), 3
        )
        self.assertFalse(any("webapp" in call.args[0] for call in run.call_args_list))

    def test_failed_build_preserves_previous_image_configuration(self) -> None:
        previous = {"images": {"api": "previous", "web": "previous", "portal": "previous"}}
        deploy.write_json(deploy.VARIABLES, previous)
        with (
            mock.patch.object(deploy, "load_environment"),
            mock.patch.object(
                deploy, "outputs", return_value={"AZURE_CONTAINER_REGISTRY_NAME": "acr"}
            ),
            mock.patch.object(deploy, "invoke", side_effect=mosaic_entra.OperationFailed("build")),
        ):
            with self.assertRaises(mosaic_entra.OperationFailed):
                deploy.build_images("https://pypi.org/simple")
        self.assertEqual(deploy.read_json(deploy.VARIABLES), previous)

    def test_package_index_cannot_expose_credentials_in_build_args(self) -> None:
        with mock.patch.object(deploy, "load_environment"):
            with self.assertRaisesRegex(mosaic_entra.OperationFailed, "without credentials"):
                deploy.build_images("https://user:password@example.com/simple")

    def test_wrong_deployment_outputs_are_not_used_for_directory_mutation(self) -> None:
        deploy.write_json(deploy.STATE, {"AZURE_ENV_NAME": "mosaic-one"})
        with mock.patch.object(
            deploy, "terraform", return_value={"AZURE_ENV_NAME": "mosaic-other"}
        ):
            with self.assertRaisesRegex(mosaic_entra.OperationFailed, "does not match"):
                deploy.outputs()

    def test_readiness_failure_is_not_reported_as_success(self) -> None:
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = b'{"status":"notReady"}'
        response.__enter__.return_value = response
        urls = {f"{app.upper()}_APP_URL": f"https://{app}.example.com" for app in deploy.APPS}
        with (
            mock.patch.object(deploy, "load_environment"),
            mock.patch.object(deploy, "outputs", return_value=urls),
            mock.patch.object(deploy.urllib.request, "urlopen", return_value=response),
            mock.patch.object(deploy.time, "monotonic", side_effect=[0, 0, 2]),
            mock.patch.object(deploy.time, "sleep"),
        ):
            with self.assertRaisesRegex(mosaic_entra.OperationFailed, "Readiness timed out"):
                deploy.check_readiness(1)

    def test_deployment_state_is_excluded_from_container_build_context(self) -> None:
        ignore = (deploy.ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("infra/terraform", ignore)
        self.assertIn("**/*.tfstate", ignore)

    def test_editable_diagram_and_render_stay_in_sync(self) -> None:
        scene, svg = private_networking.generate()
        directory = private_networking.DESTINATION
        self.assertEqual(
            deploy.read_json(directory / "mosaic-private-networking.excalidraw"), scene
        )
        self.assertEqual(
            (directory / "mosaic-private-networking.svg").read_text(encoding="utf-8"), svg
        )
        ET.fromstring(svg)


if __name__ == "__main__":
    unittest.main()
