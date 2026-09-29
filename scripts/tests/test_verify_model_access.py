import base64
import contextlib
import io
import json
import unittest
from unittest.mock import patch

import httpx

from scripts import verify_model_access as verifier


def jwt(payload: dict[str, object]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class ModelAccessVerifierTests(unittest.TestCase):
    def test_gateway_origin_is_explicit_and_https(self) -> None:
        for url in ("http://gateway.example", "https://user:password@gateway.example"):
            with self.assertRaises(verifier.VerificationFailed):
                verifier.https_origin(url)
        connection = {
            "endpoint": "https://unexpected.example/model",
            "operations": [{"method": "POST", "path": "/chat/completions"}],
        }
        with self.assertRaises(verifier.VerificationFailed):
            verifier.model_url(connection, "https://approved.example", "test-version")

    def test_model_url_never_invents_a_provider_operation(self) -> None:
        with self.assertRaises(verifier.VerificationFailed):
            verifier.model_url(
                {"endpoint": "https://gateway.example/model", "operations": []},
                "https://gateway.example",
                "test-version",
            )
        url = verifier.model_url(
            {
                "endpoint": "https://gateway.example/model",
                "operations": [{"method": "POST", "path": "/chat/completions"}],
            },
            "https://gateway.example",
            "test-version",
        )
        self.assertEqual(
            url, "https://gateway.example/model/chat/completions?api-version=test-version"
        )

    def test_failure_never_echoes_an_error_body(self) -> None:
        response = httpx.Response(500, json={"error": "sensitive-fixture-value"})
        with self.assertRaises(verifier.VerificationFailed) as raised:
            verifier.expect(response, {200}, "Read")
        self.assertNotIn("sensitive-fixture-value", str(raised.exception))

    def test_key_retrieval_requires_no_store(self) -> None:
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"key": "fixture-key"})
            )
        ) as client, self.assertRaises(verifier.VerificationFailed):
            verifier.reveal(
                client, "https://mosaic.example/api/v1", "fixture-token", "grant", "primary"
            )

    def test_missing_credentials_stop_before_network(self) -> None:
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stderr(output):
            result = verifier.main(
                [
                    "--api-base-url", "https://mosaic.example",
                    "--gateway-origin", "https://gateway.example",
                    "--user-entitlement", "user-grant",
                    "--application-entitlement", "application-grant",
                    "--api-version", "test",
                    "--send-model-requests",
                ]
            )
        self.assertEqual(result, 1)
        self.assertIn("MOSAIC_SMOKE_USER_CONTROL_TOKEN", output.getvalue())

    def test_agent_entitlement_checks_connection_and_runtime_token(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET" and request.url.path.endswith(
                "/entitlements/agent-grant/connection"
            ):
                return httpx.Response(
                    200,
                    json={
                        "endpoint": "https://gateway.example/model",
                        "operations": [{"method": "POST", "path": "/chat/completions"}],
                        "deploymentName": "deployment",
                        "principalKind": "agentIdentity",
                        "entraScope": "api://runtime/.default",
                        "requiredAppRole": "Models.Invoke.Application",
                        "appliedMethods": {"entraEnabled": True},
                        "runtime": {"status": "applied"},
                    },
                )
            if request.method == "POST" and request.url.host == "gateway.example":
                return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
            return httpx.Response(500)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            verifier.verify_agent_entitlement(
                client,
                base="https://mosaic.example/api/v1",
                entitlement_id="agent-grant",
                control_token="admin-token",
                runtime_token="agent-runtime-token",
                expected_origin="https://gateway.example",
                api_version="test",
            )

        self.assertEqual(requests[0].headers["Authorization"], "Bearer admin-token")
        self.assertEqual(requests[1].headers["Authorization"], "Bearer agent-runtime-token")

    def test_group_entitlement_refuses_keys_and_uses_member_token(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "GET" and request.url.path.endswith(
                "/entitlements/group-grant/connection"
            ):
                return httpx.Response(
                    200,
                    json={
                        "endpoint": "https://gateway.example/model",
                        "operations": [{"method": "POST", "path": "/chat/completions"}],
                        "deploymentName": "deployment",
                        "keysAvailable": False,
                    },
                )
            if request.method == "POST" and request.url.path.endswith(
                "/entitlements/group-grant/keys/reveal"
            ):
                return httpx.Response(409, json={"error": "no keys"})
            if request.method == "POST" and request.url.host == "gateway.example":
                return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
            return httpx.Response(500)

        token = jwt({"groups": ["11111111-1111-1111-1111-111111111111"]})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            verifier.verify_group_entitlement(
                client,
                base="https://mosaic.example/api/v1",
                entitlement_id="group-grant",
                control_token="admin-token",
                runtime_token=token,
                expected_origin="https://gateway.example",
                api_version="test",
            )

        self.assertEqual(requests[0].headers["Authorization"], "Bearer admin-token")
        self.assertEqual(requests[1].headers["Authorization"], "Bearer admin-token")
        self.assertEqual(requests[2].headers["Authorization"], f"Bearer {token}")

    def test_group_token_diagnostic_never_prints_claim_values(self) -> None:
        token = jwt(
            {
                "oid": "sensitive-object-id",
                "_claim_names": {"groups": "src1"},
                "_claim_sources": {"src1": {"endpoint": "https://graph.example/groups"}},
            }
        )
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            verifier.warn_group_claim_diagnostics(token)
        diagnostics = output.getvalue()
        self.assertIn("no groups claim", diagnostics)
        self.assertIn("group overage", diagnostics)
        self.assertNotIn("sensitive-object-id", diagnostics)
        self.assertNotIn("graph.example", diagnostics)

    def test_agent_option_requires_agent_runtime_token_only_when_used(self) -> None:
        env = {
            "MOSAIC_SMOKE_USER_CONTROL_TOKEN": "user-control",
            "MOSAIC_SMOKE_ADMIN_CONTROL_TOKEN": "admin-control",
            "MOSAIC_SMOKE_USER_RUNTIME_TOKEN": "user-runtime",
            "MOSAIC_SMOKE_APPLICATION_RUNTIME_TOKEN": "app-runtime",
        }
        output = io.StringIO()
        with patch.dict("os.environ", env, clear=True), contextlib.redirect_stderr(output):
            result = verifier.main(
                [
                    "--api-base-url", "https://mosaic.example",
                    "--gateway-origin", "https://gateway.example",
                    "--user-entitlement", "user-grant",
                    "--application-entitlement", "application-grant",
                    "--agent-entitlement", "agent-grant",
                    "--api-version", "test",
                    "--send-model-requests",
                ]
            )
        self.assertEqual(result, 1)
        self.assertIn("MOSAIC_SMOKE_AGENT_RUNTIME_TOKEN", output.getvalue())


if __name__ == "__main__":
    unittest.main()
