"""Offline M0 fixtures for APIM-native cost-center access; never deploys or calls Azure."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypedDict
from uuid import NAMESPACE_OID, UUID, uuid5
from xml.etree import ElementTree as ET

NAMED_VALUE_LIMIT = 4096
FRAGMENT_LIMIT = 512 * 1024
RECORD_BYTES = 17
MAX_GENERATION = (1 << 64) - 1
BUCKET_COUNTS = (8, 16, 32, 64, 128)
DEFAULT_POPULATIONS = (10, 100, 1000, 5000)
OWNER_VARIABLE = "mosaic-probe-owner"
DECISION_VARIABLE = "mosaic-probe-membership"
_MANIFEST = re.compile(r"1\|([0-9]{10})\|((?:[01][0-9a-f]{16})+)")
_SHARD = re.compile(r"1\|([0-9a-f]{16})\|([A-Za-z0-9+/=]*)")
_REFERENCE = re.compile(r"\{\{([a-z0-9-]+)\}\}")


class ProbeError(ValueError):
    pass


class Decision(StrEnum):
    DIRECT = "direct"
    GRAPH = "graph"
    ABSENT = "absent"
    EXPIRED = "expired"
    INVALID = "invalid"


class ResourceCounts(TypedDict):
    subscriptions: int
    products: int
    namedValues: int


PUBLISHED_LIMITS: dict[str, ResourceCounts] = {
    "developer": {"subscriptions": 10000, "products": 100, "namedValues": 5000},
    "basic": {"subscriptions": 15000, "products": 200, "namedValues": 5000},
    "standard": {"subscriptions": 25000, "products": 500, "namedValues": 10000},
    "premium": {"subscriptions": 75000, "products": 2000, "namedValues": 18000},
}


@dataclass(frozen=True)
class ExistingResources:
    subscriptions: int
    products: int
    named_values: int
    tier: str | None = None

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (self.subscriptions, self.products, self.named_values)
        ):
            raise ProbeError("Existing resource counts must be nonnegative integers")
        if self.tier is not None and self.tier not in PUBLISHED_LIMITS:
            raise ProbeError("The probe has no published quota fixture for this tier")

    def counts(self) -> ResourceCounts:
        return {
            "subscriptions": self.subscriptions,
            "products": self.products,
            "namedValues": self.named_values,
        }


class CapacityReport(TypedDict):
    populationPerCostCenter: int
    costCentersOnSameOffering: int
    bucketsPerCostCenter: int
    explicitReferencesInFixture: int
    forecastSubscriptions: int
    forecastProducts: int
    forecastNamedValues: int
    existingResourcesProvided: bool
    existingResources: ResourceCounts
    totalResources: ResourceCounts
    tier: str | None
    publishedResourceLimits: ResourceCounts | None
    withinPublishedResourceLimits: bool | None
    largestNamedValueCharacters: int
    largestDecodedBucketRecords: int
    fragmentSourceBytesPerCostCenter: int
    expandedFragmentBytesPerCostCenter: int
    existingPolicyBytes: int
    conservativeCombinedBytes: int
    withinConservative512KiBBudget: bool
    singleRemovalNamedValueWrites: int
    singleRemovalPolicySourceUnchanged: bool
    unchangedProjectionWrites: int
    freshnessOnlyWrites: int
    graphRemovalNamedValueWrites: int
    forecastSingleRemovalSubscriptionWrites: int
    forecastGraphRemovalSubscriptionWrites: int
    apimNativeContext: str
    apimPolicyExpressionExecution: str
    graphPaging: str
    gatewayLatency: str
    gatewayPropagation: str


@dataclass(frozen=True)
class Owner:
    object_id: UUID
    kind: Literal["u", "a"] = "u"

    def __post_init__(self) -> None:
        if not isinstance(self.object_id, UUID) or not self.object_id.int:
            raise ProbeError("An owner needs a nonempty UUID")
        if self.kind not in {"u", "a"}:
            raise ProbeError("An owner must be a user or an application")


@dataclass(frozen=True)
class Member:
    owner: Owner
    direct: bool = False
    graph: bool = True

    def __post_init__(self) -> None:
        if type(self.direct) is not bool or type(self.graph) is not bool:
            raise ProbeError("Membership sources must be booleans")
        if not self.direct and not self.graph:
            raise ProbeError("A member needs at least one eligibility source")

    @property
    def flags(self) -> int:
        return int(self.direct) | (int(self.graph) << 1) | (4 if self.owner.kind == "a" else 0)


@dataclass(frozen=True)
class Contract:
    group_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.group_id, UUID) or not self.group_id.int:
            raise ProbeError("The probe group needs a nonempty UUID")

    @property
    def prefix(self) -> str:
        return f"mosaic-cc-probe-v1-{self.group_id.hex}"

    @property
    def product_name(self) -> str:
        return f"{self.prefix}-product"

    @property
    def manifest_name(self) -> str:
        return f"{self.prefix}-manifest"

    def shard_name(self, bucket: int, bank: int) -> str:
        if not 0 <= bucket < 128 or bank not in {0, 1}:
            raise ProbeError("The shard address is outside the probe contract")
        return f"{self.prefix}-{bucket:02x}-{bank}"

    def subscription_name(self, owner: Owner) -> str:
        return f"{self.prefix}-key-{owner.kind}-{owner.object_id.hex}"

    def owner_from_native_context(self, subscription_id: str, product_id: str) -> Owner:
        if product_id != self.product_name:
            raise ProbeError("The native context is not scoped to the probe product")
        match = re.fullmatch(
            re.escape(f"{self.prefix}-key-") + r"([ua])-([0-9a-f]{32})", subscription_id
        )
        if match is None:
            raise ProbeError("The native subscription does not match the owner contract")
        kind: Literal["u", "a"] = "u" if match[1] == "u" else "a"
        return Owner(UUID(hex=match[2]), kind)


def _bucket_count(value: int) -> None:
    if value not in BUCKET_COUNTS:
        raise ProbeError("Use a supported power-of-two bucket count")


def _epoch(value: int) -> None:
    if type(value) is not int or not 0 <= value <= 9_999_999_999:
        raise ProbeError("Evidence time must be a nonnegative ten-digit Unix timestamp")


def bucket_for(owner: Owner, bucket_count: int) -> int:
    _bucket_count(bucket_count)
    return hashlib.sha256(owner.object_id.hex.encode("ascii")).digest()[0] & (bucket_count - 1)


def _records(members: Iterable[Member], bucket_count: int) -> list[bytes]:
    _bucket_count(bucket_count)
    owners: dict[UUID, tuple[Owner, int]] = {}
    for member in members:
        previous = owners.get(member.owner.object_id)
        if previous and previous[0].kind != member.owner.kind:
            raise ProbeError("One object cannot have conflicting principal kinds")
        owners[member.owner.object_id] = (
            member.owner,
            member.flags | (previous[1] if previous else 0),
        )
    buckets: list[list[bytes]] = [[] for _ in range(bucket_count)]
    for owner, flags in owners.values():
        # Guid.ToByteArray() uses this byte order in APIM's .NET policy expression.
        buckets[bucket_for(owner, bucket_count)].append(owner.object_id.bytes_le + bytes([flags]))
    return [b"".join(sorted(bucket)) for bucket in buckets]


def _shard_value(generation: int, records: bytes) -> str:
    if not 0 <= generation <= MAX_GENERATION:
        raise ProbeError("The shard generation is exhausted")
    value = f"1|{generation:016x}|{base64.b64encode(records).decode('ascii')}"
    if len(value) > NAMED_VALUE_LIMIT:
        raise ProbeError("A membership bucket exceeds the 4096-character named-value limit")
    return value


def _read_shard(value: str | None, generation: int) -> bytes:
    if value is None or len(value) > NAMED_VALUE_LIMIT:
        raise ProbeError("Membership data is missing or oversized")
    match = _SHARD.fullmatch(value)
    if match is None or int(match[1], 16) != generation:
        raise ProbeError("Membership data has an invalid version or generation")
    try:
        records = base64.b64decode(match[2], validate=True)
    except binascii.Error:
        raise ProbeError("Membership data has invalid base64") from None
    if base64.b64encode(records).decode("ascii") != match[2] or len(records) % RECORD_BYTES:
        raise ProbeError("Membership data has a noncanonical encoding or record length")
    previous = b""
    for offset in range(0, len(records), RECORD_BYTES):
        oid, flags = records[offset : offset + 16], records[offset + 16]
        if not any(oid) or oid <= previous or flags & ~7 or not flags & 3:
            raise ProbeError("Membership data has invalid, duplicated, or unordered records")
        previous = oid
    return records


def _manifest_value(expiry: int, pointers: Sequence[tuple[int, int]]) -> str:
    _epoch(expiry)
    value = f"1|{expiry:010d}|" + "".join(
        f"{bank}{generation:016x}" for bank, generation in pointers
    )
    if len(value) > NAMED_VALUE_LIMIT:
        raise ProbeError("The membership manifest exceeds the named-value limit")
    return value


@dataclass(frozen=True)
class Projection:
    contract: Contract
    bucket_count: int
    values: Mapping[str, str]

    def __post_init__(self) -> None:
        _bucket_count(self.bucket_count)

    def manifest(self) -> tuple[int, list[tuple[int, int]]]:
        value = self.values.get(self.contract.manifest_name, "")
        match = _MANIFEST.fullmatch(value) if len(value) <= NAMED_VALUE_LIMIT else None
        if match is None or len(match[2]) != 17 * self.bucket_count:
            raise ProbeError("The membership manifest is missing or invalid")
        pointers = [
            (int(match[2][offset]), int(match[2][offset + 1 : offset + 17], 16))
            for offset in range(0, len(match[2]), 17)
        ]
        return int(match[1]), pointers


def project(
    contract: Contract, members: Iterable[Member], *, bucket_count: int = 64, expiry: int
) -> Projection:
    records = _records(members, bucket_count)
    values = {
        contract.shard_name(bucket, bank): _shard_value(0, data)
        for bucket, data in enumerate(records)
        for bank in (0, 1)
    }
    values[contract.manifest_name] = _manifest_value(expiry, [(0, 0)] * bucket_count)
    return Projection(contract, bucket_count, values)


@dataclass(frozen=True)
class Update:
    projection: Projection
    # Staged data comes first; the manifest is always the last write.
    writes: tuple[tuple[str, str], ...]


def plan_update(current: Projection, members: Iterable[Member], *, expiry: int) -> Update:
    _, pointers = current.manifest()
    values = dict(current.values)
    writes: list[tuple[str, str]] = []
    for bucket, records in enumerate(_records(members, current.bucket_count)):
        bank, generation = pointers[bucket]
        old = _read_shard(values.get(current.contract.shard_name(bucket, bank)), generation)
        if records == old:
            continue
        name = current.contract.shard_name(bucket, 1 - bank)
        value = _shard_value(generation + 1, records)
        writes.append((name, value))
        values[name] = value
        pointers[bucket] = (1 - bank, generation + 1)
    manifest = _manifest_value(expiry, pointers)
    if manifest != values[current.contract.manifest_name]:
        writes.append((current.contract.manifest_name, manifest))
        values[current.contract.manifest_name] = manifest
    return Update(Projection(current.contract, current.bucket_count, values), tuple(writes))


def lookup(projection: Projection, owner: Owner, *, now: int) -> Decision:
    _epoch(now)
    try:
        expiry, pointers = projection.manifest()
        bucket = bucket_for(owner, projection.bucket_count)
        bank, generation = pointers[bucket]
        records = _read_shard(
            projection.values.get(projection.contract.shard_name(bucket, bank)), generation
        )
    except ProbeError:
        return Decision.INVALID
    for offset in range(0, len(records), RECORD_BYTES):
        oid, flags = records[offset : offset + 16], records[offset + 16]
        if oid == owner.object_id.bytes_le and bool(flags & 4) == (owner.kind == "a"):
            if flags & 1:
                return Decision.DIRECT
            return Decision.GRAPH if now < expiry else Decision.EXPIRED
    return Decision.ABSENT


def _expression(lines: Sequence[str]) -> str:
    return "@{\n" + "\n".join(lines) + "\n}"


def render_membership_fragment(contract: Contract, bucket_count: int) -> str:
    _bucket_count(bucket_count)
    lines = [
        f'var owner = context.Variables.GetValueOrDefault<string>("{OWNER_VARIABLE}", "");',
        'if (!System.Text.RegularExpressions.Regex.IsMatch(owner, @"\\A[ua]\\|[0-9a-f]{32}\\z"))'
        ' { return "absent"; }',
        'var oid = System.Guid.ParseExact(owner.Substring(2), "N");',
        'if (oid == System.Guid.Empty) { return "absent"; }',
        "var expected = oid.ToByteArray();",
        "int bucket;",
        "using (var hash = System.Security.Cryptography.SHA256.Create()) {",
        f"    bucket = hash.ComputeHash(System.Text.Encoding.UTF8.GetBytes(owner.Substring(2)))[0]"
        f" & {bucket_count - 1};",
        "}",
        f'var manifest = "{{{{{contract.manifest_name}}}}}";',
        f'if (manifest.Length > {NAMED_VALUE_LIMIT}) {{ return "invalid"; }}',
        "var parts = manifest.Split('|');",
        'if (parts.Length != 3 || parts[0] != "1" ||'
        ' !System.Text.RegularExpressions.Regex.IsMatch(parts[1], @"\\A[0-9]{10}\\z") ||',
        "    !System.Text.RegularExpressions.Regex.IsMatch(parts[2],"
        f' @"\\A(?:[01][0-9a-f]{{16}}){{{bucket_count}}}\\z")) {{ return "invalid"; }}',
        "var bank = parts[2][bucket * 17];",
        "var generation = parts[2].Substring(bucket * 17 + 1, 16);",
        "string shard;",
        "switch (bucket) {",
    ]
    for bucket in range(bucket_count):
        first, second = (contract.shard_name(bucket, bank) for bank in (0, 1))
        lines.append(
            f'case {bucket}: shard = bank == \'0\' ? "{{{{{first}}}}}" : "{{{{{second}}}}}"; break;'
        )
    lines += [
        'default: return "invalid";',
        "}",
        f'if (shard.Length > {NAMED_VALUE_LIMIT}) {{ return "invalid"; }}',
        "var data = shard.Split('|');",
        'if (data.Length != 3 || data[0] != "1" || data[1] != generation) { return "invalid"; }',
        "byte[] records;",
        "try { records = System.Convert.FromBase64String(data[2]); }",
        'catch (System.FormatException) { return "invalid"; }',
        "if (records.Length % 17 != 0 || System.Convert.ToBase64String(records) != data[2])"
        ' { return "invalid"; }',
        "int matched = 0;",
        "for (int offset = 0; offset < records.Length; offset += 17) {",
        "    int flags = records[offset + 16];",
        '    if ((flags & ~7) != 0 || (flags & 3) == 0) { return "invalid"; }',
        "    bool same = true;",
        "    bool nonzero = false;",
        "    int order = 0;",
        "    for (int j = 0; j < 16; j++) {",
        "        same = same && records[offset + j] == expected[j];",
        "        nonzero = nonzero || records[offset + j] != 0;",
        "        if (offset > 0 && order == 0) {",
        "            order = records[offset + j] - records[offset - 17 + j];",
        "        }",
        "    }",
        '    if (!nonzero || (offset > 0 && order <= 0)) { return "invalid"; }',
        "    if (same && ((flags & 4) != 0) == (owner[0] == 'a')) { matched = flags; }",
        "}",
        'if ((matched & 1) != 0) { return "direct"; }',
        'if ((matched & 2) == 0) { return "absent"; }',
        "var now = (long)(context.Timestamp -"
        " new System.DateTime(1970, 1, 1, 0, 0, 0, System.DateTimeKind.Utc)).TotalSeconds;",
        'return now < System.Convert.ToInt64(parts[1]) ? "graph" : "expired";',
    ]
    fragment = ET.Element("fragment")
    ET.SubElement(
        fragment, "set-variable", {"name": DECISION_VARIABLE, "value": _expression(lines)}
    )
    return ET.tostring(fragment, encoding="unicode")


def render_native_context_policy(contract: Contract, expected: Owner) -> str:
    """Diagnostic-only API policy: no token validation, model forwarding, or allow decision."""

    prefix = f"{contract.prefix}-key-"
    pattern = f"\\A{prefix}([ua])-([0-9a-f]{{32}})\\z"
    owner = _expression(
        [
            'if (context.Subscription == null || context.Product == null) { return ""; }',
            f'if (context.Product.Id != "{contract.product_name}") {{ return ""; }}',
            "var match = System.Text.RegularExpressions.Regex.Match(",
            f'    context.Subscription.Id ?? "", @"{pattern}");',
            'if (!match.Success) { return ""; }',
            'return match.Groups[1].Value + "|" + match.Groups[2].Value;',
        ]
    )
    policy = ET.Element("policies")
    inbound = ET.SubElement(policy, "inbound")
    ET.SubElement(inbound, "base")
    ET.SubElement(inbound, "set-variable", {"name": OWNER_VARIABLE, "value": owner})
    ET.SubElement(inbound, "include-fragment", {"fragment-id": contract.prefix})
    response = ET.SubElement(inbound, "return-response")
    ET.SubElement(response, "set-status", {"code": "200", "reason": "MOSAIC diagnostic probe"})
    header = ET.SubElement(
        response, "set-header", {"name": "Content-Type", "exists-action": "override"}
    )
    ET.SubElement(header, "value").text = "application/json"
    ET.SubElement(response, "set-body").text = _expression(
        [
            "return new Newtonsoft.Json.Linq.JObject(",
            '    new Newtonsoft.Json.Linq.JProperty("diagnosticOnly", true),',
            '    new Newtonsoft.Json.Linq.JProperty("nativeSubscriptionPresent",'
            " context.Subscription != null),",
            '    new Newtonsoft.Json.Linq.JProperty("nativeProductMatches",'
            f' context.Product != null && context.Product.Id == "{contract.product_name}"),',
            '    new Newtonsoft.Json.Linq.JProperty("ownerMatchesFixture",'
            f' context.Variables.GetValueOrDefault<string>("{OWNER_VARIABLE}", "")'
            f' == "{expected.kind}|{expected.object_id.hex}"),',
            '    new Newtonsoft.Json.Linq.JProperty("localMembershipDecision",'
            f' context.Variables.GetValueOrDefault<string>("{DECISION_VARIABLE}", "invalid"))',
            ").ToString(Newtonsoft.Json.Formatting.None);",
        ]
    )
    for section in ("backend", "outbound", "on-error"):
        ET.SubElement(ET.SubElement(policy, section), "base")
    return ET.tostring(policy, encoding="unicode")


def fixture_contract() -> Contract:
    return Contract(uuid5(NAMESPACE_OID, "mosaic-apim:cost-center-probe:group"))


def fixture_members(count: int) -> tuple[Member, ...]:
    if not 1 <= count <= 10_000:
        raise ProbeError("Fixture population must be between 1 and 10000")
    return tuple(
        Member(
            Owner(
                uuid5(NAMESPACE_OID, f"mosaic-apim:cost-center-probe:member:{index}"),
                "a" if index % 10 == 9 else "u",
            ),
            direct=index < 4990 and index % 10 == 0,
            graph=not (index < 4990 and index % 10 == 0),
        )
        for index in range(count)
    )


def expanded_fragment(fragment: str, values: Mapping[str, str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        if match[1] not in values:
            raise ProbeError("The probe fragment references missing named-value data")
        return values[match[1]]

    return _REFERENCE.sub(substitute, fragment)


def render_reference_host(projection: Projection) -> str:
    fragment = render_membership_fragment(projection.contract, projection.bucket_count)
    expanded = ET.fromstring(expanded_fragment(fragment, projection.values))
    body = expanded[0].attrib["value"].removeprefix("@{").removesuffix("}")
    return (
        "public sealed class MosaicProbeVariables :"
        " System.Collections.Generic.Dictionary<string, object> {\n"
        "    public T GetValueOrDefault<T>(string key, T fallback) {\n"
        "        object value;\n"
        "        return TryGetValue(key, out value) ? (T)value : fallback;\n"
        "    }\n"
        "}\n"
        "public sealed class MosaicProbeContext {\n"
        "    public MosaicProbeVariables Variables = new MosaicProbeVariables();\n"
        "    public System.DateTime Timestamp;\n"
        "}\n"
        "public static class MosaicCostCenterProbe {\n"
        "    public static string Evaluate(string trustedOwner, long unixTime) {\n"
        "        var context = new MosaicProbeContext();\n"
        "        context.Timestamp ="
        " System.DateTimeOffset.FromUnixTimeSeconds(unixTime).UtcDateTime;\n"
        f'        context.Variables["{OWNER_VARIABLE}"] = trustedOwner;\n'
        f"{body}\n"
        "    }\n"
        "}\n"
    )


def capacity_report(
    population: int,
    *,
    bucket_count: int,
    now: int,
    centers: int = 1,
    existing_policy_bytes: int = 0,
    inventory: ExistingResources | None = None,
) -> CapacityReport:
    if (
        type(centers) is not int
        or centers < 1
        or type(existing_policy_bytes) is not int
        or existing_policy_bytes < 0
    ):
        raise ProbeError("Cost-center count must be positive and policy size nonnegative")
    contract, members = fixture_contract(), fixture_members(population)
    projection = project(contract, members, bucket_count=bucket_count, expiry=now + 300)
    fragment = render_membership_fragment(contract, bucket_count)
    after = plan_update(projection, members[1:], expiry=now + 300)
    graph_removed = plan_update(
        projection,
        [Member(member.owner, direct=True, graph=False) for member in members if member.direct],
        expiry=0,
    )
    raw_bytes = len(fragment.encode())
    expanded_bytes = len(expanded_fragment(fragment, projection.values).encode())
    existing = inventory or ExistingResources(0, 0, 0)
    total: ResourceCounts = {
        "subscriptions": existing.subscriptions + population * centers,
        "products": existing.products + centers,
        "namedValues": existing.named_values + len(projection.values) * centers,
    }
    limits = PUBLISHED_LIMITS[existing.tier] if existing.tier else None
    fits_resources = (
        total["subscriptions"] <= limits["subscriptions"]
        and total["products"] <= limits["products"]
        and total["namedValues"] <= limits["namedValues"]
        if limits
        else None
    )
    return {
        "populationPerCostCenter": population,
        "costCentersOnSameOffering": centers,
        "bucketsPerCostCenter": bucket_count,
        "explicitReferencesInFixture": sum(member.direct for member in members)
        + int(any(member.graph for member in members)),
        "forecastSubscriptions": population * centers,
        "forecastProducts": centers,
        "forecastNamedValues": len(projection.values) * centers,
        "existingResourcesProvided": inventory is not None,
        "existingResources": existing.counts(),
        "totalResources": total,
        "tier": existing.tier,
        "publishedResourceLimits": limits,
        "withinPublishedResourceLimits": fits_resources,
        "largestNamedValueCharacters": max(map(len, projection.values.values())),
        "largestDecodedBucketRecords": max(
            len(records) // RECORD_BYTES for records in _records(members, bucket_count)
        ),
        "fragmentSourceBytesPerCostCenter": raw_bytes,
        "expandedFragmentBytesPerCostCenter": expanded_bytes,
        "existingPolicyBytes": existing_policy_bytes,
        "conservativeCombinedBytes": expanded_bytes * centers + existing_policy_bytes,
        "withinConservative512KiBBudget": (
            expanded_bytes * centers + existing_policy_bytes <= FRAGMENT_LIMIT
        ),
        "singleRemovalNamedValueWrites": len(after.writes),
        "singleRemovalPolicySourceUnchanged": (
            render_membership_fragment(after.projection.contract, bucket_count) == fragment
        ),
        "unchangedProjectionWrites": len(plan_update(projection, members, expiry=now + 300).writes),
        "freshnessOnlyWrites": len(plan_update(projection, members, expiry=now + 600).writes),
        "graphRemovalNamedValueWrites": len(graph_removed.writes),
        "forecastSingleRemovalSubscriptionWrites": 1,
        "forecastGraphRemovalSubscriptionWrites": sum(
            member.graph and not member.direct for member in members
        ),
        "apimNativeContext": "NOT_RUN",
        "apimPolicyExpressionExecution": "NOT_RUN",
        "graphPaging": "NOT_RUN",
        "gatewayLatency": "NOT_MEASURED",
        "gatewayPropagation": "NOT_MEASURED",
    }


def write_bundle(output: Path, *, population: int, bucket_count: int, now: int) -> None:
    contract, members = fixture_contract(), fixture_members(population)
    projection = project(contract, members, bucket_count=bucket_count, expiry=now + 300)
    cases = [
        {
            "owner": f"{member.owner.kind}|{member.owner.object_id.hex}",
            "unixTime": when,
            "expected": lookup(projection, member.owner, now=when),
        }
        for member in members
        for when in (now, now + 300)
    ]
    cases += [
        {"owner": "", "unixTime": now, "expected": Decision.ABSENT},
        {
            "owner": f"u|{members[0].owner.object_id.hex}\n",
            "unixTime": now,
            "expected": Decision.ABSENT,
        },
        {
            "owner": f"a|{members[0].owner.object_id.hex}",
            "unixTime": now,
            "expected": Decision.ABSENT,
        },
    ]
    payloads = {
        "membership-fragment.xml": render_membership_fragment(contract, bucket_count),
        "native-context-policy.xml": render_native_context_policy(contract, members[0].owner),
        "named-values.json": json.dumps(dict(projection.values), indent=2, sort_keys=True),
        "reference-host.cs": render_reference_host(projection),
        "reference-cases.json": json.dumps(cases, indent=2),
        "native-contract.json": json.dumps(
            {
                "diagnosticOnly": True,
                "groupIsFictional": True,
                "productName": contract.product_name,
                "fragmentName": contract.prefix,
                "subscriptionNames": [
                    contract.subscription_name(member.owner) for member in members[:2]
                ],
                "target": None,
                "createsAzureResources": False,
                "validatesEntraTokens": False,
                "containsSubscriptionKeyValues": False,
            },
            indent=2,
        ),
    }
    if any((output / name).exists() for name in payloads):
        raise ProbeError(
            "Use a fresh output directory; existing probe artifacts are not overwritten"
        )
    output.mkdir(parents=True, exist_ok=True)
    for name, content in payloads.items():
        (output / name).write_text(content + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--population", type=int, action="append")
    parser.add_argument("--bucket-count", type=int, choices=BUCKET_COUNTS, default=64)
    parser.add_argument("--centers", type=int, default=1)
    parser.add_argument("--existing-policy-bytes", type=int, default=0)
    parser.add_argument("--tier", choices=tuple(PUBLISHED_LIMITS))
    parser.add_argument("--existing-subscriptions", type=int)
    parser.add_argument("--existing-products", type=int)
    parser.add_argument("--existing-named-values", type=int)
    parser.add_argument(
        "--output", type=Path, help="Fresh directory for the largest fixture's bundle"
    )
    args = parser.parse_args(argv)
    now = int(time.time())
    populations = args.population or list(DEFAULT_POPULATIONS)
    try:
        inventory = None
        counts = (args.existing_subscriptions, args.existing_products, args.existing_named_values)
        if args.tier is not None or any(value is not None for value in counts):
            if any(value is None for value in counts):
                raise ProbeError("Resource checks require all three existing resource counts")
            inventory = ExistingResources(
                args.existing_subscriptions,
                args.existing_products,
                args.existing_named_values,
                args.tier,
            )
        reports = [
            capacity_report(
                population,
                bucket_count=args.bucket_count,
                now=now,
                centers=args.centers,
                existing_policy_bytes=args.existing_policy_bytes,
                inventory=inventory,
            )
            for population in populations
        ]
        if args.output:
            if args.centers != 1:
                raise ProbeError("A native diagnostic bundle represents exactly one cost center")
            write_bundle(
                args.output, population=max(populations), bucket_count=args.bucket_count, now=now
            )
    except (ProbeError, OSError) as error:
        parser.error(str(error))
    print(
        json.dumps(
            {"mode": "offline", "feasibilityGate": "NOT_PASSED", "reports": reports}, indent=2
        )
    )
    return (
        0
        if all(
            report["withinConservative512KiBBudget"]
            and report["withinPublishedResourceLimits"] is not False
            for report in reports
        )
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
