"""A model pool's health: how each call ended, and how each member answered its attempts.

Built on ``test_pool_usage``'s pool, Northwind Fleet, which offers Contoso Chat from chat-east,
chat-sweden and chat-ptu. The pool's policy traces each attempt with the backend it sent the
attempt to, and the host and path the gateway called. MOSAIC places each attempt on the member it
went to, and counts the minutes in which a member's breaker would have tripped. See ADR 0024.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from loganalytics_double import GatewayCall, PoolAttempt
from mosaic_api.domain import ApimResourceId, EndpointAuthMode
from mosaic_api.errors import UpstreamError, ValidationError
from mosaic_api.integrations.loganalytics import LogQueryAccessError, QueryWindow, pool_health_query
from mosaic_api.model_pools import (
    BreakerPreset,
    ModelPool,
    ModelPoolType,
    PoolMember,
    breaker_trip_rule,
    circuit_breaker,
    member_backend_name,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.pool_health import PoolHealthService
from pydantic import TypeAdapter
from test_cost import ALICE, GATEWAY, RESOURCE_ID, TENANT, Harness, _audit, harness, settings
from test_pool_usage import (
    EAST,
    EAST_BACKEND,
    MODEL_ID,
    POOL_API,
    POOL_ID,
    RESERVED,
    SWEDEN,
    _pool,
    _pool_model,
    seed,
)

__all__ = ["harness", "settings"]

HEALTH = f"/api/v1/model-pools/{POOL_ID}/health"
POOL_BACKEND = _pool_model().backend_pool_name
SWEDEN_BACKEND = member_backend_name(POOL_API, MODEL_ID, SWEDEN, "chat-sweden")
PTU_BACKEND = member_backend_name(POOL_API, MODEL_ID, RESERVED, "chat-ptu")
EAST_HOST = "contoso-east.openai.azure.com"
SWEDEN_HOST = "contoso-sweden.openai.azure.com"
PTU_HOST = "contoso-ptu.openai.azure.com"
ELSEWHERE_HOST = "elsewhere.example.com"
CANONICAL = ApimResourceId.parse(RESOURCE_ID).canonical
PRINCIPAL = "mosaic-identity-oid"
WINDOW = QueryWindow(datetime(2026, 3, 17, 16, tzinfo=UTC), datetime(2026, 3, 18, 16, tzinfo=UTC))


def _attempt(
    host: str,
    status: int = 200,
    *,
    deployment: str | None = None,
    backend: str = POOL_BACKEND,
    model: str = MODEL_ID,
    exhausted: bool = False,
    version: str = "1",
) -> PoolAttempt:
    """An attempt the pool's policy traced. With no deployment, it called the v1 route."""

    path = (
        f"/openai/deployments/{deployment}/chat/completions"
        if deployment
        else "/openai/v1/chat/completions"
    )
    return PoolAttempt(
        model=model,
        backend=backend,
        status=status,
        host=host,
        path=path,
        exhausted=exhausted,
        version=version,
    )


def _east(status: int = 200, **extra: Any) -> PoolAttempt:
    return _attempt(EAST_HOST, status, deployment="chat-east", **extra)


def _sweden(status: int = 200, **extra: Any) -> PoolAttempt:
    return _attempt(SWEDEN_HOST, status, deployment="chat-sweden", **extra)


def _ptu(status: int = 200, **extra: Any) -> PoolAttempt:
    # The v1 route names no deployment, but chat-ptu is the only member on its host.
    return _attempt(PTU_HOST, status, **extra)


def _call(minute: int, *attempts: PoolAttempt, hour: int = 10, day: int = 18) -> GatewayCall:
    last = attempts[-1].status if attempts else 200
    return GatewayCall(
        time=datetime(2026, 3, day, hour, minute, tzinfo=UTC),
        api=POOL_API,
        response_code=last if last > 0 else 502,
        attempts=list(attempts),
    )


def _untraced(minute: int, **extra: Any) -> GatewayCall:
    """A call from a policy that wrote no attempt trace."""

    return GatewayCall(
        time=datetime(2026, 3, 18, 10, minute, tzinfo=UTC),
        api=POOL_API,
        backend_url=f"https://{EAST_HOST}/openai/deployments/chat-east/chat/completions",
        **extra,
    )


def _install(harness: Harness, *, configured: bool = True) -> None:
    harness.state.pool_health_service = PoolHealthService(
        harness.state.model_pool_service,
        gateway_repository=harness.state.gateway_repository,
        endpoint_repository=harness.state.model_endpoint_repository,
        logs=harness.logs if configured else None,
        principal_id=PRINCIPAL,
        clock=lambda: harness.now,
    )


async def _seeded(harness: Harness, *calls: GatewayCall, pool: ModelPool | None = None) -> Harness:
    await seed(harness)
    if pool is not None:
        await harness.state.gateway_repository.save_model_pool(pool, _audit("pool"))
    harness.logs.calls = list(calls)
    _install(harness)
    return harness


def _health(harness: Harness, **params: Any) -> Any:
    return harness.get(HEALTH, **params)


def _health_queries(harness: Harness) -> list[tuple[str, str]]:
    return [query for query in harness.logs.queries if query[0] == "poolHealth"]


def _model(health: Any) -> Any:
    [model] = health["models"]
    return model


def _members(model: Any) -> dict[str, Any]:
    return {member["deploymentName"]: member for member in model["members"]}


def _time(value: str) -> datetime:
    return TypeAdapter(datetime).validate_python(value)


def _counts(member: Any) -> tuple[int, ...]:
    return (
        member["attempts"],
        member["succeeded"],
        member["throttled"],
        member["failed"],
        member["clientErrors"],
        member["served"],
        member["servedOk"],
    )


async def test_health_shows_how_each_member_answered_its_attempts(harness: Harness) -> None:
    await _seeded(
        harness,
        # East was throttled, so the gateway sent the request on to Sweden, which answered it.
        _call(0, _east(429), _sweden()),
        _call(1, _ptu()),
        _call(2, _ptu()),
        # A bad request: East answered it, and wasn't at fault.
        _call(3, _east(400)),
        # Every member's breaker was open, so the backend pool answered without sending it on.
        _call(4, _east(503, exhausted=True)),
        # Sweden failed, and East answered the retry.
        _call(5, _sweden(500), _east()),
    )

    health = _health(harness)

    assert (health["status"], health["message"], health["untraced"]) == ("ok", None, 0)
    assert health["hours"] == 24
    # Whole hours, up to the end of the current one.
    assert _time(health["start"]) == datetime(2026, 3, 17, 16, tzinfo=UTC)
    assert _time(health["end"]) == datetime(2026, 3, 18, 16, tzinfo=UTC)
    model = _model(health)
    assert (model["modelId"], model["publicName"], model["displayName"]) == (
        MODEL_ID,
        "contoso-chat",
        "Contoso Chat",
    )
    assert (
        model["requests"],
        model["succeeded"],
        model["unavailable"],
        model["clientErrors"],
        model["retried"],
    ) == (6, 4, 1, 1, 2)
    assert (model["exhausted"], model["unplaced"], model["overflowed"]) == (1, 0, 0)
    members = _members(model)
    assert list(members) == ["chat-east", "chat-sweden", "chat-ptu"]
    east, sweden, ptu = members.values()
    # The exhausted attempt went to no member, so it isn't East's.
    assert _counts(east) == (3, 1, 1, 0, 1, 2, 1)
    assert _counts(sweden) == (2, 1, 0, 1, 0, 1, 1)
    assert _counts(ptu) == (2, 2, 0, 0, 0, 2, 2)
    # One 429 in a minute trips the throttling breaker. A server error doesn't.
    assert [member["trippedMinutes"] for member in members.values()] == [1, 0, 0]
    assert _time(east["lastSeen"]) == datetime(2026, 3, 18, 10, 5, tzinfo=UTC)
    assert _time(ptu["lastSeen"]) == datetime(2026, 3, 18, 10, 2, tzinfo=UTC)
    assert (
        east["modelEndpointId"],
        east["endpointName"],
        east["backendName"],
        east["region"],
        east["drained"],
        east["apiKey"],
        east["overflow"],
    ) == (EAST, "Contoso East", EAST_BACKEND, "eastus2", False, False, False)


async def test_attempts_are_placed_by_backend_then_by_host_and_deployment(
    harness: Harness,
) -> None:
    # East serves the model from a second deployment too, so its host alone names no member.
    pool = _pool()
    second = member_backend_name(POOL_API, MODEL_ID, EAST, "chat-east-2")
    pool.models[0].members.append(
        PoolMember(model_endpoint_id=EAST, deployment_name="chat-east-2", backend_name=second)
    )
    await _seeded(
        harness,
        # A member's own backend names it, whatever the path.
        _call(0, _attempt(EAST_HOST, backend=EAST_BACKEND)),
        # Through the backend pool: East's host, and the deployment in the path.
        _call(1, _attempt(EAST_HOST, deployment="chat-east-2")),
        _call(2, _attempt(EAST_HOST, deployment="CHAT-EAST-2")),
        # East's host, but the v1 route names no deployment.
        _call(3, _attempt(EAST_HOST)),
        # A host no member has.
        _call(4, _attempt(ELSEWHERE_HOST, deployment="chat-east")),
        pool=pool,
    )

    model = _model(_health(harness))

    members = _members(model)
    assert members["chat-east"]["attempts"] == 1
    assert members["chat-east-2"]["attempts"] == 2
    assert (model["requests"], model["succeeded"], model["unplaced"]) == (5, 5, 2)


@pytest.mark.parametrize(
    ("pool_type", "overflow", "overflowed", "breakers"),
    [
        # A breaker pool balances across every member.
        (ModelPoolType.BREAKER, set(), 0, True),
        # A preferential pool sends a request to pay-as-you-go only once provisioned can't take it.
        (ModelPoolType.PREFERENTIAL, {"chat-east", "chat-sweden"}, 3, True),
        # A linear pool tries its members in order, and none has a breaker.
        (ModelPoolType.LINEAR, {"chat-sweden", "chat-ptu"}, 6, False),
    ],
)
async def test_overflow_counts_what_a_fallback_member_answered(
    harness: Harness,
    pool_type: ModelPoolType,
    overflow: set[str],
    overflowed: int,
    breakers: bool,
) -> None:
    attempts = [
        _east(backend=EAST_BACKEND),
        *(_sweden(backend=SWEDEN_BACKEND) for _ in range(2)),
        *(_ptu(backend=PTU_BACKEND) for _ in range(4)),
    ]
    await _seeded(
        harness,
        *(_call(minute, attempt) for minute, attempt in enumerate(attempts)),
        pool=_pool().model_copy(update={"pool_type": pool_type}),
    )

    model = _model(_health(harness))

    members = _members(model)
    assert {name for name, member in members.items() if member["overflow"]} == overflow
    assert model["overflowed"] == overflowed
    tripped = {member["trippedMinutes"] for member in members.values()}
    assert tripped == ({0} if breakers else {None})


async def test_a_member_reached_with_a_key_overflows_and_has_no_breaker(harness: Harness) -> None:
    await seed(harness)
    endpoints = harness.state.model_endpoint_repository
    sweden = await endpoints.get_endpoint(TENANT, SWEDEN)
    await endpoints.save_endpoint(
        sweden.model_copy(update={"auth_mode": EndpointAuthMode.API_KEY}), _audit("endpoint")
    )
    # East was throttled, so after the backend pool the gateway tried Sweden with its key.
    harness.logs.calls = [_call(0, _east(429), _sweden(backend=SWEDEN_BACKEND))]
    _install(harness)

    model = _model(_health(harness))

    members = _members(model)
    assert (members["chat-sweden"]["apiKey"], members["chat-sweden"]["overflow"]) == (True, True)
    assert members["chat-sweden"]["trippedMinutes"] is None
    assert members["chat-east"]["trippedMinutes"] == 1
    assert (model["overflowed"], model["retried"]) == (1, 1)


@pytest.mark.parametrize(
    ("preset", "tripped"),
    [
        # One 429 in a minute trips it: 10:00 and 10:05.
        (BreakerPreset.THROTTLING, 2),
        # Three 429s and server errors in a minute trip it: 10:00 only.
        (BreakerPreset.THROTTLING_AND_ERRORS, 1),
    ],
)
async def test_tripped_minutes_follow_the_pools_breaker(
    harness: Harness, preset: BreakerPreset, tripped: int
) -> None:
    await _seeded(
        harness,
        _call(0, _east(429)),
        _call(0, _east(500)),
        _call(0, _east(502)),
        _call(5, _east(429)),
        _call(10, _east(503)),
        _call(10, _east(504)),
        # The backend pool answered these itself, so no member's breaker counted them.
        *(_call(15, _east(503, exhausted=True)) for _ in range(3)),
        pool=_pool().model_copy(update={"breaker_preset": preset}),
    )

    model = _model(_health(harness))

    east = _members(model)["chat-east"]
    assert east["trippedMinutes"] == tripped
    assert (east["attempts"], east["throttled"], east["failed"]) == (6, 2, 4)
    assert model["exhausted"] == 3


async def test_health_needs_gateway_telemetry(harness: Harness) -> None:
    await _seeded(harness, _call(0, _east()))
    _install(harness, configured=False)

    health = _health(harness)

    assert health["status"] == "notConfigured"
    assert health["models"] == []
    assert not _health_queries(harness)


async def test_a_pool_that_isnt_on_its_gateway_has_no_health(harness: Harness) -> None:
    await _seeded(harness, _call(0, _east()), pool=_pool(applied=False))

    health = _health(harness)

    assert (health["status"], health["models"]) == ("notPublished", [])
    assert not _health_queries(harness)


async def test_a_pool_whose_gateway_is_gone_says_so(harness: Harness) -> None:
    await _seeded(harness, _call(0, _east()))
    harness.state.gateway_repository.gateways.pop(GATEWAY)

    health = _health(harness)

    assert health["status"] == "error"
    assert health["message"] == "The pool's gateway is no longer registered with MOSAIC."


async def test_a_pool_with_no_calls_lists_its_members_with_nothing_to_show(
    harness: Harness,
) -> None:
    # Before the window.
    await _seeded(harness, _call(0, _east(), hour=9, day=18))

    health = _health(harness, hours=6)

    assert health["status"] == "noData"
    assert health["message"] == "The gateway logged no calls to this pool in the last 6 hours."
    model = _model(health)
    assert model["requests"] == 0
    assert [member["attempts"] for member in model["members"]] == [0, 0, 0]


@pytest.mark.parametrize(
    ("hours", "span"),
    [(1, "hour"), (24, "24 hours"), (25, "25 hours"), (48, "2 days"), (168, "7 days")],
)
async def test_a_pool_with_no_calls_names_its_window_in_words(
    harness: Harness, hours: int, span: str
) -> None:
    await _seeded(harness)

    health = _health(harness, hours=hours)

    assert health["message"] == f"The gateway logged no calls to this pool in the last {span}."


async def test_calls_without_traces_ask_for_the_pool_to_be_applied_again(
    harness: Harness,
) -> None:
    await _seeded(
        harness,
        _untraced(0),
        _untraced(1),
        # Refused by MOSAIC's policy, so it reached no member.
        _untraced(2, denial_reason="not-entitled"),
        # Answered by the gateway itself, with no backend.
        GatewayCall(time=datetime(2026, 3, 18, 10, 3, tzinfo=UTC), api=POOL_API),
    )

    health = _health(harness)

    assert (health["status"], health["untraced"]) == ("ok", 2)
    assert health["message"] == (
        "Calls reached the pool, but none carries an attempt trace. Apply the pool again so its "
        "policy writes them."
    )
    assert _model(health)["requests"] == 0


async def test_untraced_calls_are_counted_beside_traced_ones(harness: Harness) -> None:
    await _seeded(harness, _untraced(0), _call(1, _east()))

    health = _health(harness)

    assert (health["status"], health["message"], health["untraced"]) == ("ok", None, 1)
    assert _model(health)["requests"] == 1


async def test_traces_for_other_models_and_versions_are_left_out(harness: Harness) -> None:
    await _seeded(
        harness,
        # A model the pool no longer offers.
        _call(0, _east(model="poolModel_retired")),
        # A trace this MOSAIC doesn't read.
        _call(1, _east(version="2")),
        _call(2, _east()),
    )

    health = _health(harness)

    assert health["status"] == "ok"
    model = _model(health)
    assert (model["requests"], _members(model)["chat-east"]["attempts"]) == (1, 1)


async def test_a_drained_member_is_still_listed(harness: Harness) -> None:
    pool = _pool()
    pool.models[0].members[1].drained = True
    await _seeded(harness, _call(0, _sweden()), pool=pool)

    sweden = _members(_model(_health(harness)))["chat-sweden"]

    assert (sweden["drained"], sweden["overflow"], sweden["attempts"]) == (True, False, 1)


async def test_health_explains_how_to_let_mosaic_read_the_logs(harness: Harness) -> None:
    await _seeded(harness, _call(0, _east()))
    harness.logs.failures[CANONICAL] = LogQueryAccessError("MOSAIC may not read these logs")

    health = _health(harness)

    assert health["status"] == "accessDenied"
    assert "Monitoring Reader" in health["message"]
    assert PRINCIPAL in health["command"]
    assert CANONICAL in health["command"]


async def test_a_failed_query_is_reported(harness: Harness) -> None:
    await _seeded(harness, _call(0, _east()))
    harness.logs.failures[CANONICAL] = UpstreamError(
        "Log Analytics didn't answer", details={"reason": "it timed out"}
    )

    health = _health(harness)

    assert (health["status"], health["message"]) == (
        "error",
        "Log Analytics didn't answer: it timed out",
    )


async def test_a_window_too_large_for_one_query_is_read_in_parts(harness: Harness) -> None:
    await _seeded(
        harness,
        _call(0, _east(), hour=18, day=17),
        _call(0, _east(429), hour=10),
        _call(0, _east(), hour=15),
    )
    harness.logs.max_hours = 6

    model = _model(_health(harness))

    assert harness.logs.answered == [
        (
            datetime(2026, 3, 17, 16, tzinfo=UTC) + timedelta(hours=6 * part),
            datetime(2026, 3, 17, 16, tzinfo=UTC) + timedelta(hours=6 * (part + 1)),
        )
        for part in range(4)
    ]
    east = _members(model)["chat-east"]
    assert (model["requests"], east["attempts"], east["trippedMinutes"]) == (3, 3, 1)
    assert _time(east["lastSeen"]) == datetime(2026, 3, 18, 15, tzinfo=UTC)


async def test_health_covers_an_hour_to_a_week(harness: Harness) -> None:
    await _seeded(harness)

    for hours in (0, 169):
        response = harness.client.get(HEALTH, params={"hours": hours})
        assert response.status_code == 422, response.text
    health = _health(harness, hours=168)
    assert health["hours"] == 168
    assert _time(health["start"]) == datetime(2026, 3, 11, 16, tzinfo=UTC)


@pytest.mark.parametrize("hours", [True, 0, 169, 1.5])
async def test_the_service_refuses_hours_outside_the_range(harness: Harness, hours: Any) -> None:
    await _seeded(harness)

    with pytest.raises(ValidationError):
        await harness.state.pool_health_service.health(
            Actor(object_id=ALICE, tenant_id=TENANT), POOL_ID, hours
        )


async def test_only_an_administrator_reads_a_pools_health(harness: Harness) -> None:
    await _seeded(harness)
    missing = harness.client.get("/api/v1/model-pools/modelPool_missing/health")
    assert missing.status_code == 404, missing.text

    harness.sign_in("someone-oid", ["User"], frozenset())

    assert harness.client.get(HEALTH).status_code == 403


@pytest.mark.parametrize("preset", list(BreakerPreset))
def test_the_health_query_trips_a_breaker_as_the_gateway_does(preset: BreakerPreset) -> None:
    count, errors = breaker_trip_rule(preset)
    [rule] = circuit_breaker(preset)["rules"]  # type: ignore[misc]
    condition = rule["failureCondition"]
    assert count == condition["count"]
    assert errors == ({"min": 500, "max": 599} in condition["statusCodeRanges"])

    query = pool_health_query(WINDOW, POOL_API, trip_count=count, trip_errors=errors)

    tripping = "throttled + failed" if errors else "throttled"
    assert f"countif({tripping} >= {count})" in query
    assert "mosaic-attempt" in query


@pytest.mark.parametrize("trip_count", [0, -1, True, 1.5, "3"])
def test_the_health_query_needs_a_positive_trip_count(trip_count: Any) -> None:
    with pytest.raises(ValidationError):
        pool_health_query(WINDOW, POOL_API, trip_count=trip_count, trip_errors=False)
