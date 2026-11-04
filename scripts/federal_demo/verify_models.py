"""Opt-in operator-only, bounded verification of five owned APIM model offerings.

Parent orchestration owns delegated consent, temporary direct-user grants and their restoration.
This module makes read-only control-plane requests, at most ten explicit inference requests and
local ledger writes. It never creates keys, grants, roles, consent or fabricated human usage.

Call ``verify(op, targets, runtime_token)`` with five dictionaries containing modelKey,
publicationId, entitlementId and the current MOSAIC connection. The callback receives the
connection's delegated Entra scope and must return the current operator's token, without logging
it. Optional quote_reader and trace_reader callbacks support independently obtained evidence.

A trace_reader receives only correlation metadata (no bearer token or prompt/answer). To replace
missing native pre-backend denial evidence, it must return a matching requestId, publicationId,
entitlementId, publicationAppliedAt, operationUrl, policy="llm-token-limit", section="inbound",
reason="TokenLimitExceededAfterPrompt" and backendInvoked=False. Do not synthesize that evidence:
the parent must obtain it from the actual matching gateway trace.

The local record carries cumulative worst-case reservations across failed/repeated attempts.
A failed/uncertain inference does not reset the USD 0.25 allowance. Paid retries require fresh
explicit review, not deletion of this ledger's reservation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from scripts.federal_demo.estate import ENVIRONMENT_KEYS
from scripts.federal_demo.governance import Mosaic
from scripts.federal_demo.operator import OperationError, Operator
from scripts.federal_demo.pricing import fetch_quote, meter_names
from scripts.verify_model_access import gateway_limit, guid, model_route, token_claims

MAX_SPEND_USD = Decimal("0.25")
MAX_OUTPUT_TOKENS = 256
MAX_RESPONSE_BYTES = 65_536
SHORT_CONTEXT_CEILING = 8_000
MESSAGE_TOKEN_ALLOWANCE = 512
INFERENCE_DEADLINE_SECONDS = 300
PROMPT = "This is a fictional public-data verification. Reply with the word OK only."
_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_INTEGER = re.compile(r"0|[1-9][0-9]{0,9}")
_API_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,31}")


class ModelVerificationFailed(OperationError):
    """Safe failure code and evidence report; never contains upstream text or credentials."""

    def __init__(self, code: str, report: dict):
        super().__init__(f"Model verification FAILED: {code}")
        self.code, self.report = code, report


class _Failure(Exception):
    pass


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise _Failure(code)


def _integer(value: Any, code: str, *, minimum: int = 0) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= minimum, code)
    return value


def _decimal(value: Any) -> Decimal:
    _require(isinstance(value, (str, int, float, Decimal)) and not isinstance(value, bool), "price")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise _Failure("price") from None
    _require(result.is_finite() and result > 0, "price")
    return result


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _identifier(value: Any) -> str:
    _require(isinstance(value, str) and _ID.fullmatch(value) is not None, "identifier")
    return value


def _origin(url: Any) -> str:
    _require(isinstance(url, str), "gateway-route")
    parts = urlsplit(url)
    _require(
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and parts.port is None
        and not parts.query
        and not parts.fragment,
        "gateway-route",
    )
    return f"https://{parts.hostname}"


def _token_upper(prompt: str) -> int:
    # GPT's byte-level tokenization cannot require more input tokens than UTF-8 bytes;
    # reserve an additional fixed allowance for this single plain-text message's framing.
    return len(prompt.encode("utf-8")) + MESSAGE_TOKEN_ALLOWANCE


def _publication_identity(publication: dict) -> dict:
    return {
        key: publication.get(key)
        for key in (
            "id",
            "gatewayId",
            "modelEndpointId",
            "deploymentName",
            "apiPath",
            "lastAppliedAt",
            "accessState",
            "appliedAccess",
        )
    }


def _limits(connection: dict, snapshot: dict, grant: dict, probe_upper: int) -> tuple[int, int]:
    publication = snapshot.get("publicationEnforcement") or {}
    tokens = (grant.get("enforcement") or {}).get("tokens") or {}
    _require(
        publication == connection.get("publicationLimits")
        and (grant.get("enforcement") or {}) == connection.get("grantLimits"),
        "stale-limits",
    )
    publication_limit = _integer(publication.get("tokensPerMinute"), "publication-limit", minimum=1)
    grant_limit = _integer(tokens.get("tokensPerMinute"), "grant-limit", minimum=1)
    _require(
        tokens.get("estimatePromptTokens") is True
        and publication.get("estimatePromptTokens") is True
        and grant_limit < probe_upper + MAX_OUTPUT_TOKENS < publication_limit
        and publication_limit <= SHORT_CONTEXT_CEILING,
        "pre-backend-estimation",
    )
    requests = (grant.get("enforcement") or {}).get("requests") or {}
    _require(
        requests.get("calls") is None
        or _integer(requests["calls"], "request-limit", minimum=1) >= 2,
        "request-limit-before-token-proof",
    )
    for enforcement in (publication, tokens):
        quota = enforcement.get("tokenQuota")
        if quota is not None:
            _require(
                _integer(quota, "token-quota", minimum=1)
                > probe_upper + _token_upper(PROMPT) + 2 * MAX_OUTPUT_TOKENS,
                "token-quota-before-context-proof",
            )
    return grant_limit, publication_limit


def _prepare(
    operator: Operator,
    api: Mosaic,
    target: dict,
    operator_id: str,
    runtime_token: Callable[[str], str],
    quote_reader: Callable[[str, str], dict],
    probe: str,
    api_version: str,
    models_api_version: str,
    token_parameter: str | None,
) -> dict:
    key = target["modelKey"]
    owned = operator.state["objects"][key]
    publication_id = _identifier(target["publicationId"])
    entitlement_id = _identifier(target["entitlementId"])
    _require(owned.get("publicationId") == publication_id, "unowned-publication")
    publication = api.request("GET", f"/publications/{publication_id}")
    _require(
        publication.get("id") == publication_id
        and publication.get("lastAppliedAt")
        and publication.get("accessState") == "applied"
        and publication.get("azureCloud", "commercial") == "commercial"
        and publication.get("deploymentName") == owned.get("deploymentName")
        and publication.get("modelEndpointId") == owned.get("endpointId")
        and publication.get("modelApiId") == owned.get("id"),
        "publication-not-current",
    )
    applied_at = publication["lastAppliedAt"]
    _require(
        isinstance(applied_at, str)
        and datetime.fromisoformat(applied_at.replace("Z", "+00:00")).tzinfo is not None,
        "publication-applied-at",
    )
    connection = api.request("GET", f"/entitlements/{entitlement_id}/connection")
    _require(target["connection"] == connection, "stale-connection")
    _require(
        connection.get("publicationId") == publication_id
        and connection.get("entitlementId") == entitlement_id
        and connection.get("deploymentName") == owned["deploymentName"]
        and connection.get("gatewayId") == publication["gatewayId"]
        and connection.get("tenantId") == operator.target["tenantId"]
        and connection.get("principalKind") == "user"
        and connection.get("tokenMetering") is True
        and not connection.get("poolId")
        and (connection.get("runtime") or {}).get("status") == "applied"
        and (connection.get("runtime") or {}).get("appliedAt") == applied_at
        and (connection.get("costCenter") or {}).get("code") == "general"
        and connection.get("costCenterHeader") == "x-mosaic-cost-center",
        "connection-not-applied-user-grant",
    )
    snapshot = publication.get("appliedAccess") or {}
    _require(
        (snapshot.get("settings") or {}).get("entraEnabled") is True
        and (connection.get("appliedMethods") or {}).get("entraEnabled") is True
        and snapshot.get("audience") == connection.get("entraAudience"),
        "delegated-authentication",
    )
    grants = [
        grant
        for grant in snapshot.get("grants", [])
        if grant.get("entitlementId") == entitlement_id
    ]
    _require(len(grants) == 1, "grant-not-in-applied-snapshot")
    grant = grants[0]
    _require(
        grant.get("enabled") is True
        and not grant.get("revoked")
        and (grant.get("subject") or {}).get("kind") == "user"
        and grant.get("objectId", "").casefold() == operator_id.casefold()
        and grant.get("costCenterCode") == "general"
        and grant.get("costCenterId") == connection["costCenter"]["id"],
        "not-current-operator-general-grant",
    )
    grant_limit, publication_limit = _limits(connection, snapshot, grant, _token_upper(probe))
    scope = connection.get("entraScope")
    audience = connection.get("entraAudience")
    audience_id = audience.removeprefix("api://") if isinstance(audience, str) else ""
    _require(
        guid(audience_id) is not None
        and audience_id == operator.state["objects"]["bootstrap"].get("runtimeClientId")
        and scope == f"api://{audience_id}/Models.Invoke",
        "delegated-scope",
    )
    token = runtime_token(scope)
    _require(isinstance(token, str) and 0 < len(token) < 32_768, "operator-token")
    claims = token_claims(token)
    _require(
        claims.get("oid", "").casefold() == operator_id.casefold()
        and claims.get("tid", "").casefold() == operator.target["tenantId"].casefold()
        and str(claims.get("aud", "")).removeprefix("api://").casefold() == audience_id.casefold()
        and "Models.Invoke" in str(claims.get("scp", "")).split()
        and _integer(claims.get("exp"), "token-expiry", minimum=1) > time.time() + 60,
        "not-current-operator-delegated-token",
    )
    environment = key.split(":")[1]
    binding = [
        row
        for row in operator.target.get("endpoints", [])
        if row.get("environment") == environment
        and row.get("deploymentName") == owned["deploymentName"]
    ]
    _require(len(binding) == 1, "endpoint-binding")
    binding = binding[0]
    endpoint = api.request("GET", f"/model-endpoints/{_identifier(owned['endpointId'])}")
    _require(
        endpoint.get("azureResourceId", "").casefold() == binding["resourceId"].casefold()
        and endpoint.get("environment") == ENVIRONMENT_KEYS[environment],
        "endpoint-binding",
    )
    gateway = api.request("GET", f"/gateways/{_identifier(publication['gatewayId'])}")
    _require(
        gateway.get("azureResourceId", "").casefold()
        == operator.target["gateways"][environment].casefold()
        and gateway.get("environment") == ENVIRONMENT_KEYS[environment],
        "unowned-gateway",
    )
    gateway_url = (gateway.get("capabilities") or {}).get("gatewayUrl")
    origin = _origin(gateway_url)
    _require(gateway_url.rstrip("/") == origin, "gateway-origin")
    expected_path = "mosaic/federal/" + owned["deploymentName"].replace(".", "-")
    _require(
        publication.get("apiPath") == expected_path
        and connection.get("endpoint") == f"{origin}/{expected_path}",
        "incorrect-model-route",
    )
    chat = [
        op
        for op in connection.get("operations", [])
        if op.get("name") == "chat-completions" and op.get("method") == "POST"
    ]
    _require(
        len(chat) == 1
        and chat[0].get("path")
        in (
            f"/openai/deployments/{owned['deploymentName']}/chat/completions",
            "/models/chat/completions",
        ),
        "unsupported-chat-operation",
    )
    route = model_route(
        connection,
        origin,
        api_version=api_version,
        models_api_version=models_api_version,
        token_parameter=token_parameter,
    )
    _require(
        urlsplit(route.url).path == f"/{expected_path}{chat[0]['path']}",
        "operation-url-mismatch",
    )
    resource_id = binding["resourceId"]
    _require(
        resource_id.casefold().startswith(
            f"/subscriptions/{operator.target['subscriptionId']}/resourcegroups/".casefold()
        ),
        "unowned-model-resource",
    )
    deployment = operator.request(
        "GET",
        "https://management.azure.com"
        + resource_id
        + "/deployments/"
        + quote(owned["deploymentName"], safe="")
        + "?api-version=2024-10-01",
        "https://management.azure.com/",
    )
    metadata = deployment["properties"]["model"]
    _require(
        metadata.get("name") == owned["deploymentName"]
        and isinstance(metadata.get("version"), str)
        and bool(metadata["version"])
        and str(metadata.get("format", "")).casefold() == "openai",
        "deployment-metadata",
    )
    native_limits = [
        row.get("count")
        for row in deployment["properties"].get("rateLimits", [])
        if row.get("key") == "token" and row.get("renewalPeriod") == 60
    ]
    _require(
        len(native_limits) == 1
        and _integer(native_limits[0], "native-capacity", minimum=1)
        > _token_upper(probe) + MAX_OUTPUT_TOKENS,
        "unverifiable-upstream-capacity",
    )
    sku = deployment["sku"]["name"]
    retail = quote_reader(metadata["name"], sku)
    source = urlsplit(retail.get("sourceUrl", ""))
    names = meter_names(metadata["name"], sku)
    filters = " or ".join(f"meterName eq '{name}'" for name in names)
    expected_query = {
        "api-version": ["2023-01-01-preview"],
        "$filter": [
            "serviceName eq 'Foundry Models' and productName eq 'Azure OpenAI GPT5' "
            f"and armRegionName eq 'eastus2' and ({filters})"
        ],
    }
    _require(
        retail.get("model") == metadata["name"]
        and retail.get("deploymentType") == sku
        and retail.get("currency") == "USD"
        and retail.get("meterNames") == list(names)
        and source.scheme == "https"
        and source.hostname == "prices.azure.com"
        and source.netloc == "prices.azure.com"
        and source.path == "/api/retail/prices"
        and source.username is None
        and source.password is None
        and not source.fragment
        and parse_qs(source.query) == expected_query
        and retail.get("effectiveFrom")
        and retail["effectiveFrom"] <= datetime.now(UTC).date().isoformat()
        and retail.get("retrievedOn") == datetime.now(UTC).date().isoformat(),
        "exact-retail-quote",
    )
    input_rate = max(
        _decimal(retail.get("inputPerMillion")),
        _decimal(retail.get("cachedInputPerMillion")),
    )
    output_rate = _decimal(retail.get("outputPerMillion"))
    estimate = (
        Decimal(_token_upper(PROMPT) + _token_upper(probe)) * input_rate
        + Decimal(2 * MAX_OUTPUT_TOKENS) * output_rate
    ) / Decimal(1_000_000)
    return {
        "key": key,
        "publication": publication,
        "fingerprint": _fingerprint(_publication_identity(publication)),
        "connection": connection,
        "grant": grant,
        "grantLimit": grant_limit,
        "publicationLimit": publication_limit,
        "route": route,
        "token": token,
        "metadata": metadata,
        "quote": retail,
        "estimate": estimate,
    }


def _call(http: Any, prepared: dict, prompt: str, deadline: float, reasoning_effort: str | None):
    _require(time.monotonic() < deadline, "inference-deadline")
    route = prepared["route"]
    body = dict(route.payload)
    body["messages"] = [{"role": "user", "content": prompt}]
    parameter = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
    body[parameter] = MAX_OUTPUT_TOKENS
    if reasoning_effort is not None:
        body["reasoning_effort"] = reasoning_effort
    try:
        with http.stream(
            "POST",
            route.url,
            headers={
                "Authorization": f"Bearer {prepared['token']}",
                "x-mosaic-cost-center": "general",
                "Accept": "application/json",
            },
            json=body,
            follow_redirects=False,
            timeout=httpx.Timeout(max(0.01, min(30.0, deadline - time.monotonic())), connect=10.0),
        ) as response:
            data = bytearray()
            for chunk in response.iter_bytes(chunk_size=4096):
                data.extend(chunk)
                _require(len(data) <= MAX_RESPONSE_BYTES, "response-size")
                _require(time.monotonic() < deadline, "inference-deadline")
            headers = dict(response.headers)
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            return httpx.Response(response.status_code, headers=headers, content=bytes(data))
    except httpx.HTTPError:
        raise _Failure("model-network-error") from None


def _body(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        raise _Failure("invalid-model-json") from None
    _require(isinstance(body, dict), "invalid-model-json")
    return body


def _remaining(response: httpx.Response, name: str, limit: int) -> int:
    value = response.headers.get(name, "")
    _require(_INTEGER.fullmatch(value) is not None, "missing-native-remaining-header")
    result = int(value)
    _require(0 <= result <= limit, "invalid-native-remaining-header")
    return result


def _accepted(response: httpx.Response, prepared: dict) -> dict:
    _require(response.status_code == 200, "admitted-call-not-200")
    body = _body(response)
    choices = body.get("choices")
    _require(isinstance(choices, list) and bool(choices), "empty-answer")
    text = (choices[0].get("message") or {}).get("content")
    _require(isinstance(text, str) and bool(text.strip()) and len(text) <= 4000, "empty-answer")
    metadata = prepared["metadata"]
    _require(
        body.get("model") in (metadata["name"], f"{metadata['name']}-{metadata['version']}")
        and response.headers.get("x-ms-deployment-name", metadata["name"]) == metadata["name"],
        "wrong-response-model",
    )
    usage = body.get("usage") or {}
    prompt = _integer(usage.get("prompt_tokens"), "native-metering", minimum=1)
    completion = _integer(usage.get("completion_tokens"), "native-metering", minimum=1)
    total = _integer(usage.get("total_tokens"), "native-metering", minimum=2)
    _require(
        total == prompt + completion
        and prompt <= _token_upper(PROMPT)
        and completion <= MAX_OUTPUT_TOKENS,
        "native-metering",
    )
    remaining = _remaining(response, "x-mosaic-remaining-tokens", prepared["grantLimit"])
    _require(
        remaining <= prepared["grantLimit"] - total and remaining > 0, "native-token-accounting"
    )
    tokens = prepared["grant"]["enforcement"]["tokens"]
    if tokens.get("tokenQuota") is not None:
        quota = _remaining(response, "x-mosaic-remaining-quota-tokens", tokens["tokenQuota"])
        _require(quota <= tokens["tokenQuota"] - total, "native-quota-accounting")
    for pool in prepared["publication"]["appliedAccess"].get("pools", []):
        if pool.get("costCenterId") == prepared["grant"].get("costCenterId"):
            quota = pool.get("monthlyTokens")
            if quota is not None:
                _integer(quota, "pool-token-quota", minimum=1)
                remaining_quota = _remaining(
                    response, "x-mosaic-cost-center-remaining-quota-tokens", quota
                )
                _require(remaining_quota <= quota - total, "native-pool-accounting")
    return {
        "acceptedStatus": 200,
        "promptTokens": prompt,
        "completionTokens": completion,
        "totalTokens": total,
        "remainingTokens": remaining,
    }


def _denied(response: httpx.Response, prepared: dict, trace_reader: Callable | None) -> str:
    _require(response.status_code == 429, "oversize-call-not-denied")
    try:
        body = _body(response)
    except _Failure:
        body = {}
    retry = response.headers.get("retry-after", "")
    native = (
        gateway_limit(response) == "tokens"
        and isinstance(body.get("message"), str)
        and body["message"].startswith("Token limit will exceed")
        and body.get("statusCode") in (429, "429")
        and _INTEGER.fullmatch(retry) is not None
        and 0 < int(retry) <= 60
        and not any(field in body for field in ("error", "usage", "choices"))
        and not any(
            name in response.headers
            for name in ("x-ms-deployment-name", "x-ms-region", "x-ratelimit-remaining-tokens")
        )
    )
    if native:
        return "native-after-prompt-policy-denial"
    request_id = response.headers.get("apim-request-id")
    _require(trace_reader is not None and guid(request_id) is not None, "unproven-gateway-denial")
    publication = prepared["publication"]
    expected = {
        "requestId": request_id,
        "publicationId": publication["id"],
        "entitlementId": prepared["connection"]["entitlementId"],
        "publicationAppliedAt": publication["lastAppliedAt"],
        "operationUrl": prepared["route"].url,
    }
    trace = trace_reader(dict(expected))
    _require(
        isinstance(trace, dict)
        and all(trace.get(key) == value for key, value in expected.items())
        and trace.get("policy") == "llm-token-limit"
        and trace.get("section") == "inbound"
        and trace.get("reason") == "TokenLimitExceededAfterPrompt"
        and trace.get("backendInvoked") is False,
        "unproven-gateway-denial",
    )
    return "matching-gateway-trace"


def verify(
    operator: Operator,
    targets: list[dict],
    runtime_token: Callable[[str], str],
    *,
    http: httpx.Client | None = None,
    quote_reader: Callable[[str, str], dict] = fetch_quote,
    trace_reader: Callable[[dict], dict | None] | None = None,
    api_version: str = "2024-10-21",
    models_api_version: str = "2024-05-01-preview",
    token_parameter: str | None = None,
    reasoning_effort: str | None = "low",
    probe_words: int = 3000,
    spend_limit_usd: Decimal = MAX_SPEND_USD,
) -> dict:
    """Verify all five or raise ModelVerificationFailed, persisting no partial success.

    ``http`` is an injectable synchronous HTTPX-compatible client (FakeHTTP in offline tests).
    The default client disables redirects and retries. The synthetic English probe repeats the
    article "a"; its UTF-8 byte upper bound, not a guessed tokenizer count, reserves spend.
    Pre-backend denial is proven by native after-prompt policy evidence or a matching trace.
    """
    previous = operator.state["objects"].get("modelVerification", {})
    report: dict = {
        "status": "RUNNING",
        "models": {},
        "gatewayRequests": 0,
        "cumulativeReservedUsd": previous.get("cumulativeReservedUsd", "0"),
    }
    operator.remember("modelVerification", report.copy())
    prepared = []
    client = http
    active_key = None
    phase = "preflight"
    last_status = None
    try:
        operator.verify_target()
        _require(
            _API_VERSION.fullmatch(api_version) is not None
            and _API_VERSION.fullmatch(models_api_version) is not None
            and token_parameter in (None, "max_tokens", "max_completion_tokens")
            and reasoning_effort in (None, "low", "none")
            and isinstance(probe_words, int)
            and not isinstance(probe_words, bool)
            and 1000 <= probe_words <= 3000,
            "request-configuration",
        )
        spend_limit = _decimal(spend_limit_usd)
        _require(spend_limit <= MAX_SPEND_USD, "spend-ceiling")
        previous_reservation = Decimal(str(report["cumulativeReservedUsd"]))
        _require(
            previous_reservation.is_finite()
            and previous_reservation >= 0
            and not (
                previous.get("gatewayRequests", 0) > 0 and "cumulativeReservedUsd" not in previous
            ),
            "unknown-previous-model-spend",
        )
        objects = operator.state["objects"]
        owned = {key for key in objects if key.startswith("model:")}
        _require(
            isinstance(targets, list)
            and len(targets) == 5
            and len(owned) == 5
            and all(
                isinstance(row, dict)
                and set(row) == {"modelKey", "publicationId", "connection", "entitlementId"}
                for row in targets
            )
            and {row["modelKey"] for row in targets} == owned
            and len({row["publicationId"] for row in targets}) == 5
            and len({row["entitlementId"] for row in targets}) == 5,
            "exact-five-owned-models",
        )
        me = operator.graph("GET", "/me?$select=id")
        operator_id = me.get("id") if isinstance(me, dict) else None
        _require(
            isinstance(operator_id, str)
            and operator_id
            == objects.get("operatorConsent", objects.get("bootstrap", {})).get("operatorId"),
            "current-operator-identity",
        )
        api = Mosaic(operator)
        probe = "This is a fictional test of repeated English articles. " + "a " * probe_words
        probe += "Reply with OK only."
        for target in targets:
            active_key = target["modelKey"]
            prepared.append(
                _prepare(
                    operator,
                    api,
                    target,
                    operator_id,
                    runtime_token,
                    quote_reader,
                    probe,
                    api_version,
                    models_api_version,
                    token_parameter,
                )
            )
        _require(
            all(probe_words > item["grantLimit"] for item in prepared),
            "probe-not-above-grant-limit",
        )
        estimate = sum((item["estimate"] for item in prepared), Decimal(0))
        report["estimatedMaximumUsd"] = str(estimate)
        report["spendLimitUsd"] = str(spend_limit)
        _require(estimate + previous_reservation <= spend_limit, "aggregate-spend-reservation")
        report["cumulativeReservedUsd"] = str(estimate + previous_reservation)
        operator.remember("modelVerification", report.copy())
        client = client or httpx.Client(
            transport=httpx.HTTPTransport(retries=0),
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
            trust_env=False,
        )
        deadline = time.monotonic() + INFERENCE_DEADLINE_SECONDS
        results = {}
        for item in prepared:
            active_key = item["key"]
            phase, last_status = "admitted", None
            report["gatewayRequests"] += 1
            accepted = _call(client, item, PROMPT, deadline, reasoning_effort)
            last_status = accepted.status_code
            evidence = _accepted(accepted, item)
            phase, last_status = "oversize", None
            report["gatewayRequests"] += 1
            denied = _call(client, item, probe, deadline, reasoning_effort)
            last_status = denied.status_code
            denial = _denied(denied, item, trace_reader)
            results[active_key] = {
                **evidence,
                "publicationAppliedAt": item["publication"]["lastAppliedAt"],
                "publicationSnapshotSha256": item["fingerprint"],
                "gatewayContextLimitProven": True,
                "deniedStatus": denied.status_code,
                "denialEvidence": denial,
                "grantTokensPerMinute": item["grantLimit"],
                "publicationTokensPerMinute": item["publicationLimit"],
                "modelVersion": item["metadata"]["version"],
                "deploymentType": item["quote"]["deploymentType"],
                "priceSourceUrl": item["quote"]["sourceUrl"],
                "priceInputPerMillion": item["quote"]["inputPerMillion"],
                "priceOutputPerMillion": item["quote"]["outputPerMillion"],
                "priceCachedInputPerMillion": item["quote"]["cachedInputPerMillion"],
                "priceEffectiveFrom": item["quote"]["effectiveFrom"],
                "reservedMaximumUsd": str(item["estimate"]),
                "gatewayRequests": 2,
            }
        for item in prepared:
            active_key = item["key"]
            phase, last_status = "publication-freshness", None
            current = api.request("GET", f"/publications/{item['publication']['id']}")
            _require(
                _fingerprint(_publication_identity(current)) == item["fingerprint"],
                "publication-changed-during-verification",
            )
        report.update(
            status="PASSED",
            models=results,
            caller="currentOperator",
            operatorObjectId=operator_id,
            verifiedAt=datetime.now(UTC).isoformat(),
        )
    except Exception as error:
        code = str(error) if isinstance(error, _Failure) else "unverifiable-evidence-or-request"
        failure = {"modelKey": active_key, "code": code, "phase": phase}
        if last_status is not None:
            failure["httpStatus"] = last_status
        report.update(status="FAILED", models={}, failures=[failure])
        operator.remember("modelVerification", report)
        raise ModelVerificationFailed(code, report) from None
    finally:
        for item in prepared:
            item.pop("token", None)
        if http is None and client is not None:
            try:
                client.close()
            except Exception:
                report.update(
                    status="FAILED",
                    models={},
                    failures=[{"modelKey": active_key, "code": "http-cleanup"}],
                )
                operator.remember("modelVerification", report)
                raise ModelVerificationFailed("http-cleanup", report) from None
    operator.remember("modelVerification", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--connections", type=Path, required=True)
    parser.add_argument("--invoke-paid", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not args.invoke_paid:
            raise OperationError("Explicit --invoke-paid is required")
        raw = args.connections.read_bytes()
        if len(raw) > 262_144:
            raise OperationError("Connection manifest exceeds the bounded size")
        operator = Operator(json.loads(args.target.read_text()), args.state)
        result = verify(
            operator,
            json.loads(raw),
            lambda scope: operator.token(scope.rsplit("/", 1)[0]),
        )
        print(json.dumps({"status": result["status"], "models": len(result["models"])}))
        return 0
    except ModelVerificationFailed as error:
        print(json.dumps({"status": "FAILED", "models": 0, "error": error.code}))
        return 1
    except Exception:
        print(
            json.dumps({"status": "FAILED", "models": 0, "error": "configuration-or-control-plane"})
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
