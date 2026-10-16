"""Leased, bounded scheduling for notification source reconciliation."""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime
from uuid import uuid4

import structlog

from mosaic_api.domain import utc_now
from mosaic_api.errors import ConflictError
from mosaic_api.notifications import NotificationWorkerState
from mosaic_api.repositories.base import GatewayRepository
from mosaic_api.repositories.notifications import NotificationDeliveryRepository

logger = structlog.get_logger()


class ScheduledFeatureWorker:
    def __init__(
        self,
        repository: NotificationDeliveryRepository,
        coordination: GatewayRepository,
        action: Callable[[], Awaitable[None]],
        *,
        tenant_id: str,
        name: str,
        interval_seconds: float,
        timeout_seconds: float = 45,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._coordination = coordination
        self._action = action
        self._tenant = tenant_id
        self._name = name
        self._interval = interval_seconds
        self._timeout = timeout_seconds
        self._clock = clock
        self._owner = f"{name}:{uuid4().hex}"
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=self._name)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> None:
        state = await self._repository.get_worker_state(self._tenant, self._name)
        if state and state.paused:
            return
        scope = f"scheduled:{self._name}"
        try:
            await self._coordination.acquire_scope_lease(
                self._tenant, scope, self._owner, lease_seconds=self._timeout + 30
            )
        except ConflictError:
            return
        try:
            async with asyncio.timeout(self._timeout):
                await self._action()

            def succeeded(current: NotificationWorkerState) -> NotificationWorkerState:
                now = self._clock()
                return current.model_copy(
                    update={
                        "heartbeat_at": now,
                        "last_succeeded_at": now,
                        "last_error": None,
                    }
                )

            await self._repository.update_worker_state(self._tenant, self._name, succeeded)
        except Exception as error:
            reason = (
                "Processing timed out. Check source connectivity and backlog."
                if isinstance(error, TimeoutError)
                else "Processing failed. Check storage, source readiness and API logs."
            )

            def failed(current: NotificationWorkerState) -> NotificationWorkerState:
                return current.model_copy(
                    update={
                        "heartbeat_at": self._clock(),
                        "last_error": reason,
                    }
                )

            await self._repository.update_worker_state(self._tenant, self._name, failed)
            raise
        finally:
            await self._coordination.release_scope_lease(self._tenant, scope, self._owner)

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception as error:
                logger.error(
                    "scheduled_feature_worker_failed",
                    worker=self._name,
                    error_type=type(error).__name__,
                )
            await asyncio.sleep(self._interval)
