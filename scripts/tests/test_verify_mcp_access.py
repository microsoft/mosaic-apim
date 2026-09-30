import contextlib
import io
import unittest
from unittest.mock import patch

import httpx

from scripts import verify_mcp_access as verifier


class McpAccessVerifierTests(unittest.TestCase):
    def test_https_url_rejects_unsafe_values(self) -> None:
        for url in ("http://gateway.example/mcp", "https://user:pass@gateway.example/mcp"):
            with self.assertRaises(verifier.VerificationFailed):
                verifier.https_url(url, label="fixture")

    def test_metadata_challenge_and_document_are_verified(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer error="invalid_token", '
                            'resource_metadata="https://gateway.example/.well-known/'
                            'oauth-protected-resource/mosaic/mcp/example/mcp"'
                        )
                    },
                )
            return httpx.Response(
                200,
                json={
                    "resource": "https://gateway.example/mosaic/mcp/example/mcp",
                    "authorization_servers": [
                        "https://login.microsoftonline.com/tenant-id/v2.0"
                    ],
                    "scopes_supported": ["api://runtime-client/Mcp.Invoke"],
                },
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            metadata_url = verifier.verify_metadata(
                client,
                server_url="https://gateway.example/mosaic/mcp/example/mcp",
                tenant_id="tenant-id",
                runtime_client_id="runtime-client",
            )

        self.assertEqual(
            metadata_url,
            "https://gateway.example/.well-known/oauth-protected-resource/mosaic/mcp/example/mcp",
        )
        self.assertEqual(requests[0].headers["Accept"], "application/json, text/event-stream")

    def test_metadata_must_match_server_url(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer resource_metadata="https://gateway.example/metadata"'
                        )
                    },
                )
            return httpx.Response(
                200,
                json={
                    "resource": "https://gateway.example/other/mcp",
                    "authorization_servers": [
                        "https://login.microsoftonline.com/tenant-id/v2.0"
                    ],
                    "scopes_supported": ["api://runtime-client/Mcp.Invoke"],
                },
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(verifier.VerificationFailed):
                verifier.verify_metadata(
                    client,
                    server_url="https://gateway.example/mosaic/mcp/example/mcp",
                    tenant_id="tenant-id",
                    runtime_client_id="runtime-client",
                )

    def test_denied_token_checks_insufficient_scope_without_printing_token(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                403,
                headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
            )

        output = io.StringIO()
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with contextlib.redirect_stdout(output):
                verifier.verify_denied_token(
                    client,
                    server_url="https://gateway.example/mosaic/mcp/example/mcp",
                    token="sensitive-token",
                )

        self.assertEqual(requests[0].headers["Authorization"], "Bearer sensitive-token")
        self.assertNotIn("sensitive-token", output.getvalue())

    def test_granted_initialize_accepts_json_response(self) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "mosaic-initialize",
                    "result": {"protocolVersion": "2025-11-25"},
                },
            )

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            verifier.verify_granted_initialize(
                client,
                server_url="https://gateway.example/mosaic/mcp/example/mcp",
                token="granted-token",
            )

        payload = requests[0].read().decode()
        self.assertIn('"method":"initialize"', payload)
        self.assertNotIn("tools/call", payload)

    def test_granted_initialize_accepts_sse_response(self) -> None:
        with httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/event-stream"},
                    content=(
                        b": keep-alive\n\n"
                        b'data: {"jsonrpc":"2.0","id":"mosaic-initialize",'
                        b'"result":{"protocolVersion":"2025-11-25"}}\n\n'
                    ),
                )
            )
        ) as client:
            verifier.verify_granted_initialize(
                client,
                server_url="https://gateway.example/mosaic/mcp/example/mcp",
                token="granted-token",
            )

    def test_missing_optional_token_stops_before_network(self) -> None:
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stderr(output):
            result = verifier.main(
                [
                    "--server-url",
                    "https://gateway.example/mosaic/mcp/example/mcp",
                    "--tenant-id",
                    "tenant-id",
                    "--runtime-client-id",
                    "runtime-client",
                    "--check-granted-token",
                ]
            )
        self.assertEqual(result, 1)
        self.assertIn("MOSAIC_SMOKE_MCP_GRANTED_RUNTIME_TOKEN", output.getvalue())


if __name__ == "__main__":
    unittest.main()
