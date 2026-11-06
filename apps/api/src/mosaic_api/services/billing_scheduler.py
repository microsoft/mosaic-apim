"""Opt-in monthly billing draft retrieval and restart-safe background polling worker."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress
from datetime import date, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import structlog
from pydantic import Field

from mosaic_api.billing_pricing import (
    BillingAcquisitionRun,
    BillingConnection,
    BillingReviewIssue,
    BillingRunTriggerRequest,
    BillingSchedule,
    BillingSchedulerStatusView,
    parse_source_month,
    previous_complete_month,
)
from mosaic_api.domain import AuditEvent, MosaicModel, new_id, utc_now
from mosaic_api.errors import ConflictError
from mosaic_api.repositories import GatewayRepository, PricingRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.pricing import (
    SAVE_ATTEMPTS,
    PricingService,
    _safe_billing_error_message,
)

logger = structlog.get_logger()

BILLING_SCHEDULER_ACTOR_ID = "mosaic-billing-scheduler"
DEFAULT_BILLING_LEASE_SECONDS = 180.0
NON_FAILED_RUN_STATUSES: frozenset[str] = frozenset(
    {"queued", "running", "readyForReview", "published"}
)

SCHEDULE_HOSTING_WARNING = (
    "Monthly scheduled billing draft refreshes are enabled, but "
    "MOSAIC_SCHEDULED_WORKERS_ALWAYS_ON is false. Keep at least one API instance "
    "always on so scheduled monthly draft retrievals run on time."
)
SCHEDULE_DISABLED_WARNING = (
    "Monthly scheduled billing draft refreshes are enabled on at least one billing "
    "connection, but MOSAIC_BILLING_REFRESH_ENABLED is false."
)


def billing_schedule_lease_scope(connection_id: str, source_month: str) -> str:
    """Scope lease key guarding a connection's monthly scheduled draft retrieval."""

    return f"billing-schedule:{connection_id}:{source_month}"


def billing_run_lease_scope(run_id: str) -> str:
    """Scope lease key guarding one in-flight billing acquisition run poll."""

    return f"billing-run:{run_id}"


class BillingScheduleWindow(MosaicModel):
    """Evaluated wall-time schedule window and target completed source month."""

    source_month: str
    period_start: date
    period_end: date
    days_in_month: int
    time_zone: str
    local_now: datetime
    local_date: date
    local_hhmm: str
    day_of_month: int
    time_utc: str
    is_time_reached: bool
    already_triggered_for_month: bool
    has_existing_non_failed_run: bool = False
    is_due: bool


def resolve_schedule_window(
    schedule: BillingSchedule,
    now: datetime,
    *,
    existing_runs: Sequence[BillingAcquisitionRun] = (),
) -> BillingScheduleWindow:
    """Evaluate a connection's monthly schedule against ``now`` in ``schedule.time_zone``."""

    tz = ZoneInfo(schedule.time_zone)
    now_in_tz = now.astimezone(tz)
    local_date = now_in_tz.date()
    source_month = previous_complete_month(local_date)
    period_start, period_end = parse_source_month(source_month)
    days_in_month = (period_end - period_start).days + 1
    current_hhmm = now_in_tz.strftime("%H:%M")

    is_time_reached = (now_in_tz.day > schedule.day_of_month) or (
        now_in_tz.day == schedule.day_of_month and current_hhmm >= schedule.time_utc
    )
    already_triggered = schedule.last_triggered_source_month == source_month
    has_non_failed = any(
        run.source_month == source_month and run.status in NON_FAILED_RUN_STATUSES
        for run in existing_runs
    )
    is_due = bool(
        schedule.enabled
        and is_time_reached
        and not already_triggered
        and not has_non_failed
    )

    return BillingScheduleWindow(
        source_month=source_month,
        period_start=period_start,
        period_end=period_end,
        days_in_month=days_in_month,
        time_zone=schedule.time_zone,
        local_now=now_in_tz,
        local_date=local_date,
        local_hhmm=current_hhmm,
        day_of_month=schedule.day_of_month,
        time_utc=schedule.time_utc,
        is_time_reached=is_time_reached,
        already_triggered_for_month=already_triggered,
        has_existing_non_failed_run=has_non_failed,
        is_due=is_due,
    )


def is_schedule_due(
    schedule: BillingSchedule,
    now: datetime,
    *,
    existing_runs: Sequence[BillingAcquisitionRun] = (),
) -> tuple[bool, str]:
    """Return ``(is_due, target_source_month)`` for ``schedule`` at ``now``."""

    window = resolve_schedule_window(schedule, now, existing_runs=existing_runs)
    return window.is_due, window.source_month


class BillingSchedulerTickResult(MosaicModel):
    """Outcome summary of one ``BillingPricingScheduler.run_once()`` execution."""

    tick_at: datetime
    polled_run_ids: list[str] = Field(default_factory=list)
    completed_run_ids: list[str] = Field(default_factory=list)
    failed_run_ids: list[str] = Field(default_factory=list)
    triggered_run_ids: list[str] = Field(default_factory=list)
    skipped_connection_ids: list[str] = Field(default_factory=list)
    lease_conflicts: list[str] = Field(default_factory=list)
    active_scheduled_connections: int = 0
    warning: str | None = None


class BillingPricingScheduler:
    """Leased background worker that polls in-flight runs and triggers due monthly drafts.

    Scheduled acquisitions ONLY ever create or poll draft runs (`status="running"` or
    `status="readyForReview"`). They never call `publish_billing_run` or activate prices.
    """

    def __init__(
        self,
        repository: PricingRepository,
        coordination: GatewayRepository,
        pricing_service: PricingService,
        *,
        tenant_id: str,
        interval_seconds: float = 300.0,
        lease_seconds: float = DEFAULT_BILLING_LEASE_SECONDS,
        enabled: bool = True,
        scheduled_workers_always_on: bool = False,
        clock: Callable[[], datetime] = utc_now,
        owner_id: str | None = None,
    ) -> None:
        self._repository = repository
        self._coordination = coordination
        self._pricing = pricing_service
        self._tenant_id = tenant_id
        self._interval_seconds = interval_seconds
        self._lease_seconds = lease_seconds
        self._enabled = enabled
        self._scheduled_workers_always_on = scheduled_workers_always_on
        self._clock = clock
        self._owner = owner_id or f"billing-pricing-scheduler:{uuid4().hex}"
        self._actor = Actor(
            tenant_id=tenant_id,
            object_id=BILLING_SCHEDULER_ACTOR_ID,
        )
        self._task: asyncio.Task[None] | None = None
        self._last_tick_at: datetime | None = None
        self._last_succeeded_at: datetime | None = None
        self._last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def scheduled_workers_always_on(self) -> bool:
        return self._scheduled_workers_always_on

    @property
    def owner_id(self) -> str:
        return self._owner

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    @property
    def lease_seconds(self) -> float:
        return self._lease_seconds

    @property
    def last_tick_at(self) -> datetime | None:
        return self._last_tick_at

    @property
    def last_succeeded_at(self) -> datetime | None:
        return self._last_succeeded_at

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def start(self) -> None:
        if not self._enabled:
            return
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name="billing-pricing-scheduler"
            )

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def renew_scope_lease(self, scope: str) -> None:
        """Renew ``scope`` lease held by this worker, raising ``ConflictError`` if lost."""

        scope_leases = getattr(self._coordination, "scope_leases", None)
        if isinstance(scope_leases, dict):
            current = scope_leases.get((self._tenant_id, scope))
            if (
                current is None
                or current[0] != self._owner
                or current[1] <= utc_now()
            ):
                raise ConflictError(
                    "The scope lease expired or was acquired by another worker.",
                    details={"scope": scope},
                )
        await self._coordination.release_scope_lease(
            self._tenant_id, scope, self._owner
        )
        await self._coordination.acquire_scope_lease(
            self._tenant_id,
            scope,
            self._owner,
            lease_seconds=self._lease_seconds,
        )

    async def readiness(
        self, tenant_id: str | None = None
    ) -> BillingSchedulerStatusView:
        """Return hosting readiness and active schedule summary for ``tenant_id``."""

        resolved_tenant = tenant_id or self._tenant_id
        connections, runs = await asyncio.gather(
            self._repository.list_billing_connections(resolved_tenant),
            self._repository.list_billing_runs(resolved_tenant),
        )
        active_scheduled = sum(1 for conn in connections if conn.schedule.enabled)
        in_flight = sum(1 for run in runs if run.status in ("queued", "running"))
        warnings: list[str] = []
        if active_scheduled > 0 and not self._enabled:
            warnings.append(SCHEDULE_DISABLED_WARNING)
        if active_scheduled > 0 and not self._scheduled_workers_always_on:
            warnings.append(SCHEDULE_HOSTING_WARNING)
        primary_warning = warnings[0] if warnings else None
        is_ready = bool(
            self._enabled
            and (active_scheduled == 0 or self._scheduled_workers_always_on)
        )
        return BillingSchedulerStatusView(
            enabled=self._enabled,
            scheduled_workers_always_on=self._scheduled_workers_always_on,
            billing_scheduler_always_on=self._scheduled_workers_always_on,
            active_scheduled_connections=active_scheduled,
            in_flight_runs=in_flight,
            ready=is_ready,
            warning=primary_warning,
            warnings=warnings,
            last_tick_at=self._last_tick_at,
            last_succeeded_at=self._last_succeeded_at,
            last_error=self._last_error,
        )

    async def status(
        self, tenant_id: str | None = None
    ) -> BillingSchedulerStatusView:
        return await self.readiness(tenant_id)

    async def _update_connection_schedule_cas(
        self,
        connection_id: str,
        *,
        source_month: str,
        run_id: str | None,
        run_at: datetime,
        last_error: str | None,
    ) -> BillingConnection | None:
        for attempt in range(SAVE_ATTEMPTS):
            current = await self._repository.get_billing_connection(
                self._tenant_id, connection_id
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
            if updated_schedule == current.schedule:
                return current
            updated_conn = current.model_copy(
                update={
                    "schedule": updated_schedule,
                    "updated_at": run_at,
                    "updated_by": self._actor.object_id,
                }
            )
            try:
                return await self._repository.save_billing_connection(
                    updated_conn,
                    AuditEvent(
                        id=new_id("audit"),
                        tenant_id=self._tenant_id,
                        action="pricing.billingScheduleUpdated",
                        resource_type="billingConnection",
                        resource_id=connection_id,
                        actor_object_id=self._actor.object_id,
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

    async def _mark_run_failed_fallback(
        self,
        run: BillingAcquisitionRun,
        error: Exception,
    ) -> BillingAcquisitionRun | None:
        now = self._clock()
        error_msg = _safe_billing_error_message(error)
        current = await self._repository.get_billing_run(self._tenant_id, run.id)
        if current is None or current.status not in ("queued", "running"):
            return current
        failed_issue = BillingReviewIssue(
            code="acquisitionFailed",
            severity="blocker",
            message=error_msg,
        )
        failed_run = current.model_copy(
            update={
                "status": "failed",
                "status_message": error_msg,
                "completed_at": now,
                "poll_after": None,
                "continuation_token": None,
                "issues": [*current.issues, failed_issue],
                "updated_at": now,
            }
        )
        saved = await self._repository.save_billing_run(
            failed_run,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=self._tenant_id,
                action="pricing.billingRunFailed",
                resource_type="billingAcquisitionRun",
                resource_id=current.id,
                actor_object_id=self._actor.object_id,
                details={
                    "connectionId": current.connection_id,
                    "provider": current.provider,
                    "sourceMonth": current.source_month,
                    "acquisitionKind": current.acquisition_kind,
                    "error": error_msg,
                },
            ),
        )
        if current.acquisition_kind == "scheduled" and current.connection_id:
            await self._update_connection_schedule_cas(
                current.connection_id,
                source_month=current.source_month,
                run_id=current.id,
                run_at=now,
                last_error=error_msg,
            )
        return saved

    async def _poll_in_flight_runs(
        self, result: BillingSchedulerTickResult
    ) -> None:
        runs = await self._repository.list_billing_runs(self._tenant_id)
        running_runs = sorted(
            (
                run
                for run in runs
                if run.status == "running" and run.connection_id is not None
            ),
            key=lambda item: (item.started_at, item.id),
        )
        for run in running_runs:
            now = self._clock()
            if run.poll_after is not None and now < run.poll_after:
                continue

            scope = billing_run_lease_scope(run.id)
            try:
                await self._coordination.acquire_scope_lease(
                    self._tenant_id,
                    scope,
                    self._owner,
                    lease_seconds=self._lease_seconds,
                )
            except ConflictError:
                result.lease_conflicts.append(scope)
                continue

            try:
                current_run = await self._repository.get_billing_run(
                    self._tenant_id, run.id
                )
                if current_run is None or current_run.status != "running":
                    continue
                now = self._clock()
                if (
                    current_run.poll_after is not None
                    and now < current_run.poll_after
                ):
                    continue

                run_id = current_run.id
                expected_etag = current_run.etag

                async def renew_run_lease(
                    _run_id: str = run_id,
                    _scope: str = scope,
                    _etag: str | None = expected_etag,
                ) -> None:
                    await self.renew_scope_lease(_scope)
                    latest = await self._repository.get_billing_run(
                        self._tenant_id, _run_id
                    )
                    if (
                        latest is None
                        or latest.status != "running"
                        or (_etag is not None and latest.etag != _etag)
                    ):
                        raise ConflictError(
                            "Billing acquisition run changed while polling.",
                            details={"runId": _run_id, "scope": _scope},
                        )

                try:
                    polled_view = await self._pricing.poll_billing_run(
                        self._actor,
                        current_run.id,
                        lease_renewer=renew_run_lease,
                    )
                except ConflictError:
                    result.lease_conflicts.append(scope)
                    logger.warning(
                        "billing_scheduler_poll_lease_lost",
                        run_id=current_run.id,
                        scope=scope,
                    )
                    continue
                except Exception as exc:
                    try:
                        await renew_run_lease()
                        await self._mark_run_failed_fallback(current_run, exc)
                        result.polled_run_ids.append(current_run.id)
                        result.failed_run_ids.append(current_run.id)
                    except ConflictError:
                        result.lease_conflicts.append(scope)
                        logger.warning(
                            "billing_scheduler_poll_lease_lost_on_failure",
                            run_id=current_run.id,
                            scope=scope,
                        )
                    continue

                result.polled_run_ids.append(current_run.id)
                if polled_view.status == "readyForReview":
                    result.completed_run_ids.append(current_run.id)
                elif polled_view.status == "failed":
                    result.failed_run_ids.append(current_run.id)
            finally:
                await self._coordination.release_scope_lease(
                    self._tenant_id, scope, self._owner
                )

    async def _trigger_due_scheduled_runs(
        self, result: BillingSchedulerTickResult
    ) -> None:
        connections, all_runs = await asyncio.gather(
            self._repository.list_billing_connections(self._tenant_id),
            self._repository.list_billing_runs(self._tenant_id),
        )
        runs_by_conn: dict[str, list[BillingAcquisitionRun]] = {}
        for run in all_runs:
            if run.connection_id:
                runs_by_conn.setdefault(run.connection_id, []).append(run)

        active_scheduled = sum(
            1 for conn in connections if conn.schedule.enabled
        )
        result.active_scheduled_connections = active_scheduled
        if active_scheduled > 0 and not self._scheduled_workers_always_on:
            result.warning = SCHEDULE_HOSTING_WARNING

        for connection in connections:
            if not connection.schedule.enabled:
                continue

            now = self._clock()
            conn_runs = runs_by_conn.get(connection.id, [])
            window = resolve_schedule_window(
                connection.schedule,
                now,
                existing_runs=conn_runs,
            )
            if not window.is_due:
                result.skipped_connection_ids.append(connection.id)
                continue

            source_month = window.source_month
            scope = billing_schedule_lease_scope(connection.id, source_month)
            try:
                await self._coordination.acquire_scope_lease(
                    self._tenant_id,
                    scope,
                    self._owner,
                    lease_seconds=self._lease_seconds,
                )
            except ConflictError:
                result.lease_conflicts.append(scope)
                continue

            try:
                fresh_conn = await self._repository.get_billing_connection(
                    self._tenant_id, connection.id
                )
                if fresh_conn is None or not fresh_conn.schedule.enabled:
                    result.skipped_connection_ids.append(connection.id)
                    continue

                fresh_runs = await self._repository.list_billing_runs(
                    self._tenant_id, connection_id=fresh_conn.id
                )
                fresh_window = resolve_schedule_window(
                    fresh_conn.schedule,
                    self._clock(),
                    existing_runs=fresh_runs,
                )
                if (
                    not fresh_window.is_due
                    or fresh_window.source_month != source_month
                ):
                    result.skipped_connection_ids.append(connection.id)
                    continue

                conn_id = fresh_conn.id
                expected_conn_etag = fresh_conn.etag

                async def renew_schedule_lease(
                    _conn_id: str = conn_id,
                    _month: str = source_month,
                    _scope: str = scope,
                    _etag: str | None = expected_conn_etag,
                ) -> None:
                    await self.renew_scope_lease(_scope)
                    latest_conn = await self._repository.get_billing_connection(
                        self._tenant_id, _conn_id
                    )
                    if (
                        latest_conn is None
                        or not latest_conn.schedule.enabled
                        or latest_conn.schedule.last_triggered_source_month == _month
                        or (_etag is not None and latest_conn.etag != _etag)
                    ):
                        raise ConflictError(
                            "Billing connection schedule changed while running acquisition.",
                            details={
                                "connectionId": _conn_id,
                                "sourceMonth": _month,
                                "scope": _scope,
                            },
                        )

                try:
                    run_view = await self._pricing.start_billing_run(
                        self._actor,
                        fresh_conn.id,
                        BillingRunTriggerRequest(source_month=source_month),
                        acquisition_kind="scheduled",
                        lease_renewer=renew_schedule_lease,
                    )
                except ConflictError:
                    result.lease_conflicts.append(scope)
                    logger.warning(
                        "billing_scheduler_schedule_lease_lost",
                        connection_id=fresh_conn.id,
                        source_month=source_month,
                        scope=scope,
                    )
                    continue
                except Exception as exc:
                    try:
                        await renew_schedule_lease()
                        error_msg = _safe_billing_error_message(exc)
                        await self._update_connection_schedule_cas(
                            fresh_conn.id,
                            source_month=source_month,
                            run_id=None,
                            run_at=self._clock(),
                            last_error=error_msg,
                        )
                    except ConflictError:
                        result.lease_conflicts.append(scope)
                    continue

                # Ensure schedule metadata is recorded even if start_billing_run was customized
                after_conn = await self._repository.get_billing_connection(
                    self._tenant_id, fresh_conn.id
                )
                if (
                    after_conn is not None
                    and after_conn.schedule.last_run_id != run_view.id
                ):
                    with suppress(ConflictError):
                        await self._update_connection_schedule_cas(
                            fresh_conn.id,
                            source_month=source_month,
                            run_id=run_view.id,
                            run_at=self._clock(),
                            last_error=(
                                run_view.status_message
                                if run_view.status == "failed"
                                else None
                            ),
                        )

                result.triggered_run_ids.append(run_view.id)
                if run_view.status == "readyForReview":
                    result.completed_run_ids.append(run_view.id)
                elif run_view.status == "failed":
                    result.failed_run_ids.append(run_view.id)
            finally:
                await self._coordination.release_scope_lease(
                    self._tenant_id, scope, self._owner
                )

    async def run_once(self) -> BillingSchedulerTickResult:
        """Execute one scheduler pass: resume in-flight runs and trigger due monthly drafts."""

        tick_at = self._clock()
        self._last_tick_at = tick_at
        result = BillingSchedulerTickResult(tick_at=tick_at)
        try:
            await self._poll_in_flight_runs(result)
            await self._trigger_due_scheduled_runs(result)
            self._last_succeeded_at = self._clock()
            self._last_error = None
            return result
        except Exception as exc:
            self._last_error = _safe_billing_error_message(exc)
            raise

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as error:
                logger.error(
                    "billing_pricing_scheduler_failed",
                    error_type=type(error).__name__,
                )
            await asyncio.sleep(self._interval_seconds)


__all__ = [
    "BILLING_SCHEDULER_ACTOR_ID",
    "DEFAULT_BILLING_LEASE_SECONDS",
    "NON_FAILED_RUN_STATUSES",
    "SCHEDULE_DISABLED_WARNING",
    "SCHEDULE_HOSTING_WARNING",
    "BillingPricingScheduler",
    "BillingScheduleWindow",
    "BillingSchedulerStatusView",
    "BillingSchedulerTickResult",
    "billing_run_lease_scope",
    "billing_schedule_lease_scope",
    "is_schedule_due",
    "resolve_schedule_window",
]
