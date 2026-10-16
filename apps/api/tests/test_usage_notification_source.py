from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import cast

import pytest
from mosaic_api.domain import (
    AuditEvent,
    BindingSource,
    DirectoryObject,
    Entitlement,
    EntitlementBinding,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementRuntime,
    EntitlementSubject,
    Principal,
    PrincipalKind,
    RequestEnforcement,
    ResolvedEntitlement,
    ResourceSummary,
)
from mosaic_api.integrations.graph import FakeDirectoryLookup
from mosaic_api.notifications import DigestWindow, NotificationPreferences
from mosaic_api.repositories import (
    InMemoryDirectoryRepository,
    InMemoryEnvironmentRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.repositories.memory_usage import InMemoryUsageRollupRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.portal import PortalService
from mosaic_api.services.usage import (
    DailyUsage,
    NotificationUsageContext,
    RollupUsageSource,
    UsageFreshness,
    UsageService,
)
from mosaic_api.services.usage_notification_source import PortalUsageNotificationSource
from mosaic_api.services.usage_notifications import DirectoryUsageNotificationIdentities
from mosaic_api.usage_telemetry import RolledUpApi, UsageFact, UsageHour, UsageMetrics

TENANT = "usage-notification-source"
ACTOR = Actor(object_id="alice", tenant_id=TENANT)
NOW = datetime(2026, 3, 9, 14, tzinfo=UTC)


def grant(
    identity: str = "shared", *, gateway: str = "gateway", kind: str = "modelApi"
) -> ResolvedEntitlement:
    return ResolvedEntitlement(
        entitlement=Entitlement(
            id=identity,
            tenant_id=TENANT,
            subject=EntitlementSubject(kind="securityGroup", id="group-principal"),
            resource=EntitlementResource(kind=kind, id=identity),
            enforcement=EntitlementEnforcement(
                requests=RequestEnforcement(
                    counter_key_expression="configuredCounter",
                    call_quota=100,
                    call_quota_period="Monthly",
                )
            ),
            binding=EntitlementBinding(
                gateway_id=gateway,
                attribution_key=identity,
                attribution_per_member=True,
                source=BindingSource.ORCHESTRATED,
            ),
            runtime=EntitlementRuntime(publication_id="publication", status="applied"),
        ),
        via="securityGroup",
        resource_summary=ResourceSummary(
            kind=kind, id=identity, display_name="Demo model", available=True
        ),
    )


class Portal:
    def __init__(self, grants: Sequence[ResolvedEntitlement]) -> None:
        self.grants = list(grants)
        self.actors: list[Actor] = []

    async def my_entitlements(
        self, actor: Actor, *, include_disabled: bool = False
    ) -> list[ResolvedEntitlement]:
        self.actors.append(actor)
        return self.grants


def service(portal: Portal, rollups: InMemoryUsageRollupRepository) -> UsageService:
    gateways = InMemoryGatewayRepository()
    endpoints = InMemoryModelEndpointRepository()
    return UsageService(
        cast(PortalService, portal),
        source=RollupUsageSource(rollups, clock=lambda: NOW),
        gateway_repository=gateways,
        endpoint_repository=endpoints,
        environment_repository=InMemoryEnvironmentRepository(
            gateways, endpoints, InMemoryMcpEndpointRepository()
        ),
        clock=lambda: NOW,
    )


async def covered(rollups: InMemoryUsageRollupRepository, gateway: str = "gateway") -> None:
    await rollups.update_rollup_state(
        TENANT,
        gateway,
        lambda state: state.model_copy(
            update={
                "last_success_at": NOW,
                "data_available_from": "2026-01-01",
                "queried_through": NOW,
                "instrumented_apis": ["demo-api"],
                "apis": [
                    RolledUpApi(
                        api_name="demo-api",
                        resource_id="shared",
                        kind="model",
                        display_name="Demo model",
                        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
                    )
                ],
            }
        ),
    )


async def fact(
    rollups: InMemoryUsageRollupRepository,
    day: str,
    caller: str,
    hours: Sequence[tuple[int, int]],
) -> None:
    count = sum(value for _, value in hours)
    await rollups.upsert_rollups(
        [
            UsageFact(
                id=f"{day}-{caller}",
                tenant_id=TENANT,
                day=day,
                bucket=day,
                gateway_id="gateway",
                link="trace",
                link_key="shared",
                caller_object_id=caller,
                entitlement_id="shared",
                per_member=True,
                metrics=UsageMetrics(
                    requests=count,
                    total_tokens=count * 100,
                    prompt_tokens=count * 75,
                    completion_tokens=count * 25,
                    metered_requests=count,
                ),
                hours=[
                    UsageHour(hour=hour, requests=value, total_tokens=value * 100)
                    for hour, value in hours
                ],
            )
        ]
    )


async def test_local_summary_reuses_caller_attribution_and_exact_hour_boundaries() -> None:
    rollups = InMemoryUsageRollupRepository()
    await covered(rollups)
    await fact(rollups, "2026-03-08", "alice", [(4, 1), (5, 2)])
    await fact(rollups, "2026-03-09", "alice", [(3, 3), (4, 4)])
    await fact(rollups, "2026-03-08", "bob", [(8, 5000)])
    await fact(rollups, "2026-03-09", "bob", [(3, 8000)])
    portal = Portal([grant()])
    source = PortalUsageNotificationSource(service(portal, rollups))
    snapshot = await source.observe(
        ACTOR,
        now=NOW,
        window=DigestWindow(
            period="daily",
            time_zone="America/New_York",
            start_at=datetime(2026, 3, 8, 5, tzinfo=UTC),
            end_at=datetime(2026, 3, 9, 4, tzinfo=UTC),
            due_at=NOW,
        ),
    )
    assert snapshot.coverage == "complete"
    assert snapshot.calls == 5
    assert snapshot.tokens == 500
    assert snapshot.cost_usd is None
    assert snapshot.resources[0].cost_reason == "hourlyPricingUnavailable"
    assert snapshot.unpriced_tokens == 500
    assert all(actor.object_id == ACTOR.object_id for actor in portal.actors)
    assert await source.eligible(ACTOR, snapshot)
    portal.grants.clear()
    assert not await source.eligible(ACTOR, snapshot)


async def test_personal_finite_counter_never_aggregates_other_shared_grant_members() -> None:
    rollups = InMemoryUsageRollupRepository()
    await covered(rollups)
    await fact(rollups, "2026-03-09", "alice", [(1, 85)])
    await fact(rollups, "2026-03-09", "bob", [(1, 9000)])
    source = PortalUsageNotificationSource(service(Portal([grant(kind="mcpServer")]), rollups))
    snapshot = await source.observe(ACTOR, now=NOW)
    assert len(snapshot.counters) == 1
    counter = snapshot.counters[0]
    assert counter.evidence.scope_kind == "personal"
    assert counter.evidence.used == 85
    assert counter.evidence.limit == 100
    assert counter.evidence.priced  # MCP call quota needs no token price.
    assert counter.evidence.coverage == "complete"
    assert counter.period_start_at == datetime(2026, 3, 1, tzinfo=UTC)
    assert counter.period_end_at == datetime(2026, 4, 1, tzinfo=UTC)
    assert "exactNearRealtimeRpmTpm:delayedHourlyRollup" in snapshot.unsupported
    assert "personalUsdLimit:notDefined" in snapshot.unsupported


async def test_missing_gateway_coverage_is_not_union_filled_with_another_gateways_zeroes() -> None:
    rollups = InMemoryUsageRollupRepository()
    await covered(rollups)
    usage = service(Portal([grant(), grant("other", gateway="missing")]), rollups)
    snapshot = await PortalUsageNotificationSource(usage).observe(ACTOR, now=NOW)
    assert snapshot.coverage != "complete"
    assert all(counter.evidence.coverage != "complete" for counter in snapshot.counters)
    assert snapshot.through is None


async def test_unapplied_or_legacy_shared_counter_is_explicitly_unsupported() -> None:
    rollups = InMemoryUsageRollupRepository()
    await covered(rollups)
    item = grant()
    assert item.entitlement.binding is not None
    item.entitlement.binding.attribution_per_member = False
    snapshot = await PortalUsageNotificationSource(service(Portal([item]), rollups)).observe(
        ACTOR, now=NOW
    )
    assert snapshot.counters[0].unsupported_reason == "authoritativeCounterScopeUnavailable"
    item.entitlement.binding.attribution_per_member = True
    assert item.entitlement.runtime is not None
    item.entitlement.runtime.status = "pending"
    snapshot = await PortalUsageNotificationSource(service(Portal([item]), rollups)).observe(
        ACTOR, now=NOW
    )
    assert snapshot.counters[0].unsupported_reason == "authoritativeCounterScopeUnavailable"


class ContextUsage:
    def __init__(self, context: NotificationUsageContext) -> None:
        self.context = context

    async def notification_context(
        self, actor: Actor, *, start: date, end: date
    ) -> NotificationUsageContext:
        return self.context

    async def notification_entitlements(self, actor: Actor) -> Sequence[ResolvedEntitlement]:
        return self.context.entitlements


def context(*, missing_price: bool = False) -> NotificationUsageContext:
    first, second = grant("first"), grant("second")
    day = date(2026, 3, 8)
    return NotificationUsageContext(
        entitlements=[first, second],
        series={item.entitlement.id: {day: DailyUsage(10, 750, 250)} for item in (first, second)},
        costs={"first": {day: 0.2}, "second": {day: None if missing_price else 0.3}},
        freshness=UsageFreshness(
            status="current",
            updated_at=NOW,
            data_from="2026-01-01",
            gateways=1,
            interval_minutes=15,
        ),
        coverage_start=datetime(2026, 1, 1, tzinfo=UTC),
        through=NOW,
        measured=True,
    )


@pytest.mark.parametrize("missing_price", [False, True])
async def test_unknown_cost_is_not_zero_shaped_or_a_partial_priced_sum(missing_price: bool) -> None:
    source = PortalUsageNotificationSource(
        cast(UsageService, ContextUsage(context(missing_price=missing_price)))
    )
    snapshot = await source.observe(
        ACTOR,
        now=NOW,
        window=DigestWindow(
            period="daily",
            start_at=datetime(2026, 3, 8, tzinfo=UTC),
            end_at=datetime(2026, 3, 9, tzinfo=UTC),
            due_at=NOW,
        ),
    )
    assert snapshot.calls == 20
    assert snapshot.tokens == 2000
    assert snapshot.cost_usd == (None if missing_price else 0.5)
    assert snapshot.unpriced_tokens == (1000 if missing_price else 0)


async def test_subhour_boundary_and_missing_days_are_unknown_never_prorated() -> None:
    source = PortalUsageNotificationSource(cast(UsageService, ContextUsage(context())))
    snapshot = await source.observe(
        ACTOR,
        now=NOW,
        window=DigestWindow(
            period="daily",
            time_zone="Asia/Kolkata",
            start_at=datetime(2026, 3, 7, 18, 30, tzinfo=UTC),
            end_at=datetime(2026, 3, 8, 18, 30, tzinfo=UTC),
            due_at=NOW,
        ),
    )
    assert snapshot.coverage == "incomplete"
    assert snapshot.calls is None
    assert snapshot.tokens is None
    assert snapshot.cost_usd is None
    snapshot = await source.observe(
        ACTOR,
        now=NOW,
        window=DigestWindow(
            period="weekly",
            start_at=datetime(2026, 3, 2, tzinfo=UTC),
            end_at=datetime(2026, 3, 9, tzinfo=UTC),
            due_at=NOW,
        ),
    )
    assert snapshot.coverage == "incomplete"
    assert snapshot.calls is None
    assert snapshot.cost_usd is None


async def test_stale_rollup_and_no_pricing_suppress_evidence() -> None:
    state = context(missing_price=True)
    assert state.freshness is not None
    state.freshness.updated_at = NOW - timedelta(hours=5)
    source = PortalUsageNotificationSource(cast(UsageService, ContextUsage(state)))
    snapshot = await source.observe(ACTOR, now=NOW)
    assert snapshot.coverage == "delayed"
    assert all(counter.evidence.coverage != "complete" for counter in snapshot.counters)
    assert all(not counter.evidence.priced for counter in snapshot.counters)


async def test_missing_token_metering_is_not_a_free_zero_cost_model_call() -> None:
    state = context()
    for series in state.series.values():
        assert series is not None
        for figures in series.values():
            figures.metered_requests = 0
            figures.successful_requests = 10
            figures.tokens = 0
    snapshot = await PortalUsageNotificationSource(cast(UsageService, ContextUsage(state))).observe(
        ACTOR,
        now=NOW,
        window=DigestWindow(
            period="daily",
            start_at=datetime(2026, 3, 8, tzinfo=UTC),
            end_at=datetime(2026, 3, 9, tzinfo=UTC),
            due_at=NOW,
        ),
    )
    assert snapshot.calls == 20
    assert snapshot.tokens is None
    assert snapshot.cost_usd is None
    assert snapshot.coverage == "incomplete"


async def test_unverified_api_diagnostics_cannot_turn_missing_telemetry_into_healthy_zero() -> None:
    rollups = InMemoryUsageRollupRepository()
    await covered(rollups)
    await rollups.update_rollup_state(
        TENANT, "gateway", lambda state: state.model_copy(update={"instrumented_apis": []})
    )
    snapshot = await PortalUsageNotificationSource(
        service(Portal([grant(kind="mcpServer")]), rollups)
    ).observe(ACTOR, now=NOW)
    assert snapshot.through is None
    assert snapshot.coverage != "complete"
    assert snapshot.counters[0].evidence.coverage != "complete"
    assert "telemetryCoverage:verifyApiDiagnosticsAndRollupCoverage" in snapshot.unsupported


async def test_legacy_unscoped_shared_fact_is_neither_personal_usage_nor_a_zero() -> None:
    rollups = InMemoryUsageRollupRepository()
    await covered(rollups)
    await fact(rollups, "2026-03-08", "bob", [(10, 9000)])
    original = (await rollups.list_facts(TENANT, start_day="2026-03-08", end_day="2026-03-08"))[0]
    await rollups.upsert_rollups([original.model_copy(update={"per_member": False})])
    snapshot = await PortalUsageNotificationSource(service(Portal([grant()]), rollups)).observe(
        ACTOR,
        now=NOW,
        window=DigestWindow(
            period="daily",
            start_at=datetime(2026, 3, 8, tzinfo=UTC),
            end_at=datetime(2026, 3, 9, tzinfo=UTC),
            due_at=NOW,
        ),
    )
    assert snapshot.calls is None
    assert snapshot.tokens is None
    assert snapshot.cost_usd is None
    assert snapshot.coverage == "incomplete"


async def test_identity_resolution_refreshes_groups_and_never_treats_apps_as_people() -> None:
    repository = InMemoryDirectoryRepository()
    for object_id, kind in (
        ("alice", "user"),
        ("group", "securityGroup"),
        ("app", "servicePrincipal"),
    ):
        await repository.create_principal(
            Principal(id=object_id, tenant_id=TENANT, object_id=object_id, kind=kind),
            AuditEvent(
                id=f"audit-{object_id}",
                tenant_id=TENANT,
                actor_object_id="admin",
                action="seed",
                resource_type="principal",
                resource_id=object_id,
            ),
        )
    lookup = FakeDirectoryLookup(
        [DirectoryObject(object_id="alice", kind=PrincipalKind.USER)],
        group_members={"group": ["alice"]},
    )
    identities = DirectoryUsageNotificationIdentities(repository, directory_lookup=lookup)
    actor = await identities.actor(TENANT, "alice")
    assert actor is not None and actor.group_ids == frozenset({"group"})
    assert await identities.actor(TENANT, "app") is None
    prefs: list[NotificationPreferences] = []
    assert await identities.subjects(TENANT, prefs) == [("alice", "portal")]
    local = DirectoryUsageNotificationIdentities(repository)
    actor = await local.actor(TENANT, "alice")
    assert actor is not None and not actor.group_ids
    assert await local.actor(TENANT, "app") is None
