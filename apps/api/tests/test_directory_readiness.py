import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from conftest import install_directory_lookup
from fastapi import Request
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext
from mosaic_api.domain import (
    DirectoryObject,
    DirectoryVerificationState,
    DirectoryVerificationStatus,
    PrincipalKind,
    utc_now,
)
from mosaic_api.errors import (
    ConflictError,
    DirectoryError,
    DirectoryForbiddenError,
    DirectoryThrottledError,
)
from mosaic_api.integrations.graph import FakeDirectoryLookup, GroupMembershipEvidence
from mosaic_api.integrations.graph import fake as graph_fake
from mosaic_api.repositories import InMemoryDirectoryRepository
from mosaic_api.services.directory import Actor, DirectoryService

GROUP_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_GROUP_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ACTOR = Actor(object_id="admin-test", tenant_id="tenant-test")


class Clock:
    def __init__(self) -> None:
        self.now = utc_now()

    def __call__(self) -> datetime:
        return self.now


class TrackedLookup(FakeDirectoryLookup):
    def __init__(self) -> None:
        super().__init__(
            [
                DirectoryObject(object_id=GROUP_ID, kind=PrincipalKind.SECURITY_GROUP),
                DirectoryObject(object_id=OTHER_GROUP_ID, kind=PrincipalKind.SECURITY_GROUP),
            ]
        )
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.error: DirectoryError | DirectoryForbiddenError | None = None
        self.entered = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def verify_group_members(
        self,
        group_object_id: str,
        candidate_object_ids: Sequence[str],
        *,
        timeout_seconds: float = 60,
    ) -> GroupMembershipEvidence:
        self.calls.append((group_object_id, tuple(candidate_object_ids)))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return await super().verify_group_members(
            group_object_id, candidate_object_ids, timeout_seconds=timeout_seconds
        )


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock()
    monkeypatch.setattr(graph_fake, "utc_now", clock)
    return clock


def _service(lookup: TrackedLookup, clock: Clock) -> DirectoryService:
    return DirectoryService(InMemoryDirectoryRepository(), directory_lookup=lookup, clock=clock)


@pytest.mark.asyncio
async def test_status_and_consent_never_start_a_functional_check(clock: Clock) -> None:
    lookup = TrackedLookup()
    service = _service(lookup, clock)

    for include_permissions in [False, True]:
        result = await service.directory_status(ACTOR, include_permissions=include_permissions)
        assert result.lookup_enabled
        assert result.membership_check.state == DirectoryVerificationState.NOT_CHECKED
        assert result.membership_check.checked_at is None
    assert lookup.calls == []


@pytest.mark.asyncio
async def test_shared_status_expires_without_renewing_a_cached_timestamp(clock: Clock) -> None:
    lookup = TrackedLookup()
    service = _service(lookup, clock)
    started = clock.now

    verified = await service.check_directory_group(ACTOR, GROUP_ID.upper())

    assert verified.state == DirectoryVerificationState.READY
    assert verified.checked_at == verified.last_successful_at == started
    assert verified.group_object_id == verified.last_successful_group_object_id == GROUP_ID
    assert verified.fresh_until == started + timedelta(minutes=5)
    assert lookup.calls == [(GROUP_ID, (GROUP_ID,))]

    clock.now += timedelta(minutes=4)
    current = (await service.directory_status(ACTOR)).membership_check
    assert current == verified
    clock.now += timedelta(minutes=1)
    stale = (await service.directory_status(ACTOR)).membership_check
    assert stale.state == DirectoryVerificationState.STALE
    assert stale.checked_at == verified.checked_at
    assert stale.last_successful_at == verified.last_successful_at
    assert stale.fresh_until == verified.fresh_until
    assert len(lookup.calls) == 1


@pytest.mark.asyncio
async def test_a_clock_rollback_cannot_report_fresh_readiness(clock: Clock) -> None:
    service = _service(TrackedLookup(), clock)
    verified = await service.check_directory_group(ACTOR, GROUP_ID)
    clock.now -= timedelta(seconds=1)

    status = (await service.directory_status(ACTOR)).membership_check

    assert status.state == DirectoryVerificationState.STALE
    assert status.checked_at == verified.checked_at


@pytest.mark.asyncio
async def test_slow_checks_keep_the_time_before_the_first_read(clock: Clock) -> None:
    lookup = TrackedLookup()
    lookup.release = asyncio.Event()
    service = _service(lookup, clock)
    started = clock.now
    task = asyncio.create_task(service.check_directory_group(ACTOR, GROUP_ID))
    await asyncio.wait_for(lookup.entered.wait(), 1)
    clock.now += timedelta(seconds=20)
    lookup.release.set()

    status = await task

    assert status.checked_at == started
    assert status.completed_at == started + timedelta(seconds=20)
    assert status.fresh_until == started + timedelta(minutes=5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,state",
    [
        (DirectoryForbiddenError("Grant GroupMember.Read.All"), "forbidden"),
        (DirectoryThrottledError("Graph asked us to wait", retry_after=17), "throttled"),
        (DirectoryError("The last page failed"), "unavailable"),
    ],
)
async def test_failed_checks_raise_and_keep_success_in_its_original_group_scope(
    clock: Clock, error: DirectoryError | DirectoryForbiddenError, state: str
) -> None:
    lookup = TrackedLookup()
    service = _service(lookup, clock)
    success = await service.check_directory_group(ACTOR, GROUP_ID)
    clock.now += timedelta(seconds=5)
    lookup.error = error

    with pytest.raises(type(error), match=str(error)):
        await service.check_directory_group(ACTOR, OTHER_GROUP_ID)

    status = (await service.directory_status(ACTOR)).membership_check
    assert status.state == state
    assert status.group_object_id == OTHER_GROUP_ID
    assert status.checked_at == clock.now
    assert status.last_successful_at == success.last_successful_at
    assert status.last_successful_group_object_id == GROUP_ID
    assert status.fresh_until is None
    assert status.message == str(error)


@pytest.mark.asyncio
async def test_retry_after_blocks_early_checks_without_freshening_status(clock: Clock) -> None:
    lookup = TrackedLookup()
    service = _service(lookup, clock)
    lookup.error = DirectoryThrottledError("Wait before retrying", retry_after=17)
    with pytest.raises(DirectoryThrottledError):
        await service.check_directory_group(ACTOR, GROUP_ID)
    failed = (await service.directory_status(ACTOR)).membership_check
    assert failed.retry_at == clock.now + timedelta(seconds=17)
    lookup.error = None
    clock.now += timedelta(seconds=16)

    with pytest.raises(DirectoryThrottledError) as early:
        await service.check_directory_group(ACTOR, OTHER_GROUP_ID)
    assert early.value.retry_after == 1
    assert len(lookup.calls) == 1
    assert (await service.directory_status(ACTOR)).membership_check == failed

    clock.now += timedelta(seconds=1)
    recovered = await service.check_directory_group(ACTOR, GROUP_ID)
    assert recovered.state == DirectoryVerificationState.READY
    assert recovered.checked_at == clock.now
    assert recovered.retry_at is None
    assert recovered.message is None
    assert len(lookup.calls) == 2


@pytest.mark.asyncio
async def test_same_group_checks_share_progress_and_other_groups_do_not_queue(clock: Clock) -> None:
    lookup = TrackedLookup()
    lookup.release = asyncio.Event()
    service = _service(lookup, clock)
    task = asyncio.create_task(service.check_directory_group(ACTOR, GROUP_ID))
    await asyncio.wait_for(lookup.entered.wait(), 1)

    repeated = await service.check_directory_group(ACTOR, GROUP_ID)
    assert repeated.state == DirectoryVerificationState.CHECKING
    assert repeated.last_successful_at is None
    assert repeated.fresh_until is None
    with pytest.raises(ConflictError, match="already in progress"):
        await service.check_directory_group(ACTOR, OTHER_GROUP_ID)
    assert len(lookup.calls) == 1
    lookup.release.set()
    assert (await task).state == DirectoryVerificationState.READY


@pytest.mark.asyncio
async def test_cancellation_is_visible_and_releases_the_check_lock(clock: Clock) -> None:
    lookup = TrackedLookup()
    lookup.release = asyncio.Event()
    service = _service(lookup, clock)
    task = asyncio.create_task(service.check_directory_group(ACTOR, GROUP_ID))
    await asyncio.wait_for(lookup.entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    status = (await service.directory_status(ACTOR)).membership_check
    assert status.state == DirectoryVerificationState.CANCELLED
    assert status.last_successful_at is None
    assert status.fresh_until is None
    lookup.release.set()
    assert (await service.check_directory_group(ACTOR, GROUP_ID)).state == "ready"


@pytest.mark.asyncio
async def test_unexpected_errors_propagate_without_leaving_false_progress(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = TrackedLookup()
    monkeypatch.setattr(
        lookup, "verify_group_members", AsyncMock(side_effect=ValueError("unexpected failure"))
    )
    service = _service(lookup, clock)

    with pytest.raises(ValueError, match="unexpected failure"):
        await service.check_directory_group(ACTOR, GROUP_ID)
    status = (await service.directory_status(ACTOR)).membership_check
    assert status.state == DirectoryVerificationState.UNAVAILABLE
    assert status.last_successful_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_scope", ["tenant", "group", "candidates"])
async def test_a_foreign_or_unexpected_scope_cannot_become_readiness(
    clock: Clock, wrong_scope: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookup = TrackedLookup()
    evidence = await lookup.verify_group_members(GROUP_ID, [GROUP_ID])
    if wrong_scope == "tenant":
        evidence = replace(evidence, tenant_id="other-tenant")
    elif wrong_scope == "group":
        evidence = replace(evidence, group_object_id=OTHER_GROUP_ID)
    else:
        evidence = replace(evidence, candidate_object_ids=frozenset({OTHER_GROUP_ID}))
    monkeypatch.setattr(lookup, "verify_group_members", AsyncMock(return_value=evidence))
    service = _service(lookup, clock)

    with pytest.raises(DirectoryForbiddenError, match="unexpected scope"):
        await service.check_directory_group(ACTOR, GROUP_ID)
    status = (await service.directory_status(ACTOR)).membership_check
    assert status.state == DirectoryVerificationState.FORBIDDEN
    assert status.last_successful_at is None


@pytest.mark.asyncio
async def test_shared_status_never_crosses_actor_tenants(clock: Clock) -> None:
    service = _service(TrackedLookup(), clock)
    await service.check_directory_group(ACTOR, GROUP_ID)

    foreign = await service.directory_status(Actor(object_id="other", tenant_id="other-tenant"))

    assert foreign.membership_check.state == DirectoryVerificationState.NOT_CHECKED
    assert foreign.membership_check.last_successful_at is None


def test_the_group_check_is_explicit_read_only_and_shared_by_status(
    directory_client: TestClient,
) -> None:
    lookup = TrackedLookup()
    install_directory_lookup(directory_client.app, lookup)
    before = directory_client.get("/api/v1/directory/status")
    assert before.json()["membershipCheck"]["state"] == "notChecked"
    assert {part.strip() for part in before.headers["Cache-Control"].split(",")} >= {
        "no-store",
        "private",
    }
    assert lookup.calls == []

    result = directory_client.post(
        "/api/v1/directory/group-check", json={"groupObjectId": GROUP_ID.upper()}
    )

    assert result.status_code == 200
    assert {part.strip() for part in result.headers["Cache-Control"].split(",")} >= {
        "no-store",
        "private",
    }
    assert result.json()["state"] == "ready"
    assert result.json()["groupObjectId"] == GROUP_ID
    assert "memberObjectIds" not in result.json()
    assert lookup.calls == [(GROUP_ID, (GROUP_ID,))]
    after = directory_client.get("/api/v1/directory/status")
    assert after.json()["membershipCheck"] == result.json()
    assert directory_client.get("/api/v1/groups").json() == []
    assert directory_client.get("/api/v1/principals").json() == []
    assert not directory_client.app.state.repository.audit_events


@pytest.mark.parametrize(
    "forbidden,status,state",
    [(True, 403, "forbidden"), (False, 502, "unavailable")],
)
def test_failed_check_http_status_is_not_success_shaped(
    directory_client: TestClient, forbidden: bool, status: int, state: str
) -> None:
    install_directory_lookup(
        directory_client.app,
        FakeDirectoryLookup([], forbidden=forbidden, unavailable=not forbidden),
    )

    result = directory_client.post(
        "/api/v1/directory/group-check", json={"groupObjectId": GROUP_ID}
    )

    assert result.status_code == status
    current = directory_client.get("/api/v1/directory/status").json()["membershipCheck"]
    assert current["state"] == state
    assert current["message"] == result.json()["message"]
    assert current["lastSuccessfulAt"] is None


def test_throttled_http_checks_report_retry_after_and_preserve_the_failed_attempt(
    directory_client: TestClient,
) -> None:
    lookup = TrackedLookup()
    lookup.error = DirectoryThrottledError("Retry later", retry_after=30)
    install_directory_lookup(directory_client.app, lookup)
    path = "/api/v1/directory/group-check"

    result = directory_client.post(path, json={"groupObjectId": GROUP_ID})
    before = directory_client.get("/api/v1/directory/status").json()["membershipCheck"]
    repeated = directory_client.post(path, json={"groupObjectId": OTHER_GROUP_ID})

    assert result.status_code == repeated.status_code == 429
    assert result.headers["Retry-After"] == "30"
    assert 1 <= int(repeated.headers["Retry-After"]) <= 30
    assert len(lookup.calls) == 1
    assert directory_client.get("/api/v1/directory/status").json()["membershipCheck"] == before


def test_in_progress_http_checks_are_accepted_not_reported_as_verified(
    directory_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_directory_lookup(directory_client.app, TrackedLookup())
    monkeypatch.setattr(
        directory_client.app.state.directory_service,
        "check_directory_group",
        AsyncMock(
            return_value=DirectoryVerificationStatus(
                state=DirectoryVerificationState.CHECKING, group_object_id=GROUP_ID
            )
        ),
    )

    response = directory_client.post(
        "/api/v1/directory/group-check", json={"groupObjectId": GROUP_ID}
    )

    assert response.status_code == 202
    assert response.json()["state"] == "checking"
    assert response.json()["lastSuccessfulAt"] is None


@pytest.mark.parametrize("object_id", ["invalid", "00000000-0000-0000-0000-000000000000", ""])
def test_invalid_group_checks_do_not_contact_graph_or_change_readiness(
    directory_client: TestClient, object_id: str
) -> None:
    lookup = TrackedLookup()
    install_directory_lookup(directory_client.app, lookup)

    result = directory_client.post(
        "/api/v1/directory/group-check", json={"groupObjectId": object_id}
    )

    assert result.status_code == 422
    assert lookup.calls == []
    assert directory_client.get("/api/v1/directory/status").json()["membershipCheck"]["state"] == (
        "notChecked"
    )


def test_group_checks_are_disabled_without_an_integration(client: TestClient) -> None:
    assert client.get("/api/v1/directory/status").json()["membershipCheck"]["state"] == "disabled"
    result = client.post("/api/v1/directory/group-check", json={"groupObjectId": GROUP_ID})
    assert result.status_code == 409
    assert result.json()["code"] == "directory_disabled"


@pytest.mark.parametrize("tenant,roles", [("tenant-test", ["User"]), ("other-tenant", ["Admin"])])
def test_only_administrators_in_this_tenant_can_start_a_check(
    directory_client: TestClient, tenant: str, roles: list[str]
) -> None:
    lookup = TrackedLookup()
    install_directory_lookup(directory_client.app, lookup)

    class Authenticator:
        async def authenticate(self, _request: Request) -> AuthContext:
            return AuthContext(object_id="actor", tenant_id=tenant, roles=frozenset(roles))

    directory_client.app.state.authenticator = Authenticator()

    result = directory_client.post(
        "/api/v1/directory/group-check", json={"groupObjectId": GROUP_ID}
    )

    assert result.status_code == 403
    assert lookup.calls == []
