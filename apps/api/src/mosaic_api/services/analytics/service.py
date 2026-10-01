"""Administrator analytics over MOSAIC's usage rollups. See ADR 0019.

Every report reads the ``usage-rollups`` container and never Log Analytics, so a request costs a
few Cosmos reads however much traffic the gateways carry. Breakdowns read whole months from monthly
summaries and the days either side from daily ones, so a long range reads few items.
"""

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from mosaic_api.domain import utc_now
from mosaic_api.errors import ConflictError, NotFoundError
from mosaic_api.integrations.graph import DirectoryLookup
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.pricing import month_first, month_last
from mosaic_api.repositories import (
    CostCenterRepository,
    DirectoryRepository,
    EntitlementRepository,
    EnvironmentRepository,
    GatewayRepository,
    ModelEndpointRepository,
    UsageRollupRepository,
)
from mosaic_api.services.analytics.consumers import consumers_report
from mosaic_api.services.analytics.cost import CostBook, changed_months
from mosaic_api.services.analytics.cost_report import (
    CHARGEBACK_COLUMNS,
    chargeback_rows,
    cost_report,
    spend_report,
)
from mosaic_api.services.analytics.export import COLUMNS, REPORT_FOR, filename, table, to_csv
from mosaic_api.services.analytics.limits import (
    HygieneInputs,
    LimitInputs,
    grant_links,
    hygiene_report,
    limited,
    limits_report,
)
from mosaic_api.services.analytics.models import (
    AnalyticsConsumers,
    AnalyticsCost,
    AnalyticsCostSummary,
    AnalyticsDataSource,
    AnalyticsFilters,
    AnalyticsGatewayHealth,
    AnalyticsHygiene,
    AnalyticsLimits,
    AnalyticsModels,
    AnalyticsOverview,
    AnalyticsReliability,
    AnalyticsReport,
    AnalyticsSpend,
    AnalyticsStatus,
    AnalyticsUnattributed,
    ExportView,
)
from mosaic_api.services.analytics.rows import entries
from mosaic_api.services.analytics.scope import (
    NameCache,
    Scope,
    build_grants,
    is_application,
    resolve_gateways,
    resolve_resource,
)
from mosaic_api.services.analytics.views import (
    DENIAL_ROWS,
    ROW_LIMIT,
    Context,
    DeploymentInfo,
    lag_minutes,
    models_report,
    overview,
    reliability,
    unattributed_report,
    within,
)
from mosaic_api.services.analytics.window import (
    Coverage,
    Window,
    add_months,
    coverage_of,
    resolve_window,
    split_months,
)
from mosaic_api.services.cost_centers import load_book
from mosaic_api.services.directory import Actor
from mosaic_api.services.environments import load_environment_catalog
from mosaic_api.services.model_access import with_inherited_limits
from mosaic_api.services.pricing import PricingService
from mosaic_api.services.telemetry import governed_apis
from mosaic_api.services.usage import (
    UsageFreshness,
    current_window,
    quota_definitions,
    usage_freshness,
)
from mosaic_api.services.usage_rollup import UsageRollupService
from mosaic_api.usage_telemetry import (
    RolledUpApi,
    SummaryDimension,
    SummaryPeriod,
    UsageFact,
    UsageMetrics,
    UsageRollupState,
    UsageSummary,
    UsageSummaryEntry,
    period_bucket,
)

# An export keeps this many rows of each list, where the console keeps ROW_LIMIT.
EXPORT_LIMIT = 10_000
# Callers MOSAIC has no record of are looked up in the directory, at most this many a request.
NAME_LOOKUPS = 200
# A grant is only judged unused while its gateway's figures are at most this old.
STALE_AFTER = timedelta(days=1)

NOT_CONFIGURED = (
    "This deployment doesn't read gateway telemetry, so MOSAIC has no measured usage to show. "
    "Set MOSAIC_USAGE_SOURCE to rollups to turn it on."
)
NO_GATEWAYS = (
    "No gateway in scope has a model API or MCP server MOSAIC governs, so there are no calls to "
    "report."
)
PENDING = (
    "MOSAIC hasn't rolled up any gateway telemetry yet. Figures appear after the first rollup, "
    "which runs every {minutes} minutes."
)
SUBJECT_NOTE = (
    "The subject filter narrows people, applications, groups and grants. Totals, models and APIs "
    "still count every caller."
)
COST_CENTER_NOTE = (
    "The cost center filter counts only calls through grants charged to it, with each grant's "
    "calls counted against the API it grants. Refusals, unattributed calls, client "
    "applications, reserved capacity nobody called, and figures by the hour belong to no grant, "
    "so they're left out."
)
# Figures a cost-center filter keeps as they're rolled up: they're kept per grant.
_GRANT_LINKED: frozenset[SummaryDimension] = frozenset({"grant", "grantCaller"})
# Figures kept per API, which a cost-center filter rebuilds from its grants' figures.
_FROM_GRANTS: frozenset[SummaryDimension] = frozenset({"total", "api", "model", "deployment"})
HOURS_NOTE = (
    "Breakdowns cover the whole UTC days the last 24 hours touch, because MOSAIC keeps only totals "
    "by the hour."
)
MONTHS_NOTE = "Ranges longer than 92 days are shown by whole calendar month."
HYGIENE_NOTE = "Access hygiene always covers the last 30 days, whatever range is chosen."
NO_PRICES = "This deployment has no price list, so MOSAIC can't put a cost on usage."

Reader = Callable[..., Awaitable[AnalyticsReport]]


def _day(value: date) -> str:
    return f"{value.day} {value:%B %Y}"


async def _nothing() -> list[UsageSummary]:
    return []


class AnalyticsService:
    def __init__(
        self,
        repository: UsageRollupRepository,
        *,
        gateway_repository: GatewayRepository,
        entitlement_repository: EntitlementRepository,
        directory_repository: DirectoryRepository,
        environment_repository: EnvironmentRepository | None = None,
        endpoint_repository: ModelEndpointRepository | None = None,
        rollups: UsageRollupService | None = None,
        directory_lookup: DirectoryLookup | None = None,
        pricing: PricingService | None = None,
        configured: bool = True,
        interval_seconds: int = 900,
        retention_days: int = 400,
        clock: Callable[[], datetime] = utc_now,
        cost_center_repository: CostCenterRepository | None = None,
    ) -> None:
        self._repository = repository
        self._cost_centers = cost_center_repository
        self._gateways = gateway_repository
        self._entitlements = entitlement_repository
        self._directory = directory_repository
        self._environments = environment_repository
        self._endpoints = endpoint_repository
        self._rollups = rollups
        self._names = NameCache(directory_lookup)
        self._pricing = pricing
        self._configured = configured
        self._interval = timedelta(seconds=interval_seconds)
        self._retention_days = retention_days
        self._clock = clock

    @property
    def _source(self) -> AnalyticsDataSource:
        return "logAnalytics" if self._configured else "notConfigured"

    # -- what a request may see -------------------------------------------------------------

    async def _scope(
        self, actor: Actor, filters: AnalyticsFilters, *, grants: bool = True
    ) -> Scope:
        tenant = actor.tenant_id
        gateways, catalog, governed, states = await asyncio.gather(
            self._gateways.list_gateways(tenant),
            load_environment_catalog(self._environments, tenant),
            governed_apis(self._gateways, tenant, now=self._clock()),
            self._repository.list_rollup_states(tenant),
        )
        all_gateways = {gateway.id: gateway for gateway in gateways}
        selected, gateway_ids = resolve_gateways(filters, all_gateways, catalog)
        # APIs a gateway no longer has are still named from what its rollups remember.
        apis: dict[tuple[str, str], RolledUpApi] = {}
        for state in states:
            for api in state.apis:
                apis[(state.gateway_id, api.api_name)] = api
        for gateway_id, current in governed.items():
            for api in current:
                apis[(gateway_id, api.api_name)] = api
        allowed_apis: set[tuple[str, str]] | None = None
        resource_ids: set[str] | None = None
        if filters.resource_id is not None:
            allowed_apis, resource_ids = resolve_resource(filters.resource_id, apis)
            reached = {gateway_id for gateway_id, _ in allowed_apis}
            gateway_ids = sorted(reached if gateway_ids is None else reached & set(gateway_ids))
            selected = {key: gateway for key, gateway in selected.items() if key in reached}
        scope = Scope(
            tenant_id=tenant,
            filters=filters,
            all_gateways=all_gateways,
            gateways=selected,
            gateway_ids=gateway_ids,
            states={state.gateway_id: state for state in states},
            governed=governed,
            apis=apis,
            allowed_apis=allowed_apis,
            resource_ids=resource_ids,
            environments={item.key: item.display_name for item in catalog.environments},
            grants={},
            links_by_entitlement={},
            entitlements={},
            principals_by_object={},
            principals_by_id={},
            principals_by_app={},
            groups={},
            subject_names={},
        )
        # A cost center's figures are its grants' figures, so the filter needs them.
        if grants or filters.cost_center_id is not None:
            await self._add_grants(scope)
        return scope

    async def _add_grants(self, scope: Scope) -> None:
        tenant = scope.tenant_id
        records, entitlements, principals, groups, book = await asyncio.gather(
            self._repository.list_attribution_records(tenant),
            self._entitlements.list_entitlements(tenant),
            self._directory.list_principals(tenant),
            self._directory.list_groups(tenant),
            load_book(self._cost_centers, tenant),
        )
        scope.cost_centers = {key: value.ref() for key, value in book.by_id.items()}
        wanted = scope.filters.cost_center_id
        if wanted is not None and wanted not in scope.cost_centers and not any(
            record.cost_center_id == wanted for record in records
        ):
            raise NotFoundError("No cost center has that ID", details={"costCenterId": wanted})
        scope.principals_by_id = {principal.id: principal for principal in principals}
        scope.principals_by_object = {
            principal.object_id.casefold(): principal for principal in principals
        }
        scope.principals_by_app = {
            principal.detail.casefold(): principal
            for principal in principals
            if is_application(principal) and principal.detail
        }
        scope.groups = {group.id: group for group in groups}
        # Limits a grant takes from its cost center count as its own, as the gateway applies them.
        scope.entitlements = {
            entitlement.id: with_inherited_limits(entitlement, book)
            for entitlement in entitlements
        }
        scope.grants = build_grants(records, entitlements, scope.principals_by_id, scope.groups)
        links: dict[str, set[str]] = defaultdict(set)
        names: dict[str, str] = {}
        for key, grant in scope.grants.items():
            if grant.entitlement_id:
                links[grant.entitlement_id].add(key)
            if grant.subject_object_id and grant.subject_name:
                names.setdefault(grant.subject_object_id, grant.subject_name)
        scope.links_by_entitlement = dict(links)
        scope.subject_names = names

    async def _name(self, scope: Scope, summaries: Iterable[UsageSummary]) -> None:
        """Look up callers MOSAIC has no record of, busiest first."""

        calls: dict[str, int] = defaultdict(int)
        for summary in summaries:
            if summary.dimension not in {"grantCaller", "denial"}:
                continue
            for entry in summary.entries:
                caller = (
                    entry.key.rpartition("|")[2]
                    if summary.dimension == "grantCaller"
                    else [*entry.key.split("|", 3), "", ""][1]
                )
                folded = caller.casefold()
                if (
                    folded
                    and folded not in scope.principals_by_object
                    and folded not in scope.subject_names
                ):
                    calls[folded] += entry.metrics.requests
        if calls:
            ordered = sorted(calls, key=lambda object_id: (-calls[object_id], object_id))
            scope.directory = await self._names.resolve(ordered, limit=NAME_LOOKUPS)

    # -- reading summaries ------------------------------------------------------------------

    def _horizon(self, window: Window) -> date | None:
        """The first day MOSAIC still keeps daily figures for, when a window is read by day."""

        if window.granularity == "month":
            return None
        return window.today - timedelta(days=self._retention_days - 1)

    def _span(self, window: Window) -> tuple[date, date]:
        horizon = self._horizon(window)
        first = window.first_day if horizon is None else max(window.first_day, horizon)
        return first, window.last_day

    async def _read(
        self,
        scope: Scope,
        period: SummaryPeriod,
        first: date,
        last: date,
        dimensions: Sequence[SummaryDimension],
    ) -> list[UsageSummary]:
        if first > last or (scope.gateway_ids is not None and not scope.gateway_ids):
            return []
        if scope.filters.cost_center_id is not None:
            return await self._read_for_cost_center(scope, period, first, last, dimensions)
        return await self._repository.list_summaries(
            scope.tenant_id,
            period=period,
            start=first.isoformat(),
            end=last.isoformat(),
            dimensions=list(dimensions),
            gateway_ids=scope.gateway_ids,
        )

    async def _read_for_cost_center(
        self,
        scope: Scope,
        period: SummaryPeriod,
        first: date,
        last: date,
        dimensions: Sequence[SummaryDimension],
    ) -> list[UsageSummary]:
        """One cost center's figures: only what the grants charged to it carried.

        The rollups keep totals, APIs, models and deployments by API rather than by grant, so
        those are rebuilt from the cost center's grant figures, each grant counted against the API
        it grants. Refusals, unattributed calls and client applications belong to no grant, so a
        cost center has none.
        """

        kept = [dimension for dimension in dimensions if dimension in _GRANT_LINKED]
        rebuilt = [dimension for dimension in dimensions if dimension in _FROM_GRANTS]
        read: list[SummaryDimension] = [*kept]
        if rebuilt and "grant" not in read:
            read.append("grant")
        if not read:
            return []
        summaries = await self._repository.list_summaries(
            scope.tenant_id,
            period=period,
            start=first.isoformat(),
            end=last.isoformat(),
            dimensions=read,
            gateway_ids=scope.gateway_ids,
        )
        found = [summary for summary in summaries if summary.dimension in kept]
        if rebuilt:
            found.extend(
                await self._from_grants(
                    scope,
                    [summary for summary in summaries if summary.dimension == "grant"],
                    rebuilt,
                )
            )
        return found

    async def _from_grants(
        self,
        scope: Scope,
        grants: Sequence[UsageSummary],
        dimensions: Sequence[SummaryDimension],
    ) -> list[UsageSummary]:
        observed = await self._deployments(scope) if "model" in dimensions else {}
        groups: dict[tuple[SummaryPeriod, str, str, SummaryDimension], dict[str, UsageMetrics]]
        groups = defaultdict(lambda: defaultdict(UsageMetrics))
        for summary, entry in entries(grants, scope, "grant"):
            base = (summary.period, summary.period_start, summary.gateway_id)
            if "total" in dimensions:
                groups[(*base, "total")][""].add(entry.metrics)
            api = scope.grant_api(summary.gateway_id, scope.grants.get(entry.key))
            if api is None:
                continue
            if "api" in dimensions:
                groups[(*base, "api")][api.api_name].add(entry.metrics)
            deployment = api.deployment_key
            if deployment and "deployment" in dimensions:
                groups[(*base, "deployment")][deployment].add(entry.metrics)
            if "model" in dimensions and api.kind == "model":
                info = observed.get(deployment) if deployment else None
                model = (info.model_name if info else None) or api.deployment_name or "unknown"
                groups[(*base, "model")][f"{model.casefold()}|{api.api_name}"].add(
                    entry.metrics
                )
        return [
            UsageSummary(
                id=f"costcenter-{dimension}-{gateway_id}-{period}-{period_start}",
                tenant_id=scope.tenant_id,
                period=period,
                period_start=period_start,
                bucket=period_bucket(period, period_start),
                gateway_id=gateway_id,
                dimension=dimension,
                entries=[
                    UsageSummaryEntry(key=key, metrics=metrics)
                    for key, metrics in sorted(values.items())
                ],
            )
            for (period, period_start, gateway_id, dimension), values in sorted(groups.items())
        ]

    async def _breakdown(
        self,
        scope: Scope,
        window: Window,
        dimensions: Sequence[SummaryDimension],
        costs: CostBook | None = None,
    ) -> list[UsageSummary]:
        """A window's summaries for breakdowns: whole months by the month, the rest by day.

        A whole month in which a price changes is read by the day when MOSAIC still keeps its
        days, so each day is priced at its own price.
        """

        first, last = self._span(window)
        if window.granularity == "month":
            return await self._months(scope, first, last, dimensions, costs, window.today)
        months, ranges = split_months(first, last)
        if costs is not None:
            demoted = changed_months(costs.pricer, months)
            ranges = [*ranges, *((month, month_last(month)) for month in sorted(demoted))]
            months = [month for month in months if month not in demoted]
        reads = [self._read(scope, "day", start, end, dimensions) for start, end in ranges]
        if months:
            reads.append(self._read(scope, "month", months[0], months[-1], dimensions))
        return [
            summary
            for part in await asyncio.gather(*reads)
            for summary in part
            if summary.period == "day" or date.fromisoformat(summary.period_start) in months
        ]

    async def _months(
        self,
        scope: Scope,
        first: date,
        last: date,
        dimensions: Sequence[SummaryDimension],
        costs: CostBook | None,
        today: date,
    ) -> list[UsageSummary]:
        """Whole months, except those with a price change whose days MOSAIC still keeps."""

        months: list[date] = []
        cursor = first
        while cursor <= last:
            months.append(cursor)
            cursor = add_months(cursor, 1)
        kept = today - timedelta(days=self._retention_days - 1)
        demoted = (
            {month for month in changed_months(costs.pricer, months) if month >= kept}
            if costs is not None
            else set()
        )
        reads = [self._read(scope, "month", first, last, dimensions)]
        reads.extend(
            self._read(scope, "day", month, month_last(month), dimensions)
            for month in sorted(demoted)
        )
        return [
            summary
            for part in await asyncio.gather(*reads)
            for summary in part
            if summary.period == "day"
            or date.fromisoformat(summary.period_start) not in demoted
        ]

    async def _priced_read(
        self,
        scope: Scope,
        window: Window,
        first: date,
        last: date,
        dimensions: Sequence[SummaryDimension],
        costs: CostBook | None,
    ) -> list[UsageSummary]:
        """Summaries at the window's own grain, with priced months read by the day."""

        if window.period == "month":
            return await self._months(scope, first, last, dimensions, costs, window.today)
        return await self._read(scope, "day", first, last, dimensions)

    async def _costs(self, scope: Scope, window: Window) -> CostBook | None:
        """What prices this request's usage, or None when this deployment has no price list."""

        if self._pricing is None or not self._configured:
            return None
        endpoint_ids = {
            api.model_endpoint_id for api in scope.apis.values() if api.model_endpoint_id
        }
        pricer = await self._pricing.pricer(scope.tenant_id, endpoint_ids)
        first = min(window.previous_first_day, window.first_day, month_first(window.today))
        provisioned = await self._pricing.provisioned_tokens(
            scope.tenant_id, pricer.provisioned_keys(), first, window.today
        )
        return CostBook(pricer, scope, window.today, provisioned)

    async def _spend(self, scope: Scope, costs: CostBook | None) -> AnalyticsSpend | None:
        if costs is None:
            return None
        today = costs.today
        api = await self._read(scope, "day", month_first(today), today, ["api"])
        coverage = coverage_of(scope.live_states())
        through = min(self._clock(), coverage.through) if coverage is not None else None
        return spend_report(costs, scope, api, today, through)

    async def cost_center_spend(
        self, tenant_id: str, cost_center_id: str
    ) -> AnalyticsSpend | None:
        """This month's spend and its forecast for one cost center, for background checks.

        The calls its grants carried, at list prices, exactly as the Cost tab's spend shows them
        under the cost center filter. Reserved capacity nobody called is no cost center's. None
        when this deployment has no price list.
        """

        actor = Actor(object_id="system:cost-centers", tenant_id=tenant_id)
        filters = AnalyticsFilters(cost_center_id=cost_center_id)
        window = resolve_window(filters, self._clock())
        scope = await self._scope(actor, filters)
        return await self._spend(scope, await self._costs(scope, window))

    async def _facts(self, scope: Scope, day: date, links: set[str]) -> list[UsageFact]:
        if not links or (scope.gateway_ids is not None and not scope.gateway_ids):
            return []
        return await self._repository.list_facts(
            scope.tenant_id,
            start_day=day.isoformat(),
            end_day=day.isoformat(),
            link_keys=sorted({link.partition(":")[2] for link in links}),
            gateway_ids=scope.gateway_ids,
        )

    async def _deployments(self, scope: Scope) -> dict[str, DeploymentInfo]:
        """What MOSAIC last observed of each deployment a governed model API calls."""

        if self._endpoints is None:
            return {}
        endpoint_ids = sorted(
            {api.model_endpoint_id for api in scope.apis.values() if api.model_endpoint_id}
        )
        if not endpoint_ids:
            return {}
        endpoints = {
            endpoint.id: endpoint
            for endpoint in await self._endpoints.list_endpoints(scope.tenant_id)
        }
        observed = await asyncio.gather(
            *(
                self._endpoints.list_observed_for_endpoint(
                    ObservedModelDeployment,
                    scope.tenant_id,
                    endpoint_id,
                    "observedModelDeployment",
                )
                for endpoint_id in endpoint_ids
                if endpoint_id in endpoints
            )
        )
        found: dict[str, DeploymentInfo] = {}
        for deployments in observed:
            for deployment in deployments:
                endpoint = endpoints.get(deployment.endpoint_id)
                found[f"{deployment.endpoint_id}/{deployment.deployment_name}"] = DeploymentInfo(
                    endpoint_name=endpoint.name if endpoint else None,
                    model_name=deployment.model_name,
                    model_format=deployment.model_format,
                    sku_name=deployment.sku_name,
                    sku_capacity=deployment.sku_capacity,
                )
        return found

    # -- what every report carries ----------------------------------------------------------

    def _freshness(self, scope: Scope, now: datetime) -> UsageFreshness:
        return usage_freshness(
            scope.live_states(),
            gateways=len(scope.live_gateways()),
            now=now,
            interval=self._interval,
        )

    def _health(self, scope: Scope, now: datetime) -> list[AnalyticsGatewayHealth]:
        rows: list[AnalyticsGatewayHealth] = []
        for gateway in sorted(scope.gateways.values(), key=lambda item: item.name.casefold()):
            governed = scope.governed.get(gateway.id, [])
            state = scope.states.get(gateway.id) or UsageRollupState(
                id=f"pending-{gateway.id}", tenant_id=gateway.tenant_id, gateway_id=gateway.id
            )
            freshness = usage_freshness(
                [state], gateways=1 if governed else 0, now=now, interval=self._interval
            )
            rows.append(
                AnalyticsGatewayHealth(
                    gateway_id=gateway.id,
                    name=gateway.name,
                    environment=gateway.environment,
                    environment_name=scope.environment_name(gateway.environment),
                    status=freshness.status,
                    governed_apis=len(governed),
                    instrumented_apis=len(
                        {api.api_name for api in governed}.intersection(state.instrumented_apis)
                    ),
                    last_run_at=state.last_run_at,
                    last_success_at=state.last_success_at,
                    queried_through=state.queried_through,
                    lag_minutes=lag_minutes(now, state.queried_through),
                    data_available_from=state.data_available_from,
                    last_error=state.last_error,
                    last_error_at=state.last_error_at,
                    backfill_status=state.backfill_status,
                    backfill_from=state.backfill_from,
                    backfill_next=state.backfill_next,
                    unknown_trace_versions=state.unknown_trace_versions,
                    diagnostics_error=state.diagnostics_error,
                )
            )
        return rows

    def _context(self, scope: Scope, window: Window, now: datetime, *, limit: int) -> Context:
        coverage = coverage_of(scope.live_states())
        freshness = self._freshness(scope, now)
        horizon = self._horizon(window)
        clamped = False
        if horizon is not None and coverage is not None and coverage.first_day < horizon:
            coverage = Coverage(first_day=horizon, through=coverage.through)
            clamped = True
        notes: list[str] = []
        if not self._configured:
            notes.append(NOT_CONFIGURED)
        elif freshness.status == "notLinked":
            notes.append(NO_GATEWAYS)
        elif coverage is None:
            notes.append(PENDING.format(minutes=freshness.interval_minutes))
        elif coverage.first_start > window.start:
            notes.append(
                f"MOSAIC keeps daily figures for {self._retention_days} days, so this range has "
                f"none before {_day(coverage.first_day)}. Choose 12 months to see older usage by "
                "month."
                if clamped
                else f"MOSAIC has figures from {_day(coverage.first_day)}, so earlier parts of "
                "this range show no data."
            )
        if scope.filters.subject_kind is not None:
            notes.append(SUBJECT_NOTE)
        if scope.filters.cost_center_id is not None:
            notes.append(COST_CENTER_NOTE)
        if window.granularity == "hour":
            notes.append(HOURS_NOTE)
        elif window.range == "custom" and window.granularity == "month":
            notes.append(MONTHS_NOTE)
        base = {
            "data_source": self._source,
            "generated_at": now,
            "window": window.model(),
            "freshness": freshness,
            "notes": notes,
        }
        top_limit = limit if limit > ROW_LIMIT else DENIAL_ROWS
        return Context(
            scope=scope,
            window=window,
            coverage=coverage,
            base=base,
            limit=limit,
            top_limit=top_limit,
        )

    async def _prepare(
        self,
        actor: Actor,
        filters: AnalyticsFilters,
        *,
        limit: int,
        grants: bool = True,
    ) -> tuple[Context, datetime]:
        now = self._clock()
        window = resolve_window(filters, now)
        scope = await self._scope(actor, filters, grants=grants)
        return self._context(scope, window, now, limit=limit), now

    # -- reports ----------------------------------------------------------------------------

    async def status(self, actor: Actor) -> AnalyticsStatus:
        now = self._clock()
        scope = await self._scope(actor, AnalyticsFilters(), grants=False)
        return AnalyticsStatus(
            data_source=self._source,
            rollups_enabled=self._rollups is not None,
            generated_at=now,
            freshness=self._freshness(scope, now),
            gateways=self._health(scope, now),
        )

    async def gateway(self, actor: Actor, gateway_id: str) -> AnalyticsGatewayHealth:
        """One gateway's rollup health, as the overview reports it."""

        now = self._clock()
        scope = await self._scope(actor, AnalyticsFilters(gateway_id=gateway_id), grants=False)
        return self._health(scope, now)[0]

    async def refresh(self, actor: Actor) -> AnalyticsStatus:
        """Roll up every gateway now instead of at the next interval."""

        if self._rollups is None:
            raise ConflictError(
                "This deployment doesn't roll up gateway telemetry, so there is nothing to refresh"
            )
        await self._rollups.request_refresh(actor)
        return await self.status(actor)

    async def overview(
        self, actor: Actor, filters: AnalyticsFilters, *, limit: int = ROW_LIMIT
    ) -> AnalyticsOverview:
        context, now = await self._prepare(actor, filters, limit=limit)
        scope, window = context.scope, context.window
        first, last = self._span(window)
        hourly = window.granularity == "hour"
        costs = await self._costs(scope, window)
        # The last 24 hours compare with the 24 before, which the same daily items hold.
        current, previous, breakdown, spend = await asyncio.gather(
            self._priced_read(
                scope,
                window,
                window.previous_first_day if hourly else first,
                last,
                ["api", "model"],
                costs,
            ),
            _nothing()
            if hourly
            else self._priced_read(
                scope,
                window,
                window.previous_first_day,
                window.previous_last_day,
                ["api"],
                costs,
            ),
            self._breakdown(scope, window, ["grantCaller", "unattributed"], costs),
            self._spend(scope, costs),
        )
        await self._name(scope, breakdown)
        return overview(
            context,
            api=[summary for summary in current if summary.dimension == "api"],
            previous_api=previous,
            models=[
                summary
                for summary in current
                if summary.dimension == "model" and within(summary, first, last)
            ],
            callers=breakdown,
            unattributed=breakdown,
            gateways=self._health(scope, now),
            costs=costs,
            spend=spend,
        )

    async def consumers(
        self, actor: Actor, filters: AnalyticsFilters, *, limit: int = ROW_LIMIT
    ) -> AnalyticsConsumers:
        context, _ = await self._prepare(actor, filters, limit=limit)
        costs = await self._costs(context.scope, context.window)
        summaries = await self._breakdown(
            context.scope, context.window, ["grantCaller", "clientApp"], costs
        )
        await self._name(context.scope, summaries)
        return consumers_report(context, callers=summaries, clients=summaries, costs=costs)

    async def models(
        self, actor: Actor, filters: AnalyticsFilters, *, limit: int = ROW_LIMIT
    ) -> AnalyticsModels:
        context, _ = await self._prepare(actor, filters, limit=limit, grants=False)
        scope, window = context.scope, context.window
        first, last = self._span(window)
        costs = await self._costs(scope, window)
        # Deployments are read by the day, which is where their hourly peaks are kept.
        summaries, observed = await asyncio.gather(
            self._priced_read(scope, window, first, last, ["api", "model", "deployment"], costs),
            self._deployments(scope),
        )
        return models_report(
            context,
            api=summaries,
            models=summaries,
            deployments=summaries,
            observed=observed,
            costs=costs,
        )

    async def cost(
        self, actor: Actor, filters: AnalyticsFilters, *, limit: int = ROW_LIMIT
    ) -> AnalyticsCost:
        """What the window's usage cost, this month's spend, and what couldn't be priced."""

        context, now = await self._prepare(actor, filters, limit=limit)
        scope, window = context.scope, context.window
        first, last = self._span(window)
        costs = await self._costs(scope, window)
        if costs is None:
            return AnalyticsCost(
                **context.report(NO_PRICES),
                spend=None,
                cost=AnalyticsCostSummary(total=None),
                trend=[],
                models=[],
                deployments=[],
                consumers=[],
                apis=[],
                priced=False,
            )
        summaries, callers, spend = await asyncio.gather(
            self._priced_read(scope, window, first, last, ["api", "model", "deployment"], costs),
            self._breakdown(scope, window, ["grantCaller"], costs),
            self._spend(scope, costs),
        )
        await self._name(scope, callers)
        return cost_report(
            context,
            api=summaries,
            models=summaries,
            deployments=summaries,
            callers=callers,
            costs=costs,
            spend=spend,
            now=now,
        )

    async def chargeback(
        self, actor: Actor, filters: AnalyticsFilters
    ) -> tuple[AnalyticsReport, list[dict[str, Any]]]:
        """Each month's cost by who it's charged to and the model that served it."""

        context, _ = await self._prepare(actor, filters, limit=EXPORT_LIMIT)
        costs = await self._costs(context.scope, context.window)
        report = AnalyticsReport(**context.report())
        if costs is None:
            return report, []
        summaries = await self._breakdown(
            context.scope, context.window, ["grant", "unattributed"], costs
        )
        return report, chargeback_rows(
            context, grants=summaries, unattributed=summaries, costs=costs
        )[:EXPORT_LIMIT]

    async def reliability(
        self, actor: Actor, filters: AnalyticsFilters, *, limit: int = ROW_LIMIT
    ) -> AnalyticsReliability:
        context, _ = await self._prepare(actor, filters, limit=limit)
        scope, window = context.scope, context.window
        first, last = self._span(window)
        api, denials = await asyncio.gather(
            self._read(scope, window.period, first, last, ["api"]),
            self._breakdown(scope, window, ["denial"]),
        )
        await self._name(scope, denials)
        return reliability(context, api=api, trend_api=api, denials=denials)

    async def limits(
        self, actor: Actor, filters: AnalyticsFilters, *, limit: int = ROW_LIMIT
    ) -> AnalyticsLimits:
        context, now = await self._prepare(actor, filters, limit=limit)
        scope, window = context.scope, context.window
        today = window.today
        week_start = current_window(now, "Weekly")[0].date()
        hourly: set[str] = set()
        for entitlement in limited(scope):
            if any(period == "Hourly" for _, _, period in quota_definitions(entitlement)):
                hourly.update(grant_links(scope, entitlement))
        in_window, days, months, facts = await asyncio.gather(
            self._breakdown(scope, window, ["grantCaller"]),
            self._read(scope, "day", week_start, today, ["grantCaller"]),
            self._read(scope, "month", date(today.year, 1, 1), today, ["grantCaller"]),
            self._facts(scope, today, hourly),
        )
        await self._name(scope, [*in_window, *days, *months])
        inputs = LimitInputs(facts=facts, days=days, months=months, window=in_window)
        return limits_report(context, inputs, now)

    async def hygiene(
        self, actor: Actor, filters: AnalyticsFilters, *, limit: int = ROW_LIMIT
    ) -> AnalyticsHygiene:
        """Unused grants and keys over the last 30 days, whatever range the request chose."""

        fixed = replace(filters, range="30d", start=None, end=None)
        context, now = await self._prepare(actor, fixed, limit=limit)
        if filters.range != "30d":
            context.notes.append(HYGIENE_NOTE)
        scope, window = context.scope, context.window
        current, history = await asyncio.gather(
            self._breakdown(scope, window, ["grant", "denial"]),
            self._read(
                scope, "month", add_months(window.first_day, -12), window.first_day, ["grant"]
            ),
        )
        await self._name(scope, current)
        inputs = HygieneInputs(grants=current, history=history, denials=current)
        return hygiene_report(context, inputs, now, stale_after=STALE_AFTER)

    async def unattributed(
        self, actor: Actor, filters: AnalyticsFilters, *, limit: int = ROW_LIMIT
    ) -> AnalyticsUnattributed:
        context, _ = await self._prepare(actor, filters, limit=limit, grants=False)
        costs = await self._costs(context.scope, context.window)
        summaries = await self._breakdown(
            context.scope, context.window, ["api", "unattributed"], costs
        )
        return unattributed_report(
            context, api=summaries, unattributed=summaries, costs=costs
        )

    async def export(
        self, actor: Actor, filters: AnalyticsFilters, view: ExportView
    ) -> tuple[str, str]:
        """One view's rows as CSV, with the file name to save it under."""

        if view == "chargeback":
            report, rows = await self.chargeback(actor, filters)
            window = report.window
            return (
                filename(view, window.breakdown_start, window.breakdown_end),
                to_csv(CHARGEBACK_COLUMNS, rows),
            )
        readers: dict[str, Reader] = {
            "overview": self.overview,
            "consumers": self.consumers,
            "models": self.models,
            "reliability": self.reliability,
            "limits": self.limits,
            "hygiene": self.hygiene,
            "unattributed": self.unattributed,
            "cost": self.cost,
        }
        report = await readers[REPORT_FOR[view]](actor, filters, limit=EXPORT_LIMIT)
        window = report.window
        return (
            filename(view, window.breakdown_start, window.breakdown_end),
            to_csv(COLUMNS[view], table(view, report)),
        )
