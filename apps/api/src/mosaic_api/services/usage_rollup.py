"""Rolls API Management telemetry up into the ``usage-rollups`` container. See ADR 0019.

A background loop in the API process. Each cycle it:

1. copies every grant's gateway attribution into the attribution registry, so a key keeps
   resolving after its grant is revoked;
2. for each gateway with governed APIs, takes a lease, queries Log Analytics for today and
   yesterday, plus a slice of any backfill, and folds the rows into facts and daily summaries;
3. writes only the items whose content changed, deletes the ones that disappeared, and re-folds
   the months those days fall in.

A day is always rolled up whole from complete query results. A query that fails leaves the day as
it was, and the next cycle tries again. Every item has a deterministic ID, so two overlapping runs
write the same documents; the lease only saves the duplicate queries.
"""

from __future__ import annotations

import asyncio
import time as monotonic
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from uuid import uuid4

import structlog

from mosaic_api.domain import (
    AuditEvent,
    EntitlementResource,
    EntitlementResourceKind,
    EntitlementSubject,
    EntitlementSubjectKind,
    Gateway,
    Group,
    ManagementMode,
    McpServer,
    ModelApi,
    Principal,
    new_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, DomainError, NotFoundError, ValidationError
from mosaic_api.integrations.loganalytics import (
    DEPLOYMENT_KEY,
    LogQueryAccessError,
    LogQueryTooLargeError,
    LogsQuery,
    QueryWindow,
    Row,
    calls_query,
    denials_query,
    deployment_peaks_query,
    peaks_query,
)
from mosaic_api.repositories import (
    CostCenterRepository,
    DirectoryRepository,
    EntitlementRepository,
    GatewayRepository,
    UsageRollupRepository,
)
from mosaic_api.services.cost_centers import load_book
from mosaic_api.services.directory import Actor
from mosaic_api.services.telemetry import (
    ROLLUP_ACTOR,
    TelemetryService,
    diagnostics_error,
    governed_apis,
)
from mosaic_api.usage_telemetry import (
    KEEP_FOREVER,
    LATENCY_BUCKET_COUNT,
    SUMMARY_DIMENSIONS,
    SUMMARY_SHARD_SIZE,
    AttributionRecord,
    RolledUpApi,
    SummaryDimension,
    SummaryPeriod,
    UsageBreakdown,
    UsageFact,
    UsageHour,
    UsageLink,
    UsageMetrics,
    UsageRollupState,
    UsageSummary,
    UsageSummaryEntry,
    attribution_record_id,
    content_hash,
    day_bucket,
    iso_day,
    month_start,
    period_bucket,
    subscription_attribution_key,
    usage_fact_id,
    usage_rollup_state_id,
    usage_summary_id,
)

logger = structlog.get_logger()

TRACE_VERSION = "1"
LEASE_SECONDS = 900
BACKFILL_DAYS_PER_CYCLE = 7
BACKFILL_PAUSE_SECONDS = 5.0
MAX_BACKFILL_DAYS = 730
REFRESH_COOLDOWN = timedelta(minutes=1)
_HASH_EXCLUDE = {"created_at", "updated_at", "content_hash", "etag"}
_RECORD_COMPARE_EXCLUDE = {"created_at", "updated_at", "etag", "recorded_at"}

RollupItem = UsageFact | UsageSummary
BreakdownKey = tuple[str, str | None, str | None, str | None]


class TooManyRefreshesError(ConflictError):
    status_code = 429
    code = "telemetry_refresh_throttled"


def _int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value))
        except ValueError:
            return 0
    return 0


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _hashed[ItemT: (UsageFact, UsageSummary)](item: ItemT) -> ItemT:
    payload = item.model_dump(mode="json", exclude=_HASH_EXCLUDE)
    return item.model_copy(update={"content_hash": content_hash(payload)})


def _ceil_hour(value: datetime) -> datetime:
    floor = value.replace(minute=0, second=0, microsecond=0)
    return floor if floor == value else floor + timedelta(hours=1)


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _next_month(first: date) -> date:
    return date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)


def call_metrics(row: Row) -> UsageMetrics:
    """One row of :func:`calls_query` as metrics."""

    last_seen = row.get("lastSeen")
    return UsageMetrics(
        requests=_int(row.get("requests")),
        prompt_tokens=_int(row.get("promptTokens")),
        completion_tokens=_int(row.get("completionTokens")),
        total_tokens=_int(row.get("totalTokens")),
        metered_requests=_int(row.get("metered")),
        ok=_int(row.get("ok")),
        throttled=_int(row.get("throttled")),
        quota=_int(row.get("quota")),
        client_errors=_int(row.get("clientErrors")),
        server_errors=_int(row.get("serverErrors")),
        backend_throttled=_int(row.get("backendThrottled")),
        key_requests=_int(row.get("keyRequests")),
        total_time_ms=_int(row.get("totalTime")),
        backend_time_ms=_int(row.get("backendTime")),
        latency=[_int(row.get(f"l{index}")) for index in range(LATENCY_BUCKET_COUNT)],
        last_seen=last_seen if isinstance(last_seen, datetime) else None,
    )


def _add_hour(hours: dict[int, UsageHour], hour: int, metrics: UsageMetrics) -> None:
    if not 0 <= hour <= 23:
        return
    slot = hours.setdefault(hour, UsageHour(hour=hour))
    slot.requests += metrics.requests
    slot.total_tokens += metrics.total_tokens
    slot.throttled += metrics.throttled
    slot.quota += metrics.quota
    slot.denied += metrics.denied
    slot.errors += metrics.errors


@dataclass
class _Fact:
    link: UsageLink
    link_key: str
    caller: str | None
    record: AttributionRecord | None
    member: bool
    metrics: UsageMetrics = field(default_factory=UsageMetrics)
    hours: dict[int, UsageHour] = field(default_factory=dict)
    breakdown: dict[BreakdownKey, UsageMetrics] = field(default_factory=dict)


class LinkResolver:
    """Which grant a call belongs to: its attribution trace first, then its subscription.

    A trace links a call even when MOSAIC hasn't registered its grant key yet, so the call counts
    against the grant as soon as the registry catches up. A subscription links only once
    registered, because API Management subscriptions MOSAIC didn't create belong to nobody here.
    """

    def __init__(self, gateway_id: str, registry: Mapping[tuple[str, str], AttributionRecord]):
        self._gateway_id = gateway_id
        self._registry = registry

    def link(
        self, version: str, grant: str, subscription: str
    ) -> tuple[UsageLink, str, AttributionRecord | None] | None:
        if version == TRACE_VERSION and grant:
            return "trace", grant, self._registry.get(("grant", grant))
        if subscription:
            key = subscription_attribution_key(self._gateway_id, subscription)
            record = self._registry.get(("subscription", key))
            if record is not None:
                return "subscription", key, record
        return None

    @staticmethod
    def caller(member: str, record: AttributionRecord | None) -> str | None:
        """The member the trace validated, else the grant's one subject when it has one."""

        if member:
            return member
        if record is not None and not record.per_member:
            return record.subject_object_id
        return None

    def peak_fact_key(self, link: str) -> tuple[UsageLink, str, str | None] | None:
        kind, _, rest = link.partition(":")
        if kind == "t":
            grant, _, member = rest.partition("|")
            if not grant:
                return None
            return "trace", grant, self.caller(member, self._registry.get(("grant", grant)))
        if kind == "s" and rest:
            key = subscription_attribution_key(self._gateway_id, rest)
            record = self._registry.get(("subscription", key))
            if record is None:
                return None
            return "subscription", key, self.caller("", record)
        return None


class DayFold:
    """Folds one gateway-day of query rows into facts and summary entries."""

    def __init__(self, resolver: LinkResolver, apis: Sequence[RolledUpApi]) -> None:
        self._resolver = resolver
        self._apis = {api.api_name: api for api in apis}
        self.facts: dict[tuple[UsageLink, str, str | None], _Fact] = {}
        self.entries: dict[SummaryDimension, dict[str, UsageSummaryEntry]] = {}
        self.total_hours: dict[int, UsageHour] = {}
        self.api_hours: dict[str, dict[int, UsageHour]] = {}
        self.rows = 0
        self.unknown_versions = 0

    def _entry(self, dimension: SummaryDimension, key: str) -> UsageSummaryEntry:
        return self.entries.setdefault(dimension, {}).setdefault(key, UsageSummaryEntry(key=key))

    def add_calls(self, rows: Iterable[Row]) -> None:
        for row in rows:
            self.rows += 1
            metrics = call_metrics(row)
            hour = _int(row.get("hour"))
            version = _text(row.get("v"))
            grant = _text(row.get("g")).casefold()
            member = _text(row.get("m")).casefold()
            client_app = _text(row.get("a")).casefold()
            api = _text(row.get("api")).casefold()
            subscription = _text(row.get("subscription")).casefold()
            deployment = _text(row.get("deployment")) or None
            model = _text(row.get("model")) or None
            if version and version != TRACE_VERSION:
                self.unknown_versions += metrics.requests
            application: str | None = None
            linked = self._resolver.link(version, grant, subscription)
            if linked is None:
                self._entry("unattributed", f"{api}|{subscription}").metrics.add(metrics)
            else:
                link, link_key, record = linked
                caller = self._resolver.caller(member, record)
                if (
                    record is not None
                    and not member
                    and record.subject.kind == EntitlementSubjectKind.APPLICATION
                ):
                    application = record.subject_object_id
                fact = self.facts.get((link, link_key, caller))
                if fact is None:
                    fact = _Fact(
                        link=link,
                        link_key=link_key,
                        caller=caller,
                        record=record,
                        member=bool(member),
                    )
                    self.facts[(link, link_key, caller)] = fact
                fact.metrics.add(metrics)
                _add_hour(fact.hours, hour, metrics)
                breakdown_key = (api, deployment, model, client_app or None)
                fact.breakdown.setdefault(breakdown_key, UsageMetrics()).add(metrics)
            self._entry("total", "").metrics.add(metrics)
            _add_hour(self.total_hours, hour, metrics)
            self._entry("api", api).metrics.add(metrics)
            _add_hour(self.api_hours.setdefault(api, {}), hour, metrics)
            if client_app:
                client = self._entry("clientApp", f"{client_app}|{api}")
                client.metrics.add(metrics)
                if application:
                    client.application_object_id = application
            known = self._apis.get(api)
            deployment_key = known.deployment_key if known else None
            if deployment_key:
                self._entry("deployment", deployment_key).metrics.add(metrics)
            if model:
                self._entry("model", f"{model.casefold()}|{api}").metrics.add(metrics)

    def add_peaks(self, rows: Iterable[Row]) -> None:
        for row in rows:
            key = self._resolver.peak_fact_key(_text(row.get("link")))
            fact = self.facts.get(key) if key else None
            if fact is None:
                # Peaks and calls are separate queries, so a call ingested between them has a
                # peak but no fact yet. The next cycle counts both.
                continue
            hour = _int(row.get("hour"))
            tokens = _int(row.get("peakTokens"))
            requests = _int(row.get("peakRequests"))
            slot = fact.hours.get(hour)
            if slot is not None:
                slot.peak_minute_tokens = max(slot.peak_minute_tokens, tokens)
                slot.peak_minute_requests = max(slot.peak_minute_requests, requests)
            fact.metrics.peak_minute_tokens = max(fact.metrics.peak_minute_tokens, tokens)
            fact.metrics.peak_minute_requests = max(fact.metrics.peak_minute_requests, requests)

    def add_deployment_peaks(self, rows: Iterable[Row]) -> None:
        for row in rows:
            key = _text(row.get("deploymentKey"))
            hour = _int(row.get("hour"))
            if not key or not 0 <= hour <= 23:
                continue
            entry = self._entry("deployment", key)
            tokens = _int(row.get("peakTokens"))
            requests = _int(row.get("peakRequests"))
            peaks = list(entry.hourly_peak_tokens or [0] * 24)
            peaks[hour] = max(peaks[hour], tokens)
            entry.hourly_peak_tokens = peaks
            entry.metrics.peak_minute_tokens = max(entry.metrics.peak_minute_tokens, tokens)
            entry.metrics.peak_minute_requests = max(entry.metrics.peak_minute_requests, requests)

    def add_denials(self, rows: Iterable[Row]) -> None:
        for row in rows:
            self.rows += 1
            count = _int(row.get("requests"))
            last_seen = row.get("lastSeen")
            metrics = UsageMetrics(
                requests=count,
                denied=count,
                last_seen=last_seen if isinstance(last_seen, datetime) else None,
            )
            reason = _text(row.get("reason")) or "unknown"
            caller = _text(row.get("o")).casefold()
            client_app = _text(row.get("a")).casefold()
            api = _text(row.get("api")).casefold()
            self._entry("denial", f"{reason}|{caller}|{client_app}|{api}").metrics.add(metrics)
            hour = _int(row.get("hour"))
            self._entry("total", "").metrics.add(metrics)
            self._entry("api", api).metrics.add(metrics)
            _add_hour(self.total_hours, hour, metrics)
            _add_hour(self.api_hours.setdefault(api, {}), hour, metrics)

    def _publication_for(self, fact: _Fact) -> str | None:
        if fact.record is not None and fact.record.publication_id:
            return fact.record.publication_id
        apis = {api for api, _, _, _ in fact.breakdown}
        if len(apis) == 1:
            known = self._apis.get(next(iter(apis)))
            return known.publication_id if known else None
        return None

    def items(self, tenant_id: str, day: str, gateway_id: str, ttl: int) -> list[RollupItem]:
        for fact in self.facts.values():
            grant_key = f"{fact.link}:{fact.link_key}"
            self._entry("grant", grant_key).metrics.add(fact.metrics)
            self._entry("grantCaller", f"{grant_key}|{fact.caller or ''}").metrics.add(
                fact.metrics
            )
            if fact.caller:
                self._entry("caller", fact.caller).metrics.add(fact.metrics)
        if self.total_hours:
            self._entry("total", "").hours = [
                self.total_hours[hour] for hour in sorted(self.total_hours)
            ]
        for api, hours in self.api_hours.items():
            self._entry("api", api).hours = [hours[hour] for hour in sorted(hours)]
        items: list[RollupItem] = []
        for fact in self.facts.values():
            record = fact.record
            items.append(
                _hashed(
                    UsageFact(
                        id=usage_fact_id(
                            tenant_id, day, gateway_id, fact.link, fact.link_key, fact.caller
                        ),
                        tenant_id=tenant_id,
                        day=day,
                        bucket=day_bucket(day),
                        gateway_id=gateway_id,
                        link=fact.link,
                        link_key=fact.link_key,
                        caller_object_id=fact.caller,
                        entitlement_id=record.entitlement_id if record else None,
                        publication_id=self._publication_for(fact),
                        per_member=record.per_member if record else fact.member,
                        metrics=fact.metrics,
                        hours=[fact.hours[hour] for hour in sorted(fact.hours)],
                        breakdown=[
                            UsageBreakdown(
                                api_name=api,
                                deployment=deployment,
                                model=model,
                                client_app_id=client_app,
                                metrics=metrics,
                            )
                            for (api, deployment, model, client_app), metrics in sorted(
                                fact.breakdown.items(),
                                key=lambda item: tuple(part or "" for part in item[0]),
                            )
                        ],
                        ttl=ttl,
                    )
                )
            )
        items.extend(summary_items(tenant_id, "day", day, gateway_id, self.entries, ttl=ttl))
        return items


def summary_items(
    tenant_id: str,
    period: SummaryPeriod,
    period_start: str,
    gateway_id: str,
    entries: Mapping[SummaryDimension, Mapping[str, UsageSummaryEntry]],
    *,
    ttl: int,
) -> list[RollupItem]:
    items: list[RollupItem] = []
    for dimension in SUMMARY_DIMENSIONS:
        ordered = sorted(entries.get(dimension, {}).values(), key=lambda entry: entry.key)
        for shard, offset in enumerate(range(0, len(ordered), SUMMARY_SHARD_SIZE)):
            items.append(
                _hashed(
                    UsageSummary(
                        id=usage_summary_id(
                            tenant_id, period, period_start, gateway_id, dimension, shard
                        ),
                        tenant_id=tenant_id,
                        period=period,
                        period_start=period_start,
                        bucket=period_bucket(period, period_start),
                        gateway_id=gateway_id,
                        dimension=dimension,
                        shard=shard,
                        entries=ordered[offset : offset + SUMMARY_SHARD_SIZE],
                        ttl=ttl,
                    )
                )
            )
    return items


def fold_month(
    dailies: Iterable[UsageSummary],
) -> dict[SummaryDimension, dict[str, UsageSummaryEntry]]:
    """Daily summaries folded into one month's entries. Hours and hourly peaks stay daily."""

    folded: dict[SummaryDimension, dict[str, UsageSummaryEntry]] = {}
    for summary in dailies:
        by_key = folded.setdefault(summary.dimension, {})
        for entry in summary.entries:
            month = by_key.setdefault(entry.key, UsageSummaryEntry(key=entry.key))
            month.metrics.add(entry.metrics)
            if entry.application_object_id:
                month.application_object_id = entry.application_object_id
    return folded


def merge_apis(
    remembered: Sequence[RolledUpApi], current: Sequence[RolledUpApi], now: datetime
) -> list[RolledUpApi]:
    """Current APIs, plus remembered ones marked removed so their past calls keep a name."""

    merged: dict[str, RolledUpApi] = {}
    current_by_name = {api.api_name: api for api in current}
    for api in remembered:
        fresh = current_by_name.get(api.api_name)
        if fresh is None:
            merged[api.api_name] = (
                api if api.removed_at else api.model_copy(update={"removed_at": now})
            )
        else:
            merged[api.api_name] = fresh.model_copy(
                update={"first_seen_at": api.first_seen_at, "removed_at": None}
            )
    for api in current:
        merged.setdefault(api.api_name, api)
    return [merged[name] for name in sorted(merged)]


# The state's fields that only a rollup run writes. Administrators' refresh and backfill requests,
# and enabling telemetry, write the others, and can do so while a run is going.
_RUN_FIELDS = (
    "apis",
    "last_run_at",
    "last_success_at",
    "last_duration_ms",
    "queried_through",
    "data_available_from",
    "last_error",
    "last_error_at",
    "last_rows",
    "last_written",
    "unknown_trace_versions",
)
_BACKFILL_FIELDS = ("backfill_from", "backfill_next", "backfill_status")
_DIAGNOSTIC_FIELDS = ("instrumented_apis", "diagnostics_error")


def _fields(state: UsageRollupState, names: Sequence[str]) -> dict[str, Any]:
    return {name: getattr(state, name) for name in names}


def _backfilling(state: UsageRollupState) -> bool:
    return (
        state.backfill_status == "running"
        and state.backfill_from is not None
        and state.backfill_next is not None
    )


def merge_run(
    current: UsageRollupState, *, before: UsageRollupState, run: UsageRollupState
) -> UsageRollupState:
    """A run's results on top of the state as saved now.

    ``before`` is the state the run read, and ``run`` the state it arrived at. The run keeps
    whatever was saved meanwhile: a refresh, a backfill, which it extends with any days it still
    had to read, and the outcome of enabling telemetry, to which it adds the APIs it instrumented.
    """

    update = _fields(run, _RUN_FIELDS)
    if _fields(current, _DIAGNOSTIC_FIELDS) == _fields(before, _DIAGNOSTIC_FIELDS):
        update |= _fields(run, _DIAGNOSTIC_FIELDS)
    else:
        added = set(run.instrumented_apis) - set(before.instrumented_apis)
        update["instrumented_apis"] = sorted(set(current.instrumented_apis) | added)
    if _fields(current, _BACKFILL_FIELDS) == _fields(before, _BACKFILL_FIELDS):
        update |= _fields(run, _BACKFILL_FIELDS)
    elif _backfilling(run):
        if _backfilling(current):
            update |= {
                "backfill_from": min(current.backfill_from or "", run.backfill_from or ""),
                "backfill_next": max(current.backfill_next or "", run.backfill_next or ""),
            }
        else:
            update |= _fields(run, _BACKFILL_FIELDS)
    return current.model_copy(update=update)


def _subject_identity(
    subject: EntitlementSubject,
    principals: Mapping[str, Principal],
    groups: Mapping[str, Group],
) -> tuple[str | None, str | None]:
    if subject.kind == EntitlementSubjectKind.GROUP:
        group = groups.get(subject.id)
        return None, group.name if group else None
    principal = principals.get(subject.id)
    if principal is None:
        return None, None
    object_id = None if subject.kind == EntitlementSubjectKind.SECURITY_GROUP else (
        principal.object_id.casefold()
    )
    return object_id, principal.label or principal.detail


def _resource_identity(
    resource: EntitlementResource,
    model_apis: Mapping[str, ModelApi],
    mcp_servers: Mapping[str, McpServer],
) -> tuple[str | None, Literal["model", "mcp"] | None, str | None]:
    if resource.kind == EntitlementResourceKind.MODEL_API:
        model_api = model_apis.get(resource.id)
        if model_api is not None:
            return model_api.publication_id, "model", model_api.display_name
        return None, "model", None
    if resource.kind == EntitlementResourceKind.MCP_SERVER:
        server = mcp_servers.get(resource.id)
        if server is not None:
            return server.publication_id, "mcp", server.display_name
        return None, "mcp", None
    return None, None, resource.id


def _record_changed(current: AttributionRecord, desired: AttributionRecord) -> bool:
    return current.model_dump(mode="json", exclude=_RECORD_COMPARE_EXCLUDE) != desired.model_dump(
        mode="json", exclude=_RECORD_COMPARE_EXCLUDE
    )


@dataclass
class _GatewayRun:
    state: UsageRollupState
    busy: bool


class UsageRollupService:
    def __init__(
        self,
        repository: UsageRollupRepository,
        *,
        gateway_repository: GatewayRepository,
        entitlement_repository: EntitlementRepository,
        directory_repository: DirectoryRepository,
        logs: LogsQuery,
        telemetry: TelemetryService | None,
        tenant_id: str,
        interval_seconds: float = 900,
        retention_days: int = 400,
        backfill_max_days: int = 90,
        clock: Callable[[], datetime] = utc_now,
        owner_id: str | None = None,
        cost_center_repository: CostCenterRepository | None = None,
    ) -> None:
        self._cost_centers = cost_center_repository
        self._repository = repository
        self._gateways = gateway_repository
        self._entitlements = entitlement_repository
        self._directory = directory_repository
        self._logs = logs
        self._telemetry = telemetry
        self._tenant_id = tenant_id
        self._interval = interval_seconds
        self._retention_seconds = retention_days * 86_400
        self._backfill_max_days = backfill_max_days
        self._clock = clock
        self._owner = owner_id or f"usage-rollup-{uuid4().hex}"
        self._wake = asyncio.Event()
        self._cycle = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._last_tenant_refresh: datetime | None = None
        self._listeners: list[Callable[[], None]] = []

    # -- lifecycle -------------------------------------------------------------------------

    def add_listener(self, listener: Callable[[], None]) -> None:
        """Call ``listener`` after each cycle, such as to judge budgets against the new figures."""

        self._listeners.append(listener)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="usage-rollup")

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            busy = False
            try:
                busy = await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("usage_rollup_cycle_failed")
            for listener in self._listeners:
                try:
                    listener()
                except Exception:
                    logger.exception("usage_rollup_listener_failed")
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=BACKFILL_PAUSE_SECONDS if busy else self._interval
                )
            except TimeoutError:
                pass

    # -- requests from administrators ------------------------------------------------------

    async def _gateway(self, actor: Actor, gateway_id: str) -> Gateway:
        gateway = await self._gateways.get_gateway(actor.tenant_id, gateway_id)
        if gateway is None:
            raise NotFoundError("Gateway not found", details={"gatewayId": gateway_id})
        return gateway

    async def request_refresh(self, actor: Actor, gateway_id: str | None = None) -> None:
        """Run a cycle now. Once a minute per gateway, or for the tenant, is plenty."""

        now = self._clock()
        if gateway_id is None:
            if self._last_tenant_refresh and now - self._last_tenant_refresh < REFRESH_COOLDOWN:
                raise TooManyRefreshesError("Usage was refreshed less than a minute ago")
            self._last_tenant_refresh = now
            self.wake()
            return
        gateway = await self._gateway(actor, gateway_id)

        def refresh(state: UsageRollupState) -> UsageRollupState:
            if state.refresh_requested_at and now - state.refresh_requested_at < REFRESH_COOLDOWN:
                raise TooManyRefreshesError("This gateway was refreshed less than a minute ago")
            return state.model_copy(update={"refresh_requested_at": now})

        await self._repository.update_rollup_state(gateway.tenant_id, gateway.id, refresh)
        self.wake()

    async def request_backfill(
        self, actor: Actor, gateway_id: str, days: int | None = None
    ) -> UsageRollupState:
        """Re-read up to ``days`` of history from Log Analytics, a slice each cycle. Audited."""

        span = days or self._backfill_max_days
        if not 1 <= span <= MAX_BACKFILL_DAYS:
            raise ValidationError(
                f"A backfill covers 1 to {MAX_BACKFILL_DAYS} days", details={"days": span}
            )
        gateway = await self._gateway(actor, gateway_id)
        today = self._clock().astimezone(UTC).date()
        saved = await self._repository.update_rollup_state(
            gateway.tenant_id,
            gateway.id,
            lambda state: self._with_backfill(state, today, span),
        )
        await self._entitlements.record_audit(
            AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                action="gateway.telemetryBackfillRequested",
                resource_type="gateway",
                resource_id=gateway.id,
                actor_object_id=actor.object_id,
                details={"days": span, "from": saved.backfill_from},
            )
        )
        self.wake()
        return saved

    @staticmethod
    def _with_backfill(state: UsageRollupState, today: date, days: int) -> UsageRollupState:
        """Start a backfill of ``days``, newest first. One still going keeps its older days."""

        first = today - timedelta(days=days - 1)
        if _backfilling(state):
            first = min(first, date.fromisoformat(state.backfill_from or ""))
        following = today - timedelta(days=2)
        running = following >= first
        return state.model_copy(
            update={
                "backfill_from": iso_day(first),
                "backfill_next": iso_day(following) if running else None,
                "backfill_status": "running" if running else "done",
            }
        )

    async def list_states(self, tenant_id: str) -> list[UsageRollupState]:
        return await self._repository.list_rollup_states(tenant_id)

    # -- the cycle -------------------------------------------------------------------------

    async def run_cycle(self) -> list[UsageRollupState]:
        async with self._cycle:
            runs = await self._gateway_runs()
        return [run.state for run in runs]

    async def _run_cycle(self) -> bool:
        async with self._cycle:
            runs = await self._gateway_runs()
        return any(run.busy for run in runs)

    async def _gateway_runs(self) -> list[_GatewayRun]:
        tenant_id = self._tenant_id
        now = self._clock()
        try:
            await self.sync_registry(tenant_id, now=now)
        except Exception:
            logger.exception("usage_registry_sync_failed")
        registry: dict[tuple[str, str], AttributionRecord] = {
            (record.kind, record.key): record
            for record in await self._repository.list_attribution_records(tenant_id)
        }
        apis = await governed_apis(self._gateways, tenant_id, now=now)
        runs: list[_GatewayRun] = []
        for gateway in await self._gateways.list_gateways(tenant_id):
            run = await self._leased_gateway_run(gateway, apis.get(gateway.id, []), registry)
            if run is not None:
                runs.append(run)
        return runs

    async def _leased_gateway_run(
        self,
        gateway: Gateway,
        current: Sequence[RolledUpApi],
        registry: Mapping[tuple[str, str], AttributionRecord],
    ) -> _GatewayRun | None:
        # Scope leases guard work that makes no ARM calls, and this one can write an API
        # diagnostic. That write is the same PUT whoever makes it, so a holder whose lease expired
        # can't undo anything by finishing late; the lease here only saves duplicate queries.
        scope = f"usage-rollup:{gateway.id}"
        try:
            await self._gateways.acquire_scope_lease(
                gateway.tenant_id, scope, self._owner, lease_seconds=LEASE_SECONDS
            )
        except ConflictError:
            logger.info("usage_rollup_skipped", gateway_id=gateway.id, reason="leased")
            return None
        try:
            return await self.roll_up_gateway(gateway, current, registry)
        finally:
            try:
                await self._gateways.release_scope_lease(gateway.tenant_id, scope, self._owner)
            except Exception:
                logger.warning("usage_rollup_lease_release_failed", gateway_id=gateway.id)

    async def roll_up_gateway(
        self,
        gateway: Gateway,
        current: Sequence[RolledUpApi],
        registry: Mapping[tuple[str, str], AttributionRecord],
    ) -> _GatewayRun | None:
        started = monotonic.monotonic()
        now = self._clock()
        today = now.astimezone(UTC).date()
        stored = await self._repository.get_rollup_state(gateway.tenant_id, gateway.id)
        apis = merge_apis(stored.apis if stored else [], current, now)
        if not apis:
            return None
        before = stored or UsageRollupState(
            id=usage_rollup_state_id(gateway.tenant_id, gateway.id),
            tenant_id=gateway.tenant_id,
            gateway_id=gateway.id,
        )
        state = before
        if state.last_run_at is None and state.backfill_from is None:
            # A gateway's first run reads back as far as allowed. Enabling telemetry or asking for
            # a refresh can save its state before then, so "first" means never run, not unsaved.
            state = self._with_backfill(state, today, self._backfill_max_days)
        state = self._with_catch_up(state, today)
        state = state.model_copy(update={"apis": apis, "last_run_at": now})
        state = await self._keep_instrumented(gateway, state)

        days = [today, today - timedelta(days=1), *self._backfill_slice(state)]
        resolver = LinkResolver(gateway.id, registry)
        rows = 0
        written = 0
        unknown_versions = 0
        months: set[date] = set()
        error: str | None = None
        for day in days:
            try:
                fold = await self._roll_up_day(gateway, apis, resolver, day, now)
            except LogQueryAccessError as failure:
                error = failure.message
                break
            except DomainError as failure:
                error = failure.message
                logger.warning(
                    "usage_rollup_day_failed", gateway_id=gateway.id, day=iso_day(day)
                )
                break
            if fold is None:
                continue
            fold_rows, fold_written, fold_unknown = fold
            rows += fold_rows
            written += fold_written
            if day >= today - timedelta(days=1):
                unknown_versions += fold_unknown
            months.add(month_start(day))
            state = self._advance(state, day, today)
        if error is None:
            for month in sorted(months):
                try:
                    written += await self._fold_month(gateway, month)
                except DomainError as failure:
                    error = failure.message
                    break
        finished = self._clock()
        update: dict[str, Any] = {
            "last_duration_ms": int((monotonic.monotonic() - started) * 1000),
            "last_rows": rows,
            "last_written": written,
            "unknown_trace_versions": unknown_versions,
        }
        if error is None:
            update |= {
                "last_success_at": finished,
                "queried_through": now,
                "last_error": None,
                "last_error_at": None,
            }
        else:
            update |= {"last_error": error[:500], "last_error_at": finished}
        run = state.model_copy(update=update)
        saved = await self._repository.update_rollup_state(
            gateway.tenant_id,
            gateway.id,
            lambda current: merge_run(current, before=before, run=run),
        )
        logger.info(
            "usage_rollup_gateway_done",
            gateway_id=gateway.id,
            rows=rows,
            written=written,
            failed=error is not None,
        )
        return _GatewayRun(
            state=saved, busy=error is None and saved.backfill_status == "running"
        )

    def _with_catch_up(self, state: UsageRollupState, today: date) -> UsageRollupState:
        """Re-read days a stopped or failing job missed. Each cycle only re-reads two."""

        if state.queried_through is None:
            return state
        gap_start = max(
            state.queried_through.astimezone(UTC).date(),
            today - timedelta(days=self._backfill_max_days - 1),
        )
        following = today - timedelta(days=2)
        if gap_start > following:
            return state
        running = (
            state.backfill_status == "running"
            and state.backfill_from is not None
            and state.backfill_next is not None
        )
        if running and date.fromisoformat(state.backfill_next or "") >= following:
            if date.fromisoformat(state.backfill_from or "") <= gap_start:
                return state
            return state.model_copy(update={"backfill_from": iso_day(gap_start)})
        first = gap_start
        if running:
            first = min(first, date.fromisoformat(state.backfill_from or ""))
        logger.info(
            "usage_rollup_catch_up", gateway_id=state.gateway_id, from_day=iso_day(gap_start)
        )
        return state.model_copy(
            update={
                "backfill_from": iso_day(first),
                "backfill_next": iso_day(following),
                "backfill_status": "running",
            }
        )

    def _backfill_slice(self, state: UsageRollupState) -> list[date]:
        if state.backfill_status != "running" or not state.backfill_next or not state.backfill_from:
            return []
        following = date.fromisoformat(state.backfill_next)
        first = date.fromisoformat(state.backfill_from)
        days: list[date] = []
        while following >= first and len(days) < BACKFILL_DAYS_PER_CYCLE:
            days.append(following)
            following -= timedelta(days=1)
        return days

    @staticmethod
    def _advance(state: UsageRollupState, day: date, today: date) -> UsageRollupState:
        update: dict[str, Any] = {}
        earliest = state.data_available_from
        if earliest is None or iso_day(day) < earliest:
            update["data_available_from"] = iso_day(day)
        if (
            state.backfill_status == "running"
            and state.backfill_next
            and state.backfill_from
            and day < today - timedelta(days=1)
            and iso_day(day) == state.backfill_next
        ):
            following = day - timedelta(days=1)
            if following < date.fromisoformat(state.backfill_from):
                update |= {"backfill_next": None, "backfill_status": "done"}
            else:
                update["backfill_next"] = iso_day(following)
        return state.model_copy(update=update) if update else state

    async def _keep_instrumented(
        self, gateway: Gateway, state: UsageRollupState
    ) -> UsageRollupState:
        """Write the diagnostic on APIs MOSAIC published since telemetry was enabled."""

        if (
            self._telemetry is None
            or gateway.management_mode != ManagementMode.MANAGE
            or not gateway.access.can_write
        ):
            return state
        published = [
            api for api in state.apis if api.publication_id is not None and api.removed_at is None
        ]
        pending = [api for api in published if api.api_name not in state.instrumented_apis]
        if not pending:
            return state
        try:
            if not await self._telemetry.has_logger(gateway):
                return state
            outcome = await self._telemetry.ensure_api_diagnostics(
                gateway, pending, only_missing=True
            )
        except DomainError as failure:
            return state.model_copy(update={"diagnostics_error": failure.message[:300]})
        if outcome.instrumented:
            await self._entitlements.record_audit(
                AuditEvent(
                    id=new_id("audit"),
                    tenant_id=gateway.tenant_id,
                    action="gateway.telemetryInstrumented",
                    resource_type="gateway",
                    resource_id=gateway.id,
                    actor_object_id=ROLLUP_ACTOR,
                    details={"apis": outcome.instrumented},
                )
            )
        instrumented = sorted(set(state.instrumented_apis) | set(outcome.instrumented))
        return state.model_copy(
            update={
                "instrumented_apis": instrumented,
                "diagnostics_error": diagnostics_error(outcome),
            }
        )

    async def _query(
        self, resource_id: str, build: Callable[[QueryWindow], str], window: QueryWindow
    ) -> list[Row]:
        """Run a query, halving its window until each part fits Log Analytics' limits."""

        start, end = window.timespan
        try:
            return await self._logs.query(resource_id, build(window), start=start, end=end)
        except LogQueryTooLargeError:
            if window.hours <= 1:
                raise
            first, second = window.split()
            return [
                *await self._query(resource_id, build, first),
                *await self._query(resource_id, build, second),
            ]

    async def _roll_up_day(
        self,
        gateway: Gateway,
        apis: Sequence[RolledUpApi],
        resolver: LinkResolver,
        day: date,
        now: datetime,
    ) -> tuple[int, int, int] | None:
        start = _day_start(day)
        end = min(start + timedelta(days=1), _ceil_hour(now))
        if end <= start:
            return None
        window = QueryWindow(start, end)
        names = [api.api_name for api in apis]
        deployments = {
            api.api_name: api.deployment_key
            for api in apis
            if api.deployment_key and DEPLOYMENT_KEY.fullmatch(api.deployment_key)
        }
        resource_id = gateway.azure_resource_id
        calls = await self._query(resource_id, lambda part: calls_query(part, names), window)
        peaks = await self._query(resource_id, lambda part: peaks_query(part, names), window)
        denials = await self._query(resource_id, lambda part: denials_query(part, names), window)
        deployment_peaks = (
            await self._query(
                resource_id, lambda part: deployment_peaks_query(part, deployments), window
            )
            if deployments
            else []
        )
        fold = DayFold(resolver, apis)
        fold.add_calls(calls)
        fold.add_peaks(peaks)
        fold.add_deployment_peaks(deployment_peaks)
        fold.add_denials(denials)
        if day < now.astimezone(UTC).date() - timedelta(days=1) and await self._would_lower(
            gateway, day, fold
        ):
            # Log Analytics keeps logs for its own retention, so re-reading an older day can find
            # fewer calls than MOSAIC already rolled up. A re-read never lowers a day's figures.
            logger.info("usage_rollup_day_kept", gateway_id=gateway.id, day=iso_day(day))
            return fold.rows, 0, fold.unknown_versions
        items = fold.items(gateway.tenant_id, iso_day(day), gateway.id, self._retention_seconds)
        written = await self._replace_bucket(gateway, day_bucket(iso_day(day)), items)
        return fold.rows, written, fold.unknown_versions

    async def _would_lower(self, gateway: Gateway, day: date, fold: DayFold) -> bool:
        stored = await self._repository.list_summaries(
            gateway.tenant_id,
            period="day",
            start=iso_day(day),
            end=iso_day(day),
            dimensions=["total"],
            gateway_ids=[gateway.id],
        )
        held = sum(entry.metrics.requests for summary in stored for entry in summary.entries)
        found = fold.entries.get("total", {}).get("")
        return held > (found.metrics.requests if found else 0)

    async def _replace_bucket(
        self, gateway: Gateway, bucket: str, items: Sequence[RollupItem]
    ) -> int:
        existing = await self._repository.list_bucket_hashes(gateway.tenant_id, gateway.id, bucket)
        wanted = {item.id for item in items}
        changed = [item for item in items if existing.get(item.id) != item.content_hash]
        stale = [item_id for item_id in existing if item_id not in wanted]
        if changed:
            await self._repository.upsert_rollups(changed)
        if stale:
            await self._repository.delete_rollups(gateway.tenant_id, stale)
        return len(changed) + len(stale)

    async def _fold_month(self, gateway: Gateway, first: date) -> int:
        last = _next_month(first) - timedelta(days=1)
        dailies = await self._repository.list_summaries(
            gateway.tenant_id,
            period="day",
            start=iso_day(first),
            end=iso_day(last),
            dimensions=SUMMARY_DIMENSIONS,
            gateway_ids=[gateway.id],
        )
        folded = fold_month(dailies)
        stored = await self._repository.list_summaries(
            gateway.tenant_id,
            period="month",
            start=iso_day(first),
            end=iso_day(first),
            dimensions=["total"],
            gateway_ids=[gateway.id],
        )
        held = sum(entry.metrics.requests for summary in stored for entry in summary.entries)
        found = folded.get("total", {}).get("")
        if held > (found.metrics.requests if found else 0):
            # Daily summaries expire after the retention period, so a month refolded after some of
            # its days are gone would count fewer calls than it did. A month is never lowered.
            logger.info("usage_rollup_month_kept", gateway_id=gateway.id, month=iso_day(first))
            return 0
        items = summary_items(
            gateway.tenant_id,
            "month",
            iso_day(first),
            gateway.id,
            folded,
            ttl=KEEP_FOREVER,
        )
        return await self._replace_bucket(gateway, period_bucket("month", iso_day(first)), items)

    # -- the attribution registry ----------------------------------------------------------

    async def sync_registry(self, tenant_id: str, *, now: datetime | None = None) -> int:
        """Copy every binding's grant key and subscription into the registry. Never deletes.

        A key whose binding is gone is marked revoked and kept, so its past calls still report
        against the grant and the person. Returns how many records were written.
        """

        recorded_at = now or self._clock()
        entitlements = await self._entitlements.list_entitlements(tenant_id)
        book = await load_book(self._cost_centers, tenant_id)
        principals = {
            principal.id: principal
            for principal in await self._directory.list_principals(tenant_id)
        }
        groups = {group.id: group for group in await self._directory.list_groups(tenant_id)}
        model_apis = {api.id: api for api in await self._gateways.list_model_apis(tenant_id)}
        mcp_servers = {
            server.id: server for server in await self._gateways.list_mcp_servers(tenant_id)
        }
        existing: dict[tuple[str, str], AttributionRecord] = {
            (record.kind, record.key): record
            for record in await self._repository.list_attribution_records(tenant_id)
        }
        desired: dict[tuple[str, str], AttributionRecord] = {}
        for entitlement in entitlements:
            binding = entitlement.binding
            if binding is None:
                continue
            subject_object_id, subject_name = _subject_identity(
                entitlement.subject, principals, groups
            )
            publication_id, publication_kind, resource_name = _resource_identity(
                entitlement.resource, model_apis, mcp_servers
            )
            keys: list[tuple[str, str]] = []
            if binding.attribution_key:
                keys.append(("grant", binding.attribution_key.casefold()))
            if binding.apim_subscription_name:
                keys.append(
                    (
                        "subscription",
                        subscription_attribution_key(
                            binding.gateway_id, binding.apim_subscription_name
                        ),
                    )
                )
            cost_center = book.get(entitlement.cost_center_id)
            for kind, key in keys:
                desired[(kind, key)] = AttributionRecord(
                    id=attribution_record_id(tenant_id, kind, key),
                    tenant_id=tenant_id,
                    kind="grant" if kind == "grant" else "subscription",
                    key=key,
                    gateway_id=binding.gateway_id,
                    entitlement_id=entitlement.id,
                    publication_id=publication_id,
                    publication_kind=publication_kind,
                    subject=entitlement.subject,
                    subject_object_id=subject_object_id,
                    subject_name=subject_name,
                    resource=entitlement.resource,
                    resource_name=resource_name,
                    per_member=binding.attribution_per_member,
                    cost_center_id=entitlement.cost_center_id,
                    cost_center_code=cost_center.code if cost_center else None,
                    cost_center_name=cost_center.name if cost_center else None,
                    recorded_at=recorded_at,
                )
        changes: list[AttributionRecord] = []
        for registry_key, record in desired.items():
            current = existing.get(registry_key)
            if current is not None:
                record = record.model_copy(
                    update={"recorded_at": current.recorded_at, "created_at": current.created_at}
                )
            if current is None or _record_changed(current, record):
                changes.append(record)
        for registry_key, current in existing.items():
            if registry_key not in desired and current.revoked_at is None:
                changes.append(current.model_copy(update={"revoked_at": recorded_at}))
        if changes:
            await self._repository.save_attribution_records(changes)
        return len(changes)
