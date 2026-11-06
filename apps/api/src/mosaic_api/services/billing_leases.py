"""Renew billing leases throughout provider waits and cancel work if ownership is lost."""

import asyncio
from collections.abc import Awaitable, Callable

from mosaic_api.errors import ConflictError

DEFAULT_BILLING_LEASE_SECONDS = 180.0
LeaseRenewer = Callable[[], Awaitable[None]]


def billing_run_lease_scope(run_id: str) -> str:
    return f"billing-run:{run_id}"


async def with_billing_lease_heartbeat[T](
    operation: Callable[[LeaseRenewer], Awaitable[T]],
    renew: LeaseRenewer,
    *,
    lease_seconds: float,
) -> T:
    """Keep an already-acquired lease alive until the operation and its writes finish."""

    renewal_lock = asyncio.Lock()
    lost: Exception | None = None
    active = True

    async def checked_renew() -> None:
        nonlocal lost
        async with renewal_lock:
            if not active:
                raise asyncio.CancelledError
            if lost is not None:
                raise ConflictError("The billing lease is no longer owned.") from lost
            try:
                await renew()
            except Exception as exc:
                lost = exc
                raise

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(lease_seconds / 3)
            await checked_renew()

    async def execute() -> T:
        return await operation(checked_renew)

    await checked_renew()
    worker = asyncio.create_task(execute())
    pulse = asyncio.create_task(heartbeat())
    try:
        await asyncio.wait((worker, pulse), return_when=asyncio.FIRST_COMPLETED)
        if pulse.done():
            try:
                await pulse
            except Exception as exc:
                raise ConflictError("The billing lease was lost while retrieving data.") from exc
        return await worker
    finally:
        active = False
        pulse.cancel()
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, pulse, return_exceptions=True)
