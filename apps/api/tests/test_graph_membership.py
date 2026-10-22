import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from mosaic_api.domain import DirectoryObject, PrincipalKind
from mosaic_api.errors import (
    DirectoryError,
    DirectoryForbiddenError,
    DirectoryThrottledError,
    ValidationError,
)
from mosaic_api.integrations.graph import FakeDirectoryLookup, GraphDirectoryLookup
from mosaic_api.integrations.graph import client as graph_client
from test_graph_client import GROUP_ID, USER_ID, FakeCredential, _client

COLLECTION = f"https://graph.microsoft.com/v1.0/groups/{GROUP_ID}/transitiveMembers"
NEXT = f"{COLLECTION}?$skiptoken=next"
Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


def _id(number: int) -> str:
    return str(UUID(int=number))


def _handler(page: Handler) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response | Awaitable[httpx.Response]:
        if request.url.path == f"/v1.0/groups/{GROUP_ID}":
            assert request.url.params["$select"] == "id,securityEnabled"
            return httpx.Response(200, json={"id": GROUP_ID, "securityEnabled": True})
        assert request.url.path == f"/v1.0/groups/{GROUP_ID}/transitiveMembers"
        return page(request)

    return handle


@pytest.mark.asyncio
async def test_complete_verification_covers_5000_candidates_beyond_5000_directory_members() -> None:
    all_members = [_id(number) for number in range(1, 6002)]
    candidates = [*all_members[1002:], _id(9001)]
    assert len(candidates) == 5000
    requests: list[httpx.Request] = []
    credential = FakeCredential()

    def page(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            assert request.url.params["$select"] == "id"
            assert request.url.params["$top"] == "999"
        else:
            assert "$top" not in request.url.params
        offset = int(request.url.params.get("$skiptoken", "0"))
        values = all_members[offset : offset + 999]
        payload: dict[str, Any] = {"value": [{"id": member_id} for member_id in values]}
        if offset + 999 < len(all_members):
            payload["@odata.nextLink"] = f"{COLLECTION}?$skiptoken={offset + 999}"
        return httpx.Response(200, json=payload)

    result = await _client(_handler(page), credential=credential).verify_group_members(
        GROUP_ID, candidates
    )

    assert result.tenant_id == "tenant-test"
    assert result.group_object_id == GROUP_ID
    assert result.candidate_object_ids == frozenset(candidates)
    assert result.member_object_ids == frozenset(all_members[1002:])
    assert all_members[-1] in result.member_object_ids
    assert _id(1) not in result.member_object_ids
    assert len(requests) == 7
    assert credential.tenants == ["tenant-test"] * 8


@pytest.mark.asyncio
async def test_complete_evidence_is_immutable_and_uses_the_oldest_observation_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    moment = [started]
    monkeypatch.setattr(graph_client, "utc_now", lambda: moment[0])
    calls = 0

    def page(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        moment[0] = started + timedelta(minutes=calls)
        if calls == 1:
            return httpx.Response(200, json={"value": [{"id": USER_ID}], "@odata.nextLink": NEXT})
        return httpx.Response(200, json={"value": []})

    result = await _client(_handler(page)).verify_group_members(GROUP_ID, [USER_ID])

    assert calls == 2
    assert result.checked_at == started
    assert result.member_object_ids == frozenset({USER_ID})
    with pytest.raises(FrozenInstanceError):
        result.checked_at = moment[0]  # type: ignore[misc]


@pytest.mark.asyncio
async def test_empty_group_is_complete_negative_evidence_not_a_missing_group() -> None:
    result = await _client(
        _handler(lambda _request: httpx.Response(200, json={"value": []}))
    ).verify_group_members(GROUP_ID, [USER_ID])

    assert result.candidate_object_ids == frozenset({USER_ID})
    assert result.member_object_ids == frozenset()


@pytest.mark.asyncio
async def test_candidates_are_normalized_deduplicated_and_need_no_directory_labels() -> None:
    candidate = _id(0xABCDEF)
    result = await _client(
        _handler(lambda _request: httpx.Response(200, json={"value": [{"id": candidate.upper()}]}))
    ).verify_group_members(GROUP_ID, [candidate, candidate.upper()])

    assert result.candidate_object_ids == result.member_object_ids == frozenset({candidate})


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
async def test_a_matched_candidate_never_hides_a_failed_later_page(status: int) -> None:
    calls = 0

    def page(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"value": [{"id": USER_ID}], "@odata.nextLink": NEXT})
        return httpx.Response(status, headers={"Retry-After": "20"})

    error = DirectoryForbiddenError if status in {401, 403} else DirectoryError
    with pytest.raises(error):
        await _client(_handler(page)).verify_group_members(GROUP_ID, [USER_ID])
    assert calls == (3 if status == 503 else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["group", "members"])
async def test_verification_exposes_throttling_without_returning_evidence(stage: str) -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def throttle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "20"})

    handler = throttle if stage == "group" else _handler(throttle)
    with pytest.raises(DirectoryThrottledError) as error:
        await _client(handler, sleep=sleep).verify_group_members(GROUP_ID, [USER_ID])
    assert error.value.retry_after == 20
    assert sleeps == []


@pytest.mark.asyncio
async def test_a_network_failure_on_a_later_page_cannot_return_partial_evidence() -> None:
    def page(request: httpx.Request) -> httpx.Response:
        if "$skiptoken" in request.url.params:
            raise httpx.ConnectError("fictional network failure", request=request)
        return httpx.Response(200, json={"value": [{"id": USER_ID}], "@odata.nextLink": NEXT})

    with pytest.raises(DirectoryError, match="could not be reached"):
        await _client(_handler(page)).verify_group_members(GROUP_ID, [USER_ID])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"value": None},
        {"value": {}},
        {"value": [None]},
        {"value": ["not-an-object"]},
        {"value": [{}]},
        {"value": [{"id": None}]},
        {"value": [{"id": 123}]},
        {"value": [{"id": "not-a-guid"}]},
        {"value": [{"id": "00000000-0000-0000-0000-000000000000"}]},
        {"value": [{"id": f"{USER_ID}\n"}]},
        {"value": [{"id": USER_ID}, {"id": "invalid-after-a-match"}]},
        {"value": [{"id": USER_ID}] * 1000},
    ],
)
async def test_malformed_pages_never_become_complete_evidence(payload: dict[str, Any]) -> None:
    with pytest.raises(DirectoryError):
        await _client(
            _handler(lambda _request: httpx.Response(200, json=payload))
        ).verify_group_members(GROUP_ID, [USER_ID])


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
@pytest.mark.parametrize(
    "next_link",
    [
        None,
        "",
        True,
        1,
        {},
        [],
        "/v1.0/groups/other/transitiveMembers",
        f"http://graph.microsoft.com/v1.0/groups/{GROUP_ID}/transitiveMembers?next",
        f"https://graph.microsoft.com.evil.example/v1.0/groups/{GROUP_ID}/transitiveMembers",
        f"https://graph.microsoft.com@evil.example/v1.0/groups/{GROUP_ID}/transitiveMembers",
        f"https://user@graph.microsoft.com/v1.0/groups/{GROUP_ID}/transitiveMembers",
        f"https://graph.microsoft.com:444/v1.0/groups/{GROUP_ID}/transitiveMembers",
        "https://graph.microsoft.com/v1.0/users?next",
        f"{COLLECTION}/microsoft.graph.user?next",
        f"{NEXT}#fragment",
        f" {NEXT}",
        f"{NEXT}\n",
        f"{NEXT}\x00",
    ],
)
async def test_invalid_next_links_are_not_followed_or_treated_as_the_end(
    next_link: Any, complete: bool
) -> None:
    calls = 0

    def page(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"value": [{"id": USER_ID}], "@odata.nextLink": next_link})

    with pytest.raises(DirectoryError, match="paging URL"):
        client = _client(_handler(page))
        if complete:
            await client.verify_group_members(GROUP_ID, [USER_ID])
        else:
            await client.group_members(GROUP_ID)
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_repeated_paging_cursor_is_a_failure_even_if_the_candidate_was_found(
    complete: bool,
) -> None:
    calls = 0

    def page(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"value": [{"id": USER_ID}], "@odata.nextLink": NEXT})

    with pytest.raises(DirectoryError, match="repeated"):
        client = _client(_handler(page))
        if complete:
            await client.verify_group_members(GROUP_ID, [USER_ID])
        else:
            await client.group_members(GROUP_ID)
    assert calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"id": GROUP_ID},
        {"id": GROUP_ID, "securityEnabled": False},
        {"id": GROUP_ID, "securityEnabled": "true"},
        {"id": USER_ID, "securityEnabled": True},
    ],
)
async def test_the_referenced_security_group_must_be_verified(payload: dict[str, Any]) -> None:
    with pytest.raises(DirectoryError, match="security group"):
        await _client(lambda _request: httpx.Response(200, json=payload)).verify_group_members(
            GROUP_ID, [USER_ID]
        )


@pytest.mark.asyncio
async def test_a_deleted_group_is_not_successful_empty_membership() -> None:
    with pytest.raises(DirectoryError, match="security group"):
        await _client(lambda _request: httpx.Response(404)).verify_group_members(
            GROUP_ID, [USER_ID]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        {"tid": "wrong-tenant", "idtyp": "app"},
        {"tid": "tenant-test", "scp": "GroupMember.Read.All"},
        {"tid": "tenant-test", "idtyp": "app", "scp": "", "roles": []},
        {"tid": "tenant-test", "idtyp": "user", "roles": ["GroupMember.Read.All"]},
        {"tid": "tenant-test", "idtyp": ["app"], "roles": ["GroupMember.Read.All"]},
        {"tid": "tenant-test", "idtyp": {}, "roles": ["GroupMember.Read.All"]},
        {"idtyp": "app"},
    ],
)
async def test_untrusted_app_tenant_tokens_are_rejected_before_any_graph_read(
    claims: dict[str, Any],
) -> None:
    with pytest.raises(DirectoryForbiddenError, match="app-only"):
        await _client(
            lambda _request: pytest.fail("unverified token must never be sent"),
            credential=FakeCredential(claims),
        ).verify_group_members(GROUP_ID, [USER_ID])


@pytest.mark.asyncio
async def test_each_page_uses_a_freshly_validated_tenant_bound_credential() -> None:
    credential = FakeCredential()
    calls = 0

    def page(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        credential.claims = {"tid": "wrong-tenant", "idtyp": "app"}
        return httpx.Response(200, json={"value": [{"id": USER_ID}], "@odata.nextLink": NEXT})

    with pytest.raises(DirectoryForbiddenError, match="app-only"):
        await _client(_handler(page), credential=credential).verify_group_members(
            GROUP_ID, [USER_ID]
        )
    assert calls == 1
    assert credential.tenants == ["tenant-test"] * 3


@pytest.mark.asyncio
async def test_verification_requires_an_explicit_tenant() -> None:
    client = GraphDirectoryLookup(
        FakeCredential(),
        endpoint="https://graph.microsoft.com",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: pytest.fail("no tenant means no Graph reads")
            )
        ),
    )
    with pytest.raises(DirectoryForbiddenError, match="configured tenant"):
        await client.verify_group_members(GROUP_ID, [USER_ID])


@pytest.mark.asyncio
async def test_token_acquisition_failure_is_an_explicit_directory_error() -> None:
    class BrokenCredential(FakeCredential):
        async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
            raise ClientAuthenticationError("fictional credential is unavailable")

    with pytest.raises(DirectoryError, match="could not get"):
        await _client(
            lambda _request: pytest.fail("no token means no Graph reads"),
            credential=BrokenCredential(),
        ).verify_group_members(GROUP_ID, [USER_ID])


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["group", "members"])
async def test_redirects_are_rejected_even_if_the_injected_client_allows_them(stage: str) -> None:
    calls: list[str] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(
            302, headers={"Location": "https://foreign.example/collect"}, json={"value": []}
        )

    client = GraphDirectoryLookup(
        FakeCredential(),
        endpoint="https://graph.microsoft.com",
        tenant_id="tenant-test",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(redirect if stage == "group" else _handler(redirect)),
            follow_redirects=True,
        ),
    )
    with pytest.raises(DirectoryError):
        await client.verify_group_members(GROUP_ID, [USER_ID])
    assert calls == ["graph.microsoft.com"]


@pytest.mark.asyncio
async def test_the_whole_enumeration_has_a_deadline_and_never_renews_partial_evidence() -> None:
    started_final_page = asyncio.Event()

    async def page(request: httpx.Request) -> httpx.Response:
        if "$skiptoken" in request.url.params:
            started_final_page.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json={"value": [{"id": USER_ID}], "@odata.nextLink": NEXT})

    with pytest.raises(DirectoryError, match="timed out"):
        await _client(_handler(page)).verify_group_members(GROUP_ID, [USER_ID], timeout_seconds=0.1)
    assert started_final_page.is_set()


@pytest.mark.asyncio
async def test_a_completed_page_cannot_return_evidence_after_the_read_budget_expires() -> None:
    def page(_request: httpx.Request) -> httpx.Response:
        # A synchronous response can finish without yielding to the timeout's cancellation.
        time.sleep(0.15)
        return httpx.Response(200, json={"value": [{"id": USER_ID}]})

    with pytest.raises(DirectoryError, match="timed out"):
        await _client(_handler(page)).verify_group_members(GROUP_ID, [USER_ID], timeout_seconds=0.1)


@pytest.mark.asyncio
async def test_an_expired_budget_cannot_start_graph_reads_or_return_fake_evidence() -> None:
    client = _client(lambda _request: pytest.fail("an expired budget must not call Graph"))
    group = DirectoryObject(object_id=GROUP_ID, kind=PrincipalKind.SECURITY_GROUP)
    fake = FakeDirectoryLookup([group])
    for lookup in [client, fake]:
        with pytest.raises(DirectoryError, match="timed out"):
            await lookup.verify_group_members(GROUP_ID, [USER_ID], timeout_seconds=1e-300)


@pytest.mark.asyncio
async def test_service_retry_guidance_is_finite_and_cannot_make_partial_evidence_successful() -> (
    None
):
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    with pytest.raises(DirectoryError):
        await _client(
            _handler(lambda _request: httpx.Response(503, headers={"Retry-After": "NaN"})),
            sleep=sleep,
        ).verify_group_members(GROUP_ID, [USER_ID])
    assert delays == [1]


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_without_success_or_retry() -> None:
    started_final_page = asyncio.Event()

    async def page(request: httpx.Request) -> httpx.Response:
        if "$skiptoken" in request.url.params:
            started_final_page.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json={"value": [{"id": USER_ID}], "@odata.nextLink": NEXT})

    task = asyncio.create_task(_client(_handler(page)).verify_group_members(GROUP_ID, [USER_ID]))
    await asyncio.wait_for(started_final_page.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "group_id,candidates",
    [
        ("invalid", [USER_ID]),
        (f"{GROUP_ID}\n", [USER_ID]),
        (GROUP_ID, []),
        (GROUP_ID, ["invalid"]),
        (GROUP_ID, [f" {USER_ID}"]),
        (GROUP_ID, ["00000000-0000-0000-0000-000000000000"]),
    ],
)
async def test_invalid_verification_scope_is_rejected_before_network_io(
    group_id: str, candidates: Sequence[str]
) -> None:
    client = _client(lambda _request: pytest.fail("invalid input must not call Graph"))
    with pytest.raises(ValidationError):
        await client.verify_group_members(group_id, candidates)
    with pytest.raises(ValidationError):
        await FakeDirectoryLookup([]).verify_group_members(group_id, candidates)


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_seconds", [0, -1, 61, float("nan"), float("inf"), True])
async def test_invalid_deadlines_are_rejected_consistently(timeout_seconds: float) -> None:
    client = _client(lambda _request: pytest.fail("invalid deadline must not call Graph"))
    with pytest.raises(ValidationError):
        await client.verify_group_members(GROUP_ID, [USER_ID], timeout_seconds=timeout_seconds)
    with pytest.raises(ValidationError):
        await FakeDirectoryLookup([]).verify_group_members(
            GROUP_ID, [USER_ID], timeout_seconds=timeout_seconds
        )


@pytest.mark.asyncio
async def test_configured_government_graph_endpoint_and_paging_are_preserved() -> None:
    endpoint = "https://graph.microsoft.us"
    collection = f"{endpoint}/v1.0/groups/{GROUP_ID}/transitiveMembers"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        assert request.url.host == "graph.microsoft.us"
        if request.url.path == f"/v1.0/groups/{GROUP_ID}":
            return httpx.Response(200, json={"id": GROUP_ID, "securityEnabled": True})
        if "$skiptoken" not in request.url.params:
            return httpx.Response(
                200, json={"value": [], "@odata.nextLink": f"{collection}?$skiptoken=next"}
            )
        return httpx.Response(200, json={"value": [{"id": USER_ID}]})

    credential = FakeCredential(endpoint=endpoint)
    client = GraphDirectoryLookup(
        credential,
        endpoint=endpoint,
        tenant_id="tenant-test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    result = await client.verify_group_members(GROUP_ID, [USER_ID])

    assert result.member_object_ids == frozenset({USER_ID})
    assert seen == ["graph.microsoft.us"] * 3
    assert credential.tenants == ["tenant-test"] * 3


@pytest.mark.asyncio
async def test_legacy_application_membership_fallback_is_no_longer_a_5000_member_preview() -> None:
    candidate = _id(6001)
    page_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal page_calls
        if "$filter" in request.url.params:
            return httpx.Response(400)
        if request.url.path == f"/v1.0/groups/{GROUP_ID}":
            return httpx.Response(200, json={"id": GROUP_ID, "securityEnabled": True})
        page_calls += 1
        offset = int(request.url.params.get("$skiptoken", "0"))
        values = [{"id": _id(number)} for number in range(offset + 1, min(offset + 1000, 6002))]
        payload: dict[str, Any] = {"value": values}
        if offset + len(values) < 6001:
            payload["@odata.nextLink"] = f"{COLLECTION}?$skiptoken={offset + len(values)}"
        return httpx.Response(200, json=payload)

    assert await _client(handler)._group_contains_member(GROUP_ID, candidate)
    assert page_calls == 7


@pytest.mark.asyncio
async def test_fake_verification_includes_nested_id_only_members_and_terminates_cycles() -> None:
    nested_id = _id(8001)
    other_member = _id(8002)
    group = DirectoryObject(object_id=GROUP_ID, kind=PrincipalKind.SECURITY_GROUP)
    nested = DirectoryObject(object_id=nested_id, kind=PrincipalKind.SECURITY_GROUP)
    fake = FakeDirectoryLookup(
        [group, nested],
        {GROUP_ID: [nested_id], nested_id: [GROUP_ID, USER_ID, other_member]},
    )
    result = await fake.verify_group_members(GROUP_ID, [USER_ID, other_member, _id(9001)])

    assert result.member_object_ids == frozenset({USER_ID, other_member})
    assert result.candidate_object_ids == frozenset({USER_ID, other_member, _id(9001)})
    assert result.tenant_id == "tenant-test"


@pytest.mark.asyncio
async def test_fake_complete_verification_is_not_limited_by_directory_preview_objects() -> None:
    members = [_id(number) for number in range(1, 6002)]
    group = DirectoryObject(object_id=GROUP_ID, kind=PrincipalKind.SECURITY_GROUP)
    fake = FakeDirectoryLookup([group], {GROUP_ID: members})

    result = await fake.verify_group_members(GROUP_ID, [members[-1]])

    assert result.member_object_ids == frozenset({members[-1]})
    assert not (await fake.group_members(GROUP_ID)).members


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "objects,members",
    [
        ([], {GROUP_ID: [USER_ID]}),
        ([DirectoryObject(object_id=GROUP_ID, kind=PrincipalKind.USER)], {}),
        (
            [DirectoryObject(object_id=GROUP_ID, kind=PrincipalKind.SECURITY_GROUP)],
            {GROUP_ID: ["malformed"]},
        ),
        (
            [DirectoryObject(object_id=GROUP_ID, kind=PrincipalKind.SECURITY_GROUP)],
            {GROUP_ID: [_id(8001)], _id(8001): [USER_ID]},
        ),
    ],
)
async def test_fake_verification_cannot_omit_unverifiable_directory_data(
    objects: list[DirectoryObject], members: dict[str, list[str]]
) -> None:
    with pytest.raises(DirectoryError):
        await FakeDirectoryLookup(objects, members).verify_group_members(GROUP_ID, [USER_ID])


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden", [True, False])
async def test_fake_verification_reports_graph_errors(forbidden: bool) -> None:
    fake = FakeDirectoryLookup([], forbidden=forbidden, unavailable=not forbidden)
    error = DirectoryForbiddenError if forbidden else DirectoryError
    with pytest.raises(error):
        await fake.verify_group_members(GROUP_ID, [USER_ID])
