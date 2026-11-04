import copy
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, call, patch
from urllib.parse import parse_qs, urlsplit

from scripts.federal_demo import __main__ as cli
from scripts.federal_demo import consent, verification
from scripts.federal_demo.operator import OperationError


def consent_operator(*, recorded=None, client_present=True):
    operator = Mock()
    operator.target = {
        "estate": "demo",
        "apiAppClientId": "api-client",
        "apiServicePrincipalId": "api-principal",
    }
    operator.state = {
        "objects": {
            "bootstrap": {
                "runtimeClientId": "runtime-client",
                "runtimePrincipalId": "runtime-principal",
            }
        }
    }
    if recorded is not None:
        operator.state["objects"]["operatorModelConsent"] = copy.deepcopy(recorded)
    applications = {
        "api-client": {
            "id": "api-application",
            "tags": ["azd-env:mosaic-demo"],
            "appRoles": [{"id": "admin-role", "value": "Admin"}],
            "api": {
                "oauth2PermissionScopes": [
                    {"id": "api-scope", "value": "access_as_user", "isEnabled": True}
                ]
            },
        },
        "runtime-client": {
            "id": "runtime-application",
            "tags": ["azd-env:mosaic-demo"],
            "api": {
                "oauth2PermissionScopes": [
                    {"id": "model-scope", "value": "Models.Invoke", "isEnabled": True}
                ]
            },
        },
    }
    grants = []

    def items(path):
        if path.startswith("/applications?"):
            client_id = parse_qs(urlsplit(path).query)["$filter"][0].split("'")[1]
            return [copy.deepcopy(applications[client_id])]
        if path == "/servicePrincipals/api-principal/appRoleAssignedTo":
            return [{"principalId": "operator-object", "appRoleId": "admin-role"}]
        if path.startswith("/servicePrincipals?"):
            return [{"id": "cli-principal"}] if client_present else []
        if path.startswith("/oauth2PermissionGrants?"):
            return copy.deepcopy(grants)
        raise AssertionError(f"Unexpected collection read: {path}")

    def graph(method, path, body=None):
        if method == "GET" and path.startswith("/me?"):
            return {"id": "operator-object"}
        if method == "POST" and path == "/servicePrincipals":
            return {"id": "cli-principal"}
        if path.startswith("/applications/"):
            object_id = path.split("/")[2].split("?")[0]
            app = next(app for app in applications.values() if app["id"] == object_id)
            if method == "GET":
                return copy.deepcopy(app)
            if method == "PATCH":
                app.update(copy.deepcopy(body))
                return None
        if method == "POST" and path == "/oauth2PermissionGrants":
            grant = body | {"id": "consent-grant"}
            grants.append(grant)
            return copy.deepcopy(grant)
        if path == "/oauth2PermissionGrants/consent-grant":
            if method == "GET":
                return copy.deepcopy(grants[0])
            if method == "DELETE":
                grants.clear()
                return None
        raise AssertionError(f"Unexpected Graph request: {method} {path}")

    def remember(key, value):
        operator.state["objects"][key] = copy.deepcopy(value)

    operator.items.side_effect = items
    operator.graph.side_effect = graph
    operator.remember.side_effect = remember
    return operator


def consent_ledger():
    return {
        "apiClientId": "runtime-client",
        "applicationObjectId": "runtime-application",
        "resourceId": "runtime-principal",
        "operatorId": "operator-object",
        "clientId": "cli-principal",
        "scopeId": "model-scope",
        "preauthorizedBefore": False,
        "temporary": True,
    }


def verification_operator(ledger=None):
    operator = Mock()
    operator.state = {
        "objects": {
            f"model:{index}": {"id": f"model-{index}", "publicationId": f"publication-{index}"}
            for index in range(5)
        }
    }
    if ledger is not None:
        operator.state["objects"]["verificationAccess"] = copy.deepcopy(ledger)
    operator.graph.return_value = {"id": "operator-object"}
    operator.remember.side_effect = lambda key, value: operator.state["objects"].update(
        {key: copy.deepcopy(value)}
    )
    return operator


def verification_ledger(**changes):
    return {
        "principalId": "operator-principal",
        "principalCreated": False,
        "grants": [],
        "cleanedUp": False,
    } | changes


def verification_api(principals):
    api = Mock()

    def request(method, path, body=None):
        if path == "/principals":
            return principals
        if path == "/cost-centers":
            return [{"code": "general", "id": "general-center"}]
        if path == "/entitlements":
            if method == "GET":
                return []
            if method == "POST":
                return body | {"id": body["resource"]["id"] + "-grant"}
        if method == "GET" and path.endswith("/connection"):
            return {"url": "https://example.test/model"}
        raise AssertionError(f"Unexpected MOSAIC request: {method} {path}")

    api.request.side_effect = request
    return api


class ConsentLifecycleTests(unittest.TestCase):
    def assert_no_writes(self, operator):
        self.assertTrue(all(entry.args[0] == "GET" for entry in operator.graph.call_args_list))
        operator.remember.assert_not_called()

    def test_retired_consent_is_rejected_before_any_writes(self):
        operator = consent_operator(recorded=consent_ledger() | {"cleanedUp": True})
        with self.assertRaisesRegex(OperationError, "retired"):
            consent.provision(operator, for_model_verification=True)
        self.assert_no_writes(operator)
        operator.graph.assert_not_called()

    def test_resumed_consent_validates_every_recorded_owner_before_writes(self):
        for field in (
            "apiClientId",
            "applicationObjectId",
            "resourceId",
            "operatorId",
            "clientId",
            "scopeId",
        ):
            for missing in (False, True):
                with self.subTest(field=field, missing=missing):
                    recorded = consent_ledger()
                    if missing:
                        del recorded[field]
                    else:
                        recorded[field] = "different-owner"
                    operator = consent_operator(recorded=recorded)
                    before = copy.deepcopy(operator.state)
                    with self.assertRaisesRegex(OperationError, "ownership changed"):
                        consent.provision(operator, for_model_verification=True)
                    self.assert_no_writes(operator)
                    self.assertEqual(operator.state, before)

    def test_missing_recorded_cli_principal_is_not_recreated(self):
        operator = consent_operator(recorded=consent_ledger(), client_present=False)
        with self.assertRaisesRegex(OperationError, "ownership changed"):
            consent.provision(operator, for_model_verification=True)
        self.assert_no_writes(operator)

    def test_matching_operator_can_resume_without_creating_another_grant(self):
        operator = consent_operator()
        consent.provision(operator, for_model_verification=True)
        recorded = copy.deepcopy(operator.state["objects"]["operatorModelConsent"])
        operator.graph.reset_mock()
        consent.provision(operator, for_model_verification=True)
        self.assertEqual(operator.state["objects"]["operatorModelConsent"], recorded)
        self.assertFalse(any(entry.args[0] == "POST" for entry in operator.graph.call_args_list))

    def test_provision_cleanup_provision_cannot_restore_retired_permissions(self):
        operator = consent_operator()
        consent.provision(operator, for_model_verification=True)
        consent.cleanup_model_verification(operator)
        self.assertTrue(operator.state["objects"]["operatorModelConsent"]["cleanedUp"])
        self.assertEqual(
            operator.graph.call_args_list[-2].args,
            ("GET", "/oauth2PermissionGrants/consent-grant"),
        )
        operator.graph.assert_any_call("DELETE", "/oauth2PermissionGrants/consent-grant")
        patch_body = next(
            entry.args[2]
            for entry in reversed(operator.graph.call_args_list)
            if entry.args[0] == "PATCH"
        )
        self.assertEqual(patch_body["api"]["preAuthorizedApplications"], [])
        operator.graph.reset_mock()
        operator.remember.reset_mock()
        with self.assertRaisesRegex(OperationError, "retired"):
            consent.provision(operator, for_model_verification=True)
        self.assert_no_writes(operator)

    def test_fresh_model_consent_can_create_the_cli_principal(self):
        operator = consent_operator(client_present=False)
        consent.provision(operator, for_model_verification=True)
        operator.graph.assert_any_call(
            "POST", "/servicePrincipals", {"appId": consent.AZURE_CLI_CLIENT}
        )
        self.assertTrue(operator.state["objects"]["operatorModelConsent"]["createdGrant"])

    def test_normal_operator_consent_does_not_reopen_the_retired_model_ledger(self):
        recorded = consent_ledger() | {"cleanedUp": True}
        operator = consent_operator(recorded=recorded)
        consent.provision(operator)
        self.assertEqual(operator.state["objects"]["operatorModelConsent"], recorded)
        self.assertEqual(operator.state["objects"]["operatorConsent"]["apiClientId"], "api-client")


class VerificationLifecycleTests(unittest.TestCase):
    def test_retired_access_is_rejected_before_registering_another_principal(self):
        operator = verification_operator(verification_ledger(cleanedUp=True))
        with (
            patch.object(verification, "Mosaic") as mosaic,
            patch.object(verification, "register_identity") as register,
            self.assertRaisesRegex(OperationError, "retired"),
        ):
            verification.prepare(operator)
        mosaic.assert_not_called()
        register.assert_not_called()
        operator.remember.assert_not_called()

    def test_other_operator_or_missing_principal_is_rejected_before_writes(self):
        for principals in ([], [{"id": "other-principal", "objectId": "operator-object"}]):
            with self.subTest(principals=principals):
                operator = verification_operator(verification_ledger())
                before = copy.deepcopy(operator.state)
                api = Mock()
                api.request.return_value = principals
                with (
                    patch.object(verification, "Mosaic", return_value=api),
                    patch.object(verification, "register_identity") as register,
                    self.assertRaisesRegex(OperationError, "principal ownership changed"),
                ):
                    verification.prepare(operator)
                self.assertEqual(api.request.call_args_list, [call("GET", "/principals")])
                api.apply.assert_not_called()
                register.assert_not_called()
                operator.remember.assert_not_called()
                self.assertEqual(operator.state, before)

    def test_matching_or_fresh_operator_records_its_own_grants(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                operator = verification_operator(verification_ledger() if resume else None)
                principal = {"id": "operator-principal", "objectId": "operator-object"}
                api = verification_api([principal] if resume else [])
                with (
                    patch.object(verification, "Mosaic", return_value=api),
                    patch.object(
                        verification, "register_identity", return_value=principal
                    ) as register,
                ):
                    targets = verification.prepare(operator)
                self.assertEqual(len(targets), 5)
                self.assertEqual(api.apply.call_count, 5)
                ledger = operator.state["objects"]["verificationAccess"]
                self.assertEqual(ledger["principalId"], principal["id"])
                self.assertEqual(ledger["principalCreated"], not resume)
                self.assertEqual(len(ledger["grants"]), 5)
                for entry in api.request.call_args_list:
                    if entry.args[0] == "POST":
                        self.assertEqual(entry.args[2]["subject"]["id"], ledger["principalId"])
                self.assertEqual(register.call_count, 0 if resume else 1)


class CleanupCommandTests(unittest.TestCase):
    def test_each_cleanup_is_attempted_and_each_failure_is_reported(self):
        for grant_error in (
            None,
            OperationError("publication apply failed"),
            OperationError("Verification grant ownership changed"),
        ):
            for consent_error in (None, OperationError("consent ownership changed")):
                with self.subTest(grant_error=grant_error, consent_error=consent_error):
                    operator = Mock()
                    calls = Mock()
                    calls.attach_mock(Mock(side_effect=grant_error), "grants")
                    calls.attach_mock(Mock(side_effect=consent_error), "consent")
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with (
                        patch.object(Path, "read_text", return_value="{}"),
                        patch.object(cli, "Operator", return_value=operator),
                        patch.object(cli.verification, "cleanup", calls.grants),
                        patch.object(cli.consent, "cleanup_model_verification", calls.consent),
                        redirect_stdout(stdout),
                        redirect_stderr(stderr),
                    ):
                        result = cli.main([
                            "cleanup-model-verification", "--target", "target.json",
                            "--state", "state.json", "--apply",
                        ])
                    self.assertEqual(
                        calls.mock_calls, [call.grants(operator), call.consent(operator)]
                    )
                    failed = grant_error is not None or consent_error is not None
                    self.assertEqual(result, int(failed))
                    self.assertEqual(bool(stdout.getvalue()), not failed)
                    for error in (grant_error, consent_error):
                        if error is not None:
                            self.assertIn(str(error), stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
