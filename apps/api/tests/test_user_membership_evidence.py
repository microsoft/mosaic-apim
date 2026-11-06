import asyncio
import json
from typing import Any
from uuid import UUID

import httpx
import pytest
from mosaic_api.errors import DirectoryError, DirectoryForbiddenError
from test_graph_client import USER_ID, FakeCredential, _client


async def test_strict_user_check_batches_all_candidates_without_roster_reads() -> None:
    groups = [str(UUID(int=index)) for index in range(1, 46)]
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            assert request.url.path == f"/v1.0/users/{USER_ID}"
            return httpx.Response(200, json={"id": USER_ID, "displayName": "Fictional user"})
        assert request.url.path == f"/v1.0/users/{USER_ID}/checkMemberGroups"
        batch = json.loads(request.content)["groupIds"]
        assert len(batch) <= 20
        return httpx.Response(200, json={"value": batch[:1]})

    client = _client(handler)
    evidence = await client.verify_user_memberships(USER_ID, groups)
    assert evidence.tenant_id == "tenant-test" and evidence.user_object_id == USER_ID
    assert evidence.candidate_group_ids == frozenset(groups)
    assert len(evidence.member_group_ids) == 3
    assert len(calls) == 4
    assert all("transitiveMembers" not in str(call.url) for call in calls)
    await client.close()


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"value": None},
        {"value": "not-a-list"},
        {"value": [True]},
        {"value": [999]},
        {"value": ["invalid"]},
        {"value": [USER_ID]},
        {"value": ["00000000-0000-0000-0000-000000000001"] * 2},
        {"value": [], "@odata.nextLink": "https://graph.microsoft.com/v1.0/anything"},
    ],
)
async def test_malformed_or_incomplete_batch_never_returns_partial_evidence(response: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": USER_ID, "displayName": "Fictional user"})
        return httpx.Response(200, json=response)

    client = _client(handler)
    with pytest.raises(DirectoryError):
        await client.verify_user_memberships(USER_ID, ["00000000-0000-0000-0000-000000000001"])
    await client.close()


@pytest.mark.parametrize(
    "user",
    [
        {"id": "another-user"},
        {"id": USER_ID, "@odata.type": "#microsoft.graph.agentUser"},
    ],
)
async def test_strict_user_identity_or_agent_is_rejected_before_membership_post(user: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json=user)

    client = _client(handler)
    with pytest.raises(DirectoryError):
        await client.verify_user_memberships(USER_ID, ["00000000-0000-0000-0000-000000000001"])
    await client.close()


async def test_complete_user_read_has_deadline_and_requires_bound_app_identity() -> None:
    async def slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={"id": USER_ID})

    client = _client(slow)
    with pytest.raises(DirectoryError, match="timed out"):
        await client.verify_user_memberships(
            USER_ID,
            ["00000000-0000-0000-0000-000000000001"],
            timeout_seconds=0.01,
        )
    await client.close()
    client = _client(
        lambda request: pytest.fail(f"Wrong-tenant credentials cannot call Graph: {request.url}"),
        credential=FakeCredential({"tid": "another", "idtyp": "app"}),
    )
    with pytest.raises(DirectoryForbiddenError):
        await client.verify_user_memberships(USER_ID, ["00000000-0000-0000-0000-000000000001"])
    await client.close()
