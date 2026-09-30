"""Opt-in, read-only verification against a real MOSAIC-published MCP server.

Tokens are supplied in environment variables, held in memory, and never printed. The script does
not provision resources, call tools, change grants, or save credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx


class VerificationFailed(RuntimeError):
    pass


RESOURCE_METADATA_RE = re.compile(r'resource_metadata="([^"]+)"')


def https_url(value: str, *, label: str, allow_query: bool = False) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise VerificationFailed(f"{label} must use HTTPS without embedded credentials")
    if parts.fragment:
        raise VerificationFailed(f"{label} must not contain fragments")
    if parts.query and not allow_query:
        raise VerificationFailed(f"{label} must not contain query parameters")
    return value.rstrip("/")


def credential(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise VerificationFailed(f"Set {name} before running live verification")
    return value


def bearer(token: str) -> str:
    return "Bearer " + token


def expect(response: httpx.Response, codes: set[int], label: str) -> None:
    if response.status_code not in codes:
        raise VerificationFailed(f"{label}: unexpected HTTP {response.status_code}")


def object_body(response: httpx.Response, label: str) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError:
        raise VerificationFailed(f"{label}: response was not JSON") from None
    if not isinstance(value, dict):
        raise VerificationFailed(f"{label}: response was not an object")
    return value


def first_header(response: httpx.Response, name: str) -> str:
    values = response.headers.get_list(name)
    if len(values) != 1 or not values[0].strip():
        raise VerificationFailed(f"Expected exactly one {name} header")
    return values[0]


def resource_metadata_url(header_value: str) -> str:
    match = RESOURCE_METADATA_RE.search(header_value)
    if match is None:
        raise VerificationFailed("WWW-Authenticate did not contain resource_metadata")
    return https_url(match.group(1), label="resource metadata URL", allow_query=True)


def string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise VerificationFailed(f"{label} must be a string array")
    return value


def authorization_server_matches_tenant(values: Iterable[str], tenant_id: str) -> bool:
    expected = f"https://login.microsoftonline.com/{tenant_id}/v2.0".casefold()
    return any(value.rstrip("/").casefold() == expected.rstrip("/") for value in values)


def read_mcp_response(response: httpx.Response, label: str) -> dict[str, Any]:
    content_type = response.headers.get("Content-Type", "")
    if "text/event-stream" in content_type:
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if not data or data == "[DONE]":
                continue
            try:
                parsed = json.loads(data)
            except ValueError:
                continue
            if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
                return parsed
        raise VerificationFailed(f"{label}: no JSON-RPC message was found in the event stream")
    return object_body(response, label)


def verify_metadata(
    client: httpx.Client,
    *,
    server_url: str,
    tenant_id: str,
    runtime_client_id: str,
) -> str:
    response = client.post(
        server_url,
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": "mosaic-unauthenticated", "method": "initialize"},
    )
    expect(response, {401}, "Unauthenticated MCP initialize")
    metadata_url = resource_metadata_url(first_header(response, "WWW-Authenticate"))

    metadata_response = client.get(metadata_url)
    expect(metadata_response, {200}, "Protected resource metadata")
    metadata = object_body(metadata_response, "Protected resource metadata")
    if metadata.get("resource") != server_url:
        raise VerificationFailed("Protected resource metadata did not name the MCP server URL")
    authorization_servers = string_list(
        metadata.get("authorization_servers"), "authorization_servers"
    )
    if not authorization_server_matches_tenant(authorization_servers, tenant_id):
        raise VerificationFailed("Protected resource metadata did not name the expected tenant")
    scopes = string_list(metadata.get("scopes_supported"), "scopes_supported")
    expected_scope = f"api://{runtime_client_id}/Mcp.Invoke"
    if expected_scope not in scopes:
        raise VerificationFailed("Protected resource metadata did not advertise Mcp.Invoke")
    print("PASS: unauthenticated requests advertise protected resource metadata")
    return metadata_url


def verify_denied_token(client: httpx.Client, *, server_url: str, token: str) -> None:
    response = client.post(
        server_url,
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": bearer(token),
        },
        json={"jsonrpc": "2.0", "id": "mosaic-denied", "method": "initialize"},
    )
    expect(response, {403}, "Denied MCP initialize")
    challenge = response.headers.get("WWW-Authenticate", "")
    if "insufficient_scope" not in challenge:
        raise VerificationFailed("Denied token did not receive insufficient_scope")
    print("PASS: token without an applied grant was denied")


def verify_granted_initialize(client: httpx.Client, *, server_url: str, token: str) -> None:
    response = client.post(
        server_url,
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": bearer(token),
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": "mosaic-initialize",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "mosaic-live-verifier", "version": "1.0"},
            },
        },
    )
    expect(response, {200}, "Granted MCP initialize")
    body = read_mcp_response(response, "Granted MCP initialize")
    if body.get("id") != "mosaic-initialize":
        raise VerificationFailed("MCP initialize response did not match the request id")
    if "error" in body:
        raise VerificationFailed("MCP initialize returned a JSON-RPC error")
    result = body.get("result")
    if not isinstance(result, dict):
        raise VerificationFailed("MCP initialize response did not contain a result object")
    print("PASS: granted token completed MCP initialize without calling a tool")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server-url", required=True, help="Published MCP server URL ending in /mcp"
    )
    parser.add_argument("--tenant-id", required=True, help="Expected Microsoft Entra tenant ID")
    parser.add_argument("--runtime-client-id", required=True, help="Model-runtime client ID")
    parser.add_argument(
        "--denied-token-env",
        default="MOSAIC_SMOKE_MCP_DENIED_RUNTIME_TOKEN",
        help="Environment variable containing a runtime token without an applied grant",
    )
    parser.add_argument(
        "--granted-token-env",
        default="MOSAIC_SMOKE_MCP_GRANTED_RUNTIME_TOKEN",
        help="Environment variable containing a runtime token with an applied grant",
    )
    parser.add_argument("--check-denied-token", action="store_true")
    parser.add_argument("--check-granted-token", action="store_true")
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        server_url = https_url(args.server_url, label="MCP server URL")
        if not server_url.endswith("/mcp"):
            raise VerificationFailed("MCP server URL must end in /mcp")
        if not args.tenant_id.strip():
            raise VerificationFailed("Tenant ID is required")
        if not args.runtime_client_id.strip():
            raise VerificationFailed("Runtime client ID is required")
        denied_token = credential(args.denied_token_env) if args.check_denied_token else None
        granted_token = credential(args.granted_token_env) if args.check_granted_token else None
        with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
            verify_metadata(
                client,
                server_url=server_url,
                tenant_id=args.tenant_id.strip(),
                runtime_client_id=args.runtime_client_id.strip(),
            )
            if denied_token is not None:
                verify_denied_token(client, server_url=server_url, token=denied_token)
            if granted_token is not None:
                verify_granted_initialize(client, server_url=server_url, token=granted_token)
    except (VerificationFailed, httpx.HTTPError) as error:
        message = str(error) if isinstance(error, VerificationFailed) else "HTTP transport failure"
        print(f"FAIL: {message}", file=sys.stderr)
        return 1
    print("Live MCP access checks passed for the selected scenarios.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
