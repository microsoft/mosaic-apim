"""The price list administrators manage, and the deployment facts MOSAIC prices. See ADR 0020."""

import asyncio
import hashlib
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic import ValidationError as PydanticValidationError

from mosaic_api.billing_pricing import (
    BillingAcquisitionKind,
    BillingAcquisitionRun,
    BillingCandidateMapping,
    BillingCandidateRate,
    BillingConnection,
    BillingConnectionCheckResult,
    BillingConnectionCreate,
    BillingConnectionUpdate,
    BillingConnectionView,
    BillingConnector,
    BillingConnectorStepResult,
    BillingImportRequest,
    BillingMappingsUpdateRequest,
    BillingProvider,
    BillingPublication,
    BillingPublicationView,
    BillingPublishRequest,
    BillingReviewIssue,
    BillingRunDetailView,
    BillingRunSummaryView,
    BillingRunTriggerRequest,
    BillingSchedulerStatusView,
    BillingSourceTemplateView,
    billing_connection_view,
    billing_evidence_ref,
    billing_publication_id,
    billing_publication_view,
    billing_run_detail_view,
    billing_run_summary_view,
    validate_source_month,
)
from mosaic_api.domain import (
    AuditEvent,
    KeyVaultSecretId,
    MosaicModel,
    deterministic_id,
    new_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, DomainError, NotFoundError, ValidationError
from mosaic_api.integrations.key_vault import MANAGED_BY, MANAGED_BY_TAG, KeyStore
from mosaic_api.integrations.mcp.credentials import KeyVaultSecretReader
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.pricing import (
    BUILT_IN_CLOUDS,
    CLOUD_KEY,
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
    canonical_deployment_type,
    cloud_label,
    deployment_facts,
    detect_cloud,
    endpoint_pricing_id,
    is_provisioned,
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
from mosaic_api.services.billing_ingestion import (
    DEFAULT_MAX_DECOMPRESSED_BYTES,
    DEFAULT_MAX_ROWS,
    DEFAULT_MAX_UPLOAD_BYTES,
    BillingPayloadTooLargeError,
    billing_source_templates,
    chunk_evidence_items,
    ingest_billing_bytes,
    ingest_billing_parts,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.telemetry import governed_apis
from mosaic_api.usage_telemetry import RolledUpApi

# The unpriced list shows each deployment's use over this many days.
UNPRICED_DAYS = 30
SAVE_ATTEMPTS = 3

_PROVIDER_LABELS: dict[str, str] = {
    "azure": "Azure",
    "aws": "AWS",
    "gcp": "GCP",
    "mosaicTemplate": "MOSAIC template",
}


class _InMemoryBillingSecretStore:
    """In-memory Key Vault secret store used when no external KeyStore is configured."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def new_secret(self, name: str) -> KeyVaultSecretId:
        return KeyVaultSecretId.parse(f"https://mosaic-local.vault.azure.net/secrets/{name}")

    async def put(
        self, secret: KeyVaultSecretId, value: SecretStr, *, tags: dict[str, str]
    ) -> None:
        _ = tags
        self.values[secret.versionless] = value.get_secret_value()

    async def delete(self, secret: KeyVaultSecretId) -> bool:
        return self.values.pop(secret.versionless, None) is not None

    async def read(self, uri: str) -> str:
        parsed = KeyVaultSecretId.parse(uri)
        if parsed.versionless not in self.values:
            raise ValidationError("That Key Vault secret does not exist.")
        return self.values[parsed.versionless]

PriceStatus = Literal["current", "upcoming", "past", "corrected"]


class PriceSourceView(MosaicModel):
    title: str
    url: str | None = None
    retrieved_on: date | None = None
    reference: str | None = None
    kind: Literal["external", "billingRun"] = "external"
    run_id: str | None = None
    source_month: str | None = None


class PriceView(MosaicModel):
    id: str
    line_id: str
    origin: Literal["seed", "admin", "billing"]
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
    billing_run_id: str | None = None
    publication_id: str | None = None
    source_month: str | None = None
    billing_provider: str | None = None


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
    pricing_basis: Literal["publicSeed", "customManual", "billingDerived", "mixed"] = "publicSeed"
    billing_connections: int = 0
    pending_billing_reviews: int = 0
    last_billing_published_at: datetime | None = None
    last_billing_source_month: str | None = None
    billing_scheduler_always_on: bool = False
    scheduled_workers_always_on: bool = False
    scheduled_billing_connections: int = 0
    billing_schedule_warning: str | None = None


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
            PriceSourceView(
                title=source.title,
                url=source.url,
                retrieved_on=source.retrieved_on,
                reference=source.reference,
                kind=source.kind,
                run_id=source.run_id,
                source_month=source.source_month,
            )
            for source in entry.sources
        ],
        note=entry.note,
        overrides=entry.overrides,
        billing_run_id=entry.billing_run_id,
        publication_id=entry.publication_id,
        source_month=entry.source_month,
        billing_provider=entry.billing_provider,
    )


def _host(url: str) -> str | None:
    return urlsplit(url).hostname


def _fronted_since(api: RolledUpApi) -> list[tuple[str, datetime]]:
    """Each deployment an API's calls reach, and since when MOSAIC has seen it do so."""

    if api.kind == "pool":
        return [
            (member.deployment_key, member.first_seen_at or api.first_seen_at)
            for member in api.members
        ]
    key = api.deployment_key
    return [(key, api.first_seen_at)] if key else []


_REDACT_URL_QUERY_RE = re.compile(r"(https?://[^\s\"'<>?]+)\?[^\s\"'<>]*", re.IGNORECASE)
_REDACT_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_REDACT_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_REDACT_PEM_RE = re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL)


def _safe_billing_error_message(
    error: Exception,
    *,
    secret_value: str | None = None,
    max_length: int = 500,
) -> str:
    raw = (
        error.summary
        if isinstance(error, DomainError)
        else (str(error).strip() or f"{type(error).__name__} during billing acquisition.")
    )
    redacted = _REDACT_PEM_RE.sub("[REDACTED_PRIVATE_KEY]", raw)
    redacted = _REDACT_URL_QUERY_RE.sub(r"\1", redacted)
    redacted = _REDACT_BEARER_RE.sub("******", redacted)
    redacted = _REDACT_AWS_KEY_RE.sub("[REDACTED_AWS_KEY]", redacted)
    if secret_value and len(secret_value.strip()) >= 4:
        redacted = redacted.replace(secret_value.strip(), "[REDACTED]")
    compact = " ".join("".join(ch if ch.isprintable() else " " for ch in redacted).split())
    if len(compact) > max_length:
        return f"{compact[: max_length - 1].rstrip()}…"
    return compact or "Billing acquisition failed."


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
        billing_connectors: Mapping[str, BillingConnector] | None = None,
        secret_store: KeyStore | None = None,
        secret_reader: KeyVaultSecretReader | Callable[[str], Awaitable[str]] | None = None,
        scheduled_workers_always_on: bool = False,
    ) -> None:
        self._repository = repository
        self._endpoints = endpoint_repository
        self._gateways = gateway_repository
        self._rollups = rollup_repository
        self._seed = seed or load_seed()
        self._clock = clock
        self._billing_connectors: dict[str, BillingConnector] = (
            dict(billing_connectors) if billing_connectors is not None else {}
        )
        default_store = _InMemoryBillingSecretStore()
        self._secret_store: KeyStore = secret_store or default_store
        self._secret_reader = secret_reader
        self._default_secret_store = default_store
        self._scheduled_workers_always_on = scheduled_workers_always_on

    @property
    def scheduled_workers_always_on(self) -> bool:
        return self._scheduled_workers_always_on

    def register_billing_connector(self, connector: BillingConnector) -> None:
        self._billing_connectors[connector.provider] = connector

    async def _read_secret(self, secret_uri: str) -> str:
        try:
            parsed = KeyVaultSecretId.parse(secret_uri)
            if parsed.versionless in self._default_secret_store.values:
                return self._default_secret_store.values[parsed.versionless]
        except ValueError:
            pass
        if self._secret_reader is not None:
            if hasattr(self._secret_reader, "read"):
                return await self._secret_reader.read(secret_uri)
            return await self._secret_reader(secret_uri)
        if hasattr(self._secret_store, "read"):
            read_fn = self._secret_store.read
            return await read_fn(secret_uri)  # type: ignore[no-any-return]
        return await self._default_secret_store.read(secret_uri)

    def _today(self) -> date:
        return self._clock().date()

    # -- what pricing reads ---------------------------------------------------------------------

    async def book(self, tenant_id: str) -> PriceBook:
        versions, publications = await asyncio.gather(
            self._repository.list_price_versions(tenant_id),
            self._repository.list_billing_publications(tenant_id),
        )
        return build_book(
            versions,
            self._seed,
            published_ids={publication.id for publication in publications},
        )

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
                for key, since in _fronted_since(api):
                    day = since.date()
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
        book, facts, connections, runs, publications = await asyncio.gather(
            self.book(actor.tenant_id),
            self.deployments(actor.tenant_id),
            self._repository.list_billing_connections(actor.tenant_id),
            self._repository.list_billing_runs(actor.tenant_id),
            self._repository.list_billing_publications(actor.tenant_id),
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
        matched_origins: set[str] = set()
        priced = 0
        for item in facts.values():
            match = book.match(item, today)
            if isinstance(match, PriceMatch):
                priced += 1
                matched_origins.add(match.entry.origin)
        non_seed_origins = {
            current.origin
            for versions in lines.values()
            if (current := _current(versions, today)) is not None and current.origin != "seed"
        }
        pricing_basis: Literal["publicSeed", "customManual", "billingDerived", "mixed"] = (
            "publicSeed"
        )
        if matched_origins:
            if matched_origins == {"seed"} and not non_seed_origins:
                pricing_basis = "publicSeed"
            elif matched_origins == {"admin"}:
                pricing_basis = "customManual"
            elif matched_origins == {"billing"}:
                pricing_basis = "billingDerived"
            else:
                pricing_basis = "mixed"
        elif non_seed_origins == {"admin"}:
            pricing_basis = "customManual"
        elif non_seed_origins == {"billing"}:
            pricing_basis = "billingDerived"
        elif len(non_seed_origins) > 1:
            pricing_basis = "mixed"

        latest_publication = (
            max(publications, key=lambda item: (item.published_at, item.id))
            if publications
            else None
        )
        scheduled_connections = sum(1 for conn in connections if conn.schedule.enabled)
        schedule_warning = (
            "Monthly scheduled billing draft refreshes are enabled, but "
            "MOSAIC_SCHEDULED_WORKERS_ALWAYS_ON is false. Keep at least one API instance "
            "always on so scheduled monthly draft retrievals run on time."
            if scheduled_connections > 0 and not self._scheduled_workers_always_on
            else None
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
            pricing_basis=pricing_basis,
            billing_connections=len(connections),
            pending_billing_reviews=sum(1 for run in runs if run.status == "readyForReview"),
            last_billing_published_at=(
                latest_publication.published_at if latest_publication else None
            ),
            last_billing_source_month=(
                latest_publication.source_month if latest_publication else None
            ),
            billing_scheduler_always_on=self._scheduled_workers_always_on,
            scheduled_workers_always_on=self._scheduled_workers_always_on,
            scheduled_billing_connections=scheduled_connections,
            billing_schedule_warning=schedule_warning,
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
            facts = await self.deployments(actor.tenant_id, [request.deployment.partition("/")[0]])
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
        """Every deployment MOSAIC knows that has no price today, adopted model APIs whose
        deployment MOSAIC can't tell, and pools' calls MOSAIC couldn't place on a member, busiest
        first."""

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
        # A pool API's calls that no member is known to have served.
        unplaced: dict[tuple[str, str], tuple[int, int]] = defaultdict(lambda: (0, 0))
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
                        members = (metrics.members or {}).values()
                        requests, tokens = unplaced[api_key]
                        unplaced[api_key] = (
                            requests + max(metrics.requests - sum(m.requests for m in members), 0),
                            tokens
                            + max(metrics.total_tokens - sum(m.total_tokens for m in members), 0),
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
            if api.kind == "pool":
                requests, tokens = unplaced.get((gateway_id, api_name), (0, 0))
                if tokens > 0:
                    rows.append(
                        UnpricedRow(
                            key=f"{gateway_id}/{api_name}",
                            kind="api",
                            label=api.display_name,
                            gateway_name=names.get(gateway_id, "Removed gateway"),
                            reason="unknownDeployment",
                            message=(
                                "MOSAIC couldn't tell which of this pool's deployments served "
                                "some of its calls, so it can't price them."
                            ),
                            requests=requests,
                            total_tokens=tokens,
                        )
                    )
                continue
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
            detected = (
                endpoint.azure_cloud
                if endpoint.azure_resource_id
                else detect_cloud(str(endpoint.endpoint))
            )
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
        typed = {item.deployment_name.casefold() for item in observed if item.sku_name}
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
        detected = (
            endpoint.azure_cloud
            if endpoint.azure_resource_id
            else detect_cloud(str(endpoint.endpoint))
        )
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

    # -- billing-derived imports and review -----------------------------------------------------

    async def billing_templates(self, actor: Actor) -> list[BillingSourceTemplateView]:
        _ = actor
        return billing_source_templates()

    async def import_billing_data(
        self,
        actor: Actor,
        *,
        provider: BillingProvider,
        pricing_cloud: str,
        source_month: str | None = None,
        file_name: str = "upload.csv",
        content: bytes,
        source_scope: str | None = None,
        force: bool = False,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> BillingRunDetailView:
        cleaned_cloud = pricing_cloud.strip().casefold()
        if not CLOUD_KEY.fullmatch(cleaned_cloud):
            raise ValidationError(
                "A pricing cloud key is up to 40 lowercase letters, digits, and hyphens",
                details={"pricingCloud": pricing_cloud},
            )
        today = self._today()
        try:
            resolved_month = validate_source_month(source_month, today=today)
        except ValueError as exc:
            raise ValidationError(str(exc), details={"sourceMonth": source_month}) from exc

        if len(content) > max_upload_bytes:
            raise BillingPayloadTooLargeError(
                f"Billing upload ({len(content):,} bytes) exceeds the maximum allowed size "
                f"({max_upload_bytes:,} bytes).",
                details={"fileName": file_name, "maxUploadBytes": max_upload_bytes},
            )

        cleaned_scope = source_scope.strip() if source_scope and source_scope.strip() else None
        cleaned_file_name = file_name.strip() if file_name and file_name.strip() else "upload.csv"
        fingerprint = hashlib.sha256(content).hexdigest()

        existing_runs = await self._repository.list_billing_runs(actor.tenant_id)
        duplicate_run = next(
            (
                run
                for run in existing_runs
                if run.status not in ("failed", "superseded")
                and run.provider == provider
                and run.pricing_cloud == cleaned_cloud
                and run.source_month == resolved_month
                and run.content_fingerprint == fingerprint
            ),
            None,
        )
        if duplicate_run is not None and not force:
            existing_chunks = await self._repository.list_billing_evidence_chunks(
                actor.tenant_id, duplicate_run.id
            )
            dup_issue = BillingReviewIssue(
                code="duplicateUpload",
                severity="info",
                message=(
                    f"Uploaded billing file matches existing acquisition run {duplicate_run.id} "
                    f"(SHA-256 fingerprint {fingerprint[:12]}...); returning the existing run."
                ),
                details={"duplicateOfRunId": duplicate_run.id, "contentFingerprint": fingerprint},
            )
            dup_copy = duplicate_run.model_copy(
                update={
                    "duplicate_of_run_id": duplicate_run.id,
                    "issues": [*duplicate_run.issues, dup_issue],
                }
            )
            return billing_run_detail_view(dup_copy, existing_chunks)

        book, deployments = await asyncio.gather(
            self.book(actor.tenant_id),
            self.deployments(actor.tenant_id),
        )
        ingested = ingest_billing_bytes(
            content,
            file_name=cleaned_file_name,
            provider=provider,
            pricing_cloud=cleaned_cloud,
            source_month=resolved_month,
            source_scope=cleaned_scope,
            deployments=deployments,
            book=book,
            today=today,
            max_upload_bytes=max_upload_bytes,
            max_decompressed_bytes=max_decompressed_bytes,
            max_rows=max_rows,
        )

        now = self._clock()
        run_id = new_id("billrun")
        chunks = chunk_evidence_items(actor.tenant_id, run_id, ingested.evidence_items)
        run = BillingAcquisitionRun(
            id=run_id,
            tenant_id=actor.tenant_id,
            provider=provider,
            pricing_cloud=cleaned_cloud,
            acquisition_kind="upload",
            source_month=resolved_month,
            invoice_months=ingested.invoice_months,
            status="readyForReview",
            status_message=(
                f"Normalized {ingested.eligible_row_count} eligible row(s) and derived "
                f"{len(ingested.candidates)} candidate rate(s)."
            ),
            source_scope=ingested.source_scope,
            source_file_name=cleaned_file_name,
            content_fingerprint=fingerprint,
            duplicate_of_run_id=duplicate_run.id if duplicate_run is not None else None,
            started_at=now,
            completed_at=now,
            effective_from_default=today,
            raw_row_count=ingested.raw_row_count,
            eligible_row_count=ingested.eligible_row_count,
            excluded_row_count=ingested.excluded_row_count,
            total_read_bytes=ingested.total_read_bytes,
            total_eligible_cost_usd=ingested.total_eligible_cost_usd,
            total_excluded_cost_usd=ingested.total_excluded_cost_usd,
            evidence_chunk_count=len(chunks),
            evidence_item_count=len(ingested.evidence_items),
            candidates=ingested.candidates,
            mappings=ingested.mappings,
            issues=ingested.issues,
            created_by=actor.object_id,
        )

        await self._repository.save_billing_evidence_chunks(actor.tenant_id, run_id, chunks)
        saved_run = await self._repository.create_billing_run(
            run,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                action="pricing.billingImportRecorded",
                resource_type="billingAcquisitionRun",
                resource_id=run_id,
                actor_object_id=actor.object_id,
                details={
                    "provider": provider,
                    "pricingCloud": cleaned_cloud,
                    "sourceMonth": resolved_month,
                    "sourceFileName": cleaned_file_name,
                    "contentFingerprint": fingerprint,
                    "rawRowCount": ingested.raw_row_count,
                    "eligibleRowCount": ingested.eligible_row_count,
                    "candidateCount": len(ingested.candidates),
                },
            ),
        )

        # Mark any older readyForReview upload draft for the same source/month as superseded
        for old_run in existing_runs:
            if (
                old_run.id != saved_run.id
                and old_run.status == "readyForReview"
                and old_run.connection_id is None
                and old_run.provider == provider
                and old_run.pricing_cloud == cleaned_cloud
                and old_run.source_month == resolved_month
                and old_run.source_scope == ingested.source_scope
            ):
                try:
                    await self._repository.save_billing_run(
                        old_run.model_copy(update={"status": "superseded"}),
                        AuditEvent(
                            id=new_id("audit"),
                            tenant_id=actor.tenant_id,
                            action="pricing.billingRunSuperseded",
                            resource_type="billingAcquisitionRun",
                            resource_id=old_run.id,
                            actor_object_id=actor.object_id,
                            details={"supersededByRunId": saved_run.id},
                        ),
                    )
                except ConflictError:
                    pass

        return billing_run_detail_view(saved_run, chunks)

    async def list_billing_runs(
        self, actor: Actor, *, connection_id: str | None = None
    ) -> list[BillingRunSummaryView]:
        runs = await self._repository.list_billing_runs(
            actor.tenant_id, connection_id=connection_id
        )
        return [billing_run_summary_view(run) for run in runs]

    async def get_billing_run(self, actor: Actor, run_id: str) -> BillingRunDetailView:
        run = await self._repository.get_billing_run(actor.tenant_id, run_id)
        if run is None:
            raise NotFoundError(
                "Billing acquisition run was not found", details={"id": run_id}
            )
        chunks = await self._repository.list_billing_evidence_chunks(actor.tenant_id, run.id)
        return billing_run_detail_view(run, chunks)

    # -- connected billing sources and acquisition runs -----------------------------------------

    async def list_billing_connections(self, actor: Actor) -> list[BillingConnectionView]:
        connections = await self._repository.list_billing_connections(actor.tenant_id)
        return [billing_connection_view(item) for item in connections]

    async def get_billing_connection(
        self, actor: Actor, connection_id: str
    ) -> BillingConnectionView:
        connection = await self._repository.get_billing_connection(
            actor.tenant_id, connection_id
        )
        if connection is None:
            raise NotFoundError(
                "Billing connection was not found", details={"id": connection_id}
            )
        return billing_connection_view(connection)

    async def create_billing_connection(
        self, actor: Actor, request: BillingConnectionCreate
    ) -> BillingConnectionView:
        connection_id = new_id("billconn")
        now = self._clock()
        secret_id: KeyVaultSecretId | None = None
        credential_secret_uri: str | None = None

        if request.credential_secret is not None:
            secret_id = self._secret_store.new_secret(
                f"mosaic-billing-{connection_id.replace('_', '-')}"
            )
            credential_secret_uri = secret_id.versionless
        elif request.credential_secret_uri is not None:
            try:
                parsed = KeyVaultSecretId.parse(request.credential_secret_uri)
            except ValueError as exc:
                raise ValidationError(
                    str(exc), details={"field": "credentialSecretUri"}
                ) from exc
            credential_secret_uri = parsed.versionless

        try:
            connection = BillingConnection(
                id=connection_id,
                tenant_id=actor.tenant_id,
                name=request.name,
                provider=request.provider,
                pricing_cloud=request.pricing_cloud,
                azure_cloud=request.azure_cloud,
                azure_connection_id=request.azure_connection_id,
                azure_scope=request.azure_scope,
                azure_retrieval_mode=request.azure_retrieval_mode,
                azure_export_manifest_url=request.azure_export_manifest_url,
                aws_account_id=request.aws_account_id,
                aws_role_arn=request.aws_role_arn,
                aws_external_id=request.aws_external_id,
                aws_audience=request.aws_audience,
                aws_region=request.aws_region,
                aws_cur_bucket=request.aws_cur_bucket,
                aws_cur_prefix=request.aws_cur_prefix,
                aws_export_name=request.aws_export_name,
                gcp_billing_account_id=request.gcp_billing_account_id,
                gcp_project_id=request.gcp_project_id,
                gcp_dataset_id=request.gcp_dataset_id,
                gcp_table_id=request.gcp_table_id,
                gcp_location=request.gcp_location,
                gcp_service_account_email=request.gcp_service_account_email,
                gcp_workload_identity_audience=request.gcp_workload_identity_audience,
                gcp_max_bytes_billed=request.gcp_max_bytes_billed,
                credential_secret_uri=credential_secret_uri,
                schedule=request.schedule.model_copy(deep=True),
                cost_notice_acknowledged=request.cost_notice_acknowledged,
                created_at=now,
                updated_at=now,
                updated_by=actor.object_id,
            )
        except PydanticValidationError as exc:
            raise ValidationError(
                "Invalid billing connection configuration.",
                details={"errors": exc.errors(include_input=False)},
            ) from exc

        if secret_id is not None and request.credential_secret is not None:
            await self._secret_store.put(
                secret_id,
                request.credential_secret,
                tags={
                    MANAGED_BY_TAG: MANAGED_BY,
                    "mosaicBillingConnection": connection_id,
                },
            )

        try:
            saved = await self._repository.create_billing_connection(
                connection,
                AuditEvent(
                    id=new_id("audit"),
                    tenant_id=actor.tenant_id,
                    action="pricing.billingConnectionCreated",
                    resource_type="billingConnection",
                    resource_id=connection_id,
                    actor_object_id=actor.object_id,
                    details={
                        "name": connection.name,
                        "provider": connection.provider,
                        "pricingCloud": connection.pricing_cloud,
                        "hasStoredSecret": bool(connection.credential_secret_uri),
                    },
                ),
            )
        except Exception:
            if secret_id is not None:
                await self._secret_store.delete(secret_id)
            raise

        return billing_connection_view(saved)

    async def update_billing_connection(
        self,
        actor: Actor,
        connection_id: str,
        request: BillingConnectionUpdate,
    ) -> BillingConnectionView:
        current = await self._repository.get_billing_connection(
            actor.tenant_id, connection_id
        )
        if current is None:
            raise NotFoundError(
                "Billing connection was not found", details={"id": connection_id}
            )
        if request.version is not None and request.version != current.etag:
            raise ConflictError(
                "The billing connection changed; reload it and try again",
                details={"id": connection_id},
            )

        fields = request.model_fields_set
        if "cost_notice_acknowledged" in fields and request.cost_notice_acknowledged is False:
            raise ValidationError(
                "Acknowledge that billing queries and export reads can incur cloud provider charges"
            )

        new_secret_id: KeyVaultSecretId | None = None
        retired_uris = list(current.retired_secret_uris)
        credential_secret_uri = current.credential_secret_uri

        if request.clear_stored_secret:
            if current.credential_secret_uri and current.credential_secret_uri not in retired_uris:
                retired_uris.append(current.credential_secret_uri)
            credential_secret_uri = None

        if request.credential_secret is not None:
            new_secret_id = self._secret_store.new_secret(
                f"mosaic-billing-{new_id('cred').replace('_', '-')}"
            )
            if current.credential_secret_uri and current.credential_secret_uri not in retired_uris:
                retired_uris.append(current.credential_secret_uri)
            credential_secret_uri = new_secret_id.versionless
        elif "credential_secret_uri" in fields and request.credential_secret_uri is not None:
            try:
                parsed = KeyVaultSecretId.parse(request.credential_secret_uri)
            except ValueError as exc:
                raise ValidationError(
                    str(exc), details={"field": "credentialSecretUri"}
                ) from exc
            if (
                current.credential_secret_uri
                and current.credential_secret_uri != parsed.versionless
                and current.credential_secret_uri not in retired_uris
            ):
                retired_uris.append(current.credential_secret_uri)
            credential_secret_uri = parsed.versionless

        merged_data: dict[str, Any] = current.model_dump(by_alias=False)
        for field_name in (
            "name",
            "pricing_cloud",
            "azure_cloud",
            "azure_connection_id",
            "azure_scope",
            "azure_retrieval_mode",
            "azure_export_manifest_url",
            "aws_account_id",
            "aws_role_arn",
            "aws_external_id",
            "aws_audience",
            "aws_region",
            "aws_cur_bucket",
            "aws_cur_prefix",
            "aws_export_name",
            "gcp_billing_account_id",
            "gcp_project_id",
            "gcp_dataset_id",
            "gcp_table_id",
            "gcp_location",
            "gcp_service_account_email",
            "gcp_workload_identity_audience",
            "gcp_max_bytes_billed",
            "schedule",
            "cost_notice_acknowledged",
        ):
            if field_name not in fields:
                continue
            val = getattr(request, field_name)
            if field_name in ("name", "pricing_cloud", "schedule", "cost_notice_acknowledged"):
                if val is not None:
                    merged_data[field_name] = val
            else:
                merged_data[field_name] = val

        now = self._clock()
        merged_data["credential_secret_uri"] = credential_secret_uri
        merged_data["retired_secret_uris"] = []
        merged_data["revision"] = current.revision + 1
        merged_data["updated_at"] = now
        merged_data["updated_by"] = actor.object_id
        merged_data["etag"] = current.etag

        try:
            updated_conn = BillingConnection.model_validate(merged_data)
        except PydanticValidationError as exc:
            raise ValidationError(
                "Invalid billing connection update.",
                details={"errors": exc.errors(include_input=False)},
            ) from exc

        if new_secret_id is not None and request.credential_secret is not None:
            await self._secret_store.put(
                new_secret_id,
                request.credential_secret,
                tags={
                    MANAGED_BY_TAG: MANAGED_BY,
                    "mosaicBillingConnection": connection_id,
                },
            )

        try:
            saved = await self._repository.save_billing_connection(
                updated_conn,
                AuditEvent(
                    id=new_id("audit"),
                    tenant_id=actor.tenant_id,
                    action="pricing.billingConnectionUpdated",
                    resource_type="billingConnection",
                    resource_id=connection_id,
                    actor_object_id=actor.object_id,
                    details={
                        "name": updated_conn.name,
                        "provider": updated_conn.provider,
                        "pricingCloud": updated_conn.pricing_cloud,
                        "revision": updated_conn.revision,
                        "hasStoredSecret": bool(updated_conn.credential_secret_uri),
                    },
                ),
            )
        except Exception:
            if new_secret_id is not None:
                await self._secret_store.delete(new_secret_id)
            raise

        for retired_uri in retired_uris:
            try:
                await self._secret_store.delete(KeyVaultSecretId.parse(retired_uri))
            except ValueError:
                pass

        return billing_connection_view(saved)

    async def delete_billing_connection(
        self,
        actor: Actor,
        connection_id: str,
        *,
        version: str | None = None,
    ) -> None:
        current = await self._repository.get_billing_connection(
            actor.tenant_id, connection_id
        )
        if current is None:
            raise NotFoundError(
                "Billing connection was not found", details={"id": connection_id}
            )
        if version is not None and version != current.etag:
            raise ConflictError(
                "The billing connection changed; reload it and try again",
                details={"id": connection_id},
            )

        uris_to_delete = [
            uri
            for uri in [current.credential_secret_uri, *current.retired_secret_uris]
            if uri
        ]
        for uri in uris_to_delete:
            try:
                await self._secret_store.delete(KeyVaultSecretId.parse(uri))
            except ValueError:
                pass

        await self._repository.delete_billing_connection(
            current,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                action="pricing.billingConnectionDeleted",
                resource_type="billingConnection",
                resource_id=connection_id,
                actor_object_id=actor.object_id,
                details={
                    "name": current.name,
                    "provider": current.provider,
                    "pricingCloud": current.pricing_cloud,
                },
            ),
        )

    def _require_connector(self, connection: BillingConnection) -> BillingConnector:
        connector = self._billing_connectors.get(connection.provider)
        if connector is None:
            raise ValidationError(
                f"No billing connector is registered for provider {connection.provider!r}.",
                details={"provider": connection.provider, "connectionId": connection.id},
            )
        return connector

    async def check_billing_connection(
        self, actor: Actor, connection_id: str
    ) -> BillingConnectionCheckResult:
        connection = await self._repository.get_billing_connection(
            actor.tenant_id, connection_id
        )
        if connection is None:
            raise NotFoundError(
                "Billing connection was not found", details={"id": connection_id}
            )
        connector = self._require_connector(connection)
        secret_value = (
            await self._read_secret(connection.credential_secret_uri)
            if connection.credential_secret_uri
            else None
        )
        raw_result = await connector.check_access(connection, secret_value=secret_value)
        checked_at = self._clock()
        result = raw_result.model_copy(
            update={
                "connection_id": connection.id,
                "provider": connection.provider,
                "has_stored_secret": bool(connection.credential_secret_uri),
                "checked_at": checked_at,
            }
        )
        updated_conn = connection.model_copy(
            update={
                "status": result.status,
                "status_message": result.message,
                "checked_at": checked_at,
                "updated_at": checked_at,
                "updated_by": actor.object_id,
            }
        )
        await self._repository.save_billing_connection(
            updated_conn,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                action="pricing.billingConnectionChecked",
                resource_type="billingConnection",
                resource_id=connection.id,
                actor_object_id=actor.object_id,
                details={
                    "provider": connection.provider,
                    "status": result.status,
                    "message": result.message,
                },
            ),
        )
        return result

    async def _record_scheduled_connection_outcome(
        self,
        actor: Actor,
        connection_id: str,
        *,
        source_month: str,
        run_id: str | None,
        run_at: datetime,
        last_error: str | None,
    ) -> BillingConnection | None:
        for attempt in range(SAVE_ATTEMPTS):
            current = await self._repository.get_billing_connection(
                actor.tenant_id, connection_id
            )
            if current is None:
                return None
            updated_schedule = current.schedule.model_copy(
                update={
                    "last_triggered_source_month": source_month,
                    "last_run_id": run_id or current.schedule.last_run_id,
                    "last_run_at": run_at,
                    "last_error": last_error,
                }
            )
            updated_conn = current.model_copy(
                update={
                    "schedule": updated_schedule,
                    "updated_at": run_at,
                    "updated_by": actor.object_id,
                }
            )
            try:
                return await self._repository.save_billing_connection(
                    updated_conn,
                    AuditEvent(
                        id=new_id("audit"),
                        tenant_id=actor.tenant_id,
                        action="pricing.billingScheduleUpdated",
                        resource_type="billingConnection",
                        resource_id=connection_id,
                        actor_object_id=actor.object_id,
                        details={
                            "sourceMonth": source_month,
                            "runId": run_id,
                            "lastError": last_error,
                        },
                    ),
                )
            except ConflictError:
                if attempt == SAVE_ATTEMPTS - 1:
                    raise
        return None

    async def start_billing_run(
        self,
        actor: Actor,
        connection_id: str,
        request: BillingRunTriggerRequest | None = None,
        *,
        acquisition_kind: BillingAcquisitionKind = "connected",
        lease_renewer: Callable[[], Awaitable[None]] | None = None,
    ) -> BillingRunDetailView:
        connection = await self._repository.get_billing_connection(
            actor.tenant_id, connection_id
        )
        if connection is None:
            raise NotFoundError(
                "Billing connection was not found", details={"id": connection_id}
            )
        req = request or BillingRunTriggerRequest()
        today = self._today()
        reference_today = today
        if connection.schedule.time_zone:
            try:
                tz_today = self._clock().astimezone(ZoneInfo(connection.schedule.time_zone)).date()
                if tz_today > reference_today:
                    reference_today = tz_today
            except Exception:
                pass
        try:
            resolved_month = validate_source_month(req.source_month, today=reference_today)
        except ValueError as exc:
            raise ValidationError(
                str(exc), details={"sourceMonth": req.source_month}
            ) from exc

        if acquisition_kind == "scheduled" and not req.force_rerun:
            existing_runs = await self._repository.list_billing_runs(
                actor.tenant_id, connection_id=connection.id
            )
            existing_active = next(
                (
                    run
                    for run in existing_runs
                    if run.source_month == resolved_month
                    and run.status in ("queued", "running", "readyForReview", "published")
                ),
                None,
            )
            if existing_active is not None:
                existing_chunks = await self._repository.list_billing_evidence_chunks(
                    actor.tenant_id, existing_active.id
                )
                return billing_run_detail_view(existing_active, existing_chunks)

        now = self._clock()
        default_scope = (
            connection.azure_scope
            or connection.aws_cur_bucket
            or (
                f"{connection.gcp_project_id}.{connection.gcp_dataset_id}.{connection.gcp_table_id}"
                if connection.gcp_project_id
                else None
            )
        )
        run_id = new_id("billrun")
        initial_run = BillingAcquisitionRun(
            id=run_id,
            tenant_id=actor.tenant_id,
            connection_id=connection.id,
            connection_name=connection.name,
            provider=connection.provider,
            pricing_cloud=connection.pricing_cloud,
            acquisition_kind=acquisition_kind,
            source_month=resolved_month,
            status="running",
            source_scope=default_scope,
            started_at=now,
            effective_from_default=today,
            created_by=actor.object_id,
        )

        secret_value: str | None = None
        try:
            connector = self._require_connector(connection)
            secret_value = (
                await self._read_secret(connection.credential_secret_uri)
                if connection.credential_secret_uri
                else None
            )
            step = await connector.start_or_poll_acquisition(
                connection, initial_run, secret_value=secret_value
            )
            if lease_renewer is not None:
                await lease_renewer()
            if step.status == "running":
                running_run = initial_run.model_copy(
                    update={
                        "status": "running",
                        "status_message": (
                            step.status_message
                            or f"Retrieving {resolved_month} billing data from {connection.name}."
                        ),
                        "source_scope": step.source_scope or initial_run.source_scope,
                        "source_manifest_id": step.source_manifest_id,
                        "operation_id": step.operation_id,
                        "operation_location": step.operation_location,
                        "continuation_token": step.continuation_token,
                        "poll_after": step.poll_after,
                        "total_read_bytes": step.total_read_bytes,
                    }
                )
                saved_run = await self._repository.create_billing_run(
                    running_run,
                    AuditEvent(
                        id=new_id("audit"),
                        tenant_id=actor.tenant_id,
                        action="pricing.billingRunStarted",
                        resource_type="billingAcquisitionRun",
                        resource_id=run_id,
                        actor_object_id=actor.object_id,
                        details={
                            "connectionId": connection.id,
                            "provider": connection.provider,
                            "sourceMonth": resolved_month,
                            "acquisitionKind": acquisition_kind,
                            "operationId": step.operation_id,
                        },
                    ),
                )
                if acquisition_kind == "scheduled":
                    await self._record_scheduled_connection_outcome(
                        actor,
                        connection.id,
                        source_month=resolved_month,
                        run_id=saved_run.id,
                        run_at=now,
                        last_error=None,
                    )
                return billing_run_detail_view(saved_run, [])

            return await self._complete_connected_run(
                actor,
                connection,
                initial_run,
                step,
                is_new=True,
                lease_renewer=lease_renewer,
            )
        except ConflictError:
            raise
        except Exception as exc:
            if acquisition_kind != "scheduled":
                raise
            if lease_renewer is not None:
                await lease_renewer()
            fail_now = self._clock()
            error_msg = _safe_billing_error_message(exc, secret_value=secret_value)
            failed_issue = BillingReviewIssue(
                code="acquisitionFailed",
                severity="blocker",
                message=error_msg,
            )
            failed_run = initial_run.model_copy(
                update={
                    "status": "failed",
                    "status_message": error_msg,
                    "completed_at": fail_now,
                    "poll_after": None,
                    "continuation_token": None,
                    "issues": [failed_issue],
                    "updated_at": fail_now,
                }
            )
            saved_failed = await self._repository.create_billing_run(
                failed_run,
                AuditEvent(
                    id=new_id("audit"),
                    tenant_id=actor.tenant_id,
                    action="pricing.billingRunFailed",
                    resource_type="billingAcquisitionRun",
                    resource_id=run_id,
                    actor_object_id=actor.object_id,
                    details={
                        "connectionId": connection.id,
                        "provider": connection.provider,
                        "sourceMonth": resolved_month,
                        "acquisitionKind": acquisition_kind,
                        "error": error_msg,
                    },
                ),
            )
            await self._record_scheduled_connection_outcome(
                actor,
                connection.id,
                source_month=resolved_month,
                run_id=saved_failed.id,
                run_at=fail_now,
                last_error=error_msg,
            )
            return billing_run_detail_view(saved_failed, [])

    async def poll_billing_run(
        self,
        actor: Actor,
        run_id: str,
        *,
        lease_renewer: Callable[[], Awaitable[None]] | None = None,
    ) -> BillingRunDetailView:
        run = await self._repository.get_billing_run(actor.tenant_id, run_id)
        if run is None:
            raise NotFoundError(
                "Billing acquisition run was not found", details={"id": run_id}
            )
        if run.status in ("readyForReview", "published"):
            chunks = await self._repository.list_billing_evidence_chunks(
                actor.tenant_id, run.id
            )
            return billing_run_detail_view(run, chunks)
        if run.status not in ("queued", "running"):
            raise ConflictError(
                f"Billing run {run.id} is in status {run.status!r} and cannot be polled.",
                details={"runId": run.id, "status": run.status},
            )
        if not run.connection_id:
            raise ValidationError(
                f"Billing run {run.id} is an upload run and cannot be polled.",
                details={"runId": run.id},
            )
        connection = await self._repository.get_billing_connection(
            actor.tenant_id, run.connection_id
        )
        if connection is None:
            raise NotFoundError(
                "Billing connection was not found", details={"id": run.connection_id}
            )

        secret_value: str | None = None
        try:
            connector = self._require_connector(connection)
            secret_value = (
                await self._read_secret(connection.credential_secret_uri)
                if connection.credential_secret_uri
                else None
            )
            step = await connector.start_or_poll_acquisition(
                connection, run, secret_value=secret_value
            )
            if lease_renewer is not None:
                await lease_renewer()
            if step.status == "running":
                now = self._clock()
                updated_run = run.model_copy(
                    update={
                        "status": "running",
                        "status_message": step.status_message or run.status_message,
                        "source_scope": step.source_scope or run.source_scope,
                        "source_manifest_id": step.source_manifest_id or run.source_manifest_id,
                        "operation_id": step.operation_id or run.operation_id,
                        "operation_location": step.operation_location or run.operation_location,
                        "continuation_token": step.continuation_token,
                        "poll_after": step.poll_after,
                        "total_read_bytes": max(step.total_read_bytes, run.total_read_bytes),
                        "updated_at": now,
                    }
                )
                saved_run = await self._repository.save_billing_run(
                    updated_run,
                    AuditEvent(
                        id=new_id("audit"),
                        tenant_id=actor.tenant_id,
                        action="pricing.billingRunPolled",
                        resource_type="billingAcquisitionRun",
                        resource_id=run.id,
                        actor_object_id=actor.object_id,
                        details={
                            "connectionId": connection.id,
                            "status": "running",
                            "operationId": updated_run.operation_id,
                        },
                    ),
                )
                return billing_run_detail_view(saved_run, [])

            return await self._complete_connected_run(
                actor,
                connection,
                run,
                step,
                is_new=False,
                lease_renewer=lease_renewer,
            )
        except ConflictError:
            raise
        except Exception as exc:
            if lease_renewer is not None:
                await lease_renewer()
            fail_now = self._clock()
            error_msg = _safe_billing_error_message(exc, secret_value=secret_value)
            failed_issue = BillingReviewIssue(
                code="acquisitionFailed",
                severity="blocker",
                message=error_msg,
            )
            failed_run = run.model_copy(
                update={
                    "status": "failed",
                    "status_message": error_msg,
                    "completed_at": fail_now,
                    "poll_after": None,
                    "continuation_token": None,
                    "issues": [*run.issues, failed_issue],
                    "updated_at": fail_now,
                }
            )
            saved_failed = await self._repository.save_billing_run(
                failed_run,
                AuditEvent(
                    id=new_id("audit"),
                    tenant_id=actor.tenant_id,
                    action="pricing.billingRunFailed",
                    resource_type="billingAcquisitionRun",
                    resource_id=run.id,
                    actor_object_id=actor.object_id,
                    details={
                        "connectionId": connection.id,
                        "provider": connection.provider,
                        "sourceMonth": run.source_month,
                        "acquisitionKind": run.acquisition_kind,
                        "error": error_msg,
                    },
                ),
            )
            if run.acquisition_kind == "scheduled":
                await self._record_scheduled_connection_outcome(
                    actor,
                    connection.id,
                    source_month=run.source_month,
                    run_id=run.id,
                    run_at=fail_now,
                    last_error=error_msg,
                )
            return billing_run_detail_view(saved_failed, [])

    async def _complete_connected_run(
        self,
        actor: Actor,
        connection: BillingConnection,
        base_run: BillingAcquisitionRun,
        step: BillingConnectorStepResult,
        *,
        is_new: bool,
        lease_renewer: Callable[[], Awaitable[None]] | None = None,
    ) -> BillingRunDetailView:
        now = self._clock()
        today = self._today()

        if not step.parts:
            empty_msg = (
                step.status_message
                or (step.warnings[0] if step.warnings else None)
                or f"No billing data was returned from {connection.name} for "
                f"{base_run.source_month}."
            )
            empty_issues = [
                BillingReviewIssue(
                    code="noEligibleModelUsage",
                    severity="blocker",
                    message=empty_msg,
                ),
                *[
                    BillingReviewIssue(
                        code="connectorWarning",
                        severity="warning",
                        message=warn,
                    )
                    for warn in step.warnings
                ],
            ]
            if lease_renewer is not None:
                await lease_renewer()
            failed_empty_run = base_run.model_copy(
                update={
                    "status": "failed",
                    "status_message": empty_msg,
                    "source_scope": step.source_scope or base_run.source_scope,
                    "source_manifest_id": step.source_manifest_id or base_run.source_manifest_id,
                    "operation_id": step.operation_id or base_run.operation_id,
                    "operation_location": step.operation_location or base_run.operation_location,
                    "continuation_token": None,
                    "poll_after": None,
                    "completed_at": now,
                    "effective_from_default": today,
                    "total_read_bytes": max(step.total_read_bytes, base_run.total_read_bytes),
                    "issues": empty_issues,
                    "updated_at": now,
                }
            )
            empty_audit = AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                action="pricing.billingRunFailed",
                resource_type="billingAcquisitionRun",
                resource_id=base_run.id,
                actor_object_id=actor.object_id,
                details={
                    "connectionId": connection.id,
                    "provider": connection.provider,
                    "pricingCloud": connection.pricing_cloud,
                    "sourceMonth": base_run.source_month,
                    "reason": "noBillingData",
                    "error": empty_msg,
                },
            )
            saved_empty = (
                await self._repository.create_billing_run(failed_empty_run, empty_audit)
                if is_new
                else await self._repository.save_billing_run(failed_empty_run, empty_audit)
            )
            if base_run.acquisition_kind == "scheduled":
                await self._record_scheduled_connection_outcome(
                    actor,
                    connection.id,
                    source_month=base_run.source_month,
                    run_id=saved_empty.id,
                    run_at=now,
                    last_error=empty_msg,
                )
            return billing_run_detail_view(saved_empty, [])

        hasher = hashlib.sha256()
        for part in step.parts:
            if lease_renewer is not None:
                await lease_renewer()
            hasher.update(part.name.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(part.content)
        fingerprint = hasher.hexdigest()

        book, deployments = await asyncio.gather(
            self.book(actor.tenant_id),
            self.deployments(actor.tenant_id),
        )
        ingested = ingest_billing_parts(
            step.parts,
            provider=connection.provider,
            pricing_cloud=connection.pricing_cloud,
            source_month=base_run.source_month,
            source_scope=step.source_scope or base_run.source_scope,
            deployments=deployments,
            book=book,
            today=today,
        )
        issues = list(ingested.issues)
        for warn in step.warnings:
            issues.append(
                BillingReviewIssue(
                    code="connectorWarning",
                    severity="warning",
                    message=warn,
                )
            )

        if lease_renewer is not None:
            await lease_renewer()
        chunks = chunk_evidence_items(
            actor.tenant_id, base_run.id, ingested.evidence_items
        )
        await self._repository.save_billing_evidence_chunks(
            actor.tenant_id, base_run.id, chunks
        )
        if lease_renewer is not None:
            await lease_renewer()

        now = self._clock()
        total_bytes = max(step.total_read_bytes, ingested.total_read_bytes)

        if not ingested.candidates:
            no_cand_msg = (
                f"No eligible AI model usage rows were found in {connection.name} for "
                f"{base_run.source_month}."
            )
            failed_run = base_run.model_copy(
                update={
                    "status": "failed",
                    "status_message": no_cand_msg,
                    "source_scope": (
                        step.source_scope or ingested.source_scope or base_run.source_scope
                    ),
                    "source_manifest_id": step.source_manifest_id or base_run.source_manifest_id,
                    "content_fingerprint": fingerprint,
                    "operation_id": step.operation_id or base_run.operation_id,
                    "operation_location": step.operation_location or base_run.operation_location,
                    "continuation_token": None,
                    "poll_after": None,
                    "completed_at": now,
                    "effective_from_default": today,
                    "invoice_months": ingested.invoice_months,
                    "raw_row_count": ingested.raw_row_count,
                    "eligible_row_count": ingested.eligible_row_count,
                    "excluded_row_count": ingested.excluded_row_count,
                    "total_read_bytes": total_bytes,
                    "total_eligible_cost_usd": ingested.total_eligible_cost_usd,
                    "total_excluded_cost_usd": ingested.total_excluded_cost_usd,
                    "evidence_chunk_count": len(chunks),
                    "evidence_item_count": len(ingested.evidence_items),
                    "candidates": [],
                    "mappings": [],
                    "issues": issues,
                    "updated_at": now,
                }
            )
            failed_audit = AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                action="pricing.billingRunFailed",
                resource_type="billingAcquisitionRun",
                resource_id=base_run.id,
                actor_object_id=actor.object_id,
                details={
                    "connectionId": connection.id,
                    "provider": connection.provider,
                    "pricingCloud": connection.pricing_cloud,
                    "sourceMonth": base_run.source_month,
                    "rawRowCount": ingested.raw_row_count,
                    "eligibleRowCount": 0,
                    "candidateCount": 0,
                    "error": no_cand_msg,
                },
            )
            saved_failed = (
                await self._repository.create_billing_run(failed_run, failed_audit)
                if is_new
                else await self._repository.save_billing_run(failed_run, failed_audit)
            )
            if base_run.acquisition_kind == "scheduled":
                await self._record_scheduled_connection_outcome(
                    actor,
                    connection.id,
                    source_month=base_run.source_month,
                    run_id=saved_failed.id,
                    run_at=now,
                    last_error=no_cand_msg,
                )
            return billing_run_detail_view(saved_failed, chunks)

        completed_run = base_run.model_copy(
            update={
                "status": "readyForReview",
                "status_message": (
                    step.status_message
                    or f"Normalized {ingested.eligible_row_count} eligible row(s) and derived "
                    f"{len(ingested.candidates)} candidate rate(s)."
                ),
                "source_scope": (
                    step.source_scope or ingested.source_scope or base_run.source_scope
                ),
                "source_manifest_id": step.source_manifest_id or base_run.source_manifest_id,
                "content_fingerprint": fingerprint,
                "operation_id": step.operation_id or base_run.operation_id,
                "operation_location": step.operation_location or base_run.operation_location,
                "continuation_token": None,
                "poll_after": None,
                "completed_at": now,
                "effective_from_default": today,
                "invoice_months": ingested.invoice_months,
                "raw_row_count": ingested.raw_row_count,
                "eligible_row_count": ingested.eligible_row_count,
                "excluded_row_count": ingested.excluded_row_count,
                "total_read_bytes": total_bytes,
                "total_eligible_cost_usd": ingested.total_eligible_cost_usd,
                "total_excluded_cost_usd": ingested.total_excluded_cost_usd,
                "evidence_chunk_count": len(chunks),
                "evidence_item_count": len(ingested.evidence_items),
                "candidates": ingested.candidates,
                "mappings": ingested.mappings,
                "issues": issues,
                "updated_at": now,
            }
        )

        audit = AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            action="pricing.billingRunCompleted",
            resource_type="billingAcquisitionRun",
            resource_id=base_run.id,
            actor_object_id=actor.object_id,
            details={
                "connectionId": connection.id,
                "provider": connection.provider,
                "pricingCloud": connection.pricing_cloud,
                "sourceMonth": base_run.source_month,
                "rawRowCount": ingested.raw_row_count,
                "eligibleRowCount": ingested.eligible_row_count,
                "candidateCount": len(ingested.candidates),
            },
        )
        if is_new:
            saved_run = await self._repository.create_billing_run(completed_run, audit)
        else:
            saved_run = await self._repository.save_billing_run(completed_run, audit)

        if base_run.acquisition_kind == "scheduled":
            await self._record_scheduled_connection_outcome(
                actor,
                connection.id,
                source_month=base_run.source_month,
                run_id=saved_run.id,
                run_at=now,
                last_error=None,
            )

        existing_runs = await self._repository.list_billing_runs(
            actor.tenant_id, connection_id=connection.id
        )
        for old_run in existing_runs:
            if (
                old_run.id != saved_run.id
                and old_run.status == "readyForReview"
                and old_run.source_month == saved_run.source_month
            ):
                try:
                    await self._repository.save_billing_run(
                        old_run.model_copy(
                            update={"status": "superseded", "updated_at": now}
                        ),
                        AuditEvent(
                            id=new_id("audit"),
                            tenant_id=actor.tenant_id,
                            action="pricing.billingRunSuperseded",
                            resource_type="billingAcquisitionRun",
                            resource_id=old_run.id,
                            actor_object_id=actor.object_id,
                            details={"supersededByRunId": saved_run.id},
                        ),
                    )
                except ConflictError:
                    pass

        return billing_run_detail_view(saved_run, chunks)

    # -- candidate mapping review and atomic rate publication -----------------------------------

    @staticmethod
    def _candidate_account_name(candidate: BillingCandidateRate) -> str | None:
        if candidate.source_resource_name:
            return candidate.source_resource_name.strip() or None
        if candidate.source_resource_id:
            match = re.search(
                r"/accounts/([^/]+)", candidate.source_resource_id, re.IGNORECASE
            )
            if match:
                return match.group(1)
        return None

    def _validate_candidate_deployment_binding(
        self,
        candidate: BillingCandidateRate,
        mapping: BillingCandidateMapping,
        deployments: Mapping[str, DeploymentFacts],
        deployments_by_folded: Mapping[str, DeploymentFacts],
    ) -> DeploymentFacts:
        if not mapping.deployment_key:
            raise ValidationError(
                f"Candidate {candidate.candidate_id} ({candidate.model}) mapped with scope "
                "'deployment' requires a deploymentKey.",
                details={"candidateId": candidate.candidate_id},
            )
        target = deployments.get(mapping.deployment_key) or deployments_by_folded.get(
            mapping.deployment_key.casefold()
        )
        if target is None:
            raise ValidationError(
                f"MOSAIC has no record of deployment {mapping.deployment_key!r}.",
                details={
                    "candidateId": candidate.candidate_id,
                    "deploymentKey": mapping.deployment_key,
                },
            )

        cand_is_prov = (
            candidate.ptu_hourly is not None
            or candidate.monthly_amount is not None
            or is_provisioned(candidate.deployment_type)
        )
        if target.provisioned != cand_is_prov:
            raise ValidationError(
                f"Candidate {candidate.candidate_id} ({candidate.model}) is a "
                f"{'provisioned' if cand_is_prov else 'token'} rate and cannot be mapped to "
                f"{'provisioned' if target.provisioned else 'token'} deployment {target.key}.",
                details={
                    "candidateId": candidate.candidate_id,
                    "deploymentKey": target.key,
                },
            )
        if (
            candidate.model != "*"
            and target.model
            and candidate.model.casefold() != target.model.casefold()
        ):
            raise ValidationError(
                f"Candidate {candidate.candidate_id} is for model {candidate.model!r}, but "
                f"deployment {target.key} serves {target.model!r}.",
                details={
                    "candidateId": candidate.candidate_id,
                    "deploymentKey": target.key,
                    "candidateModel": candidate.model,
                    "deploymentModel": target.model,
                },
            )

        cand_account = self._candidate_account_name(candidate)
        cand_has_res = bool(candidate.source_resource_id or cand_account)
        target_has_res = bool(target.azure_resource_id or target.account_name)
        res_match = False
        if candidate.source_resource_id and target.azure_resource_id:
            src_res = candidate.source_resource_id.rstrip("/").casefold()
            dep_res = target.azure_resource_id.rstrip("/").casefold()
            if src_res == dep_res or src_res.startswith(dep_res + "/"):
                res_match = True
        if not res_match and cand_account and target.account_name:
            if cand_account.casefold() == target.account_name.casefold():
                res_match = True
        if not res_match and candidate.source_resource_id:
            if candidate.source_resource_id.casefold() == target.key.casefold():
                res_match = True
        if not res_match and candidate.source_resource_name:
            if candidate.source_resource_name.casefold() in {
                target.deployment_name.casefold(),
                target.endpoint_name.casefold(),
                target.key.casefold(),
            }:
                res_match = True

        if cand_has_res and target_has_res and not res_match and not mapping.confirmed:
            raise ValidationError(
                f"Candidate {candidate.candidate_id} comes from resource/account "
                f"{cand_account or candidate.source_resource_id!r}, which does not match "
                f"deployment {target.key} on {target.account_name or target.endpoint_name!r}. "
                "Set confirmed=True only if this cross-account binding is intentional.",
                details={
                    "candidateId": candidate.candidate_id,
                    "deploymentKey": target.key,
                },
            )

        if candidate.mapping_confidence == "ambiguous" and not mapping.confirmed:
            raise ValidationError(
                f"Candidate {candidate.candidate_id} ({candidate.model}) has an ambiguous "
                f"deployment mapping; set confirmed=True to bind it to {target.key}.",
                details={
                    "candidateId": candidate.candidate_id,
                    "deploymentKey": target.key,
                    "ambiguousDeploymentKeys": candidate.ambiguous_deployment_keys,
                },
            )

        if (
            candidate.suggested_deployment_key
            and target.key.casefold() != candidate.suggested_deployment_key.casefold()
            and not mapping.confirmed
        ):
            raise ValidationError(
                f"Mapping candidate {candidate.candidate_id} ({candidate.model}) to "
                f"{target.key} instead of {candidate.suggested_deployment_key} requires "
                "confirmed=True.",
                details={
                    "candidateId": candidate.candidate_id,
                    "deploymentKey": target.key,
                },
            )

        return target

    def _lookup_existing_price_for_scope(
        self,
        candidate: BillingCandidateRate,
        mapping: BillingCandidateMapping,
        *,
        target_facts: DeploymentFacts | None,
        book: PriceBook,
        today: date,
    ) -> PriceEntry | None:
        if mapping.scope == "deployment" and target_facts is not None:
            match = book.match(target_facts, today)
            if isinstance(match, PriceMatch):
                return match.entry
            return None

        resolved_cloud = (mapping.cloud or candidate.pricing_cloud).strip().casefold()
        cand_is_prov = (
            candidate.ptu_hourly is not None
            or candidate.monthly_amount is not None
            or is_provisioned(candidate.deployment_type)
        )
        probe_region = (
            normalize_region(candidate.region)
            if candidate.provider == "azure"
            else candidate.region
        )
        probe_type = candidate.deployment_type or (
            "ProvisionedManaged" if cand_is_prov else "GlobalStandard"
        )
        probe_facts = DeploymentFacts(
            key=(
                target_facts.key
                if target_facts is not None
                else candidate.suggested_deployment_key or "__probe__/default"
            ),
            endpoint_id="__probe__",
            endpoint_name="Probe",
            deployment_name="default",
            provider=candidate.provider,
            cloud=resolved_cloud,
            cloud_source="override",
            detected_cloud=resolved_cloud,
            region=probe_region,
            publisher=candidate.publisher,
            model=candidate.model if candidate.model != "*" else "gpt-4o",
            version=candidate.version,
            deployment_type=probe_type,
            deployment_type_source="admin",
            capacity=100 if cand_is_prov else None,
        )
        probe_match = book.match(probe_facts, today)
        if isinstance(probe_match, PriceMatch):
            return probe_match.entry
        return None

    async def update_billing_mappings(
        self,
        actor: Actor,
        run_id: str,
        request: BillingMappingsUpdateRequest,
    ) -> BillingRunDetailView:
        run = await self._repository.get_billing_run(actor.tenant_id, run_id)
        if run is None:
            raise NotFoundError(
                "Billing acquisition run was not found", details={"id": run_id}
            )
        if run.status != "readyForReview":
            raise ConflictError(
                f"Billing run {run.id} is in status {run.status!r}; mappings can only be "
                "updated while readyForReview.",
                details={"runId": run.id, "status": run.status},
            )
        if request.version is not None and request.version != run.etag:
            raise ConflictError(
                "The billing acquisition run changed; reload it and try again.",
                details={"runId": run.id},
            )
        if (
            request.preview_revision is not None
            and request.preview_revision != run.preview_revision
        ):
            raise ConflictError(
                "The billing preview revision is stale; reload the run and review the latest "
                "candidates before saving mappings.",
                details={
                    "runId": run.id,
                    "expectedPreviewRevision": request.preview_revision,
                    "currentPreviewRevision": run.preview_revision,
                },
            )

        candidates_by_id = {cand.candidate_id: cand for cand in run.candidates}
        seen_ids: set[str] = set()
        for item in request.mappings:
            if item.candidate_id in seen_ids:
                raise ValidationError(
                    f"Duplicate mapping for candidate {item.candidate_id!r}.",
                    details={"candidateId": item.candidate_id},
                )
            seen_ids.add(item.candidate_id)
            if item.candidate_id not in candidates_by_id:
                raise ValidationError(
                    f"Unknown candidate ID {item.candidate_id!r} for billing run {run.id}.",
                    details={"candidateId": item.candidate_id, "runId": run.id},
                )

        book, deployments = await asyncio.gather(
            self.book(actor.tenant_id),
            self.deployments(actor.tenant_id),
        )
        deployments_by_folded = {k.casefold(): v for k, v in deployments.items()}
        today = self._today()

        incoming_by_id = {item.candidate_id: item for item in request.mappings}
        existing_mappings_by_id = {item.candidate_id: item for item in run.mappings}

        updated_candidates: list[BillingCandidateRate] = []
        updated_mappings: list[BillingCandidateMapping] = []
        conflict_issues: list[BillingReviewIssue] = []

        for cand in run.candidates:
            mapping = incoming_by_id.get(cand.candidate_id) or existing_mappings_by_id.get(
                cand.candidate_id
            )
            if mapping is None:
                mapping = BillingCandidateMapping(
                    candidate_id=cand.candidate_id,
                    scope=cand.suggested_scope,
                    deployment_key=cand.suggested_deployment_key,
                    cloud=cand.pricing_cloud,
                )

            target_facts: DeploymentFacts | None = None
            resolved_cloud = (
                (mapping.cloud or cand.pricing_cloud or run.pricing_cloud).strip().casefold()
            )
            if not CLOUD_KEY.fullmatch(resolved_cloud):
                raise ValidationError(
                    "A pricing cloud key is up to 40 lowercase letters, digits, and hyphens.",
                    details={"candidateId": cand.candidate_id, "cloud": mapping.cloud},
                )

            if mapping.scope == "deployment":
                target_facts = self._validate_candidate_deployment_binding(
                    cand, mapping, deployments, deployments_by_folded
                )
                normalized_mapping = mapping.model_copy(
                    update={
                        "deployment_key": target_facts.key,
                        "cloud": target_facts.cloud or resolved_cloud,
                    }
                )
            elif mapping.scope == "cloudModel":
                if cand.monthly_amount is not None and cand.ptu_hourly is None:
                    raise ValidationError(
                        f"Candidate {cand.candidate_id} ({cand.model}) is a monthly commitment "
                        "amount and must be mapped to a specific deployment.",
                        details={"candidateId": cand.candidate_id},
                    )
                if (
                    cand. candidate_id in incoming_by_id
                    and cand.mapping_confidence == "ambiguous"
                    and not mapping.confirmed
                ):
                    raise ValidationError(
                        f"Candidate {cand.candidate_id} ({cand.model}) has an ambiguous "
                        "mapping; set confirmed=True to publish at cloud-model scope.",
                        details={"candidateId": cand.candidate_id},
                    )
                normalized_mapping = mapping.model_copy(
                    update={
                        "deployment_key": None,
                        "cloud": resolved_cloud,
                    }
                )
            else:
                normalized_mapping = mapping.model_copy(
                    update={
                        "cloud": resolved_cloud,
                    }
                )

            existing_entry = self._lookup_existing_price_for_scope(
                cand,
                normalized_mapping,
                target_facts=target_facts,
                book=book,
                today=today,
            )
            conflicts_manual = (
                normalized_mapping.scope != "excluded"
                and existing_entry is not None
                and existing_entry.origin == "admin"
            )
            cleaned_warnings = [
                w
                for w in cand.warnings
                if "overlaps an administrator manual price" not in w
            ]
            if conflicts_manual and existing_entry is not None:
                conflict_msg = (
                    f"Candidate rate for {cand.model} overlaps an administrator manual price "
                    f"({existing_entry.id}); replacing it requires explicit confirmation."
                )
                cleaned_warnings.append(conflict_msg)
                conflict_issues.append(
                    BillingReviewIssue(
                        code="manualOverrideConflict",
                        severity="warning",
                        message=conflict_msg,
                        model=cand.model,
                        region=cand.region,
                        source_resource_id=cand.source_resource_id,
                        details={"existingPriceId": existing_entry.id},
                    )
                )

            updated_cand = cand.model_copy(
                update={
                    "existing_price_id": existing_entry.id if existing_entry else None,
                    "existing_line_id": existing_entry.line_id if existing_entry else None,
                    "existing_origin": existing_entry.origin if existing_entry else None,
                    "existing_input_per_million": (
                        existing_entry.input_per_million if existing_entry else None
                    ),
                    "existing_cached_input_per_million": (
                        existing_entry.cached_input_per_million if existing_entry else None
                    ),
                    "existing_output_per_million": (
                        existing_entry.output_per_million if existing_entry else None
                    ),
                    "existing_ptu_hourly": (
                        existing_entry.ptu_hourly if existing_entry else None
                    ),
                    "existing_monthly_amount": (
                        existing_entry.monthly_amount if existing_entry else None
                    ),
                    "conflicts_with_manual_override": conflicts_manual,
                    "warnings": cleaned_warnings,
                }
            )
            updated_candidates.append(updated_cand)
            updated_mappings.append(normalized_mapping)

        retained_issues = [
            issue for issue in run.issues if issue.code != "manualOverrideConflict"
        ]
        updated_run = run.model_copy(
            update={
                "candidates": updated_candidates,
                "mappings": updated_mappings,
                "issues": [*retained_issues, *conflict_issues],
                "preview_revision": run.preview_revision + 1,
                "updated_at": self._clock(),
            }
        )
        saved_run = await self._repository.save_billing_run(
            updated_run,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                action="pricing.billingMappingsUpdated",
                resource_type="billingAcquisitionRun",
                resource_id=run.id,
                actor_object_id=actor.object_id,
                details={
                    "previewRevision": updated_run.preview_revision,
                    "mappingCount": len(updated_mappings),
                },
            ),
        )
        chunks = await self._repository.list_billing_evidence_chunks(actor.tenant_id, run.id)
        return billing_run_detail_view(saved_run, chunks)

    @staticmethod
    def _blocker_affects_candidate(
        issue: BillingReviewIssue, candidate: BillingCandidateRate
    ) -> bool:
        if (
            issue.model is None
            and issue.region is None
            and issue.source_resource_id is None
        ):
            return True
        if issue.model is not None and candidate.model.casefold() != issue.model.casefold():
            return False
        if (
            issue.region is not None
            and (candidate.region or "").casefold() != issue.region.casefold()
        ):
            return False
        if (
            issue.source_resource_id is not None
            and (candidate.source_resource_id or "").casefold()
            != issue.source_resource_id.casefold()
        ):
            return False
        return True

    async def publish_billing_run(
        self,
        actor: Actor,
        run_id: str,
        request: BillingPublishRequest | None = None,
    ) -> BillingPublicationView:
        run = await self._repository.get_billing_run(actor.tenant_id, run_id)
        if run is None:
            raise NotFoundError(
                "Billing acquisition run was not found", details={"id": run_id}
            )
        req = request or BillingPublishRequest()

        # Idempotent return when the run was already published with the same effective date
        if run.status == "published" and run.publication_id:
            existing_pub = await self._repository.get_billing_publication(
                actor.tenant_id, run.publication_id
            )
            if existing_pub is not None:
                if (
                    req.effective_from is None
                    or req.effective_from == existing_pub.effective_from
                ):
                    return billing_publication_view(existing_pub)
                raise ConflictError(
                    f"Billing run {run.id} was already published with effectiveFrom="
                    f"{existing_pub.effective_from.isoformat()}; cannot republish with "
                    f"effectiveFrom={req.effective_from.isoformat()}.",
                    details={
                        "runId": run.id,
                        "publicationId": existing_pub.id,
                        "effectiveFrom": existing_pub.effective_from.isoformat(),
                    },
                )

        if run.status != "readyForReview":
            raise ConflictError(
                f"Billing run {run.id} is in status {run.status!r} and cannot be published.",
                details={"runId": run.id, "status": run.status},
            )
        if req.version is not None and req.version != run.etag:
            raise ConflictError(
                "The billing acquisition run changed; reload it and try again.",
                details={"runId": run.id},
            )
        if req.preview_revision is not None and req.preview_revision != run.preview_revision:
            raise ConflictError(
                "The billing preview revision is stale; reload the run and review the latest "
                "candidates before publishing.",
                details={
                    "runId": run.id,
                    "expectedPreviewRevision": req.preview_revision,
                    "currentPreviewRevision": run.preview_revision,
                },
            )

        today = self._today()
        effective_from = req.effective_from or today
        backdated = effective_from < today
        if backdated and not req.acknowledge_backdate_impact:
            raise ValidationError(
                "Backdating changes historical cost reports and current-month budget "
                "calculations; confirm acknowledgeBackdateImpact to proceed.",
                details={
                    "effectiveFrom": effective_from.isoformat(),
                    "today": today.isoformat(),
                },
            )

        book, deployments = await asyncio.gather(
            self.book(actor.tenant_id),
            self.deployments(actor.tenant_id),
        )
        deployments_by_folded = {k.casefold(): v for k, v in deployments.items()}
        mappings_by_id = {m.candidate_id: m for m in run.mappings}

        candidate_pairs: list[tuple[BillingCandidateRate, BillingCandidateMapping]] = []
        for cand in run.candidates:
            mapping = mappings_by_id.get(cand.candidate_id) or BillingCandidateMapping(
                candidate_id=cand.candidate_id,
                scope=cand.suggested_scope,
                deployment_key=cand.suggested_deployment_key,
                cloud=cand.pricing_cloud,
                confirmed=(
                    cand.publishable
                    and cand.mapping_confidence == "exact"
                    and not cand.conflicts_with_manual_override
                ),
            )
            candidate_pairs.append((cand, mapping))

        # Check blocker issues unless every candidate affected by the blocker is excluded
        for issue in run.issues:
            if issue.severity != "blocker":
                continue
            affected = [
                (cand, mapping)
                for cand, mapping in candidate_pairs
                if self._blocker_affects_candidate(issue, cand)
            ]
            if not affected or any(mapping.scope != "excluded" for _, mapping in affected):
                raise ValidationError(
                    f"Cannot publish billing run {run.id} with unresolved blocker "
                    f"({issue.code}): {issue.message}",
                    details={"runId": run.id, "issueCode": issue.code},
                )

        included_pairs = [
            (cand, mapping)
            for cand, mapping in candidate_pairs
            if mapping.scope in ("deployment", "cloudModel")
        ]
        if not included_pairs:
            raise ValidationError(
                "At least one publishable candidate rate must be included to publish a billing "
                "run.",
                details={"runId": run.id},
            )

        now = self._clock()
        publication_id = billing_publication_id(actor.tenant_id, run.id)
        provider_label = _PROVIDER_LABELS.get(run.provider, run.provider)
        source_title = f"{provider_label} billing ({run.source_month})"
        note_text = req.note or f"Derived from {run.source_month} {run.provider} billing data"

        prepared_versions: list[PriceVersion] = []
        affected_deployment_keys: list[str] = []
        replaced_manual_override_ids: list[str] = []

        for cand, _ in included_pairs:
            if not cand.publishable:
                reason = (
                    cand.unpublishable_reason
                    or "incomplete or unsupported billing dimensions"
                )
                raise ValidationError(
                    f"Candidate {cand.candidate_id} ({cand.model}) cannot be published: "
                    f"{reason}.",
                    details={
                        "candidateId": cand.candidate_id,
                        "model": cand.model,
                        "reason": cand.unpublishable_reason,
                    },
                )

        for cand, mapping in included_pairs:
            target_facts: DeploymentFacts | None = None
            if mapping.scope == "deployment":
                target_facts = self._validate_candidate_deployment_binding(
                    cand, mapping, deployments, deployments_by_folded
                )
            else:
                if cand.monthly_amount is not None and cand.ptu_hourly is None:
                    raise ValidationError(
                        f"Candidate {cand.candidate_id} ({cand.model}) is a monthly commitment "
                        "amount and must be mapped to a specific deployment.",
                        details={"candidateId": cand.candidate_id},
                    )
                if cand.mapping_confidence == "ambiguous" and not mapping.confirmed:
                    raise ValidationError(
                        f"Candidate {cand.candidate_id} ({cand.model}) has an ambiguous "
                        "deployment mapping; confirm the mapping before publishing.",
                        details={"candidateId": cand.candidate_id},
                    )

            existing_entry = self._lookup_existing_price_for_scope(
                cand,
                mapping,
                target_facts=target_facts,
                book=book,
                today=today,
            )
            if existing_entry is None and effective_from != today:
                existing_entry = self._lookup_existing_price_for_scope(
                    cand,
                    mapping,
                    target_facts=target_facts,
                    book=book,
                    today=effective_from,
                )

            conflicts_manual = cand.conflicts_with_manual_override or (
                existing_entry is not None and existing_entry.origin == "admin"
            )
            existing_price_id = (
                existing_entry.id if existing_entry is not None else cand.existing_price_id
            )
            if conflicts_manual and not (
                mapping.replace_manual_override and req.acknowledge_manual_override_replacement
            ):
                raise ValidationError(
                    f"Candidate {cand.candidate_id} ({cand.model}) conflicts with an existing "
                    "manual administrator price override; confirm replaceManualOverride on the "
                    "mapping and acknowledgeManualOverrideReplacement on the publish request.",
                    details={
                        "candidateId": cand.candidate_id,
                        "model": cand.model,
                        "existingPriceId": existing_price_id,
                    },
                )

            if conflicts_manual and existing_price_id:
                if existing_price_id not in replaced_manual_override_ids:
                    replaced_manual_override_ids.append(existing_price_id)

            if mapping.scope == "deployment" and target_facts is not None:
                if target_facts.key not in affected_deployment_keys:
                    affected_deployment_keys.append(target_facts.key)
                if (
                    existing_entry is not None
                    and existing_entry.deployment
                    and existing_entry.deployment.casefold() == target_facts.key.casefold()
                ):
                    v_cloud = existing_entry.cloud
                    v_publisher = existing_entry.publisher
                    v_model = existing_entry.model
                    v_version = existing_entry.version
                    v_dep_type = existing_entry.deployment_type
                    v_regions = (
                        list(existing_entry.regions) if existing_entry.regions else None
                    )
                else:
                    v_cloud = (
                        target_facts.cloud
                        or mapping.cloud
                        or cand.pricing_cloud
                        or run.pricing_cloud
                    )
                    v_publisher = cand.publisher or target_facts.publisher
                    v_model = (
                        (target_facts.model or cand.model)
                        if cand.model == "*"
                        else cand.model
                    )
                    v_version = cand.version or target_facts.version
                    v_dep_type = canonical_deployment_type(
                        cand.deployment_type or target_facts.deployment_type
                    )
                    cand_region = (
                        normalize_region(cand.region)
                        if cand.provider == "azure"
                        else cand.region
                    )
                    v_regions = (
                        [cand_region]
                        if cand_region
                        else ([target_facts.region] if target_facts.region else None)
                    )
                v_deployment: str | None = target_facts.key
            else:
                v_cloud = (
                    mapping.cloud or cand.pricing_cloud or run.pricing_cloud
                ).strip().casefold()
                v_publisher = cand.publisher
                v_model = cand.model
                v_version = cand.version
                v_dep_type = canonical_deployment_type(cand.deployment_type)
                cand_region = (
                    normalize_region(cand.region)
                    if cand.provider == "azure"
                    else cand.region
                )
                if (
                    existing_entry is not None
                    and existing_entry.deployment is None
                    and existing_entry.regions is None
                    and v_dep_type
                    and v_dep_type.startswith(("Global", "DataZone"))
                ):
                    v_regions = None
                else:
                    v_regions = [cand_region] if cand_region else None
                v_deployment = None
                for dep_facts in deployments.values():
                    if (
                        dep_facts.cloud
                        and dep_facts.cloud.casefold() == v_cloud.casefold()
                        and dep_facts.model
                        and (
                            v_model == "*"
                            or dep_facts.model.casefold() == v_model.casefold()
                        )
                    ):
                        if dep_facts.key not in affected_deployment_keys:
                            affected_deployment_keys.append(dep_facts.key)

            version_id = deterministic_id(
                "price", actor.tenant_id, publication_id, cand.candidate_id
            )
            prepared_versions.append(
                PriceVersion(
                    id=version_id,
                    tenant_id=actor.tenant_id,
                    origin="billing",
                    cloud=v_cloud,
                    publisher=v_publisher,
                    model=v_model,
                    aliases=[],
                    version=v_version,
                    deployment_type=v_dep_type,
                    regions=v_regions,
                    deployment=v_deployment,
                    input_per_million=cand.input_per_million,
                    cached_input_per_million=cand.cached_input_per_million,
                    output_per_million=cand.output_per_million,
                    ptu_hourly=cand.ptu_hourly,
                    monthly_amount=cand.monthly_amount,
                    effective_from=effective_from,
                    source_url=None,
                    note=note_text,
                    overrides=existing_price_id,
                    recorded_by=actor.object_id,
                    publication_id=publication_id,
                    billing_run_id=run.id,
                    billing_provider=run.provider,
                    source_month=run.source_month,
                    source_title=source_title,
                    source_reference=billing_evidence_ref(run.id, cand.candidate_id),
                    created_at=now,
                    updated_at=now,
                )
            )

        # Phase 1: Write prepared PriceVersion documents (invisible until publication commits)
        await self._repository.create_prepared_price_versions(prepared_versions)

        # Phase 2: Commit BillingPublication and published BillingAcquisitionRun atomically with CAS
        publication = BillingPublication(
            id=publication_id,
            tenant_id=actor.tenant_id,
            run_id=run.id,
            connection_id=run.connection_id,
            provider=run.provider,
            pricing_cloud=run.pricing_cloud,
            source_month=run.source_month,
            effective_from=effective_from,
            backdated=backdated,
            price_version_ids=[v.id for v in prepared_versions],
            affected_deployment_keys=affected_deployment_keys,
            replaced_manual_override_ids=replaced_manual_override_ids,
            note=note_text,
            published_by=actor.object_id,
            published_at=now,
            created_at=now,
            updated_at=now,
        )
        published_run = run.model_copy(
            update={
                "status": "published",
                "published_at": now,
                "published_by": actor.object_id,
                "publication_id": publication_id,
                "updated_at": now,
            }
        )
        saved_pub = await self._repository.create_billing_publication(
            publication,
            published_run,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                action="pricing.billingRatesPublished",
                resource_type="billingPublication",
                resource_id=publication_id,
                actor_object_id=actor.object_id,
                details={
                    "runId": run.id,
                    "provider": run.provider,
                    "pricingCloud": run.pricing_cloud,
                    "sourceMonth": run.source_month,
                    "effectiveFrom": effective_from.isoformat(),
                    "backdated": backdated,
                    "priceVersionIds": [v.id for v in prepared_versions],
                    "affectedDeploymentKeys": affected_deployment_keys,
                    "replacedManualOverrideIds": replaced_manual_override_ids,
                },
            ),
        )
        return billing_publication_view(saved_pub)

    async def list_billing_publications(
        self, actor: Actor
    ) -> list[BillingPublicationView]:
        publications = await self._repository.list_billing_publications(actor.tenant_id)
        return [
            billing_publication_view(pub)
            for pub in sorted(
                publications, key=lambda item: (item.published_at, item.id), reverse=True
            )
        ]


__all__ = [
    "BillingConnectionCheckResult",
    "BillingConnectionCreate",
    "BillingConnectionUpdate",
    "BillingConnectionView",
    "BillingImportRequest",
    "BillingMappingsUpdateRequest",
    "BillingPayloadTooLargeError",
    "BillingPublicationView",
    "BillingPublishRequest",
    "BillingRunDetailView",
    "BillingRunSummaryView",
    "BillingRunTriggerRequest",
    "BillingSchedulerStatusView",
    "BillingSourceTemplateView",
    "CloudView",
    "DeploymentPricingView",
    "EndpointPricingView",
    "PriceHistoryView",
    "PriceLineView",
    "PriceListView",
    "PriceSourceView",
    "PriceStatus",
    "PriceView",
    "PricingOverview",
    "PricingService",
    "UnpricedReport",
    "UnpricedRow",
    "price_view",
]
