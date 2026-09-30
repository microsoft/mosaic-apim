from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from azure.core.credentials import AccessToken
from mosaic_api.domain import DirectorySearchKind, PrincipalKind
from mosaic_api.errors import DirectoryError, DirectoryForbiddenError, ValidationError
from mosaic_api.integrations.graph.client import GraphDirectoryLookup

USER_ID = "11111111-1111-1111-1111-111111111111"
AGENT_USER_ID = "22222222-2222-2222-2222-222222222222"
AGENT_ID = "33333333-3333-3333-3333-333333333333"
GROUP_ID = "44444444-4444-4444-4444-444444444444"


class FakeCredential:
    async def get_token(self, *scopes: str, **_kwargs: Any) -> AccessToken:
        assert scopes == ("https://graph.microsoft.com/.default",)
        return AccessToken("token", 9999999999)


async def _no_sleep(_seconds: float) -> None:
    return None


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleep: Callable[[float], Awaitable[None]] = _no_sleep,
) -> GraphDirectoryLookup:
    return GraphDirectoryLookup(
        FakeCredential(),
        endpoint="https://graph.microsoft.com",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=sleep,
    )


@pytest.mark.asyncio
async def test_search_escapes_query_and_filters_security_groups() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/v1.0/groups"
        assert "securityEnabled+eq+true" in str(request.url)
        assert "%5C" not in str(request.url)
        assert "%22bad" not in str(request.url)
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": GROUP_ID,
                        "displayName": "Sec Group",
                        "mailNickname": "sec",
                        "securityEnabled": True,
                    },
                    {
                        "id": "55555555-5555-5555-5555-555555555555",
                        "displayName": "Mail Group",
                        "securityEnabled": False,
                    },
                ]
            },
        )

    result = await _client(handler).search(DirectorySearchKind.GROUP, 'bad"\\query', limit=99)

    assert len(seen) == 1
    assert len(result) == 1
    assert result[0].kind == PrincipalKind.SECURITY_GROUP


@pytest.mark.asyncio
async def test_guid_search_shortcuts_to_get_object() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == f"/v1.0/users/{USER_ID}":
            return httpx.Response(
                200,
                json={
                    "id": USER_ID,
                    "displayName": "Ada",
                    "userPrincipalName": "ada@example.com",
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    result = await _client(handler).search(DirectorySearchKind.USER, USER_ID, limit=20)

    assert [item.object_id for item in result] == [USER_ID]
    assert paths == [f"/v1.0/users/{USER_ID}"]


@pytest.mark.asyncio
async def test_user_search_merges_agent_users_with_agent_user_winning() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/users":
            return httpx.Response(
                200,
                json={"value": [{"id": AGENT_USER_ID, "displayName": "Plain user"}]},
            )
        if request.url.path == "/v1.0/users/microsoft.graph.agentUser":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": AGENT_USER_ID,
                            "displayName": "Agent user",
                            "identityParentId": AGENT_ID,
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    result = await _client(handler).search(DirectorySearchKind.USER, "agent", limit=20)

    assert len(result) == 1
    assert result[0].kind == PrincipalKind.AGENT_USER
    assert result[0].identity_parent_id == AGENT_ID


@pytest.mark.asyncio
async def test_agent_search_maps_agent_identity_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1.0/servicePrincipals/microsoft.graph.agentIdentity":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": AGENT_ID,
                            "displayName": "Agent",
                            "appId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                            "agentIdentityBlueprintId": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                        }
                    ]
                },
            )
        if request.url.path == "/v1.0/users/microsoft.graph.agentUser":
            return httpx.Response(200, json={"value": []})
        raise AssertionError(f"unexpected path {request.url.path}")

    result = await _client(handler).search(DirectorySearchKind.AGENT, "agent", limit=20)

    assert result[0].kind == PrincipalKind.AGENT_IDENTITY
    assert result[0].detail == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert result[0].app_id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert result[0].blueprint_id == "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.mark.asyncio
async def test_group_members_paginates_truncates_and_rejects_foreign_next_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(
            f"https://graph.microsoft.com/v1.0/groups/{GROUP_ID}/transitiveMembers"
        ):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "@odata.type": "#microsoft.graph.user",
                            "id": USER_ID,
                            "displayName": "Ada",
                        }
                    ],
                    "@odata.nextLink": "https://evil.example/v1.0/groups/page",
                },
            )
        raise AssertionError(f"unexpected path {request.url}")

    with pytest.raises(DirectoryError, match="unexpected paging URL"):
        await _client(handler).group_members(GROUP_ID, limit=2)


@pytest.mark.asyncio
async def test_group_members_sets_truncated_when_limit_reached_before_next_page() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "value": [
                    {"@odata.type": "#microsoft.graph.user", "id": USER_ID},
                ],
                "@odata.nextLink": (
                    f"https://graph.microsoft.com/v1.0/groups/{GROUP_ID}/transitiveMembers?$skip=1"
                ),
            },
        )

    page = await _client(handler).group_members(GROUP_ID, limit=1)

    assert page.truncated is True
    assert len(page.members) == 1


@pytest.mark.asyncio
async def test_error_mapping_and_retry() -> None:
    attempts = 0
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, headers={"Retry-After": "10"})
        return httpx.Response(403, headers={"request-id": "req-1"}, json={})

    with pytest.raises(DirectoryForbiddenError, match=r"GroupMember\.Read\.All"):
        await _client(handler, sleep=sleep).search(DirectorySearchKind.GROUP, "ada")

    assert attempts == 2
    assert sleeps == [5.0]


@pytest.mark.asyncio
async def test_malformed_json_maps_to_directory_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    with pytest.raises(DirectoryError, match="malformed JSON"):
        await _client(handler).search(DirectorySearchKind.GROUP, "sec")


@pytest.mark.asyncio
async def test_non_security_group_get_object_is_validation_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1.0/users/{GROUP_ID}":
            return httpx.Response(404, json={})
        if request.url.path == f"/v1.0/groups/{GROUP_ID}":
            return httpx.Response(200, json={"id": GROUP_ID, "securityEnabled": False})
        raise AssertionError(f"unexpected path {request.url.path}")

    with pytest.raises(ValidationError, match="not a security-enabled group"):
        await _client(handler).get_object(GROUP_ID)
