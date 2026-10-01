"""The price list administrators manage, and the deployment facts MOSAIC prices. See ADR 0020."""

import asyncio
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from mosaic_api.domain import AuditEvent, MosaicModel, new_id, utc_now
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.pricing import (
    BUILT_IN_CLOUDS,
    CURRENCY,
    DEPLOYMENT_TYPES,
    DeploymentFacts,
    DeploymentPricing,
    EndpointPricing,
    EndpointPricingUpdate,
    PriceBook,
    PriceCreate,
    PriceEntry,
    PriceMatch,
    Pricer,
    PriceSeed,
    PriceVersion,
    Unpriced,
    UnpricedReason,
    build_book,
    cloud_label,
    deployment_facts,
    detect_cloud,
    endpoint_pricing_id,
    load_seed,
    month_first,
    normalize_region,
)
from mosaic_api.repositories import (
    GatewayRepository,
    ModelEndpointRepository,
    PricingRepository,
    UsageRollupRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.telemetry import governed_apis

# The unpriced list shows each deployment's use over this many days.
UNPRICED_DAYS = 30
SAVE_ATTEMPTS = 3

PriceStatus = Literal["current", "upcoming", "past", "corrected"]


class PriceSourceView(MosaicModel):
    title: str
    url: str
    retrieved_on: date | None = None


class PriceView(MosaicModel):
    id: str
    line_id: str
    origin: Literal["seed", "admin"]
    status: PriceStatus
    cloud: str
    cloud_label: str
    publisher: str | None
    model: str
    aliases: list[str]
    version: str | None
    deployment_type: str | None
    regions: list[str] | None
    deployment: str | None
    input_per_million: float | None
    cached_input_per_million: float | None
    output_per_million: float | None
    ptu_hourly: float | None
    monthly_amount: float | None
    effective_from: date
    effective_until: date | None = None
    recorded_at: datetime
    recorded_by: str | None
    sources: list[PriceSourceView]
    note: str | None
    overrides: str | None


class PriceLineView(MosaicModel):
    line_id: str
    # The version in effect today, and the next one, if one is scheduled.
    current: PriceView | None
    upcoming: PriceView | None
    versions: int


class PriceListView(MosaicModel):
    cloud: str
    cloud_label: str
    currency: Literal["USD"]
    as_of: date
    lines: list[PriceLineView]


class PriceHistoryView(MosaicModel):
    line_id: str
    versions: list[PriceView]


class CloudView(MosaicModel):
    key: str
    label: str
    built_in: bool
    prices: int
    endpoints: int


class PricingOverview(MosaicModel):
    currency: Literal["USD"]
    as_of: date
    seed_last_updated: date
    sources: list[PriceSourceView]
    clouds: list[CloudView]
    deployment_types: list[str]
    deployments: int
    priced_deployments: int


class UnpricedRow(MosaicModel):
    key: str
    kind: Literal["deployment", "api"]
    label: str
    endpoint_id: str | None = None
    endpoint_name: str | None = None
    deployment_name: str | None = None
    gateway_name: str | None = None
    model: str | None = None
    version: str | None = None
    deployment_type: str | None = None
    cloud: str | None = None
    region: str | None = None
    declared: bool = False
    reason: UnpricedReason
    message: str
    requests: int = 0
    total_tokens: int = 0


class UnpricedReport(MosaicModel):
    as_of: date
    days: int
    deployments: int
    priced_deployments: int
    rows: list[UnpricedRow]


class DeploymentPricingView(MosaicModel):
    deployment_name: str
    model: str | None
    version: str | None
    deployment_type: str | None
    deployment_type_source: Literal["observed", "admin"] | None
    capacity: int | None
    declared: bool
    priced: bool
    price_id: str | None = None
    reason: UnpricedReason | None = None
    message: str | None = None


class EndpointPricingView(MosaicModel):
    endpoint_id: str
    name: str
    provider: str
    host: str | None
    detected_cloud: str | None
    cloud: str | None
    cloud_label: str
    cloud_source: Literal["detected", "override"] | None
    region: str | None
    region_source: Literal["detected", "override"] | None
    deployments: list[DeploymentPricingView]
    updated_by: str | None = None
    updated_at: datetime | None = None
    # The version of the saved facts, to send back with an update. None until any are saved.
    version: str | None = None


def _status(entry: PriceEntry, versions: list[PriceEntry], today: date) -> PriceStatus:
    later_same_day = [
        other
        for other in versions
        if other.effective_from == entry.effective_from and other.precedence > entry.precedence
    ]
    if later_same_day:
        return "corrected"
    if entry.effective_from > today:
        return "upcoming"
    current = _current(versions, today)
    return "current" if current is not None and current.id == entry.id else "past"


def _current(versions: list[PriceEntry], today: date) -> PriceEntry | None:
    effective = [entry for entry in versions if entry.in_effect(today)]
    if not effective:
        return None
    return max(effective, key=lambda entry: entry.precedence)


def _upcoming(versions: list[PriceEntry], today: date) -> PriceEntry | None:
    future = [entry for entry in versions if entry.effective_from > today]
    if not future:
        return None
    first = min(entry.effective_from for entry in future)
    return max(
        (entry for entry in future if entry.effective_from == first),
        key=lambda entry: entry.precedence,
    )


def price_view(entry: PriceEntry, status: PriceStatus) -> PriceView:
    return PriceView(
        id=entry.id,
        line_id=entry.line_id,
        origin=entry.origin,
        status=status,
        cloud=entry.cloud,
        cloud_label=cloud_label(entry.cloud),
        publisher=entry.publisher,
        model=entry.model,
        aliases=list(entry.aliases),
        version=entry.version,
        deployment_type=entry.deployment_type,
        regions=list(entry.regions) if entry.regions else None,
        deployment=entry.deployment,
        input_per_million=entry.input_per_million,
        cached_input_per_million=entry.cached_input_per_million,
        output_per_million=entry.output_per_million,
        ptu_hourly=entry.ptu_hourly,
        monthly_amount=entry.monthly_amount,
        effective_from=entry.effective_from,
        effective_until=entry.effective_until,
        recorded_at=entry.recorded_at,
        recorded_by=entry.recorded_by,
        sources=[
            PriceSourceView(title=source.title, url=source.url, retrieved_on=source.retrieved_on)
            for source in entry.sources
        ],
        note=entry.note,
        overrides=entry.overrides,
    )


def _host(url: str) -> str | None:
    return urlsplit(url).hostname


class PricingService:
    def __init__(
        self,
        repository: PricingRepository,
        *,
        endpoint_repository: ModelEndpointRepository,
        gateway_repository: GatewayRepository,
        rollup_repository: UsageRollupRepository | None = None,
        seed: PriceSeed | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._endpoints = endpoint_repository
        self._gateways = gateway_repository
        self._rollups = rollup_repository
        self._seed = seed or load_seed()
        self._clock = clock

    def _today(self) -> date:
        return self._clock().date()

    # -- what pricing reads ---------------------------------------------------------------------

    async def book(self, tenant_id: str) -> PriceBook:
        return build_book(await self._repository.list_price_versions(tenant_id), self._seed)

    async def deployments(
        self, tenant_id: str, endpoint_ids: Iterable[str] | None = None
    ) -> dict[str, DeploymentFacts]:
        """Every deployment MOSAIC knows on the chosen endpoints: observed, then declared."""

        wanted = None if endpoint_ids is None else set(endpoint_ids)
        endpoints, stored = await asyncio.gather(
            self._endpoints.list_endpoints(tenant_id),
            self._repository.list_endpoint_pricing(tenant_id),
        )
        settings = {item.endpoint_id: item for item in stored}
        chosen = [endpoint for endpoint in endpoints if wanted is None or endpoint.id in wanted]
        observed = await asyncio.gather(
            *(
                self._endpoints.list_observed_for_endpoint(
                    ObservedModelDeployment, tenant_id, endpoint.id, "observedModelDeployment"
                )
                for endpoint in chosen
            )
        )
        facts: dict[str, DeploymentFacts] = {}
        for endpoint, deployments in zip(chosen, observed, strict=True):
            seen: set[str] = set()
            for deployment in deployments:
                item = deployment_facts(
                    endpoint, observed=deployment, settings=settings.get(endpoint.id)
                )
                facts[item.key] = item
                seen.add(deployment.deployment_name.casefold())
            for declared in endpoint.declared_deployments:
                if declared.deployment_name.casefold() in seen:
                    continue
                item = deployment_facts(
                    endpoint, declared=declared, settings=settings.get(endpoint.id)
                )
                facts[item.key] = item
        return facts

    async def pricer(self, tenant_id: str, endpoint_ids: Iterable[str] | None = None) -> Pricer:
        book, facts, since = await asyncio.gather(
            self.book(tenant_id),
            self.deployments(tenant_id, endpoint_ids),
            self._first_seen(tenant_id),
        )
        return Pricer(book, facts, since)

    async def _first_seen(self, tenant_id: str) -> dict[str, date]:
        """The day MOSAIC first saw a governed API fronting each deployment."""

        if self._rollups is None:
            return {}
        seen: dict[str, date] = {}
        for state in await self._rollups.list_rollup_states(tenant_id):
            for api in state.apis:
                key = api.deployment_key
                if not key:
                    continue
                day = api.first_seen_at.date()
                folded = key.casefold()
                if folded not in seen or day < seen[folded]:
                    seen[folded] = day
        return seen

    async def provisioned_tokens(
        self, tenant_id: str, keys: Iterable[str], first: date, last: date
    ) -> dict[tuple[str, date], int]:
        """Each provisioned deployment's tokens by month, across every gateway and caller.

        The rollup job folds a month's total only after a cycle succeeds, so a month's total can
        trail its days, for example after a backfill cycle failed part way. So the days are added
        up too, for every month they're still kept for, and the larger figure counts. Shares of
        a month's reserved cost then never add up to more than the whole.
        """

        wanted = {key.casefold() for key in keys}
        if not wanted or self._rollups is None:
            return {}
        summaries = await self._rollups.list_summaries(
            tenant_id,
            period="month",
            start=month_first(first).isoformat(),
            end=month_first(last).isoformat(),
            dimensions=["deployment"],
        )
        totals: dict[tuple[str, date], int] = defaultdict(int)
        for summary in summaries:
            month = date.fromisoformat(summary.period_start)
            for entry in summary.entries:
                if entry.key.casefold() in wanted:
                    totals[(entry.key.casefold(), month)] += entry.metrics.total_tokens
        until = min(last, self._today())
        if month_first(first) <= until:
            days: dict[tuple[str, date], int] = defaultdict(int)
            for summary in await self._rollups.list_summaries(
                tenant_id,
                period="day",
                start=month_first(first).isoformat(),
                end=until.isoformat(),
                dimensions=["deployment"],
            ):
                month = month_first(date.fromisoformat(summary.period_start))
                for entry in summary.entries:
                    if entry.key.casefold() in wanted:
                        days[(entry.key.casefold(), month)] += entry.metrics.total_tokens
            for month_key, tokens in days.items():
                totals[month_key] = max(totals.get(month_key, 0), tokens)
        return dict(totals)

    # -- the price list -------------------------------------------------------------------------

    def _lines(self, book: PriceBook) -> dict[str, list[PriceEntry]]:
        lines: dict[str, list[PriceEntry]] = defaultdict(list)
        for entry in book.entries:
            lines[entry.line_id].append(entry)
        return lines

    async def overview(self, actor: Actor) -> PricingOverview:
        book, facts = await asyncio.gather(
            self.book(actor.tenant_id), self.deployments(actor.tenant_id)
        )
        today = self._today()
        lines = self._lines(book)
        endpoint_clouds: dict[str, set[str]] = defaultdict(set)
        for item in facts.values():
            if item.cloud:
                endpoint_clouds[item.cloud].add(item.endpoint_id)
        clouds = [
            *BUILT_IN_CLOUDS,
            *sorted((set(book.clouds()) | set(endpoint_clouds)) - set(BUILT_IN_CLOUDS)),
        ]
        priced = sum(
            1 for item in facts.values() if isinstance(book.match(item, today), PriceMatch)
        )
        return PricingOverview(
            currency=CURRENCY,
            as_of=today,
            seed_last_updated=self._seed.last_updated,
            sources=[
                PriceSourceView(
                    title=source.title, url=source.url, retrieved_on=source.retrieved_on
                )
                for source in self._seed.sources
            ],
            clouds=[
                CloudView(
                    key=cloud,
                    label=cloud_label(cloud),
                    built_in=cloud in BUILT_IN_CLOUDS,
                    prices=sum(
                        1
                        for versions in lines.values()
                        if versions[0].cloud == cloud
                        and (_current(versions, today) or _upcoming(versions, today))
                    ),
                    endpoints=len(endpoint_clouds.get(cloud, set())),
                )
                for cloud in clouds
            ],
            deployment_types=list(DEPLOYMENT_TYPES),
            deployments=len(facts),
            priced_deployments=priced,
        )

    async def price_list(self, actor: Actor, cloud: str) -> PriceListView:
        book = await self.book(actor.tenant_id)
        today = self._today()
        lines: list[PriceLineView] = []
        for line_id, versions in self._lines(book).items():
            if versions[0].cloud != cloud:
                continue
            current = _current(versions, today)
            upcoming = _upcoming(versions, today)
            if current is None and upcoming is None:
                # A replaced seeded price that has ended. It still prices the days it covered.
                continue
            lines.append(
                PriceLineView(
                    line_id=line_id,
                    current=price_view(current, "current") if current else None,
                    upcoming=price_view(upcoming, "upcoming") if upcoming else None,
                    versions=len(versions),
                )
            )

        def order(line: PriceLineView) -> tuple[str, ...]:
            shown = line.current or line.upcoming
            assert shown is not None
            return (
                (shown.publisher or "").casefold(),
                "~" if shown.model == "*" else shown.model.casefold(),
                shown.version or "",
                shown.deployment_type or "",
                ",".join(shown.regions or []),
                shown.deployment or "",
            )

        lines.sort(key=order)
        return PriceListView(
            cloud=cloud,
            cloud_label=cloud_label(cloud),
            currency=CURRENCY,
            as_of=today,
            lines=lines,
        )

    async def history(self, actor: Actor, line_id: str) -> PriceHistoryView:
        book = await self.book(actor.tenant_id)
        versions = self._lines(book).get(line_id)
        if not versions:
            raise NotFoundError("No price has that ID", details={"lineId": line_id})
        today = self._today()
        ordered = sorted(versions, key=lambda entry: entry.precedence, reverse=True)
        return PriceHistoryView(
            line_id=line_id,
            versions=[price_view(entry, _status(entry, versions, today)) for entry in ordered],
        )

    async def add_price(self, actor: Actor, request: PriceCreate) -> PriceView:
        """Record a price, or a new version of one. Nothing already recorded changes."""

        if request.deployment is not None:
            facts = await self.deployments(
                actor.tenant_id, [request.deployment.partition("/")[0]]
            )
            if request.deployment not in facts:
                raise ValidationError(
                    "MOSAIC has no record of that deployment",
                    details={"deployment": request.deployment},
                )
        if request.overrides is not None:
            book = await self.book(actor.tenant_id)
            if not any(entry.id == request.overrides for entry in book.entries):
                raise ValidationError(
                    "The price this overrides doesn't exist",
                    details={"overrides": request.overrides},
                )
        version = PriceVersion(
            id=new_id("price"),
            tenant_id=actor.tenant_id,
            cloud=request.cloud,
            publisher=request.publisher,
            model=request.model,
            aliases=request.aliases,
            version=request.version,
            deployment_type=request.deployment_type,
            regions=request.regions,
            deployment=request.deployment,
            input_per_million=request.input_per_million,
            cached_input_per_million=request.cached_input_per_million,
            output_per_million=request.output_per_million,
            ptu_hourly=request.ptu_hourly,
            monthly_amount=request.monthly_amount,
            effective_from=request.effective_from,
            source_url=request.source_url,
            note=request.note,
            overrides=request.overrides,
            recorded_by=actor.object_id,
        )
        saved = await self._repository.create_price_version(
            version,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                action="pricing.priceRecorded",
                resource_type="priceVersion",
                resource_id=version.id,
                actor_object_id=actor.object_id,
                details={
                    "cloud": version.cloud,
                    "model": version.model,
                    "version": version.version,
                    "deploymentType": version.deployment_type,
                    "regions": version.regions,
                    "deployment": version.deployment,
                    "inputPerMillion": version.input_per_million,
                    "cachedInputPerMillion": version.cached_input_per_million,
                    "outputPerMillion": version.output_per_million,
                    "ptuHourly": version.ptu_hourly,
                    "monthlyAmount": version.monthly_amount,
                    "effectiveFrom": version.effective_from.isoformat(),
                    "sourceUrl": version.source_url,
                    "overrides": version.overrides,
                },
            ),
        )
        book = await self.book(actor.tenant_id)
        entry = next(item for item in book.entries if item.id == saved.id)
        versions = self._lines(book)[entry.line_id]
        return price_view(entry, _status(entry, versions, self._today()))

    # -- what isn't priced ----------------------------------------------------------------------

    async def unpriced(self, actor: Actor) -> UnpricedReport:
        """Every deployment MOSAIC knows that has no price today, and adopted model APIs whose
        deployment MOSAIC can't tell, busiest first."""

        tenant = actor.tenant_id
        today = self._today()
        first = today - timedelta(days=UNPRICED_DAYS - 1)
        book, facts, gateways, governed = await asyncio.gather(
            self.book(tenant),
            self.deployments(tenant),
            self._gateways.list_gateways(tenant),
            governed_apis(self._gateways, tenant, now=self._clock()),
        )
        states = await self._rollups.list_rollup_states(tenant) if self._rollups else []
        usage: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
        api_usage: dict[tuple[str, str], tuple[int, int]] = defaultdict(lambda: (0, 0))
        if self._rollups is not None:
            summaries = await self._rollups.list_summaries(
                tenant,
                period="day",
                start=first.isoformat(),
                end=today.isoformat(),
                dimensions=["deployment", "api"],
            )
            for summary in summaries:
                for entry in summary.entries:
                    metrics = entry.metrics
                    if summary.dimension == "deployment":
                        requests, tokens = usage[entry.key.casefold()]
                        usage[entry.key.casefold()] = (
                            requests + metrics.requests,
                            tokens + metrics.total_tokens,
                        )
                    else:
                        api_key = (summary.gateway_id, entry.key)
                        requests, tokens = api_usage[api_key]
                        api_usage[api_key] = (
                            requests + metrics.requests,
                            tokens + metrics.total_tokens,
                        )
        rows: list[UnpricedRow] = []
        priced = 0
        for item in facts.values():
            match = book.match(item, today)
            if isinstance(match, PriceMatch):
                priced += 1
                continue
            requests, tokens = usage.get(item.key.casefold(), (0, 0))
            rows.append(
                UnpricedRow(
                    key=item.key,
                    kind="deployment",
                    label=item.deployment_name,
                    endpoint_id=item.endpoint_id,
                    endpoint_name=item.endpoint_name,
                    deployment_name=item.deployment_name,
                    model=item.model,
                    version=item.version,
                    deployment_type=item.deployment_type,
                    cloud=item.cloud,
                    region=item.region,
                    declared=item.declared,
                    reason=match.reason,
                    message=match.message,
                    requests=requests,
                    total_tokens=tokens,
                )
            )
        names = {gateway.id: gateway.name for gateway in gateways}
        apis = {(state.gateway_id, api.api_name): api for state in states for api in state.apis}
        for gateway_id, current in governed.items():
            for api in current:
                apis[(gateway_id, api.api_name)] = api
        for (gateway_id, api_name), api in sorted(apis.items()):
            if api.kind != "model" or api.deployment_key is not None:
                continue
            requests, tokens = api_usage.get((gateway_id, api_name), (0, 0))
            if tokens <= 0:
                continue
            rows.append(
                UnpricedRow(
                    key=f"{gateway_id}/{api_name}",
                    kind="api",
                    label=api.display_name,
                    gateway_name=names.get(gateway_id, "Removed gateway"),
                    reason="unknownDeployment",
                    message=(
                        "MOSAIC adopted this API rather than publishing it, so it doesn't know "
                        "which deployment its calls reach."
                    ),
                    requests=requests,
                    total_tokens=tokens,
                )
            )
        rows.sort(key=lambda row: (-row.total_tokens, -row.requests, row.label.casefold()))
        return UnpricedReport(
            as_of=today,
            days=UNPRICED_DAYS,
            deployments=len(facts),
            priced_deployments=priced,
            rows=rows,
        )

    # -- each endpoint's pricing facts ----------------------------------------------------------

    async def endpoints(self, actor: Actor) -> list[EndpointPricingView]:
        tenant = actor.tenant_id
        endpoints, stored, facts, book = await asyncio.gather(
            self._endpoints.list_endpoints(tenant),
            self._repository.list_endpoint_pricing(tenant),
            self.deployments(tenant),
            self.book(tenant),
        )
        settings = {item.endpoint_id: item for item in stored}
        today = self._today()
        by_endpoint: dict[str, list[DeploymentFacts]] = defaultdict(list)
        for item in facts.values():
            by_endpoint[item.endpoint_id].append(item)
        views: list[EndpointPricingView] = []
        for endpoint in endpoints:
            current = settings.get(endpoint.id)
            detected = detect_cloud(str(endpoint.endpoint))
            override = current.cloud if current else None
            cloud = override or detected
            detected_region = normalize_region(endpoint.capabilities.location)
            region_override = current.region if current else None
            deployments: list[DeploymentPricingView] = []
            for item in sorted(
                by_endpoint.get(endpoint.id, []), key=lambda value: value.deployment_name.casefold()
            ):
                match = book.match(item, today)
                deployments.append(
                    DeploymentPricingView(
                        deployment_name=item.deployment_name,
                        model=item.model,
                        version=item.version,
                        deployment_type=item.deployment_type,
                        deployment_type_source=item.deployment_type_source,
                        capacity=item.capacity,
                        declared=item.declared,
                        priced=isinstance(match, PriceMatch),
                        price_id=match.entry.id if isinstance(match, PriceMatch) else None,
                        reason=match.reason if isinstance(match, Unpriced) else None,
                        message=match.message if isinstance(match, Unpriced) else None,
                    )
                )
            views.append(
                EndpointPricingView(
                    endpoint_id=endpoint.id,
                    name=endpoint.name,
                    provider=str(endpoint.provider),
                    host=_host(str(endpoint.endpoint)),
                    detected_cloud=detected,
                    cloud=cloud,
                    cloud_label=cloud_label(cloud),
                    cloud_source="override" if override else "detected" if detected else None,
                    region=region_override or detected_region,
                    region_source=(
                        "override" if region_override else "detected" if detected_region else None
                    ),
                    deployments=deployments,
                    updated_by=current.updated_by if current else None,
                    updated_at=current.updated_at if current else None,
                    version=current.etag if current else None,
                )
            )
        return views

    async def update_endpoint(
        self, actor: Actor, endpoint_id: str, update: EndpointPricingUpdate
    ) -> EndpointPricingView:
        """Change the facts named in ``update``, keeping every other one as it is saved now."""

        tenant = actor.tenant_id
        endpoint = await self._endpoints.get_endpoint(tenant, endpoint_id)
        if endpoint is None:
            raise NotFoundError("Model endpoint was not found", details={"id": endpoint_id})
        observed = await self._endpoints.list_observed_for_endpoint(
            ObservedModelDeployment, tenant, endpoint_id, "observedModelDeployment"
        )
        typed = {
            item.deployment_name.casefold() for item in observed if item.sku_name
        }
        known = {item.deployment_name.casefold() for item in observed} | {
            item.deployment_name.casefold() for item in endpoint.declared_deployments
        }
        for item in update.deployments:
            name = item.deployment_name.casefold()
            if name not in known:
                raise ValidationError(
                    f"{endpoint.name} has no deployment named {item.deployment_name}",
                    details={"deploymentName": item.deployment_name},
                )
            if name in typed and (item.deployment_type or item.capacity):
                raise ValidationError(
                    f"Azure reports {item.deployment_name}'s deployment type and capacity, so "
                    "MOSAIC uses those.",
                    details={"deploymentName": item.deployment_name},
                )
        detected = detect_cloud(str(endpoint.endpoint))
        fields = update.model_fields_set
        for attempt in range(SAVE_ATTEMPTS):
            current = await self._repository.get_endpoint_pricing(tenant, endpoint_id)
            if "version" in fields and update.version != (current.etag if current else None):
                raise ConflictError(
                    "Someone changed this endpoint's pricing after you opened it. Reload it and "
                    "make your change again.",
                    details={"endpointId": endpoint_id},
                )
            settings = current or EndpointPricing(
                id=endpoint_pricing_id(tenant, endpoint_id),
                tenant_id=tenant,
                endpoint_id=endpoint_id,
            )
            cloud = settings.cloud
            if "cloud" in fields:
                cloud = None if update.cloud == detected else update.cloud
            region = settings.region
            if "region" in fields:
                region = update.region
            deployments = {item.deployment_name.casefold(): item for item in settings.deployments}
            for item in update.deployments:
                name = item.deployment_name.casefold()
                if item.deployment_type is None and item.capacity is None:
                    deployments.pop(name, None)
                else:
                    deployments[name] = DeploymentPricing(
                        deployment_name=item.deployment_name,
                        deployment_type=item.deployment_type,
                        capacity=item.capacity,
                    )
            changed = settings.model_copy(
                update={
                    "cloud": cloud,
                    "region": region,
                    "deployments": sorted(
                        deployments.values(), key=lambda value: value.deployment_name.casefold()
                    ),
                    "updated_by": actor.object_id,
                    "updated_at": utc_now(),
                }
            )
            try:
                await self._repository.save_endpoint_pricing(
                    changed,
                    AuditEvent(
                        id=new_id("audit"),
                        tenant_id=tenant,
                        action="pricing.endpointUpdated",
                        resource_type="modelEndpoint",
                        resource_id=endpoint_id,
                        actor_object_id=actor.object_id,
                        details={
                            "cloud": changed.cloud,
                            "detectedCloud": detected,
                            "region": changed.region,
                            "deployments": [
                                item.model_dump(by_alias=True) for item in changed.deployments
                            ],
                        },
                    ),
                )
                break
            except ConflictError:
                # The facts are merged field by field, so applying them to what someone else
                # just saved keeps both changes.
                if attempt == SAVE_ATTEMPTS - 1:
                    raise
        views = await self.endpoints(actor)
        return next(view for view in views if view.endpoint_id == endpoint_id)
