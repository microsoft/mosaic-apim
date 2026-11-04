import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from scripts.federal_demo.budgets import configure as configure_budgets
from scripts.federal_demo.directory import provision
from scripts.federal_demo.estate import (
    ENVIRONMENTS,
    PEOPLE,
    SERVICES,
    TEAMS,
    inventory,
    validate_grant,
    validate_target,
)
from scripts.federal_demo.governance import Mosaic, grant, matches, seed_tools
from scripts.federal_demo.jobs import runtime_records
from scripts.federal_demo.operator import OperationError, Operator
from scripts.federal_demo.pricing import meter_names, select_quote
from scripts.federal_demo.verification import cleanup as cleanup_verification


class EstateTests(unittest.TestCase):
    def test_verification_cleanup_preserves_disabled_audit_references_not_active_access(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                operator = Mock()
                operator.state = {
                    "objects": {
                        "verificationAccess": {
                            "principalId": "operator",
                            "principalCreated": True,
                            "grants": [],
                            "cleanedUp": False,
                        }
                    }
                }
                api = Mock()
                api.request.side_effect = [
                    [],
                    [
                        {
                            "appliedAccess": {
                                "grants": [{"subject": {"id": "operator"}, "enabled": enabled}]
                            }
                        }
                    ],
                ]
                with patch("scripts.federal_demo.verification.Mosaic", return_value=api):
                    if enabled:
                        with self.assertRaises(OperationError):
                            cleanup_verification(operator)
                        operator.remember.assert_not_called()
                    else:
                        with self.assertLogs("scripts.federal_demo.verification", level="WARNING"):
                            cleanup_verification(operator)
                        ledger = operator.remember.call_args.args[1]
                        self.assertTrue(ledger["cleanedUp"])
                        self.assertIn("principalRetained", ledger)
                self.assertTrue(all(call.args[0] == "GET" for call in api.request.call_args_list))

    def test_apply_requires_the_exact_run_to_finish_before_reading_the_snapshot(self):
        for final in ("succeeded", "failed", "interrupted"):
            with self.subTest(final=final):
                api = Mosaic.__new__(Mosaic)
                api.operator = Mock()
                run = {
                    "id": "run",
                    "planId": "plan",
                    "planDigest": "digest",
                    "startedAt": "2026-01-01T00:00:00Z",
                    "completedAt": "2026-01-01T00:00:02Z",
                }
                api.request = Mock(
                    side_effect=[
                        {"id": "plan", "digest": "digest"},
                        run,
                        run | {"status": "running"},
                        run | {"status": final},
                        {"accessState": "applied", "lastAppliedAt": "2026-01-01T00:00:01Z"},
                    ]
                )
                with patch("scripts.federal_demo.governance.time.sleep"):
                    if final == "succeeded":
                        api.apply("publications", {"id": "publication"})
                    else:
                        with self.assertRaises(OperationError):
                            api.apply("publications", {"id": "publication"})
                calls = api.request.call_args_list
                self.assertEqual(calls[2].args, ("GET", "/publications/publication/runs/run"))
                self.assertEqual(calls[3].args, calls[2].args)
                self.assertEqual(len(calls), 5 if final == "succeeded" else 4)

    def test_independent_ledger_writes_preserve_each_other(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            first = Operator(self.target(), state)
            second = Operator(self.target(), state)
            first.remember("one", {"id": "first"})
            second.remember("two", {"id": "second"})
            latest = Operator(self.target(), state)
            self.assertEqual(set(latest.state["objects"]), {"one", "two"})

    def test_budgets_refuse_without_live_model_evidence(self):
        class Unverified:
            def __init__(self):
                self.state = {"objects": {}}

            def verify_target(self):
                pass

        with self.assertRaises(OperationError):
            configure_budgets(Unverified())

    def test_exact_job_evidence_parsing(self):
        expected = {"outcome": "ok", "target_count": 24}
        self.assertEqual(runtime_records(expected), [expected])
        self.assertEqual(
            runtime_records('2026-10-29T01:00:00Z {"outcome":"ok","target_count":24}'), [expected]
        )
        self.assertEqual(
            runtime_records([{"Log": '{"outcome":"ok","target_count":24}'}]), [expected]
        )
        self.assertEqual(runtime_records("container started successfully"), [])
        self.assertEqual(runtime_records('{"outcome":"error"}'), [{"outcome": "error"}])

    def test_directory_rerun_does_not_reset_passwords(self):
        class FakeOperator:
            target: ClassVar[dict] = {
                "estate": "federal-demo",
                "domain": "example.test",
                "onboardingVault": "example-vault",
                "apiServicePrincipalId": "api",
                "userRoleId": "user-role",
            }

            def __init__(self):
                self.groups = {}
                self.users = {}
                self.secrets = {}
                self.password_writes = 0

            def verify_target(self):
                pass

            def items(self, path):
                query = parse_qs(urlsplit(path).query)
                expected = query.get("$filter", [""])[0].split("'")
                if path.startswith("/users?"):
                    user = self.users.get(expected[1])
                    return [user] if user else []
                if path.startswith("/groups?"):
                    group = self.groups.get(expected[1])
                    return [group] if group else []
                return []

            def graph(self, method, path, body):
                if path == "/groups":
                    record = body | {"id": f"group-{len(self.groups)}"}
                    self.groups[body["displayName"]] = record
                    return record
                if path == "/users":
                    record = {
                        key: value for key, value in body.items() if key != "passwordProfile"
                    } | {"id": f"user-{len(self.users)}"}
                    self.users[body["userPrincipalName"]] = record
                    return record
                return {}

            def request(self, method, url, resource, body=None):
                if method == "PUT":
                    self.password_writes += 1
                    self.secrets[url] = body["value"]
                if method == "GET" and url not in self.secrets:
                    raise OperationError("Lost onboarding secret")
                return {}

            def remember(self, key, value):
                pass

        operator = FakeOperator()
        provision(operator)
        self.assertEqual(len(operator.users), 20)
        self.assertEqual(len(operator.groups), 6)
        self.assertEqual(len(set(operator.secrets.values())), 20)
        self.assertEqual(operator.password_writes, 20)
        provision(operator)
        self.assertEqual(operator.password_writes, 20)
        self.assertTrue(all(person["accountEnabled"] for person in operator.users.values()))

    def test_grant_and_pool_match_the_api_contract(self):
        from mosaic_api.cost_centers import CostCenterLimitsUpdate
        from mosaic_api.domain import EntitlementCreate

        class Api:
            def ensure(self, path, payload, match):
                self.grant = EntitlementCreate.model_validate(payload)
                return payload

            def request(self, method, path, body=None):
                if method == "GET":
                    return {"limits": []}
                self.pool = CostCenterLimitsUpdate.model_validate(body)

        for kind in ("user", "securityGroup", "managedIdentity"):
            for general in (False, True):
                for calls_only in (False, True):
                    api = Api()
                    grant(
                        api,
                        {"id": "test-subject", "kind": kind},
                        {"id": "test-center", "code": "NASA-MISSION"},
                        {"kind": "mcpServer" if calls_only else "modelApi", "id": "test-resource"},
                        "prod",
                        general=general,
                        calls_only=calls_only,
                    )
                    self.assertEqual(
                        api.grant.subject.kind,
                        {
                            "user": "user",
                            "securityGroup": "securityGroup",
                            "managedIdentity": "application",
                        }[kind],
                    )
                    self.assertEqual(api.grant.enforcement.tokens is None, calls_only)
                    self.assertEqual(
                        api.pool.limits[0].pool.monthly_calls, 1000 if general else 5000
                    )

    def test_api_subset_matching_preserves_idempotency(self):
        self.assertTrue(
            matches(
                {"resource": {"kind": "modelApi", "id": "test", "scopeId": None}},
                {"resource": {"kind": "modelApi", "id": "test"}},
            )
        )
        self.assertFalse(matches({"id": "different"}, {"id": "test"}))

    def test_unreachable_tool_gets_one_preflight_and_never_bypasses_access(self):
        class FakeOperator:
            target: ClassVar[dict] = {
                "mcpBindings": [
                    {
                        "environment": env,
                        "serviceKey": service.key,
                        "url": "https://example.test/mcp",
                        "audience": "api://example",
                    }
                    for env in ENVIRONMENTS
                    for service in SERVICES
                ],
                "gateways": dict.fromkeys(ENVIRONMENTS, "gateway-resource"),
            }

            def verify_target(self):
                pass

        class ReachedSync(Exception):
            pass

        class Api:
            def __init__(self, accessible):
                self.accessible = accessible
                self.preflights = 0

            def ensure(self, path, payload, match):
                return {
                    "id": "endpoint",
                    "environment": "development",
                    "access": {"canDiscover": False},
                }

            def request(self, method, path, body=None):
                if path == "/cost-centers":
                    return [{"code": team.code, "id": team.code} for team in TEAMS]
                if path == "/principals":
                    return []
                if path == "/gateways":
                    return [{"azureResourceId": "gateway-resource", "id": "gateway"}]
                if method == "POST" and path.endswith("/preflight"):
                    self.preflights += 1
                    return {"id": "endpoint", "access": {"canDiscover": self.accessible}}
                raise AssertionError((method, path))

            def sync(self, collection, record):
                if not self.accessible:
                    raise AssertionError("Inaccessible endpoint reached sync")
                raise ReachedSync()

        for accessible in (False, True):
            api = Api(accessible)
            with (
                self.subTest(accessible=accessible),
                patch("scripts.federal_demo.governance.Mosaic", return_value=api),
                self.assertLogs("scripts.federal_demo.governance", level="WARNING"),
                self.assertRaises(ReachedSync if accessible else OperationError),
            ):
                seed_tools(FakeOperator())
            self.assertEqual(api.preflights, 1)

    def test_exact_price_meters(self):
        for model in ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.1"):
            for sku in ("GlobalStandard", "DataZoneStandard"):
                names = meter_names(model, sku)
                rows = [
                    {
                        "meterName": name,
                        "currencyCode": "USD",
                        "unitOfMeasure": "1M",
                        "type": "Consumption",
                        "isPrimaryMeterRegion": True,
                        "productName": "Azure OpenAI GPT5",
                        "retailPrice": index + 0.1,
                        "effectiveStartDate": "2026-08-01T00:00:00Z",
                    }
                    for index, name in enumerate(names)
                ]
                quote = select_quote(model, sku, rows, "https://prices.azure.com")
                self.assertEqual(quote["inputPerMillion"], 0.1)
                self.assertEqual(quote["outputPerMillion"], 1.1)
                with self.assertRaises(OperationError):
                    select_quote(model, sku, rows[:1], "https://prices.azure.com")
                with self.assertRaises(OperationError):
                    select_quote(
                        model,
                        sku,
                        [*rows, rows[0] | {"retailPrice": 99}],
                        "https://prices.azure.com",
                    )

    def test_no_pricing_alias_fallback(self):
        for model, sku in (
            ("gpt-5.6-luna-extra", "GlobalStandard"),
            ("gpt-5.6-luna", "ProvisionedManaged"),
        ):
            with self.assertRaises(OperationError):
                meter_names(model, sku)

    def test_exact_inventory(self):
        result = inventory()
        self.assertEqual(len(result["mcpDeployments"]), 24)
        self.assertEqual(len(PEOPLE), 20)
        self.assertEqual(len(TEAMS), 6)
        self.assertEqual(result["monthlyModelBudgetsUsd"], 600)
        self.assertEqual(len({p.local_part for p in PEOPLE}), 20)
        for agency in ("NASA", "FAA", "DOE", "USDA"):
            self.assertEqual(sum(p.agency == agency for p in PEOPLE), 5)

    def test_host_names_and_grants(self):
        names = {s.host_name(e) for s in SERVICES for e in ENVIRONMENTS}
        self.assertEqual(len(names), 24)
        self.assertTrue(all(len(name) <= 32 for name in names))
        for service in SERVICES:
            for env in ENVIRONMENTS:
                validate_grant(service.cost_center(env), env)
        for env in ("dev", "stg", "invalid"):
            with self.assertRaises(ValueError):
                validate_grant("FAA-OPS", env)

    def test_carlos_overlapping_membership(self):
        carlos = next(p for p in PEOPLE if p.name == "Carlos Rivera")
        self.assertEqual(carlos.centers, ("general", "FAA-OPS", "FAA-RND"))

    def target(self):
        return {
            "tenantId": "11111111-1111-4111-8111-111111111111",
            "subscriptionId": "22222222-2222-4222-8222-222222222222",
            "domain": "example.test",
            "estate": "federal-demo",
        }

    def test_rejects_target_injection(self):
        for field in ("domain", "estate", "subscriptionId"):
            with self.assertRaises(ValueError):
                validate_target(self.target() | {field: "bad';--"})

    def test_wrong_live_target_fails_before_network(self):
        with TemporaryDirectory() as temporary:
            operator = Operator(self.target(), Path(temporary) / "state.json")
            with patch(
                "scripts.federal_demo.operator.command",
                return_value={
                    "tenantId": self.target()["tenantId"],
                    "id": "different",
                    "environmentName": "AzureCloud",
                },
            ):
                with self.assertRaises(OperationError):
                    operator.verify_target()

    def test_destination_allowlist(self):
        with TemporaryDirectory() as temporary:
            operator = Operator(self.target(), Path(temporary) / "state.json")
            for url in (
                "http://graph.microsoft.com/v1.0/users",
                "https://evil.example/users",
                "https://user:password@graph.microsoft.com/v1.0/users",
            ):
                with self.assertRaises(OperationError):
                    operator.request("POST", url, "https://graph.microsoft.com", {})

    def test_interrupted_reads_are_bounded_and_writes_are_not_retried(self):
        for method, expected_attempts in (("GET", 3), ("POST", 1), ("PUT", 1), ("DELETE", 1)):
            with self.subTest(method=method), TemporaryDirectory() as temporary:
                operator = Operator(self.target(), Path(temporary) / "state.json")
                with (
                    patch.object(operator, "token", return_value="not-a-real-token"),
                    patch("scripts.federal_demo.operator.time.sleep"),
                    patch("scripts.federal_demo.operator.build_opener") as opener,
                    self.assertLogs("scripts.federal_demo.operator", level="WARNING")
                    if method == "GET"
                    else self.assertNoLogs("scripts.federal_demo.operator"),
                ):
                    opener.return_value.open.side_effect = URLError("temporary interruption")
                    with self.assertRaises(OperationError):
                        operator.request(
                            method,
                            "https://graph.microsoft.com/v1.0/users",
                            "https://graph.microsoft.com",
                        )
                    self.assertEqual(opener.return_value.open.call_count, expected_attempts)

    def test_http_refusals_are_not_retried(self):
        with TemporaryDirectory() as temporary:
            operator = Operator(self.target(), Path(temporary) / "state.json")
            with (
                patch.object(operator, "token", return_value="not-a-real-token"),
                patch("scripts.federal_demo.operator.build_opener") as opener,
            ):
                url = "https://graph.microsoft.com/v1.0/users"
                opener.return_value.open.side_effect = HTTPError(url, 403, "Denied", {}, None)
                with self.assertRaisesRegex(OperationError, "HTTP 403"):
                    operator.request("GET", url, "https://graph.microsoft.com")
                self.assertEqual(opener.return_value.open.call_count, 1)

    def test_ledger_bound_to_target(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.json"
            first = Operator(self.target(), state)
            first.remember("group:general", {"id": "fictional"})
            with self.assertRaises(OperationError):
                Operator(self.target() | {"estate": "another-estate"}, state)


if __name__ == "__main__":
    unittest.main()
