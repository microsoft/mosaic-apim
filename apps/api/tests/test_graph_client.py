import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import jwt
import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from mosaic_api.domain import DirectorySearchKind, PrincipalKind
from mosaic_api.errors import (
    DirectoryError,
    DirectoryForbiddenError,
    DirectoryThrottledError,
    ValidationError,
)
from mosaic_api.integrations.graph.client import GraphDirectoryLookup
from mosaic_api.integrations.graph.photos import PHOTO_MAX_BYTES
from photo_double import corrupted_png, photo_image

USER_ID = "11111111-1111-1111-1111-111111111111"
AGENT_USER_ID = "22222222-2222-2222-2222-222222222222"
AGENT_ID = "33333333-3333-3333-3333-333333333333"
GROUP_ID = "44444444-4444-4444-4444-444444444444"


class FakeCredential:
    def __init__(self, claims: dict[str, Any] | None = None) -> None:
        self.claims = claims or {
            "tid": "tenant-test",
            "idtyp": "app",
            "roles": ["ProfilePhoto.Read.All"],
        }
        self.tenants: list[str | None] = []

    async def get_token(self, *scopes: str, **_kwargs: Any) -> AccessToken:
        assert scopes == ("https://graph.microsoft.com/.default",)
        self.tenants.append(_kwargs.get("tenant_id"))
        token = jwt.encode(
            self.claims, "test-only-key-that-is-at-least-32-bytes", algorithm="HS256"
        )
        return AccessToken(token, 9999999999)


async def _no_sleep(_seconds: float) -> None:
    return None


def _client(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
    *,
    sleep: Callable[[float], Awaitable[None]] = _no_sleep,
    **kwargs: Any,
) -> GraphDirectoryLookup:
    return GraphDirectoryLookup(
        FakeCredential(),
        endpoint="https://graph.microsoft.com",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=sleep,
        tenant_id="tenant-test",
        **kwargs,
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
async def test_application_search_is_bounded_read_only_and_excludes_agents() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1.0/servicePrincipals"
        assert request.url.params["$top"] == "25"
        assert request.url.params["$select"] == "id,displayName,appId,servicePrincipalType"
        assert "startswith(displayName,'O''Brien')" in request.url.params["$filter"]
        assert "servicePrincipalType eq 'ManagedIdentity'" in request.url.params["$filter"]
        assert request.headers["ConsistencyLevel"] == "eventual"
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": USER_ID,
                        "displayName": "O'Brien app",
                        "servicePrincipalType": "Application",
                    },
                    {
                        "id": GROUP_ID,
                        "displayName": "O'Brien identity",
                        "servicePrincipalType": "ManagedIdentity",
                    },
                    {
                        "id": AGENT_ID,
                        "@odata.type": "#microsoft.graph.agentIdentityBlueprintPrincipal",
                        "servicePrincipalType": "Application",
                    },
                    {"id": AGENT_USER_ID, "servicePrincipalType": "Legacy"},
                ],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/servicePrincipals?next",
            },
        )

    result = await _client(handler).search(DirectorySearchKind.APPLICATION, "O'Brien", limit=99)
    assert [item.kind for item in result] == [
        PrincipalKind.SERVICE_PRINCIPAL,
        PrincipalKind.MANAGED_IDENTITY,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("object_found", [True, False])
async def test_application_guid_search_checks_object_then_app_id(object_found: bool) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        item = {
            "id": USER_ID,
            "displayName": "Application",
            "appId": AGENT_ID,
            "servicePrincipalType": "Application",
        }
        if request.url.path == f"/v1.0/servicePrincipals/{AGENT_ID}":
            return httpx.Response(200, json=item) if object_found else httpx.Response(404)
        assert request.url.path == "/v1.0/servicePrincipals"
        assert f"appId eq '{AGENT_ID}'" in request.url.params["$filter"]
        return httpx.Response(200, json={"value": [item]})

    result = await _client(handler).search(DirectorySearchKind.APPLICATION, AGENT_ID.upper())
    assert result[0].object_id == USER_ID
    assert result[0].app_id == AGENT_ID
    assert len(paths) == (1 if object_found else 2)


@pytest.mark.asyncio
async def test_application_search_permission_error_names_read_only_permission() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    with pytest.raises(DirectoryForbiddenError, match=r"Application\.Read\.All"):
        await _client(handler).search(DirectorySearchKind.APPLICATION, "Contoso")


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


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["PNG", "JPEG"])
async def test_photo_reads_only_a_verified_user_thumbnail(format: str) -> None:
    photo = photo_image(format)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"].startswith("Bearer ")
        assert request.headers["Accept-Encoding"] == "identity"
        assert "token" not in str(request.url)
        if request.url.path == f"/v1.0/users/{USER_ID}":
            assert request.url.params["$select"] == "id"
            return httpx.Response(200, json={"id": USER_ID, "@odata.type": "#microsoft.graph.user"})
        assert request.url.path == f"/v1.0/users/{USER_ID}/photos/96x96/$value"
        return httpx.Response(
            200, content=photo.content, headers={"Content-Type": photo.content_type}
        )

    lookup = _client(handler)
    assert await lookup.user_photo("tenant-test", USER_ID.upper()) == photo
    assert len(seen) == 2
    assert lookup._credential.tenants == ["tenant-test"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_status", "kind", "photo_status", "calls"),
    [
        (404, "", 200, 1),
        (200, "#microsoft.graph.agentUser", 200, 1),
        (200, "#microsoft.graph.servicePrincipal", 200, 1),
        (200, "#microsoft.graph.user", 404, 2),
    ],
)
async def test_photo_missing_or_unsupported_user_is_not_an_error(
    user_status: int, kind: str, photo_status: int, calls: int
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if request.url.path.endswith(USER_ID):
            return httpx.Response(user_status, json={"id": USER_ID, "@odata.type": kind})
        return httpx.Response(photo_status)

    lookup = _client(handler)
    assert await lookup.user_photo("tenant-test", USER_ID) is None
    assert await lookup.user_photo("tenant-test", USER_ID) is None
    assert requests == calls


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_photo_permission_denial_names_separate_consent_and_is_not_cached(
    status: int,
) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if request.url.path.endswith(USER_ID):
            return httpx.Response(200, json={"id": USER_ID})
        return httpx.Response(status, json={"error": {"message": "private upstream text"}})

    lookup = _client(handler)
    for _ in range(2):
        with pytest.raises(DirectoryForbiddenError, match=r"ProfilePhoto\.Read\.All"):
            await lookup.user_photo("tenant-test", USER_ID)
    assert requests == 4
    assert not lookup._photo_cache


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["user", "photo"])
@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [("17", 17), ("900", 300), ("-10", 1), ("bad", 1), ("nan", 1), ("inf", 1)],
)
async def test_photo_throttle_retries_once_and_reports_bounded_wait(
    operation: str, retry_after: str, expected: int
) -> None:
    attempts = 0
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if operation == "photo" and request.url.path.endswith(USER_ID):
            return httpx.Response(200, json={"id": USER_ID})
        attempts += 1
        return httpx.Response(429, headers={"Retry-After": retry_after})

    lookup = _client(handler, sleep=sleep)
    with pytest.raises(DirectoryThrottledError) as raised:
        await lookup.user_photo("tenant-test", USER_ID)
    assert raised.value.retry_after == expected
    assert attempts == 2
    assert len(sleeps) == 1
    assert 0 <= sleeps[0] <= 5
    assert not lookup._photo_cache


@pytest.mark.asyncio
async def test_photo_throttle_accepts_http_date() -> None:
    retry = format_datetime(datetime.now(UTC) + timedelta(seconds=60), usegmt=True)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": retry})

    with pytest.raises(DirectoryThrottledError) as raised:
        await _client(handler).user_photo("tenant-test", USER_ID)
    assert 58 <= raised.value.retry_after <= 60


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 500, 503])
async def test_photo_failure_does_not_follow_redirects(status: int) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.host)
        return httpx.Response(status, headers={"Location": "https://external.example/photo"})

    with pytest.raises(DirectoryError):
        await _client(handler).user_photo("tenant-test", USER_ID)
    assert requests == ["graph.microsoft.com"] * (2 if status == 503 else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        {"tid": "other-tenant", "idtyp": "app"},
        {"tid": "tenant-test", "scp": "ProfilePhoto.Read.All"},
        {"tid": "tenant-test", "idtyp": "user", "roles": ["ProfilePhoto.Read.All"]},
        {"tid": "tenant-test"},
    ],
)
async def test_photo_rejects_wrong_tenant_and_non_app_credentials_before_graph(
    claims: dict[str, Any],
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("Graph must not be called")

    lookup = _client(handler)
    lookup._credential = FakeCredential(claims)
    with pytest.raises(DirectoryForbiddenError):
        await lookup.user_photo("tenant-test", USER_ID)


@pytest.mark.asyncio
async def test_photo_tenant_gate_precedes_cache_and_credential() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("Graph must not be called")

    lookup = _client(handler)
    lookup._photo_cache[("other-tenant", USER_ID)] = (float("inf"), photo_image())
    with pytest.raises(DirectoryForbiddenError):
        await lookup.user_photo("other-tenant", USER_ID)
    with pytest.raises(ValidationError):
        await lookup.user_photo("tenant-test", "https://external.example/photo")
    assert lookup._credential.tenants == []


@pytest.mark.asyncio
async def test_photo_credential_failure_is_coded_without_upstream_details() -> None:
    class FailedCredential(FakeCredential):
        async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
            raise ClientAuthenticationError("private credential details")

    lookup = _client(lambda _request: httpx.Response(200))
    lookup._credential = FailedCredential()
    with pytest.raises(DirectoryError, match="could not be completed") as raised:
        await lookup.user_photo("tenant-test", USER_ID)
    assert "private" not in raised.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content", [b"not-json", b"[]", b"{}", b'{"id":"different-user"}']
)
async def test_photo_rejects_malformed_user_verification_before_reading_photo(
    content: bytes,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(USER_ID)
        return httpx.Response(200, content=content)

    lookup = _client(handler)
    with pytest.raises(DirectoryError):
        await lookup.user_photo("tenant-test", USER_ID)
    assert not lookup._photo_cache


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"", "image/png"),
        (b"not an image", "image/jpeg"),
        (b"\xff\xd8\xff\xd9", "image/jpeg"),
        (photo_image().content, "image/jpeg"),
        (photo_image().content, "image/svg+xml"),
        (photo_image().content, "text/html"),
        (photo_image("JPEG").content[:-20], "image/jpeg"),
        (photo_image(size=(257, 1)).content, "image/png"),
        (photo_image("GIF").content, "image/gif"),
        (corrupted_png(), "image/png"),
        (b"x" * (PHOTO_MAX_BYTES + 1), "image/png"),
    ],
    ids=[
        "empty",
        "not-image",
        "empty-jpeg",
        "mime-mismatch",
        "svg",
        "html",
        "truncated",
        "dimensions",
        "gif",
        "corrupt-png",
        "oversized",
    ],
)
async def test_photo_rejects_invalid_type_data_dimensions_and_size(
    content: bytes, content_type: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(USER_ID):
            return httpx.Response(200, json={"id": USER_ID})
        return httpx.Response(200, content=content, headers={"Content-Type": content_type})

    lookup = _client(handler)
    with pytest.raises(DirectoryError):
        await lookup.user_photo("tenant-test", USER_ID)
    assert not lookup._photo_cache


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": str(PHOTO_MAX_BYTES + 1)},
        {"Content-Length": "-1"},
        {"Content-Length": "not-a-number"},
        {"Content-Encoding": "gzip"},
    ],
)
async def test_photo_rejects_bad_headers_before_streaming(headers: dict[str, str]) -> None:
    class UnreadStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            pytest.fail("Invalid header must be rejected before reading")
            yield b""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(USER_ID):
            return httpx.Response(200, json={"id": USER_ID})
        return httpx.Response(200, headers=headers, stream=UnreadStream())

    with pytest.raises(DirectoryError):
        await _client(handler).user_photo("tenant-test", USER_ID)


@pytest.mark.asyncio
async def test_photo_stream_aborts_at_size_limit_without_content_length() -> None:
    class HugeStream(httpx.AsyncByteStream):
        blocks = 0
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(1000):
                self.blocks += 1
                yield b"x" * 8192

        async def aclose(self) -> None:
            self.closed = True

    stream = HugeStream()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(USER_ID):
            return httpx.Response(200, json={"id": USER_ID})
        return httpx.Response(200, stream=stream, headers={"Content-Type": "image/png"})

    with pytest.raises(DirectoryError, match="oversized"):
        await _client(handler).user_photo("tenant-test", USER_ID)
    assert stream.blocks == 9
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("photo_found", [True, False])
async def test_photo_cache_is_bounded_lru_short_lived_and_cleared_on_close(
    photo_found: bool,
) -> None:
    now = 100.0
    requests = 0
    photo = photo_image()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if not request.url.path.endswith("$value"):
            return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[1]})
        return (
            httpx.Response(200, content=photo.content, headers={"Content-Type": photo.content_type})
            if photo_found
            else httpx.Response(404)
        )

    lookup = _client(handler, photo_cache_entries=2, clock=lambda: now)
    for object_id in [USER_ID, AGENT_USER_ID, USER_ID, GROUP_ID]:
        await lookup.user_photo("tenant-test", object_id)
    assert requests == 6
    assert list(lookup._photo_cache) == [("tenant-test", USER_ID), ("tenant-test", GROUP_ID)]
    await lookup.user_photo("tenant-test", AGENT_USER_ID)
    assert requests == 8
    assert len(lookup._photo_cache) == 2
    now += 120 if photo_found else 30
    await lookup.user_photo("tenant-test", AGENT_USER_ID)
    assert requests == 10
    assert len(lookup._photo_cache) == 1
    await lookup.close()
    assert not lookup._photo_cache


@pytest.mark.asyncio
async def test_photo_requests_coalesce_and_cache_capacity_cannot_be_unbounded() -> None:
    photo = photo_image()
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0)
        if request.url.path.endswith(USER_ID):
            return httpx.Response(200, json={"id": USER_ID})
        return httpx.Response(
            200, content=photo.content, headers={"Content-Type": photo.content_type}
        )

    lookup = _client(handler, photo_cache_entries=10000, photo_cache_seconds=36000)
    assert await asyncio.gather(
        *(lookup.user_photo("tenant-test", USER_ID) for _ in range(20))
    ) == [photo] * 20
    assert requests == 2
    assert lookup._photo_cache_entries == 128
    assert lookup._photo_cache_seconds == 120
