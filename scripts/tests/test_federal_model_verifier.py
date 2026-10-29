import base64
import copy
import gzip
import io
import json
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import urlencode

import httpx

from scripts.federal_demo import verify_models as verifier
from scripts.federal_demo.pricing import meter_names, select_quote

TENANT = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION = "22222222-2222-2222-2222-222222222222"
OPERATOR_ID = "33333333-3333-3333-3333-333333333333"
RUNTIME_ID = "44444444-4444-4444-4444-444444444444"
STAMP = "2026-10-29T02:00:00Z"
PROFILES = (
    ("dev", "gpt-5.6-luna", "GlobalStandard"),
    ("stg", "gpt-5.6-luna", "GlobalStandard"),
    ("prod", "gpt-5.6-luna", "DataZoneStandard"),
    ("prod", "gpt-5.6-terra", "GlobalStandard"),
    ("prod", "gpt-5.1", "DataZoneStandard"),
)


def runtime_token(**changes):
    claims = {
        "oid": OPERATOR_ID,
        "tid": TENANT,
        "aud": RUNTIME_ID,
        "scp": "Models.Invoke",
        "exp": int(time.time()) + 3600,
    } | changes
    encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{encoded}.private-signature"


class FakeHTTP:
    def __init__(self, world):
        self.world = world
        self.calls = []
        self.responses = {}
        self.network_failure = False

    @contextmanager
    def stream(self, method, url, *, headers, json, follow_redirects, timeout):
        call = {
            "method": method,
            "url": url,
            "headers": dict(headers),
            "body": copy.deepcopy(json),
            "follow_redirects": follow_redirects,
            "timeout": timeout,
        }
        self.calls.append(call)
        if self.network_failure:
            raise httpx.ConnectError(f"private request {headers['Authorization']}")
        index = len(self.calls) - 1
        response = self.responses.get(index)
        if response is None:
            if index % 2:
                response = httpx.Response(
                    429,
                    headers={"Retry-After": "60"},
                    json={"statusCode": 429, "message": "Token limit will exceed the limit."},
                )
            else:
                model = PROFILES[index // 2][1]
                response = httpx.Response(
                    200,
                    headers={
                        "x-mosaic-remaining-tokens": "947",
                        "x-mosaic-remaining-quota-tokens": "24947",
                    },
                    json={
                        "model": model,
                        "choices": [{"message": {"content": "private answer: OK"}}],
                        "usage": {"prompt_tokens": 45, "completion_tokens": 8, "total_tokens": 53},
                    },
                )
        try:
            yield response
        finally:
            response.close()


class FakeOperator:
    def __init__(self):
        self.state = {
            "objects": {
                "bootstrap": {"runtimeClientId": RUNTIME_ID, "operatorId": OPERATOR_ID},
                "operatorConsent": {"operatorId": OPERATOR_ID},
                "modelVerification": {"models": {"stale": {"gatewayContextLimitProven": True}}},
            }
        }
        self.target = {
            "tenantId": TENANT,
            "subscriptionId": SUBSCRIPTION,
            "mosaicApiUrl": "https://owned-api.azurewebsites.net",
            "apiAppClientId": "55555555-5555-5555-5555-555555555555",
            "gateways": {
                env: f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-fed-{env}/providers/"
                f"Microsoft.ApiManagement/service/apim-fed-{env}"
                for env in ("dev", "stg", "prod")
            },
            "endpoints": [],
        }
        self.remembered = []
        self.requests = []
        self.deployments = {}

    def verify_target(self):
        pass

    def graph(self, method, path):
        assert (method, path) == ("GET", "/me?$select=id")
        return {"id": OPERATOR_ID}

    def request(self, method, url, resource):
        assert method == "GET"
        assert resource == "https://management.azure.com/"
        self.requests.append(url)
        return copy.deepcopy(next(value for key, value in self.deployments.items() if key in url))

    def remember(self, key, value):
        assert key == "modelVerification"
        self.remembered.append(copy.deepcopy(value))
        self.state["objects"][key] = copy.deepcopy(value)


class FakeAPI:
    def __init__(self):
        self.records = {}
        self.calls = []
        self.publication_reads = {}
        self.stale_after_requests = False

    def request(self, method, path):
        assert method == "GET"
        self.calls.append((method, path))
        value = copy.deepcopy(self.records[path])
        if path.startswith("/publications/"):
            self.publication_reads[path] = self.publication_reads.get(path, 0) + 1
            if self.stale_after_requests and self.publication_reads[path] > 1:
                value["lastAppliedAt"] = "2026-10-29T02:01:00Z"
        return value


class World:
    def __init__(self):
        self.op, self.api = FakeOperator(), FakeAPI()
        self.targets = []
        self.quotes = []
        self.quote_rate = Decimal("0.2")
        self.token = runtime_token()
        self.token_scopes = []
        for index, (env, name, sku) in enumerate(PROFILES):
            key = f"model:{env}:{name}"
            pub_id, ent_id = f"publication-{index}", f"entitlement-{index}"
            enforcement = {
                "tokens": {
                    "counterKeyExpression": "@(context.Subscription.Id)",
                    "tokensPerMinute": 1000,
                    "tokenQuota": 25000,
                    "tokenQuotaPeriod": "Monthly",
                    "estimatePromptTokens": True,
                },
                "requests": {"calls": 10, "renewalPeriodSeconds": 60},
            }
            publication_limit = {
                "counterKeyExpression": "@(context.Subscription.Id)",
                "tokensPerMinute": 8000,
                "estimatePromptTokens": True,
            }
            grant = {
                "entitlementId": ent_id,
                "subject": {"kind": "user", "id": "operator-principal"},
                "objectId": OPERATOR_ID,
                "enabled": True,
                "enforcement": enforcement,
                "costCenterId": "general-center",
                "costCenterCode": "general",
            }
            publication = {
                "id": pub_id,
                "gatewayId": f"gateway-{env}",
                "modelEndpointId": f"endpoint-{index}",
                "modelApiId": f"model-{index}",
                "deploymentName": name,
                "apiPath": "mosaic/federal/" + name.replace(".", "-"),
                "lastAppliedAt": STAMP,
                "accessState": "applied",
                "appliedAccess": {
                    "version": 1,
                    "audience": RUNTIME_ID,
                    "settings": {"entraEnabled": True, "keysEnabled": False},
                    "publicationEnforcement": publication_limit,
                    "grants": [grant],
                },
            }
            connection = {
                "entitlementId": ent_id,
                "publicationId": pub_id,
                "gatewayId": publication["gatewayId"],
                "endpoint": f"https://gateway-{env}.azure-api.net/{publication['apiPath']}",
                "deploymentName": name,
                "tenantId": TENANT,
                "runtime": {"status": "applied", "appliedAt": STAMP},
                "appliedMethods": {"entraEnabled": True, "keysEnabled": False},
                "entraAudience": RUNTIME_ID,
                "entraScope": f"api://{RUNTIME_ID}/Models.Invoke",
                "operations": [
                    {
                        "name": "chat-completions",
                        "method": "POST",
                        "path": f"/openai/deployments/{name}/chat/completions",
                    }
                ],
                "publicationLimits": publication_limit,
                "grantLimits": enforcement,
                "principalKind": "user",
                "costCenter": {"id": "general-center", "code": "general"},
                "costCenterHeader": "x-mosaic-cost-center",
                "tokenMetering": True,
            }
            self.op.state["objects"][key] = {
                "id": f"model-{index}",
                "publicationId": pub_id,
                "endpointId": publication["modelEndpointId"],
                "deploymentName": name,
            }
            resource = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-fed-{env}/providers/"
            resource += f"Microsoft.CognitiveServices/accounts/ai-{index}"
            self.op.target["endpoints"].append(
                {"environment": env, "deploymentName": name, "resourceId": resource}
            )
            self.op.deployments[f"ai-{index}/deployments/"] = {
                "properties": {
                    "model": {"name": name, "version": "2026-07-09", "format": "OpenAI"},
                    "rateLimits": [{"key": "token", "renewalPeriod": 60, "count": 10000}],
                },
                "sku": {"name": sku, "capacity": 10},
            }
            self.api.records[f"/publications/{pub_id}"] = publication
            self.api.records[f"/entitlements/{ent_id}/connection"] = copy.deepcopy(connection)
            self.api.records[f"/model-endpoints/endpoint-{index}"] = {
                "azureResourceId": resource,
                "environment": {"dev": "development", "stg": "staging", "prod": "production"}[env],
            }
            self.api.records[f"/gateways/gateway-{env}"] = {
                "azureResourceId": self.op.target["gateways"][env],
                "environment": {"dev": "development", "stg": "staging", "prod": "production"}[env],
                "capabilities": {"gatewayUrl": f"https://gateway-{env}.azure-api.net"},
            }
            self.targets.append(
                {
                    "modelKey": key,
                    "publicationId": pub_id,
                    "entitlementId": ent_id,
                    "connection": copy.deepcopy(connection),
                }
            )
        self.http = FakeHTTP(self)

    def quote(self, model, sku):
        names = meter_names(model, sku)
        rows = [
            {
                "meterName": name,
                "currencyCode": "USD",
                "unitOfMeasure": "1M",
                "type": "Consumption",
                "isPrimaryMeterRegion": True,
                "productName": "Azure OpenAI GPT5",
                "retailPrice": float(self.quote_rate * (index + 1)),
                "effectiveStartDate": "2025-01-01T00:00:00Z",
            }
            for index, name in enumerate(names)
        ]
        source = "https://prices.azure.com/api/retail/prices?" + urlencode(
            {
                "api-version": "2023-01-01-preview",
                "$filter": "serviceName eq 'Foundry Models' and productName eq 'Azure OpenAI GPT5' "
                "and armRegionName eq 'eastus2' and ("
                + " or ".join(f"meterName eq '{name}'" for name in names)
                + ")",
            }
        )
        quote = select_quote(model, sku, rows, source)
        self.quotes.append(quote)
        return quote

    def get_token(self, scope):
        self.token_scopes.append(scope)
        return self.token

    def run(self, **kwargs):
        with patch.object(verifier, "Mosaic", return_value=self.api):
            return verifier.verify(
                self.op,
                self.targets,
                self.get_token,
                http=self.http,
                quote_reader=self.quote,
                **kwargs,
            )

    def failed(self, code=None, **kwargs):
        with unittest.TestCase().assertRaises(verifier.ModelVerificationFailed) as raised:
            self.run(**kwargs)
        if code:
            assert raised.exception.code == code, raised.exception.code
        proof = self.op.state["objects"]["modelVerification"]
        assert proof["status"] == "FAILED"
        assert proof["models"] == {}
        assert self.token not in json.dumps(proof)
        assert "private answer" not in json.dumps(proof)
        return raised.exception

    def update_connection(self, index, function):
        function(self.targets[index]["connection"])
        self.api.records[f"/entitlements/entitlement-{index}/connection"] = copy.deepcopy(
            self.targets[index]["connection"]
        )


class FederalModelVerifierTests(unittest.TestCase):
    def test_exact_five_current_operator_proofs_are_saved_only_after_all_pass(self):
        world = World()
        output = io.StringIO()
        with redirect_stdout(output):
            proof = world.run()
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(proof["status"], "PASSED")
        self.assertEqual(set(proof["models"]), {target["modelKey"] for target in world.targets})
        self.assertEqual(proof["operatorObjectId"], OPERATOR_ID)
        self.assertEqual(proof["gatewayRequests"], 10)
        self.assertLessEqual(Decimal(proof["estimatedMaximumUsd"]), Decimal("0.25"))
        self.assertEqual(world.op.remembered[0]["status"], "RUNNING")
        self.assertEqual(world.op.remembered[0]["models"], {})
        self.assertEqual(world.op.remembered[-1]["status"], "PASSED")
        self.assertEqual(len(world.token_scopes), 5)
        self.assertTrue(
            all(scope == f"api://{RUNTIME_ID}/Models.Invoke" for scope in world.token_scopes)
        )
        for target in world.targets:
            item = proof["models"][target["modelKey"]]
            self.assertEqual(item["publicationAppliedAt"], STAMP)
            self.assertEqual(item["acceptedStatus"], 200)
            self.assertTrue(item["gatewayContextLimitProven"])
            self.assertEqual(item["totalTokens"], 53)
            self.assertEqual(item["denialEvidence"], "native-after-prompt-policy-denial")
        for index, call in enumerate(world.http.calls):
            env, deployment, _ = PROFILES[index // 2]
            self.assertEqual(
                call["url"],
                f"https://gateway-{env}.azure-api.net/mosaic/federal/{deployment.replace('.', '-')}"
                f"/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21",
            )
            self.assertEqual(call["headers"]["Authorization"], f"Bearer {world.token}")
            self.assertEqual(call["headers"]["x-mosaic-cost-center"], "general")
            self.assertFalse(call["follow_redirects"])
            self.assertEqual(call["body"]["max_completion_tokens"], 256)
            self.assertFalse(call["body"]["stream"])
            self.assertNotIn("Ocp-Apim-Subscription-Key", call["headers"])
            if index % 2:
                self.assertGreater(len(call["body"]["messages"][0]["content"]), 6000)
        serialized = json.dumps(proof)
        self.assertNotIn(world.token, serialized)
        self.assertNotIn("private answer", serialized)
        self.assertNotIn(verifier.PROMPT, serialized)
        self.assertTrue(all(method == "GET" for method, _ in world.api.calls))

    def test_budget_is_reserved_for_both_calls_on_all_models_before_any_paid_request(self):
        world = World()
        world.quote_rate = Decimal("1000")
        world.failed("aggregate-spend-reservation")
        self.assertEqual(world.http.calls, [])
        self.assertEqual(len(world.quotes), 5)

    def test_spend_cap_cannot_be_raised(self):
        world = World()
        world.failed("spend-ceiling", spend_limit_usd=Decimal("0.251"))
        self.assertEqual(world.http.calls, [])

    def test_five_must_be_exact_owned_offerings_and_current_connections(self):
        for kind in ("missing", "duplicate", "foreign", "stale"):
            with self.subTest(kind=kind):
                world = World()
                if kind == "missing":
                    world.targets.pop()
                elif kind == "duplicate":
                    world.targets[-1] = copy.deepcopy(world.targets[0])
                elif kind == "foreign":
                    world.targets[0]["publicationId"] = "unowned-publication"
                else:
                    world.targets[0]["connection"]["endpoint"] += "/unapplied"
                world.failed()
                self.assertEqual(world.http.calls, [])

    def test_fake_person_and_application_tokens_cannot_substitute_for_current_operator(self):
        for changes in (
            {"oid": "66666666-6666-6666-6666-666666666666"},
            {"tid": "66666666-6666-6666-6666-666666666666"},
            {"aud": "66666666-6666-6666-6666-666666666666"},
            {"scp": "", "roles": ["Models.Invoke.Application"]},
            {"exp": 1},
        ):
            with self.subTest(changes=changes):
                world = World()
                world.token = runtime_token(**changes)
                world.failed()
                self.assertEqual(world.http.calls, [])

    def test_token_acquisition_failure_is_safe_and_does_not_make_anonymous_calls(self):
        world = World()

        def fail(scope):
            raise RuntimeError(f"private token {world.token}")

        world.get_token = fail
        error = world.failed()
        self.assertNotIn(world.token, str(error))
        self.assertEqual(world.http.calls, [])

    def test_wrong_center_wrong_person_and_non_estimated_grants_fail_before_requests(self):
        for field in ("objectId", "costCenterCode", "estimatePromptTokens"):
            with self.subTest(field=field):
                world = World()
                grant = world.api.records["/publications/publication-0"]["appliedAccess"]["grants"][
                    0
                ]
                if field == "objectId":
                    grant[field] = "not-the-operator"
                elif field == "costCenterCode":
                    grant[field] = "NASA-MISSION"
                else:
                    grant["enforcement"]["tokens"][field] = False
                world.failed()
                self.assertEqual(world.http.calls, [])

    def test_bad_routes_are_never_sent_directly_upstream(self):
        for endpoint in (
            "https://upstream.openai.azure.com",
            "https://gateway-prod.azure-api.net/mosaic/federal/gpt-5-6-luna",
            "https://user:password@gateway-dev.azure-api.net/mosaic/federal/gpt-5-6-luna",
        ):
            with self.subTest(endpoint=endpoint):
                world = World()
                world.update_connection(0, lambda c, endpoint=endpoint: c.update(endpoint=endpoint))
                world.failed()
                self.assertEqual(world.http.calls, [])

    def test_unsupported_operation_and_unknown_upstream_capacity_remain_failed(self):
        world = World()
        world.update_connection(0, lambda c: c.update(operations=[]))
        world.failed("unsupported-chat-operation")
        self.assertEqual(world.http.calls, [])
        world = World()
        next(iter(world.op.deployments.values()))["properties"]["rateLimits"] = []
        world.failed("unverifiable-upstream-capacity")
        self.assertEqual(world.http.calls, [])

    def test_generic_and_upstream_429s_are_never_gateway_denial_proof(self):
        responses = (
            httpx.Response(429, json={"error": {"message": "Token limit will exceed"}}),
            httpx.Response(429, json={"message": "Token limit is exceeded."}),
            httpx.Response(429, json={"message": "Rate limit is exceeded."}),
            httpx.Response(
                429,
                headers={"Retry-After": "60", "x-ms-region": "upstream-region"},
                json={"statusCode": 429, "message": "Token limit will exceed limit"},
            ),
            httpx.Response(429, text="plain upstream throttle"),
        )
        for response in responses:
            with self.subTest(response=response.status_code):
                world = World()
                world.http.responses[1] = response
                world.failed()
                self.assertEqual(len(world.http.calls), 2)

    def test_matching_real_trace_can_prove_missing_policy_body_but_mismatches_cannot(self):
        for field in (None, "backendInvoked", "requestId", "section", "publicationAppliedAt"):
            with self.subTest(field=field):
                world = World()
                world.http.responses[1] = httpx.Response(
                    429,
                    headers={"apim-request-id": "77777777-7777-7777-7777-777777777777"},
                    text="Unrecognized deployment-specific wording",
                )

                def trace(expected, field=field, world=world):
                    proof = expected | {
                        "policy": "llm-token-limit",
                        "section": "inbound",
                        "reason": "TokenLimitExceededAfterPrompt",
                        "backendInvoked": False,
                    }
                    self.assertNotIn(world.token, json.dumps(expected))
                    if field:
                        proof[field] = True if field == "backendInvoked" else "mismatch"
                    return proof

                if field:
                    world.failed("unproven-gateway-denial", trace_reader=trace)
                else:
                    proof = world.run(trace_reader=trace)
                    self.assertEqual(
                        proof["models"][world.targets[0]["modelKey"]]["denialEvidence"],
                        "matching-gateway-trace",
                    )

    def test_statuses_empty_answers_wrong_models_and_missing_metering_are_not_admitted(self):
        responses = [
            httpx.Response(status, text="private backend body")
            for status in (301, 401, 403, 429, 500)
        ]
        responses += [
            httpx.Response(200, json={"choices": []}),
            httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
            httpx.Response(
                200,
                json={"model": "wrong", "choices": [{"message": {"content": "private answer"}}]},
            ),
            httpx.Response(
                200,
                json={
                    "model": "gpt-5.6-luna",
                    "choices": [{"message": {"content": "private answer"}}],
                },
            ),
        ]
        for response in responses:
            with self.subTest(status=response.status_code):
                world = World()
                world.http.responses[0] = response
                world.failed()
                self.assertEqual(len(world.http.calls), 1)

    def test_native_remaining_headers_and_usage_must_actually_match_bound(self):
        for kind in ("missing", "unchanged", "bad-total", "over-output", "missing-quota"):
            with self.subTest(kind=kind):
                world = World()
                headers = {
                    "x-mosaic-remaining-tokens": "947",
                    "x-mosaic-remaining-quota-tokens": "24947",
                }
                usage = {"prompt_tokens": 45, "completion_tokens": 8, "total_tokens": 53}
                if kind == "missing":
                    headers.pop("x-mosaic-remaining-tokens")
                elif kind == "unchanged":
                    headers["x-mosaic-remaining-tokens"] = "1000"
                elif kind == "bad-total":
                    usage["total_tokens"] = 54
                elif kind == "over-output":
                    usage.update(completion_tokens=257, total_tokens=302)
                else:
                    headers.pop("x-mosaic-remaining-quota-tokens")
                world.http.responses[0] = httpx.Response(
                    200,
                    headers=headers,
                    json={
                        "model": "gpt-5.6-luna",
                        "choices": [{"message": {"content": "private answer"}}],
                        "usage": usage,
                    },
                )
                world.failed()

    def test_probe_success_instead_of_denial_fails_with_no_partial_model_record(self):
        world = World()
        world.http.responses[1] = httpx.Response(
            200, json={"choices": [{"message": {"content": "private answer"}}]}
        )
        world.failed("oversize-call-not-denied")
        self.assertEqual(len(world.http.calls), 2)

    def test_last_model_failure_does_not_publish_four_successes(self):
        world = World()
        world.http.responses[8] = httpx.Response(500, text=f"private {world.token}")
        world.failed("admitted-call-not-200")
        self.assertEqual(len(world.http.calls), 9)

    def test_publication_changes_during_run_invalidate_all_five_models(self):
        world = World()
        world.api.stale_after_requests = True
        world.failed("publication-changed-during-verification")
        self.assertEqual(len(world.http.calls), 10)

    def test_network_failure_and_oversized_results_never_retry_or_record_success(self):
        world = World()
        world.http.network_failure = True
        error = world.failed("model-network-error")
        self.assertNotIn(world.token, str(error))
        self.assertEqual(len(world.http.calls), 1)
        world = World()
        world.http.responses[0] = httpx.Response(200, content=b"x" * 65537)
        world.failed("response-size")
        self.assertEqual(len(world.http.calls), 1)

    def test_runnable_helper_requires_explicit_paid_invocation_and_echoes_no_private_paths(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = verifier.main(
                [
                    "--target",
                    "private-target.json",
                    "--state",
                    "private-state.json",
                    "--connections",
                    "private-connections.json",
                ]
            )
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(output.getvalue()),
            {"status": "FAILED", "models": 0, "error": "configuration-or-control-plane"},
        )

    def test_failed_attempt_does_not_reset_aggregate_quarter_dollar_spend_reservation(self):
        world = World()
        world.quote_rate = Decimal("2")
        world.http.network_failure = True
        world.failed("model-network-error")
        previous = world.op.state["objects"]["modelVerification"]["cumulativeReservedUsd"]
        self.assertGreater(Decimal(previous), Decimal("0"))
        self.assertEqual(len(world.http.calls), 1)
        world.http.network_failure = False
        world.failed("aggregate-spend-reservation")
        self.assertEqual(len(world.http.calls), 1)
        self.assertEqual(
            world.op.state["objects"]["modelVerification"]["cumulativeReservedUsd"], previous
        )

    def test_exact_quote_metadata_and_positive_finite_rates_are_required_before_paid_calls(self):
        for field, value in (
            ("model", "older-unrelated-model"),
            ("currency", "EUR"),
            ("deploymentType", "unreviewed-SKU"),
            ("sourceUrl", "https://prices.example/api/retail/prices"),
            ("meterNames", ["unrelated meter"]),
            ("retrievedOn", "2000-01-01"),
            ("inputPerMillion", -1),
            ("outputPerMillion", float("inf")),
        ):
            with self.subTest(field=field):
                world = World()
                original = world.quote

                def altered(model, sku, field=field, value=value, original=original):
                    return original(model, sku) | {field: value}

                world.quote = altered
                world.failed()
                self.assertEqual(world.http.calls, [])

    def test_probe_size_and_all_grant_limits_are_checked_before_any_paid_call(self):
        world = World()
        world.failed("probe-not-above-grant-limit", probe_words=1000)
        self.assertEqual(world.http.calls, [])
        world = World()
        world.failed("request-configuration", probe_words=3001)
        self.assertEqual(world.http.calls, [])

    def test_center_pool_native_remaining_header_is_required_when_applied(self):
        world = World()
        world.api.records["/publications/publication-0"]["appliedAccess"]["pools"] = [
            {"costCenterId": "general-center", "monthlyTokens": 100000}
        ]
        world.failed("missing-native-remaining-header")
        self.assertEqual(len(world.http.calls), 1)

    def test_compressed_native_response_is_bounded_after_decoding_and_not_decoded_twice(self):
        world = World()
        payload = {
            "model": "gpt-5.6-luna",
            "choices": [{"message": {"content": "private answer"}}],
            "usage": {"prompt_tokens": 45, "completion_tokens": 8, "total_tokens": 53},
        }
        world.http.responses[0] = httpx.Response(
            200,
            headers={
                "Content-Encoding": "gzip",
                "x-mosaic-remaining-tokens": "947",
                "x-mosaic-remaining-quota-tokens": "24947",
            },
            content=gzip.compress(json.dumps(payload).encode()),
        )
        self.assertEqual(world.run()["status"], "PASSED")

    def test_replacement_global_standard_luna_uses_actual_sku_not_prior_datazone_assumption(self):
        world = World()
        world.op.deployments["ai-2/deployments/"]["sku"]["name"] = "GlobalStandard"
        proof = world.run()
        result = proof["models"]["model:prod:gpt-5.6-luna"]
        self.assertEqual(result["deploymentType"], "GlobalStandard")
        self.assertIn("ShortCo", result["priceSourceUrl"])


if __name__ == "__main__":
    unittest.main()
