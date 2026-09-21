import contextlib
import io
import unittest
from unittest.mock import patch

import httpx

from scripts import verify_model_access as verifier


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


if __name__ == "__main__":
    unittest.main()
