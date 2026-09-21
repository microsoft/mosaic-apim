"""Opt-in, read-only control-plane verification against a real APIM model endpoint.

Credentials are supplied in environment variables, held in memory, and never printed. The
script does not provision resources, rotate keys, or change grants/authentication settings.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from urllib.parse import urlsplit

import httpx


class VerificationFailed(RuntimeError):
    pass


def https_origin(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise VerificationFailed("Endpoints must use HTTPS without embedded credentials")
    if parts.fragment:
        raise VerificationFailed("Endpoints must not contain fragments")
    return f"https://{parts.netloc.casefold()}"


def credential(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise VerificationFailed(f"Set {name} before running live verification")
    return value


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


def model_url(connection: dict[str, Any], expected_origin: str, api_version: str) -> str:
    endpoint = connection.get("endpoint")
    if not isinstance(endpoint, str) or https_origin(endpoint) != expected_origin:
        raise VerificationFailed("Connection metadata does not match the approved gateway origin")
    if urlsplit(endpoint).query:
        raise VerificationFailed("Model base endpoints must not contain query parameters")
    operations = connection.get("operations", [])
    for operation in operations:
        path = operation.get("path") if isinstance(operation, dict) else None
        if (
            isinstance(path, str)
            and path.endswith("/chat/completions")
            and operation.get("method") == "POST"
        ):
            url = f"{endpoint.rstrip('/')}/{path.lstrip('/')}"
            if https_origin(url) != expected_origin:
                raise VerificationFailed("The model operation changed the approved gateway origin")
            return str(httpx.URL(url).copy_add_param("api-version", api_version))
    raise VerificationFailed("The publication does not expose a chat-completions operation")


def reveal(
    client: httpx.Client, base: str, token: str, route: str, slot: str
) -> str:
    response = client.post(
        f"{base}/{route}/keys/reveal",
        headers={"Authorization": f"Bearer {token}"},
        json={"slot": slot},
    )
    expect(response, {200}, f"{slot} key retrieval")
    if "no-store" not in response.headers.get("Cache-Control", ""):
        raise VerificationFailed("Key responses must not be cacheable")
    value = object_body(response, "Key retrieval").get("key")
    if not isinstance(value, str) or not value:
        raise VerificationFailed("Key retrieval did not return a usable key")
    return value


def verify_grant(
    client: httpx.Client,
    *,
    base: str,
    route: str,
    control_token: str,
    runtime_token: str,
    expected_origin: str,
    api_version: str,
    label: str,
    prove_budget: bool,
) -> None:
    response = client.get(
        f"{base}/{route}/connection", headers={"Authorization": f"Bearer {control_token}"}
    )
    expect(response, {200}, f"{label} connection details")
    connection = object_body(response, "Connection details")
    methods = connection.get("appliedMethods") or {}
    if not methods.get("keysEnabled") or not methods.get("entraEnabled"):
        raise VerificationFailed(f"{label}: both methods must be applied for this verification")
    runtime = connection.get("runtime") or {}
    if runtime.get("status") != "applied":
        raise VerificationFailed(f"{label}: access must be applied with no pending changes")
    if prove_budget:
        requests = (connection.get("grantLimits") or {}).get("requests") or {}
        if requests.get("calls") != 2 or requests.get("renewalPeriodSeconds") != 300:
            raise VerificationFailed(
                "Shared-budget verification requires isolated grants limited to 2 calls/300 seconds"
            )

    url = model_url(connection, expected_origin, api_version)
    primary = reveal(client, base, control_token, route, "primary")
    secondary = reveal(client, base, control_token, route, "secondary") if prove_budget else None
    deployment = connection.get("deploymentName")
    if not isinstance(deployment, str) or not deployment:
        raise VerificationFailed("Connection metadata has no model deployment name")
    payload = {
        "model": deployment,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "max_completion_tokens": 8,
        "stream": False,
    }
    custom_payload = os.environ.get("MOSAIC_SMOKE_PAYLOAD")
    if custom_payload:
        try:
            parsed_payload = json.loads(custom_payload)
        except ValueError:
            raise VerificationFailed("MOSAIC_SMOKE_PAYLOAD must be valid JSON") from None
        if not isinstance(parsed_payload, dict):
            raise VerificationFailed("MOSAIC_SMOKE_PAYLOAD must be a JSON object")
        payload = parsed_payload

    # Check denied requests first, before the two authorized calls consume a test budget.
    for name, headers in (
        ("anonymous", {}),
        ("invalid key", {"Ocp-Apim-Subscription-Key": "invalid-fixture-key"}),
        ("control-plane audience", {"Authorization": f"Bearer {control_token}"}),
        (
            "invalid bearer with valid key",
            {"Ocp-Apim-Subscription-Key": primary, "Authorization": "Bearer invalid-fixture-token"},
        ),
    ):
        denied = client.post(url, headers=headers, json=payload)
        expect(denied, {401, 403}, f"{label} {name} rejection")

    started = time.monotonic()
    for method, headers in (
        ("subscription key", {"Ocp-Apim-Subscription-Key": primary}),
        ("Entra token", {"Authorization": f"Bearer {runtime_token}"}),
    ):
        called = client.post(url, headers=headers, json=payload)
        expect(called, {200}, f"{label} {method} model call")
        choices = object_body(called, "Model response").get("choices")
        if not isinstance(choices, list) or not choices:
            raise VerificationFailed(f"{label}: APIM did not return a chat-completions response")
        print(f"PASS: {label} {method} reached the model")
    if prove_budget:
        if time.monotonic() - started >= 150:
            raise VerificationFailed("Requests crossed the budget observation interval; retry")
        exhausted = client.post(
            url, headers={"Ocp-Apim-Subscription-Key": secondary or ""}, json=payload
        )
        expect(exhausted, {429}, f"{label} shared request budget")
        print(f"PASS: {label} primary key, Entra, and secondary key share the request budget")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base-url", required=True, help="MOSAIC API origin, without /api/v1")
    parser.add_argument(
        "--gateway-origin", required=True, help="Explicitly approved APIM HTTPS origin"
    )
    parser.add_argument("--user-entitlement", required=True)
    parser.add_argument("--application-entitlement", required=True)
    parser.add_argument(
        "--api-version", required=True, help="A version supported by the deployment"
    )
    parser.add_argument("--send-model-requests", action="store_true", required=True)
    parser.add_argument("--prove-shared-budget", action="store_true")
    args = parser.parse_args(argv)
    try:
        https_origin(args.api_base_url)
        if urlsplit(args.api_base_url).query:
            raise VerificationFailed("The control-plane base URL must not contain a query string")
        origin = https_origin(args.gateway_origin)
        if urlsplit(args.gateway_origin).path not in {"", "/"}:
            raise VerificationFailed("Supply the gateway origin without an API path")
        owner_token = credential("MOSAIC_SMOKE_USER_CONTROL_TOKEN")
        admin_token = credential("MOSAIC_SMOKE_ADMIN_CONTROL_TOKEN")
        user_runtime = credential("MOSAIC_SMOKE_USER_RUNTIME_TOKEN")
        app_runtime = credential("MOSAIC_SMOKE_APPLICATION_RUNTIME_TOKEN")
        for identifier in (args.user_entitlement, args.application_entitlement):
            if not identifier or any(char in identifier for char in "/\\?#%"):
                raise VerificationFailed("Supply an entitlement identifier, not a URL")
        base = f"{args.api_base_url.rstrip('/')}/api/v1"
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            foreign = client.post(
                f"{base}/me/entitlements/{args.application_entitlement}/keys/reveal",
                headers={"Authorization": f"Bearer {owner_token}"},
                json={"slot": "primary"},
            )
            expect(foreign, {403, 404}, "Unowned application-key disclosure")
            print("PASS: an end user cannot retrieve the application's key")
            verify_grant(
                client, base=base, route=f"me/entitlements/{args.user_entitlement}",
                control_token=owner_token, runtime_token=user_runtime,
                expected_origin=origin, api_version=args.api_version, label="User",
                prove_budget=args.prove_shared_budget,
            )
            verify_grant(
                client, base=base, route=f"entitlements/{args.application_entitlement}",
                control_token=admin_token, runtime_token=app_runtime,
                expected_origin=origin, api_version=args.api_version, label="Application",
                prove_budget=args.prove_shared_budget,
            )
    except (VerificationFailed, httpx.HTTPError) as error:
        # HTTP exceptions can carry request headers or URLs; never print their representation.
        message = str(error) if isinstance(error, VerificationFailed) else "HTTP transport failure"
        print(f"FAIL: {message}", file=sys.stderr)
        return 1
    print(
        "Live key/token checks passed. "
        "Method toggles, rotation, and revocation need separate checks."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
