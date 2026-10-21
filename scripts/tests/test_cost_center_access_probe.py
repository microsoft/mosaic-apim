import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_OID, UUID, uuid5
from xml.etree import ElementTree as ET

from scripts import cost_center_access_probe as probe

NOW = 1_800_000_000
EXPIRY = NOW + 300


class ProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = probe.fixture_contract()
        self.members = probe.fixture_members(100)
        self.projection = probe.project(self.contract, self.members, expiry=EXPIRY)

    def test_exact_membership_and_principal_kind(self) -> None:
        for member in self.members:
            expected = probe.Decision.DIRECT if member.direct else probe.Decision.GRAPH
            self.assertEqual(probe.lookup(self.projection, member.owner, now=NOW), expected)
            other_kind: Literal["u", "a"] = "a" if member.owner.kind == "u" else "u"
            other = probe.Owner(member.owner.object_id, other_kind)
            self.assertEqual(probe.lookup(self.projection, other, now=NOW), probe.Decision.ABSENT)
        stranger = probe.Owner(uuid5(NAMESPACE_OID, "stranger"))
        self.assertEqual(probe.lookup(self.projection, stranger, now=NOW), probe.Decision.ABSENT)

    def test_guid_encoding_matches_dotnet_byte_order(self) -> None:
        owner = probe.Owner(UUID("00112233-4455-6677-8899-aabbccddeeff"))
        projected = probe.project(self.contract, [probe.Member(owner)], expiry=EXPIRY)
        bucket = probe.bucket_for(owner, projected.bucket_count)
        value = projected.values[self.contract.shard_name(bucket, 0)]
        records = base64.b64decode(value.split("|")[2])
        self.assertEqual(records[:16].hex(), "33221100554477668899aabbccddeeff")

    def test_expiry_does_not_break_direct_members(self) -> None:
        for member in self.members:
            expected = probe.Decision.DIRECT if member.direct else probe.Decision.EXPIRED
            self.assertEqual(probe.lookup(self.projection, member.owner, now=EXPIRY), expected)

    def test_alternative_sources_merge_and_remain_direct_after_expiry(self) -> None:
        owner = self.members[1].owner
        members = [probe.Member(owner), probe.Member(owner, direct=True, graph=False)]
        projected = probe.project(self.contract, members, expiry=EXPIRY)
        self.assertEqual(probe.lookup(projected, owner, now=EXPIRY), probe.Decision.DIRECT)
        with self.assertRaises(probe.ProbeError):
            probe.project(
                self.contract,
                [probe.Member(owner), probe.Member(probe.Owner(owner.object_id, "a"))],
                expiry=EXPIRY,
            )

    def test_member_order_does_not_cause_writes(self) -> None:
        result = probe.plan_update(self.projection, reversed(self.members), expiry=EXPIRY)
        self.assertEqual(result.writes, ())

    def test_removal_stages_one_bucket_then_manifest_without_policy_change(self) -> None:
        owner = self.members[0].owner
        update = probe.plan_update(self.projection, self.members[1:], expiry=EXPIRY)
        self.assertEqual(len(update.writes), 2)
        self.assertEqual(update.writes[-1][0], self.contract.manifest_name)
        self.assertEqual(probe.lookup(update.projection, owner, now=NOW), probe.Decision.ABSENT)
        self.assertEqual(
            probe.render_membership_fragment(self.contract, 64),
            probe.render_membership_fragment(update.projection.contract, 64),
        )
        values = dict(self.projection.values)
        values[update.writes[0][0]] = update.writes[0][1]
        staged_only = replace(self.projection, values=values)
        self.assertEqual(probe.lookup(staged_only, owner, now=NOW), probe.Decision.DIRECT)

    def test_manifest_arriving_first_denies_only_the_affected_bucket(self) -> None:
        owner = self.members[0].owner
        update = probe.plan_update(self.projection, self.members[1:], expiry=EXPIRY)
        values = dict(self.projection.values)
        values[update.writes[-1][0]] = update.writes[-1][1]
        partial = replace(self.projection, values=values)
        self.assertEqual(probe.lookup(partial, owner, now=NOW), probe.Decision.INVALID)
        bucket = probe.bucket_for(owner, 64)
        unaffected = next(
            member for member in self.members if probe.bucket_for(member.owner, 64) != bucket
        )
        self.assertNotEqual(
            probe.lookup(partial, unaffected.owner, now=NOW), probe.Decision.INVALID
        )

    def test_freshness_renewal_does_not_rewrite_member_data(self) -> None:
        update = probe.plan_update(self.projection, self.members, expiry=EXPIRY + 300)
        self.assertEqual(len(update.writes), 1)
        self.assertEqual(update.writes[0][0], self.contract.manifest_name)

    def test_group_removal_preserves_direct_members_and_uses_bounded_data_writes(self) -> None:
        remaining = [
            probe.Member(member.owner, direct=True, graph=False)
            for member in self.members
            if member.direct
        ]
        update = probe.plan_update(self.projection, remaining, expiry=0)
        self.assertLessEqual(len(update.writes), self.projection.bucket_count + 1)
        for member in self.members:
            expected = probe.Decision.DIRECT if member.direct else probe.Decision.ABSENT
            self.assertEqual(probe.lookup(update.projection, member.owner, now=NOW), expected)

    def test_multiple_updates_reuse_only_the_inactive_bank(self) -> None:
        removed = probe.plan_update(self.projection, self.members[1:], expiry=EXPIRY)
        restored = probe.plan_update(removed.projection, self.members, expiry=EXPIRY)
        for member in self.members:
            self.assertNotEqual(
                probe.lookup(restored.projection, member.owner, now=NOW), probe.Decision.ABSENT
            )
        self.assertNotEqual(removed.writes[0][0], restored.writes[0][0])

    def test_no_op_and_failed_update_preserve_the_original_projection(self) -> None:
        before = dict(self.projection.values)
        no_op = probe.plan_update(self.projection, self.members, expiry=EXPIRY)
        self.assertEqual(no_op.projection.values, before)
        with self.assertRaises(probe.ProbeError):
            probe.plan_update(self.projection, self.members[1:], expiry=-1)
        self.assertEqual(self.projection.values, before)

    def test_exhausted_generation_cannot_wrap_into_an_older_version(self) -> None:
        owner = self.members[0].owner
        bucket = probe.bucket_for(owner, 64)
        values = dict(self.projection.values)
        manifest = values[self.contract.manifest_name].split("|")
        offset = bucket * 17
        manifest[2] = manifest[2][:offset] + "0ffffffffffffffff" + manifest[2][offset + 17 :]
        values[self.contract.manifest_name] = "|".join(manifest)
        name = self.contract.shard_name(bucket, 0)
        data = values[name].split("|")
        data[1] = "ffffffffffffffff"
        values[name] = "|".join(data)
        exhausted = replace(self.projection, values=values)
        with self.assertRaisesRegex(probe.ProbeError, "exhausted"):
            probe.plan_update(exhausted, self.members[1:], expiry=EXPIRY)

    def test_missing_corrupt_and_oversized_metadata_fail_explicitly(self) -> None:
        owner = self.members[0].owner
        bucket = probe.bucket_for(owner, 64)
        name = self.contract.shard_name(bucket, 0)
        for invalid in ("", "1|0000000000000000|!", "x" * 4097, "2|0000000000000000|"):
            with self.subTest(value=invalid[:20]):
                values = dict(self.projection.values)
                values[name] = invalid
                self.assertEqual(
                    probe.lookup(replace(self.projection, values=values), owner, now=NOW),
                    probe.Decision.INVALID,
                )
        for missing in (name, self.contract.manifest_name):
            values = dict(self.projection.values)
            del values[missing]
            self.assertEqual(
                probe.lookup(replace(self.projection, values=values), owner, now=NOW),
                probe.Decision.INVALID,
            )

    def test_malformed_trailing_record_cannot_hide_behind_a_valid_member(self) -> None:
        owner = self.members[0].owner
        projected = probe.project(self.contract, [probe.Member(owner)], expiry=EXPIRY)
        name = self.contract.shard_name(probe.bucket_for(owner, 64), 0)
        for suffix in (b"\x01", owner.object_id.bytes_le + b"\x00", b"\x00" * 17):
            records = owner.object_id.bytes_le + b"\x02" + suffix
            values = dict(projected.values)
            values[name] = f"1|{'0' * 16}|{base64.b64encode(records).decode()}"
            self.assertEqual(
                probe.lookup(replace(projected, values=values), owner, now=NOW),
                probe.Decision.INVALID,
            )

    def test_hash_skew_cannot_overflow_silently(self) -> None:
        members: list[probe.Member] = []
        index = 0
        while len(members) < 180:
            owner = probe.Owner(uuid5(NAMESPACE_OID, f"skew:{index}"))
            if probe.bucket_for(owner, 8) == 0:
                members.append(probe.Member(owner))
            index += 1
        with self.assertRaisesRegex(probe.ProbeError, "4096"):
            probe.project(self.contract, members, bucket_count=8, expiry=EXPIRY)

    def test_invalid_contracts_are_rejected(self) -> None:
        with self.assertRaises(probe.ProbeError):
            probe.Owner(UUID(int=0))
        with self.assertRaises(probe.ProbeError):
            probe.Contract(UUID(int=0))
        with self.assertRaises(probe.ProbeError):
            probe.Member(self.members[0].owner, direct=False, graph=False)
        with self.assertRaises(probe.ProbeError):
            probe.project(self.contract, self.members, bucket_count=3, expiry=EXPIRY)
        with self.assertRaises(probe.ProbeError):
            probe.project(self.contract, self.members, expiry=-1)


class NativeContractTests(unittest.TestCase):
    def test_owner_recovery_requires_exact_product_and_canonical_native_identifier(self) -> None:
        contract = probe.fixture_contract()
        for member in probe.fixture_members(10):
            name = contract.subscription_name(member.owner)
            self.assertLessEqual(len(name), 256)
            self.assertEqual(
                contract.owner_from_native_context(name, contract.product_name), member.owner
            )
            for bad_name, bad_product in (
                (name, "some-other-product"),
                (name + "-suffix", contract.product_name),
                (name.upper(), contract.product_name),
                (name + "\n", contract.product_name),
                (name.replace("-key-", "-key-x-"), contract.product_name),
                (f"{contract.prefix}-key-u-{'0' * 32}", contract.product_name),
            ):
                with self.subTest(name=bad_name):
                    with self.assertRaises(probe.ProbeError):
                        contract.owner_from_native_context(bad_name, bad_product)

    def test_another_cost_centers_native_key_cannot_be_adopted(self) -> None:
        first = probe.fixture_contract()
        second = probe.Contract(uuid5(NAMESPACE_OID, "mosaic-apim:other-probe-group"))
        owner = probe.fixture_members(1)[0].owner
        with self.assertRaises(probe.ProbeError):
            first.owner_from_native_context(second.subscription_name(owner), first.product_name)
        with self.assertRaises(probe.ProbeError):
            first.owner_from_native_context(first.subscription_name(owner), second.product_name)

    def test_membership_fragment_has_only_static_local_inputs_and_no_nested_fragment(self) -> None:
        contract = probe.fixture_contract()
        text = probe.render_membership_fragment(contract, 64)
        fragment = ET.fromstring(text)
        self.assertEqual(fragment.tag, "fragment")
        self.assertEqual([node.tag for node in fragment], ["set-variable"])
        expression = fragment[0].attrib["value"]
        self.assertEqual(expression.count("case "), 64)
        self.assertIn("ToByteArray()", expression)
        self.assertIn("SHA256.Create()", expression)
        self.assertIn("System.Convert.ToBase64String(records) != data[2]", expression)
        self.assertNotIn("context.Request", expression)
        for forbidden in (
            "cache-lookup-value",
            "cache-store-value",
            "send-request",
            "include-fragment",
        ):
            self.assertNotIn(forbidden, text)

    def test_native_policy_is_diagnostic_only_and_does_not_echo_secrets_or_subjects(self) -> None:
        contract, members = probe.fixture_contract(), probe.fixture_members(10)
        policy = ET.fromstring(probe.render_native_context_policy(contract, members[0].owner))
        self.assertIsNotNone(policy.find("inbound/return-response"))
        self.assertIsNone(policy.find(".//forward-request"))
        self.assertIsNone(policy.find(".//validate-jwt"))
        body = policy.findtext("inbound/return-response/set-body", "")
        self.assertIn('"diagnosticOnly", true', body)
        self.assertIn('"ownerMatchesFixture"', body)
        self.assertNotIn("PrimaryKey", body)
        self.assertNotIn("SecondaryKey", body)
        self.assertNotIn("context.Request", body)


class CapacityAndCliTests(unittest.TestCase):
    def test_local_dotnet_reference_host_executes_generated_expression(self) -> None:
        powershell = shutil.which("pwsh")
        if powershell is None:
            self.skipTest("Local .NET expression execution requires PowerShell 7")
        command = r"""
$ErrorActionPreference = 'Stop'
$bundle = $env:MOSAIC_COST_CENTER_PROBE_BUNDLE
Add-Type -Path (Join-Path $bundle 'reference-host.cs')
$cases = Get-Content (Join-Path $bundle 'reference-cases.json') -Raw | ConvertFrom-Json
foreach ($case in $cases) {
    $actual = [MosaicCostCenterProbe]::Evaluate($case.owner, $case.unixTime)
    if ($actual -cne $case.expected) {
        throw 'Generated C# membership result disagrees with the Python reference'
    }
}
Write-Output $cases.Count
"""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "bundle"
            probe.write_bundle(directory, population=100, bucket_count=64, now=NOW)
            result = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
                env={**os.environ, "MOSAIC_COST_CENTER_PROBE_BUNDLE": str(directory)},
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "203")

    def test_requested_populations_fit_one_center_conservative_data_budget(self) -> None:
        for population in probe.DEFAULT_POPULATIONS:
            with self.subTest(population=population):
                report = probe.capacity_report(population, bucket_count=64, now=NOW)
                self.assertLessEqual(report["largestNamedValueCharacters"], 4096)
                self.assertTrue(report["withinConservative512KiBBudget"])
                self.assertEqual(report["forecastSubscriptions"], population)
                self.assertEqual(report["forecastProducts"], 1)
                self.assertEqual(report["forecastNamedValues"], 129)
                self.assertEqual(report["singleRemovalNamedValueWrites"], 2)
                self.assertEqual(report["freshnessOnlyWrites"], 1)
                self.assertEqual(report["unchangedProjectionWrites"], 0)
                self.assertTrue(report["singleRemovalPolicySourceUnchanged"])
                self.assertEqual(report["apimNativeContext"], "NOT_RUN")
                self.assertEqual(report["gatewayLatency"], "NOT_MEASURED")
                self.assertFalse(report["existingResourcesProvided"])
                self.assertIsNone(report["withinPublishedResourceLimits"])
                self.assertLessEqual(report["explicitReferencesInFixture"], 500)

    def test_existing_resources_can_exhaust_native_subscription_headroom(self) -> None:
        report = probe.capacity_report(
            5000,
            bucket_count=64,
            now=NOW,
            inventory=probe.ExistingResources(23000, 20, 4000, tier="standard"),
        )
        self.assertTrue(report["existingResourcesProvided"])
        self.assertEqual(report["totalResources"]["subscriptions"], 28000)
        self.assertEqual(report["totalResources"]["products"], 21)
        self.assertEqual(report["totalResources"]["namedValues"], 4129)
        self.assertFalse(report["withinPublishedResourceLimits"])

    def test_population_above_5000_is_not_silently_truncated(self) -> None:
        members = probe.fixture_members(5001)
        projection = probe.project(probe.fixture_contract(), members, expiry=EXPIRY)
        self.assertEqual(len(members), 5001)
        self.assertEqual(probe.lookup(projection, members[-1].owner, now=NOW), probe.Decision.GRAPH)
        self.assertEqual(sum(member.direct for member in members) + 1, 500)

    def test_every_supported_manifest_shape_fits_a_named_value(self) -> None:
        for buckets in probe.BUCKET_COUNTS:
            projection = probe.project(
                probe.fixture_contract(),
                probe.fixture_members(100),
                bucket_count=buckets,
                expiry=EXPIRY,
            )
            self.assertLessEqual(len(projection.values[projection.contract.manifest_name]), 4096)

    def test_shared_offerings_and_existing_policy_budget_are_counted(self) -> None:
        report = probe.capacity_report(
            5000, bucket_count=64, now=NOW, centers=3, existing_policy_bytes=64 * 1024
        )
        self.assertFalse(report["withinConservative512KiBBudget"])
        self.assertEqual(report["forecastSubscriptions"], 15000)
        self.assertEqual(report["forecastProducts"], 3)
        self.assertEqual(report["forecastNamedValues"], 387)

    def test_full_group_loss_forecasts_credential_writes_separately_from_data_writes(self) -> None:
        report = probe.capacity_report(5000, bucket_count=64, now=NOW)
        self.assertEqual(report["graphRemovalNamedValueWrites"], 65)
        self.assertEqual(report["forecastGraphRemovalSubscriptionWrites"], 4501)
        self.assertEqual(report["forecastSingleRemovalSubscriptionWrites"], 1)
        self.assertEqual(report["largestDecodedBucketRecords"], 97)

    def test_cli_never_claims_live_success_and_writes_only_requested_local_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "bundle"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = probe.main(["--population", "100", "--output", str(directory)])
            report = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(report["mode"], "offline")
            self.assertEqual(report["feasibilityGate"], "NOT_PASSED")
            contract = json.loads((directory / "native-contract.json").read_text())
            self.assertFalse(contract["createsAzureResources"])
            self.assertFalse(contract["validatesEntraTokens"])
            self.assertIsNone(contract["target"])
            before = (directory / "membership-fragment.xml").read_bytes()
            with self.assertRaises(probe.ProbeError):
                probe.write_bundle(directory, population=100, bucket_count=64, now=NOW)
            self.assertEqual((directory / "membership-fragment.xml").read_bytes(), before)

    def test_cli_reports_capacity_failure_as_nonzero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            code = probe.main(["--population", "5000", "--centers", "3"])
        self.assertEqual(code, 1)

    def test_cli_reports_resource_headroom_failure_as_nonzero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            code = probe.main(
                [
                    "--population",
                    "5000",
                    "--tier",
                    "standard",
                    "--existing-subscriptions",
                    "23000",
                    "--existing-products",
                    "20",
                    "--existing-named-values",
                    "4000",
                ]
            )
        self.assertEqual(code, 1)

    def test_cli_does_not_treat_partial_inventory_as_complete(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            probe.main(["--existing-subscriptions", "0"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
