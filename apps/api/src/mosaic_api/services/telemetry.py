"""Whether a gateway's telemetry reaches MOSAIC, and turning it on where MOSAIC may. See ADR 0019.

Usage views are only as good as the logs behind them. For each gateway this checks the four
things that decide it, says exactly what to fix when one is missing, and fixes what MOSAIC is
allowed to: the ``azuremonitor`` logger and the diagnostics of the APIs MOSAIC published, on
gateways it manages. It never writes a diagnostic setting, which needs a role on the service MOSAIC
doesn't hold, and never touches the diagnostics of an API it only adopted, whose logging belongs to
someone else.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import structlog

from mosaic_api.domain import (
    ApimResourceId,
    AuditEvent,
    Gateway,
    ManagementMode,
    MosaicModel,
    new_id,
    utc_now,
)
from mosaic_api.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    UpstreamAuthorizationError,
    UpstreamError,
)
from mosaic_api.integrations.apim import ApimClient, ApimWriter
from mosaic_api.integrations.apim.diagnostics import (
    AZURE_MONITOR,
    ApiDiagnosticGap,
    JsonObject,
    LogRouting,
    api_diagnostic_gaps,
    api_diagnostic_payload,
    diagnostic_setting_command,
    evaluate_diagnostic_settings,
    monitoring_reader_command,
)
from mosaic_api.integrations.loganalytics import (
    API_NAME,
    PROBE_HOURS,
    LogQueryAccessError,
    LogsQuery,
    probe_query,
)
from mosaic_api.repositories import (
    EntitlementRepository,
    GatewayRepository,
    UsageRollupRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.usage_telemetry import (
    RolledUpApi,
    UsageRollupState,
    rollup_stale_after,
)

logger = structlog.get_logger()

ClientFactory = Callable[[ApimResourceId], ApimClient]
WriterFactory = Callable[[ApimResourceId], ApimWriter]
IdentityResolver = Callable[[], Awaitable[str | None]]

# Background writes are audited under this actor, following ``system:bootstrap``.
ROLLUP_ACTOR = "system:usage-rollup"
_DIAGNOSTIC_READS = 4

CheckId = Literal["logger", "logRouting", "logAccess", "apiDiagnostics", "rollups"]
CheckStatus = Literal["ok", "warning", "error", "unknown"]


class TelemetryCheck(MosaicModel):
    id: CheckId
    status: CheckStatus
    title: str
    detail: str
    command: str | None = None


class ApiTelemetry(MosaicModel):
    api_name: str
    display_name: str
    kind: Literal["model", "mcp"]
    published: bool
    # The API has no diagnostic of its own, so it logs as the service's All APIs setting says.
    all_apis: bool = False
    gaps: list[ApiDiagnosticGap]


class TelemetryProbe(MosaicModel):
    hours: int
    gateway_rows: int
    traced_rows: int
    llm_rows: int
    last_seen: datetime | None


class RollupStatus(MosaicModel):
    last_run_at: datetime | None
    last_success_at: datetime | None
    last_duration_ms: int | None
    queried_through: datetime | None
    lag_minutes: int | None
    data_available_from: str | None
    last_error: str | None
    last_error_at: datetime | None
    backfill_status: Literal["idle", "running", "done", "failed"]
    backfill_from: str | None
    backfill_next: str | None
    last_rows: int
    last_written: int
    unknown_trace_versions: int
    diagnostics_error: str | None


class GatewayTelemetry(MosaicModel):
    gateway_id: str
    gateway_name: str
    management_mode: ManagementMode
    ready: bool
    can_enable: bool
    rollups_enabled: bool
    checked_at: datetime
    workspace_id: str | None
    checks: list[TelemetryCheck]
    apis: list[ApiTelemetry]
    probe: TelemetryProbe | None
    rollup: RollupStatus | None


@dataclass(frozen=True)
class DiagnosticsOutcome:
    instrumented: list[str]
    failed: dict[str, str]
    llm_unsupported: list[str]


async def governed_apis(
    gateway_repository: GatewayRepository, tenant_id: str, *, now: datetime | None = None
) -> dict[str, list[RolledUpApi]]:
    """Every model API and MCP server MOSAIC governs, by gateway ID, named as the logs name them.

    Published and adopted APIs alike: a call to an adopted API can still be linked by its
    subscription, and is otherwise reported as unattributed so administrators see it.
    """

    seen_at = now or utc_now()
    model_apis = await gateway_repository.list_model_apis(tenant_id)
    mcp_servers = await gateway_repository.list_mcp_servers(tenant_id)
    publications = {
        publication.id: publication
        for publication in await gateway_repository.list_publications(tenant_id)
    }
    apis: dict[str, dict[str, RolledUpApi]] = {}
    for model_api in model_apis:
        name = model_api.api_name.casefold()
        if not API_NAME.fullmatch(name):
            logger.warning("usage_api_name_skipped", gateway_id=model_api.gateway_id)
            continue
        publication = (
            publications.get(model_api.publication_id) if model_api.publication_id else None
        )
        apis.setdefault(model_api.gateway_id, {})[name] = RolledUpApi(
            api_name=name,
            resource_id=model_api.id,
            publication_id=model_api.publication_id,
            kind="model",
            display_name=model_api.display_name,
            model_endpoint_id=publication.model_endpoint_id if publication else None,
            deployment_name=publication.deployment_name if publication else None,
            subscription_name=publication.subscription_name if publication else None,
            first_seen_at=seen_at,
        )
    for server in mcp_servers:
        name = server.api_name.casefold()
        if not API_NAME.fullmatch(name):
            logger.warning("usage_api_name_skipped", gateway_id=server.gateway_id)
            continue
        apis.setdefault(server.gateway_id, {}).setdefault(
            name,
            RolledUpApi(
                api_name=name,
                resource_id=server.id,
                publication_id=server.publication_id,
                kind="mcp",
                display_name=server.display_name,
                first_seen_at=seen_at,
            ),
        )
    return {
        gateway_id: sorted(by_name.values(), key=lambda api: api.api_name)
        for gateway_id, by_name in apis.items()
    }


def _problem_text(routing: LogRouting) -> str:
    if routing.problem == "missing":
        return "The gateway sends no resource logs to a Log Analytics workspace."
    if routing.problem == "noGatewayLogs":
        return "The gateway's diagnostic setting doesn't send the GatewayLogs category."
    if routing.problem == "legacyTable":
        return (
            "The gateway's logs go to the legacy AzureDiagnostics table. MOSAIC reads the "
            "resource-specific tables, so the setting needs the resource-specific destination."
        )
    if routing.problem == "noLlmLogs":
        return (
            "The gateway's diagnostic setting doesn't send the GatewayLlmLogs category, so "
            "MOSAIC can count calls but not tokens."
        )
    return "Gateway and LLM logs go to a Log Analytics workspace as resource-specific tables."


def _error_reason(error: DomainError) -> str:
    reason = error.details.get("reason") if error.details else None
    return f"{error.message}: {reason}" if isinstance(reason, str) and reason else error.message


class TelemetryService:
    def __init__(
        self,
        *,
        gateway_repository: GatewayRepository,
        rollup_repository: UsageRollupRepository,
        entitlement_repository: EntitlementRepository,
        client_factory: ClientFactory,
        writer_factory: WriterFactory,
        logs: LogsQuery | None,
        rollups_enabled: bool,
        interval_seconds: int,
        principal_id: str | None = None,
        identity_resolver: IdentityResolver | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._gateways = gateway_repository
        self._rollups = rollup_repository
        self._entitlements = entitlement_repository
        self._client_factory = client_factory
        self._writer_factory = writer_factory
        self._logs = logs
        self._rollups_enabled = rollups_enabled
        self._interval = timedelta(seconds=interval_seconds)
        self._principal_id = principal_id
        self._identity_resolver = identity_resolver
        self._identity_resolved = principal_id is not None
        self._clock = clock

    async def _principal(self) -> str | None:
        if self._identity_resolved or self._identity_resolver is None:
            return self._principal_id
        self._identity_resolved = True
        try:
            self._principal_id = await self._identity_resolver()
        except Exception:
            logger.warning("telemetry_identity_lookup_failed")
        return self._principal_id

    async def _gateway(self, actor: Actor, gateway_id: str) -> Gateway:
        gateway = await self._gateways.get_gateway(actor.tenant_id, gateway_id)
        if gateway is None:
            raise NotFoundError("Gateway not found", details={"gatewayId": gateway_id})
        return gateway

    @staticmethod
    def _can_enable(gateway: Gateway) -> bool:
        return gateway.management_mode == ManagementMode.MANAGE and gateway.access.can_write

    async def status(self, actor: Actor, gateway_id: str) -> GatewayTelemetry:
        gateway = await self._gateway(actor, gateway_id)
        now = self._clock()
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        client = self._client_factory(resource)
        apis = (await governed_apis(self._gateways, actor.tenant_id, now=now)).get(gateway.id, [])
        principal = await self._principal()
        state = await self._rollups.get_rollup_state(actor.tenant_id, gateway.id)
        published = any(api.publication_id is not None for api in apis)
        (logger_check, routing_check, routing), access, api_rows = await asyncio.gather(
            self._routing_checks(client, resource, principal, self._can_enable(gateway)),
            self._access_check(resource, principal, now, published=published),
            self._api_checks(client, apis),
        )
        access_check, probe = access
        checks = [
            logger_check,
            routing_check,
            access_check,
            self._api_diagnostics_check(gateway, api_rows),
            self._rollup_check(state, now),
        ]
        return GatewayTelemetry(
            gateway_id=gateway.id,
            gateway_name=gateway.name,
            management_mode=gateway.management_mode,
            ready=all(check.status == "ok" for check in checks),
            can_enable=self._can_enable(gateway),
            rollups_enabled=self._rollups_enabled,
            checked_at=now,
            workspace_id=routing.workspace_id if routing else None,
            checks=checks,
            apis=api_rows,
            probe=probe,
            rollup=self._rollup_status(state, now),
        )

    async def _routing_checks(
        self,
        client: ApimClient,
        resource: ApimResourceId,
        principal: str | None,
        can_enable: bool,
    ) -> tuple[TelemetryCheck, TelemetryCheck, LogRouting | None]:
        reader = monitoring_reader_command(resource.canonical, principal)
        try:
            found = await client.get_logger(AZURE_MONITOR)
        except UpstreamAuthorizationError:
            logger_check = TelemetryCheck(
                id="logger",
                status="unknown",
                title="Azure Monitor logger",
                detail="MOSAIC can't read the gateway's loggers.",
            )
        except DomainError as error:
            logger_check = TelemetryCheck(
                id="logger",
                status="unknown",
                title="Azure Monitor logger",
                detail=_error_reason(error),
            )
        else:
            logger_check = TelemetryCheck(
                id="logger",
                status="ok" if found else "error",
                title="Azure Monitor logger",
                detail=(
                    "The gateway has the azuremonitor logger that API diagnostics write through."
                    if found
                    else "The gateway has no azuremonitor logger, so no API can log to Azure "
                    "Monitor. "
                    + (
                        "Enable API diagnostics to create it."
                        if can_enable
                        else "MOSAIC can't change this gateway, so its owner must add the logger."
                    )
                ),
            )
        try:
            settings = await client.list_diagnostic_settings()
        except UpstreamAuthorizationError:
            return (
                logger_check,
                TelemetryCheck(
                    id="logRouting",
                    status="unknown",
                    title="Logs sent to Log Analytics",
                    detail=(
                        "MOSAIC can't read the gateway's diagnostic settings. Monitoring Reader "
                        "on the gateway lets it."
                    ),
                    command=reader,
                ),
                None,
            )
        except DomainError as error:
            return (
                logger_check,
                TelemetryCheck(
                    id="logRouting",
                    status="unknown",
                    title="Logs sent to Log Analytics",
                    detail=_error_reason(error),
                ),
                None,
            )
        routing = evaluate_diagnostic_settings(settings)
        return (
            logger_check,
            TelemetryCheck(
                id="logRouting",
                status="ok" if routing.ready else "error",
                title="Logs sent to Log Analytics",
                detail=_problem_text(routing),
                command=None
                if routing.ready
                else diagnostic_setting_command(resource.canonical, routing),
            ),
            routing,
        )

    async def _access_check(
        self, resource: ApimResourceId, principal: str | None, now: datetime, *, published: bool
    ) -> tuple[TelemetryCheck, TelemetryProbe | None]:
        title = "MOSAIC can read the logs"
        if self._logs is None:
            return (
                TelemetryCheck(
                    id="logAccess",
                    status="unknown",
                    title=title,
                    detail="This deployment doesn't read gateway telemetry.",
                ),
                None,
            )
        try:
            rows = await self._logs.query(
                resource.canonical,
                probe_query(),
                start=now - timedelta(hours=PROBE_HOURS),
                end=now,
            )
        except LogQueryAccessError:
            return (
                TelemetryCheck(
                    id="logAccess",
                    status="error",
                    title=title,
                    detail=(
                        "MOSAIC's identity may not read this gateway's logs. Monitoring Reader on "
                        "the gateway lets it, if the workspace allows resource permissions."
                    ),
                    command=monitoring_reader_command(resource.canonical, principal),
                ),
                None,
            )
        except DomainError as error:
            return (
                TelemetryCheck(
                    id="logAccess", status="unknown", title=title, detail=_error_reason(error)
                ),
                None,
            )
        by_source = {str(row.get("source")): row for row in rows}
        gateway_row = by_source.get("gateway", {})
        llm_row = by_source.get("llm", {})
        last_seen = [
            value
            for value in (gateway_row.get("lastSeen"), llm_row.get("lastSeen"))
            if isinstance(value, datetime)
        ]
        probe = TelemetryProbe(
            hours=PROBE_HOURS,
            gateway_rows=int(gateway_row.get("rows") or 0),
            traced_rows=int(gateway_row.get("traced") or 0),
            llm_rows=int(llm_row.get("rows") or 0),
            last_seen=max(last_seen) if last_seen else None,
        )
        if probe.gateway_rows == 0:
            status: CheckStatus = "warning"
            detail = (
                f"MOSAIC can read the logs, but no gateway calls were logged in the last "
                f"{PROBE_HOURS} hours."
            )
        elif probe.traced_rows == 0:
            status = "warning"
            detail = "Calls are logged, but none carries MOSAIC's attribution trace. " + (
                "Re-apply the gateway's publications, and check that their API diagnostics log "
                "at Information."
                if published
                else "MOSAIC has published nothing on this gateway, so it counts the calls but "
                "can't say who made them."
            )
        else:
            status = "ok"
            detail = (
                f"{probe.gateway_rows:,} calls logged in the last {PROBE_HOURS} hours, "
                f"{probe.traced_rows:,} of them attributed."
            )
        return TelemetryCheck(id="logAccess", status=status, title=title, detail=detail), probe

    async def _api_checks(
        self, client: ApimClient, apis: Sequence[RolledUpApi]
    ) -> list[ApiTelemetry]:
        if not apis:
            return []
        slots = asyncio.Semaphore(_DIAGNOSTIC_READS)

        async def read(api_name: str | None) -> tuple[JsonObject | None, bool]:
            """A diagnostic, or the service's when no API is named, and whether it was read."""

            async with slots:
                try:
                    if api_name is None:
                        return await client.get_diagnostic(AZURE_MONITOR), True
                    return await client.get_api_diagnostic(api_name, AZURE_MONITOR), True
                except DomainError:
                    return None, False

        (shared, _), *own = await asyncio.gather(read(None), *(read(api.api_name) for api in apis))
        rows: list[ApiTelemetry] = []
        for api, (diagnostic, readable) in zip(apis, own, strict=True):
            # An API without a diagnostic of its own logs under the one set for All APIs.
            inherits = readable and diagnostic is None and shared is not None
            rows.append(
                ApiTelemetry(
                    api_name=api.api_name,
                    display_name=api.display_name,
                    kind=api.kind,
                    published=api.publication_id is not None,
                    all_apis=inherits,
                    gaps=api_diagnostic_gaps(
                        shared if inherits else diagnostic, llm=api.kind == "model"
                    ),
                )
            )
        return rows

    def _api_diagnostics_check(
        self, gateway: Gateway, apis: Sequence[ApiTelemetry]
    ) -> TelemetryCheck:
        title = "API diagnostics"
        if not apis:
            return TelemetryCheck(
                id="apiDiagnostics",
                status="unknown",
                title=title,
                detail="MOSAIC governs no APIs on this gateway yet.",
            )
        missing = [api for api in apis if api.gaps]
        if not missing:
            return TelemetryCheck(
                id="apiDiagnostics",
                status="ok",
                title=title,
                detail=f"All {len(apis)} governed APIs log every call at Information.",
            )
        fixable = [api for api in missing if api.published]
        detail = (
            f"{len(missing)} of {len(apis)} governed APIs don't log what MOSAIC needs."
            if len(missing) != 1
            else f"1 of {len(apis)} governed APIs doesn't log what MOSAIC needs."
        )
        if fixable and self._can_enable(gateway):
            detail += " Enable API diagnostics to fix the ones MOSAIC published."
        elif len(fixable) < len(missing):
            detail += (
                " MOSAIC doesn't change adopted APIs: set their azuremonitor diagnostic, or the "
                "one for All APIs, to Information, sampling 100%, with LLM logs on for model APIs."
            )
        return TelemetryCheck(
            id="apiDiagnostics",
            status="warning" if len(missing) < len(apis) else "error",
            title=title,
            detail=detail,
        )

    def _rollup_check(self, state: UsageRollupState | None, now: datetime) -> TelemetryCheck:
        title = "Usage rollups"
        if not self._rollups_enabled:
            return TelemetryCheck(
                id="rollups",
                status="unknown",
                title=title,
                detail="This deployment doesn't roll up gateway telemetry.",
            )
        if state is None or state.last_run_at is None:
            return TelemetryCheck(
                id="rollups",
                status="unknown",
                title=title,
                detail="MOSAIC hasn't rolled up this gateway's telemetry yet.",
            )
        if state.last_error and (
            state.last_success_at is None
            or (state.last_error_at is not None and state.last_error_at > state.last_success_at)
        ):
            return TelemetryCheck(
                id="rollups",
                status="error",
                title=title,
                detail=f"The last rollup failed: {state.last_error}",
            )
        if state.last_success_at is None or now - state.last_success_at > rollup_stale_after(
            self._interval
        ):
            return TelemetryCheck(
                id="rollups",
                status="warning",
                title=title,
                detail="Rollups are behind; usage views may be out of date.",
            )
        return TelemetryCheck(
            id="rollups",
            status="ok",
            title=title,
            detail="Usage is rolled up on schedule.",
        )

    @staticmethod
    def _rollup_status(state: UsageRollupState | None, now: datetime) -> RollupStatus | None:
        if state is None:
            return None
        lag = (
            None
            if state.last_success_at is None
            else max(0, int((now - state.last_success_at).total_seconds() // 60))
        )
        return RollupStatus(
            last_run_at=state.last_run_at,
            last_success_at=state.last_success_at,
            last_duration_ms=state.last_duration_ms,
            queried_through=state.queried_through,
            lag_minutes=lag,
            data_available_from=state.data_available_from,
            last_error=state.last_error,
            last_error_at=state.last_error_at,
            backfill_status=state.backfill_status,
            backfill_from=state.backfill_from,
            backfill_next=state.backfill_next,
            last_rows=state.last_rows,
            last_written=state.last_written,
            unknown_trace_versions=state.unknown_trace_versions,
            diagnostics_error=state.diagnostics_error,
        )

    async def enable(self, actor: Actor, gateway_id: str) -> GatewayTelemetry:
        """Create the logger and the published APIs' diagnostics. Audited."""

        gateway = await self._gateway(actor, gateway_id)
        if gateway.management_mode != ManagementMode.MANAGE:
            raise ConflictError(
                "MOSAIC only changes gateways it manages. Switch the gateway to manage mode, or "
                "create the azuremonitor logger yourself.",
                details={"gatewayId": gateway.id},
            )
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        writer = self._writer_factory(resource)
        await writer.put_azure_monitor_logger()
        apis = (await governed_apis(self._gateways, actor.tenant_id)).get(gateway.id, [])
        outcome = await self.ensure_api_diagnostics(gateway, apis, only_missing=True)
        await self._record_outcome(gateway, outcome)
        await self._entitlements.record_audit(
            AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                action="gateway.telemetryEnabled",
                resource_type="gateway",
                resource_id=gateway.id,
                actor_object_id=actor.object_id,
                details={
                    "logger": AZURE_MONITOR,
                    "apis": outcome.instrumented,
                    "failed": sorted(outcome.failed),
                    "llmUnsupported": outcome.llm_unsupported,
                },
            )
        )
        return await self.status(actor, gateway_id)

    async def has_logger(self, gateway: Gateway) -> bool:
        client = self._client_factory(ApimResourceId.parse(gateway.azure_resource_id))
        return await client.get_logger(AZURE_MONITOR) is not None

    async def ensure_api_diagnostics(
        self, gateway: Gateway, apis: Sequence[RolledUpApi], *, only_missing: bool = False
    ) -> DiagnosticsOutcome:
        """Write MOSAIC's diagnostic on each API it published on a managed gateway.

        With ``only_missing``, each diagnostic is read first and written only if it lacks
        something, which is how the rollup job keeps new publications instrumented.
        """

        resource = ApimResourceId.parse(gateway.azure_resource_id)
        writer = self._writer_factory(resource)
        client = self._client_factory(resource)
        logger_id = writer.resource_id(f"loggers/{AZURE_MONITOR}")
        instrumented: list[str] = []
        failed: dict[str, str] = {}
        llm_unsupported: list[str] = []
        for api in apis:
            if api.publication_id is None or api.removed_at is not None:
                continue
            llm = api.kind == "model"
            try:
                if only_missing:
                    current = await client.get_api_diagnostic(api.api_name, AZURE_MONITOR)
                    if not api_diagnostic_gaps(current, llm=llm):
                        instrumented.append(api.api_name)
                        continue
                try:
                    await writer.put_api_diagnostic(
                        api.api_name, api_diagnostic_payload(logger_id, llm=llm)
                    )
                except UpstreamError as error:
                    # A gateway whose tier or version has no LLM logging refuses the setting;
                    # calls are still counted without it.
                    if not llm or error.details.get("statusCode") != 400:
                        raise
                    await writer.put_api_diagnostic(
                        api.api_name, api_diagnostic_payload(logger_id, llm=False)
                    )
                    llm_unsupported.append(api.api_name)
            except DomainError as error:
                failed[api.api_name] = _error_reason(error)[:300]
                logger.warning(
                    "api_diagnostic_write_failed", gateway_id=gateway.id, api=api.api_name
                )
                continue
            instrumented.append(api.api_name)
        return DiagnosticsOutcome(
            instrumented=sorted(instrumented),
            failed=failed,
            llm_unsupported=sorted(llm_unsupported),
        )

    async def _record_outcome(self, gateway: Gateway, outcome: DiagnosticsOutcome) -> None:
        update = {
            "instrumented_apis": outcome.instrumented,
            "diagnostics_error": diagnostics_error(outcome),
        }
        await self._rollups.update_rollup_state(
            gateway.tenant_id, gateway.id, lambda state: state.model_copy(update=update)
        )


def diagnostics_error(outcome: DiagnosticsOutcome) -> str | None:
    if outcome.failed:
        names = ", ".join(sorted(outcome.failed)[:5])
        return f"MOSAIC couldn't write the diagnostic for {names}"
    if outcome.llm_unsupported:
        names = ", ".join(outcome.llm_unsupported[:5])
        return f"The gateway refused LLM logging for {names}, so their tokens aren't counted"
    return None
