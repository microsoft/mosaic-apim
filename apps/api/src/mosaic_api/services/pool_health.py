"""How a model pool's members answered the calls the gateway sent them. See ADR 0024.

A pool's policy writes a trace after each attempt it makes. This reads the traces back from the
gateway's logs over a window of whole hours: how each pool model's calls ended, and how each member
answered the attempts sent to it, with the minutes in which its breaker would have tripped.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from mosaic_api.domain import ApimResourceId, ApiShape, utc_now
from mosaic_api.errors import DomainError, ValidationError
from mosaic_api.integrations.apim.diagnostics import monitoring_reader_command
from mosaic_api.integrations.loganalytics import (
    LogQueryAccessError,
    LogQueryTooLargeError,
    LogsQuery,
    QueryWindow,
    Row,
    pool_health_query,
)
from mosaic_api.model_pools import (
    ModelPoolDetail,
    ModelPoolType,
    PoolHealth,
    PoolMemberHealth,
    PoolMemberView,
    PoolModelHealth,
    breaker_trip_rule,
    uses_backend_pools,
)
from mosaic_api.repositories import GatewayRepository, ModelEndpointRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_pools import ModelPoolService
from mosaic_api.services.telemetry import member_host

logger = structlog.get_logger()

IdentityResolver = Callable[[], Awaitable[str | None]]

HEALTH_HOURS = 24
MAX_HEALTH_HOURS = 168


def _count(row: Row, name: str) -> int:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return int(value)


def _text(row: Row, name: str) -> str:
    value = row.get(name)
    return value.casefold() if isinstance(value, str) else ""


def _latest(current: datetime | None, value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return current
    return value if current is None or value > current else current


def _error_reason(error: DomainError) -> str:
    reason = error.details.get("reason") if error.details else None
    return f"{error.message}: {reason}" if isinstance(reason, str) and reason else error.message


def _overflow(pool_type: ModelPoolType, view: PoolMemberView) -> bool:
    """Whether a member takes a model's requests only once the members before it can't."""

    if view.drained:
        return False
    if pool_type == ModelPoolType.LINEAR:
        return (view.order or 0) > 1
    if pool_type == ModelPoolType.PREFERENTIAL:
        return view.api_key or view.priority == 2
    # A breaker pool balances across its backend pool, and tries a keyed member only after it.
    return view.api_key


@dataclass
class _Member:
    view: PoolMemberView
    host: str
    overflow: bool
    health: PoolMemberHealth


class _ModelRows:
    """Adds one pool model's rows up, placing each attempt on the member it was sent to."""

    def __init__(self, health: PoolModelHealth, members: list[_Member], routed: bool) -> None:
        self.health = health
        self.members = members
        self._by_backend = {member.view.backend_name.casefold(): member for member in members}
        self._by_host: dict[str, list[_Member]] = {}
        for member in members:
            if member.host:
                self._by_host.setdefault(member.host, []).append(member)
        # Whether the path an attempt called names its deployment, as an Azure OpenAI path does.
        self._routed = routed

    def place(self, row: Row) -> _Member | None:
        """The member an attempt went to: by its backend, else by its host and deployment.

        A linear member, and a member reached with an API key, has a backend of its own. An attempt
        through a backend pool names the pool, so it's placed by the host it called, and when one
        host serves several of the model's deployments, by the deployment in its path. It stays
        unplaced when that doesn't name exactly one member.
        """

        member = self._by_backend.get(_text(row, "b"))
        if member is not None:
            return member
        on_host = self._by_host.get(_text(row, "h"), [])
        deployment = _text(row, "d") if self._routed else ""
        if deployment:
            on_host = [m for m in on_host if m.view.deployment_name.casefold() == deployment]
        return on_host[0] if len(on_host) == 1 else None

    def add_attempts(self, row: Row) -> None:
        health = self.health
        health.requests += _count(row, "served")
        health.succeeded += _count(row, "servedOk")
        health.unavailable += _count(row, "servedUnavailable")
        health.retried += _count(row, "retried")
        attempts = _count(row, "attempts")
        if row.get("exhausted") is True:
            # The backend pool answered itself, so no member saw the attempt.
            health.exhausted += attempts
            return
        member = self.place(row)
        if member is None:
            health.unplaced += attempts
            return
        counted = member.health
        counted.attempts += attempts
        counted.succeeded += _count(row, "succeeded")
        counted.throttled += _count(row, "throttled")
        counted.failed += _count(row, "failed")
        counted.client_errors += _count(row, "clientErrors")
        counted.served += _count(row, "served")
        counted.served_ok += _count(row, "servedOk")
        counted.last_seen = _latest(counted.last_seen, row.get("lastSeen"))
        if member.overflow:
            health.overflowed += _count(row, "servedOk")

    def add_trips(self, row: Row) -> None:
        member = self.place(row)
        if member is not None and member.health.tripped_minutes is not None:
            member.health.tripped_minutes += _count(row, "trippedMinutes")

    def finish(self) -> PoolModelHealth:
        health = self.health
        health.client_errors = max(0, health.requests - health.succeeded - health.unavailable)
        health.members = [member.health for member in self.members]
        return health


class PoolHealthService:
    def __init__(
        self,
        pools: ModelPoolService,
        *,
        gateway_repository: GatewayRepository,
        endpoint_repository: ModelEndpointRepository,
        logs: LogsQuery | None,
        principal_id: str | None = None,
        identity_resolver: IdentityResolver | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._pools = pools
        self._gateways = gateway_repository
        self._endpoints = endpoint_repository
        self._logs = logs
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
            logger.warning("pool_health_identity_lookup_failed")
        return self._principal_id

    async def _query(
        self, resource_id: str, build: Callable[[QueryWindow], str], window: QueryWindow
    ) -> list[Row]:
        """Run a query, halving its window until each part fits Log Analytics' limits."""

        assert self._logs is not None
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

    async def health(self, actor: Actor, pool_id: str, hours: int = HEALTH_HOURS) -> PoolHealth:
        """How the pool's calls ended over the last ``hours`` whole hours, up to the next hour."""

        if isinstance(hours, bool) or not isinstance(hours, int):
            raise ValidationError("Health covers a whole number of hours")
        if not 1 <= hours <= MAX_HEALTH_HOURS:
            raise ValidationError(
                f"Health covers 1 to {MAX_HEALTH_HOURS} hours", details={"hours": hours}
            )
        detail = await self._pools.detail(actor, pool_id)
        pool = detail.pool
        now = self._clock().astimezone(UTC)
        end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        window = QueryWindow(end - timedelta(hours=hours), end)
        result = PoolHealth(status="noData", hours=hours, start=window.start, end=window.end)
        if self._logs is None:
            return result.model_copy(
                update={
                    "status": "notConfigured",
                    "message": "This deployment doesn't read gateway telemetry.",
                }
            )
        if not pool.has_applied_api():
            return result.model_copy(
                update={
                    "status": "notPublished",
                    "message": "The pool isn't on its gateway, so no calls reach it.",
                }
            )
        gateway = await self._gateways.get_gateway(actor.tenant_id, pool.gateway_id)
        if gateway is None:
            return result.model_copy(
                update={
                    "status": "error",
                    "message": "The pool's gateway is no longer registered with MOSAIC.",
                }
            )
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        trip_count, trip_errors = breaker_trip_rule(pool.breaker_preset)
        try:
            rows = await self._query(
                resource.canonical,
                lambda part: pool_health_query(
                    part, pool.api_name, trip_count=trip_count, trip_errors=trip_errors
                ),
                window,
            )
        except LogQueryAccessError:
            return result.model_copy(
                update={
                    "status": "accessDenied",
                    "message": (
                        "MOSAIC's identity may not read this gateway's logs. Monitoring Reader on "
                        "the gateway lets it, if the workspace allows resource permissions."
                    ),
                    "command": monitoring_reader_command(
                        resource.canonical, await self._principal()
                    ),
                }
            )
        except DomainError as error:
            return result.model_copy(update={"status": "error", "message": _error_reason(error)})
        models = await self._models(actor, detail)
        traced = False
        untraced = 0
        for row in rows:
            kind = row.get("rowKind")
            if kind == "untraced":
                untraced += _count(row, "requests")
                continue
            if kind == "attempts":
                traced = True
            model = models.get(_text(row, "m"))
            if model is None:
                # A model the pool no longer offers.
                continue
            if kind == "attempts":
                model.add_attempts(row)
            elif kind == "trips":
                model.add_trips(row)
        status = "ok" if traced or untraced else "noData"
        message = None
        if not traced and untraced:
            message = (
                "Calls reached the pool, but none carries an attempt trace. Apply the pool again "
                "so its policy writes them."
            )
        elif not traced:
            message = f"The gateway logged no calls to this pool in the last {hours} hours."
        return result.model_copy(
            update={
                "status": status,
                "message": message,
                "untraced": untraced,
                "models": [model.finish() for model in models.values()],
            }
        )

    async def _models(self, actor: Actor, detail: ModelPoolDetail) -> dict[str, _ModelRows]:
        """Each pool model, keyed by the ID its trace carries, with its members to place on."""

        pool = detail.pool
        endpoints = (
            {
                endpoint.id: endpoint
                for endpoint in await self._endpoints.list_endpoints(actor.tenant_id)
            }
            if any(model.members for model in detail.models)
            else {}
        )
        breakers = uses_backend_pools(pool.pool_type)
        models: dict[str, _ModelRows] = {}
        for model in detail.models:
            members: list[_Member] = []
            for view in model.members:
                overflow = _overflow(pool.pool_type, view)
                members.append(
                    _Member(
                        view=view,
                        host=member_host(pool.api_shape, endpoints.get(view.model_endpoint_id)),
                        overflow=overflow,
                        health=PoolMemberHealth(
                            model_endpoint_id=view.model_endpoint_id,
                            endpoint_name=view.endpoint_name,
                            deployment_name=view.deployment_name,
                            backend_name=view.backend_name,
                            region=view.region,
                            drained=view.drained,
                            api_key=view.api_key,
                            overflow=overflow,
                            # A member reached with an API key isn't in the backend pool, so it
                            # has no breaker.
                            tripped_minutes=0 if breakers and not view.api_key else None,
                        ),
                    )
                )
            models[model.id.casefold()] = _ModelRows(
                PoolModelHealth(
                    model_id=model.id,
                    public_name=model.public_name,
                    display_name=model.display_name,
                ),
                members,
                routed=pool.api_shape == ApiShape.AZURE_OPENAI,
            )
        return models
