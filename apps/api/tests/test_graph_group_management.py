import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any
from uuid import UUID

import httpx
import pytest
from azure.core.exceptions import ClientAuthenticationError
from mosaic_api.domain import DirectoryObject, PrincipalKind
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.graph import (
    DirectoryLookup,
    FakeDirectoryLookup,
    FakeDirectoryState,
    FakeGroupManagement,
    GraphGroup,
    GraphGroupManagement,
    GroupFailureKind,
    GroupManagementError,
    GroupWriteOutcome,
)
from test_graph_client import FakeCredential

TENANT = "10000000-0000-0000-0000-000000000001"
GROUP = "20000000-0000-0000-0000-000000000002"
USER = "30000000-0000-0000-0000-000000000003"
WORKLOAD = "40000000-0000-0000-0000-000000000004"
API_OBJECT_ID = "50000000-0000-0000-0000-000000000005"
API_CLIENT_ID = "60000000-0000-0000-0000-000000000006"
TRACE = "70000000-0000-0000-0000-000000000007"
NESTED = "80000000-0000-0000-0000-000000000008"
BASELINE = ("User.ReadBasic.All", "GroupMember.Read.All", "AgentIdentity.Read.All")
USER_WRITES = (*BASELINE, "GroupMember.ReadWrite.All")
ALL_WRITES = (*USER_WRITES, "Group.Create", "Application.ReadWrite.All")
ROOT = "https://graph.microsoft.com"
MEMBERS = f"{ROOT}/beta/groups/{GROUP}/members"
Handler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


async def _no_sleep(_seconds: float) -> None:
    pass


def _client(
    handler: Handler,
    *,
    permissions: tuple[str, ...] = ALL_WRITES,
    claims: dict[str, Any] | None = None,
    credential: FakeCredential | None = None,
    endpoint: str = ROOT,
    sleep: Callable[[float], Awaitable[None]] = _no_sleep,
) -> GraphGroupManagement:
    return GraphGroupManagement(
        credential
        or FakeCredential(
            claims
            if claims is not None
            else {
                "tid": TENANT,
                "idtyp": "app",
                "oid": API_OBJECT_ID,
                "appid": API_CLIENT_ID,
                "roles": list(permissions),
            },
            endpoint=endpoint,
        ),
        endpoint=endpoint,
        tenant_id=TENANT,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=sleep,
    )


def _group(**overrides: Any) -> dict[str, Any]:
    return {
        "id": GROUP,
        "displayName": "Fictional Kestrel research",
        "securityEnabled": True,
        "mailEnabled": False,
        "groupTypes": [],
        "isAssignableToRole": False,
        "onPremisesSyncEnabled": None,
        "onPremisesSecurityIdentifier": None,
        "membershipRule": None,
        **overrides,
    }


def _write_handler(handler: Handler, *, group: dict[str, Any] | None = None) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response | Awaitable[httpx.Response]:
        if request.method == "GET" and request.url.path == f"/v1.0/groups/{GROUP}":
            selected = set(request.url.params["$select"].split(","))
            assert {"isAssignableToRole", "groupTypes", "onPremisesSyncEnabled"} <= selected
            return httpx.Response(200, json=group if group is not None else _group())
        return handler(request)

    return handle


def _no_http(request: httpx.Request) -> httpx.Response:
    pytest.fail(f"Unexpected Graph request: {request.method} {request.url}")


@pytest.mark.asyncio
async def test_read_only_contract_is_not_extended_with_mutation_methods() -> None:
    assert not hasattr(DirectoryLookup, "create_group")
    assert not hasattr(DirectoryLookup, "add_member")
    assert not hasattr(DirectoryLookup, "remove_member")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("permissions", "create", "users", "workloads", "remove"),
    [
        (BASELINE, False, False, False, False),
        ((*BASELINE, "Group.Create"), True, False, False, False),
        (USER_WRITES, False, True, False, True),
        ((*BASELINE, "Application.ReadWrite.All"), False, False, False, False),
        (ALL_WRITES, True, True, True, True),
    ],
)
async def test_capabilities_are_advisory_token_inspection_without_probes(
    permissions: tuple[str, ...], create: bool, users: bool, workloads: bool, remove: bool
) -> None:
    capabilities = await _client(_no_http, permissions=permissions).capabilities()
    assert capabilities.tenant_id == TENANT
    assert capabilities.service_principal_object_id == API_OBJECT_ID
    assert capabilities.application_permissions == frozenset(permissions)
    assert capabilities.can_read_groups
    assert capabilities.can_create_groups is create
    assert capabilities.can_add_users is users
    assert capabilities.can_add_service_principals is workloads
    assert capabilities.can_remove_members is remove
    assert capabilities.advisory and capabilities.member_listing_api == "beta"


@pytest.mark.asyncio
async def test_app_id_is_not_a_service_principal_object_id_for_creation() -> None:
    client = _client(
        _no_http,
        claims={"tid": TENANT, "idtyp": "app", "appid": API_CLIENT_ID, "roles": ["Group.Create"]},
    )
    assert not (await client.capabilities()).can_create_groups
    with pytest.raises(GroupManagementError) as error:
        await client.create_group("Fictional group")
    assert error.value.kind == GroupFailureKind.FORBIDDEN
    assert error.value.outcome == GroupWriteOutcome.NOT_APPLIED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claims",
    [
        {"tid": USER, "idtyp": "app", "roles": list(ALL_WRITES)},
        {"tid": TENANT, "idtyp": "user", "roles": list(ALL_WRITES)},
        {"tid": TENANT, "idtyp": "app", "scp": "", "roles": list(ALL_WRITES)},
        {"tid": TENANT, "scp": "GroupMember.ReadWrite.All"},
        {"tid": TENANT},
        {"tid": TENANT, "idtyp": "app", "roles": None},
        {"tid": TENANT, "idtyp": "app", "roles": [""]},
        {"tid": TENANT, "idtyp": "app", "roles": [None]},
    ],
)
async def test_wrong_tenant_delegated_and_malformed_tokens_cannot_reach_graph(
    claims: dict[str, Any],
) -> None:
    with pytest.raises(GroupManagementError) as error:
        await _client(_no_http, claims=claims).create_group("Fictional group")
    assert error.value.kind == GroupFailureKind.FORBIDDEN


@pytest.mark.asyncio
async def test_no_consent_is_reported_as_no_capabilities() -> None:
    capabilities = await _client(_no_http, claims={"tid": TENANT, "idtyp": "app"}).capabilities()
    assert not capabilities.can_create_groups
    assert not capabilities.can_add_users
    assert not capabilities.can_read_groups


@pytest.mark.asyncio
async def test_token_acquisition_failure_has_no_possible_write() -> None:
    class FailedCredential(FakeCredential):
        async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
            raise ClientAuthenticationError("Fictional authentication failure")

    with pytest.raises(GroupManagementError) as error:
        await _client(_no_http, credential=FailedCredential()).create_group("Fictional group")
    assert error.value.kind == GroupFailureKind.UNAVAILABLE
    assert error.value.outcome == GroupWriteOutcome.NOT_APPLIED


@pytest.mark.parametrize("tenant", ["", "tenant-test", USER + "\n", str(UUID(int=0))])
def test_configured_tenant_must_be_a_nonzero_guid(tenant: str) -> None:
    with pytest.raises(ValidationError):
        GraphGroupManagement(FakeCredential(), endpoint=ROOT, tenant_id=tenant)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://graph.microsoft.com",
        ROOT + "/v1.0",
        ROOT + "?other",
        ROOT + "#other",
        "https://user@graph.microsoft.com",
        ROOT + ":444",
        " " + ROOT,
        ROOT + "\n",
    ],
)
def test_graph_host_is_a_server_configured_https_origin(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        GraphGroupManagement(FakeCredential(), endpoint=endpoint, tenant_id=TENANT)


@pytest.mark.asyncio
async def test_create_201_is_self_owned_and_returns_unverified_identity() -> None:
    requests: list[httpx.Request] = []
    credential = FakeCredential(
        {
            "tid": TENANT,
            "idtyp": "app",
            "oid": API_OBJECT_ID,
            "appid": API_CLIENT_ID,
            "roles": ["Group.Create"],
        }
    )

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST" and request.url.path == "/v1.0/groups"
        body = json.loads(request.content)
        assert body == {
            "displayName": "Fictional Kestrel",
            "description": "Fictional evaluation estate",
            "mailNickname": "fictional-kestrel",
            "mailEnabled": False,
            "securityEnabled": True,
            "groupTypes": [],
            "owners@odata.bind": [f"{ROOT}/v1.0/servicePrincipals/{API_OBJECT_ID}"],
        }
        assert API_CLIENT_ID not in request.content.decode()
        assert request.headers["Authorization"].startswith("Bearer ")
        assert request.headers["client-request-id"] == TRACE
        return httpx.Response(201, json={"id": GROUP}, headers={"request-id": "fictional-request"})

    result = await _client(handle, credential=credential).create_group(
        " Fictional Kestrel ",
        description="Fictional evaluation estate",
        mail_nickname="fictional-kestrel",
        request_id=TRACE,
    )
    assert len(requests) == 1
    assert credential.tenants == [TENANT]
    assert result.group_object_id == GROUP
    assert result.operation == "create_group"
    assert result.outcome == GroupWriteOutcome.CONFIRMED
    assert result.request_id == "fictional-request"
    assert result.client_request_id == TRACE
    assert not hasattr(result, "verified_at")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"id": GROUP}),
        httpx.Response(204),
        httpx.Response(201, text="not JSON"),
        httpx.Response(201, json=[]),
        httpx.Response(201, json={"id": "invalid"}),
        httpx.Response(500),
        httpx.Response(503, headers={"Retry-After": "1"}),
        httpx.Response(408),
    ],
)
async def test_create_unverifiable_results_are_unknown_and_never_blindly_retried(
    response: httpx.Response,
) -> None:
    calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    with pytest.raises(GroupManagementError) as error:
        await _client(handle).create_group("Fictional group", request_id=TRACE)
    assert calls == 1
    assert error.value.kind == GroupFailureKind.UNKNOWN_OUTCOME
    assert error.value.outcome == GroupWriteOutcome.UNKNOWN
    assert error.value.client_request_id == TRACE


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create_group", "add_member", "remove_member"])
async def test_timed_out_writes_have_unknown_outcomes_and_no_retry(operation: str) -> None:
    writes = 0

    def timeout(request: httpx.Request) -> httpx.Response:
        nonlocal writes
        writes += 1
        raise httpx.ReadTimeout("Fictional response lost", request=request)

    client = _client(_write_handler(timeout))
    with pytest.raises(GroupManagementError) as error:
        if operation == "create_group":
            await client.create_group("Fictional group")
        elif operation == "add_member":
            await client.add_member(GROUP, USER, member_kind=PrincipalKind.USER)
        else:
            await client.remove_member(GROUP, USER)
    assert writes == 1
    assert error.value.outcome == GroupWriteOutcome.UNKNOWN


@pytest.mark.asyncio
async def test_group_verification_reads_required_properties_without_mutation() -> None:
    group = await _client(
        _write_handler(_no_http, group=_group(isAssignableToRole=None, onPremisesSyncEnabled=False))
    ).validate_group(GROUP.upper())
    assert group == GraphGroup(
        TENANT,
        GROUP,
        "Fictional Kestrel research",
        is_assignable_to_role=None,
        on_premises_sync_enabled=False,
    )
    assert group.management_unsupported_reasons == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"securityEnabled": False},
        {"mailEnabled": True},
        {"groupTypes": ["DynamicMembership"]},
        {"groupTypes": ["Unified"]},
        {"isAssignableToRole": True},
        {"onPremisesSyncEnabled": True},
        {"onPremisesSecurityIdentifier": "fictional-synchronized-sid"},
        {"membershipRule": "(fictional rule)"},
    ],
)
@pytest.mark.parametrize("operation", ["validate_group", "add_member", "remove_member"])
async def test_unsupported_groups_never_receive_membership_writes(
    overrides: dict[str, Any],
    operation: str,
) -> None:
    client = _client(_write_handler(_no_http, group=_group(**overrides)))
    with pytest.raises(GroupManagementError) as error:
        if operation == "validate_group":
            await client.validate_group(GROUP)
        elif operation == "add_member":
            await client.add_member(GROUP, USER, member_kind=PrincipalKind.USER)
        else:
            await client.remove_member(GROUP, USER)
    assert error.value.kind == GroupFailureKind.UNSUPPORTED_GROUP
    assert error.value.outcome == GroupWriteOutcome.NOT_APPLIED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing",
    [
        "id",
        "securityEnabled",
        "mailEnabled",
        "groupTypes",
        "isAssignableToRole",
        "onPremisesSyncEnabled",
        "onPremisesSecurityIdentifier",
        "membershipRule",
    ],
)
async def test_missing_group_properties_are_not_assumed_safe(missing: str) -> None:
    data = _group()
    del data[missing]
    with pytest.raises(GroupManagementError) as error:
        await _client(_write_handler(_no_http, group=data)).validate_group(GROUP)
    assert error.value.kind == GroupFailureKind.INVALID_RESPONSE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"id": USER},
        {"securityEnabled": 1},
        {"mailEnabled": "false"},
        {"groupTypes": None},
        {"groupTypes": [None]},
        {"isAssignableToRole": "false"},
        {"onPremisesSyncEnabled": 0},
        {"onPremisesSecurityIdentifier": []},
        {"membershipRule": {}},
    ],
)
async def test_malformed_group_properties_never_pass_validation(overrides: dict[str, Any]) -> None:
    with pytest.raises(GroupManagementError) as error:
        await _client(_write_handler(_no_http, group=_group(**overrides))).validate_group(GROUP)
    assert error.value.kind == GroupFailureKind.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_direct_beta_pages_keep_workloads_limited_information_and_unsupported_rows() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET" and request.url.path == f"/beta/groups/{GROUP}/members"
        if len(requests) == 1:
            assert request.url.params == httpx.QueryParams({"$top": "2"})
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": USER,
                            "@odata.type": "#microsoft.graph.user",
                            "displayName": "Avery Demo",
                        },
                        {"id": WORKLOAD, "@odata.type": "#microsoft.graph.servicePrincipal"},
                    ],
                    "@odata.nextLink": f"{MEMBERS}?$skiptoken=fictional",
                },
            )
        assert request.url.params == httpx.QueryParams({"$skiptoken": "fictional"})
        return httpx.Response(
            200,
            json={
                "value": [
                    {"id": NESTED, "@odata.type": "#microsoft.graph.group"},
                    {"id": API_OBJECT_ID, "@odata.type": "#microsoft.graph.device"},
                ]
            },
        )

    client = _client(handle, permissions=BASELINE)
    first = await client.list_members(GROUP, page_size=2)
    second = await client.list_members(GROUP, next_link=first.next_link)
    assert len(requests) == 2
    assert first.api_version == "beta"
    assert first.members[1].object_id == WORKLOAD
    assert first.members[1].kind == PrincipalKind.SERVICE_PRINCIPAL
    assert first.members[1].display_name is None
    assert second.members[0].kind == PrincipalKind.SECURITY_GROUP
    assert second.members[1].kind is None
    assert second.members[1].odata_type == "#microsoft.graph.device"
    assert second.next_link is None


@pytest.mark.asyncio
async def test_large_roster_uses_server_pages_without_per_member_fanout() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == f"/beta/groups/{GROUP}/members"
        offset = int(request.url.params.get("$skiptoken", "0"))
        data: dict[str, Any] = {
            "value": [
                {"id": str(UUID(int=number)), "@odata.type": "#microsoft.graph.servicePrincipal"}
                for number in range(offset + 1, min(offset + 999, 2500) + 1)
            ]
        }
        if offset + 999 < 2500:
            data["@odata.nextLink"] = f"{MEMBERS}?$skiptoken={offset + 999}"
        return httpx.Response(200, json=data)

    client = _client(handle, permissions=BASELINE)
    next_link = None
    count = 0
    while True:
        page = await client.list_members(GROUP, page_size=999, next_link=next_link)
        count += len(page.members)
        next_link = page.next_link
        if next_link is None:
            break
    assert count == 2500 and calls == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_link",
    [
        None,
        "",
        1,
        {},
        f"{MEMBERS}\n",
        f"{MEMBERS}\x00",
        f"{MEMBERS}#fragment",
        f"{MEMBERS}?$skiptoken=1#fragment",
        f"{MEMBERS.replace('https:', 'http:')}?next",
        f"{MEMBERS.replace('graph.microsoft.com', 'graph.microsoft.com.evil.example')}?next",
        f"{MEMBERS.replace('graph.microsoft.com', 'user@graph.microsoft.com')}?next",
        f"{ROOT}:444/beta/groups/{GROUP}/members?next",
        f"{ROOT}/v1.0/groups/{GROUP}/members?next",
        f"{ROOT}/beta/groups/{USER}/members?next",
        f"{ROOT}/beta/users?next",
        f"{MEMBERS}/microsoft.graph.user?next",
        f"{ROOT}/beta/groups/{GROUP}/members%2fother?next",
    ],
)
async def test_foreign_malformed_or_wrong_resource_next_links_are_never_followed(
    next_link: Any,
) -> None:
    calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"value": [], "@odata.nextLink": next_link})

    with pytest.raises(GroupManagementError) as error:
        await _client(handle).list_members(GROUP)
    assert error.value.kind == GroupFailureKind.INVALID_RESPONSE
    assert calls == 1


@pytest.mark.asyncio
async def test_request_supplied_next_link_cannot_escape_host_or_group() -> None:
    with pytest.raises(GroupManagementError):
        await _client(_no_http).list_members(GROUP, next_link=f"{ROOT}/v1.0/users?next")


@pytest.mark.asyncio
async def test_repeated_page_cannot_be_reported_as_progress() -> None:
    next_link = f"{MEMBERS}?$skiptoken=repeat"
    with pytest.raises(GroupManagementError, match="repeated"):
        await _client(
            lambda _request: httpx.Response(
                200,
                json={
                    "value": [],
                    "@odata.nextLink": next_link,
                },
            )
        ).list_members(GROUP, next_link=next_link)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"value": None},
        {"value": [None]},
        {"value": [{}]},
        {
            "value": [{"id": USER, "@odata.type": None}],
        },
    ],
)
async def test_invalid_member_pages_do_not_return_partial_or_empty_success(data: Any) -> None:
    with pytest.raises(GroupManagementError) as error:
        await _client(lambda _request: httpx.Response(200, json=data)).list_members(GROUP)
    assert error.value.kind == GroupFailureKind.INVALID_RESPONSE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    [
        PrincipalKind.USER,
        PrincipalKind.AGENT_USER,
        PrincipalKind.SERVICE_PRINCIPAL,
        PrincipalKind.MANAGED_IDENTITY,
        PrincipalKind.AGENT_IDENTITY,
        PrincipalKind.SECURITY_GROUP,
    ],
)
async def test_supported_additions_use_one_reference_and_expect_204(kind: PrincipalKind) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == f"/v1.0/groups/{GROUP}/members/$ref"
        assert json.loads(request.content) == {"@odata.id": f"{ROOT}/v1.0/directoryObjects/{USER}"}
        return httpx.Response(204)

    result = await _client(_write_handler(handle)).add_member(
        GROUP, USER, member_kind=kind, request_id=TRACE
    )
    assert result.member_object_id == USER
    assert result.outcome == GroupWriteOutcome.CONFIRMED


@pytest.mark.asyncio
async def test_advanced_workload_add_permission_is_separate_from_user_membership_write() -> None:
    with pytest.raises(GroupManagementError, match=r"Application\.ReadWrite\.All"):
        await _client(_no_http, permissions=USER_WRITES).add_member(
            GROUP, WORKLOAD, member_kind=PrincipalKind.SERVICE_PRINCIPAL
        )


@pytest.mark.asyncio
async def test_removal_is_only_relationship_delete_and_does_not_need_application_write() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == f"/v1.0/groups/{GROUP}/members/{WORKLOAD}/$ref"
        assert request.content == b""
        return httpx.Response(204)

    result = await _client(_write_handler(handle), permissions=USER_WRITES).remove_member(
        GROUP, WORKLOAD
    )
    assert result.operation == "remove_member" and result.member_object_id == WORKLOAD


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "message", "kind", "outcome"),
    [
        (
            403,
            "Authorization_RequestDenied",
            "Fictional denial",
            GroupFailureKind.FORBIDDEN,
            GroupWriteOutcome.NOT_APPLIED,
        ),
        (
            404,
            "Request_ResourceNotFound",
            "Fictional missing object",
            GroupFailureKind.NOT_FOUND,
            GroupWriteOutcome.NOT_APPLIED,
        ),
        (
            400,
            "Request_BadRequest",
            "One or more added object references already exist",
            GroupFailureKind.REJECTED,
            GroupWriteOutcome.NOT_APPLIED,
        ),
        (
            400,
            "Request_BadRequest",
            "Fictional invalid member type",
            GroupFailureKind.REJECTED,
            GroupWriteOutcome.NOT_APPLIED,
        ),
        (
            400,
            "Request_BadRequest",
            "The source resource object or one of the objects being referenced don't exist.",
            GroupFailureKind.REPLICATION_PENDING,
            GroupWriteOutcome.NOT_APPLIED,
        ),
        (
            503,
            "ServiceUnavailable",
            "Fictional unavailable",
            GroupFailureKind.UNKNOWN_OUTCOME,
            GroupWriteOutcome.UNKNOWN,
        ),
        (200, "", "", GroupFailureKind.UNKNOWN_OUTCOME, GroupWriteOutcome.UNKNOWN),
    ],
)
async def test_member_write_errors_are_structured_and_never_local_success(
    status: int,
    code: str,
    message: str,
    kind: GroupFailureKind,
    outcome: GroupWriteOutcome,
) -> None:
    calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            json={"error": {"code": code, "message": message}},
            headers={"request-id": "fictional-graph-request"},
        )

    with pytest.raises(GroupManagementError) as error:
        await _client(_write_handler(handle)).add_member(
            GROUP, USER, member_kind=PrincipalKind.USER, request_id=TRACE
        )
    assert calls == 1
    assert error.value.kind == kind and error.value.outcome == outcome
    assert error.value.http_status == status
    assert error.value.graph_code == (code or None)
    assert error.value.request_id == "fictional-graph-request"
    assert error.value.client_request_id == TRACE
    if kind == GroupFailureKind.REPLICATION_PENDING:
        assert error.value.retry_after == 2


@pytest.mark.asyncio
async def test_small_throttle_has_one_bounded_retry_but_long_retry_after_is_surfaced() -> None:
    sleeps: list[float] = []
    responses = [
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(201, json={"id": GROUP}),
    ]

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    result = await _client(lambda _request: responses.pop(0), sleep=sleep).create_group(
        "Fictional group"
    )
    assert result.group_object_id == GROUP
    assert sleeps == [2]
    with pytest.raises(GroupManagementError) as error:
        await _client(
            lambda _request: httpx.Response(429, headers={"Retry-After": "20"}), sleep=sleep
        ).create_group("Fictional group")
    assert error.value.kind == GroupFailureKind.THROTTLED
    assert error.value.retry_after == 20
    assert error.value.outcome == GroupWriteOutcome.NOT_APPLIED
    assert sleeps == [2]


@pytest.mark.asyncio
async def test_repeated_throttle_is_not_unbounded_and_retains_retry_after() -> None:
    calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "1"})

    with pytest.raises(GroupManagementError) as error:
        await _client(handle).create_group("Fictional group")
    assert calls == 2 and error.value.retry_after == 1


@pytest.mark.asyncio
async def test_safe_read_503_retries_but_redirects_are_never_followed() -> None:
    calls = 0

    def handle(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "1"})
        return httpx.Response(200, json=_group())

    assert (await _client(handle).validate_group(GROUP)).object_id == GROUP
    assert calls == 2
    calls = 0

    def redirect(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"Location": "https://evil.fictional.example/groups"})

    with pytest.raises(GroupManagementError):
        await _client(redirect).create_group("Fictional group")
    assert calls == 1


@pytest.mark.asyncio
async def test_configured_graph_origin_is_used_for_scope_owner_and_member_references() -> None:
    endpoint = "https://graph.fictional.example"

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "graph.fictional.example"
        assert json.loads(request.content)["owners@odata.bind"] == [
            f"{endpoint}/v1.0/servicePrincipals/{API_OBJECT_ID}"
        ]
        return httpx.Response(201, json={"id": GROUP})

    await _client(handle, endpoint=endpoint).create_group("Fictional group")


@pytest.mark.asyncio
@pytest.mark.parametrize("object_id", ["not-a-guid", USER + "\n", str(UUID(int=0)), ROOT])
async def test_invalid_mutation_targets_never_reach_graph(object_id: str) -> None:
    client = _client(_no_http)
    with pytest.raises(ValidationError):
        await client.add_member(GROUP, object_id, member_kind=PrincipalKind.USER)
    with pytest.raises(ValidationError):
        await client.remove_member(object_id, USER)


@pytest.mark.asyncio
@pytest.mark.parametrize("page_size", [0, -1, 1000, True])
async def test_membership_page_size_is_bounded(page_size: int) -> None:
    with pytest.raises(ValidationError):
        await _client(_no_http).list_members(GROUP, page_size=page_size)


@pytest.mark.asyncio
async def test_invalid_member_kind_and_trace_are_rejected_before_io() -> None:
    client = _client(_no_http)
    with pytest.raises(ValidationError):
        await client.add_member(GROUP, NESTED, member_kind="device")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        await client.create_group("Fictional group", request_id="not-a-guid")


@pytest.mark.asyncio
async def test_injected_http_client_lifetime_is_owned_by_the_composer() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(_no_http))
    adapter = GraphGroupManagement(FakeCredential(), endpoint=ROOT, tenant_id=TENANT, client=client)
    await adapter.close()
    assert not client.is_closed
    await client.aclose()


def _fake(
    *,
    permissions: tuple[str, ...] = ALL_WRITES,
) -> tuple[FakeDirectoryLookup, FakeGroupManagement]:
    directory = FakeDirectoryLookup(
        [
            DirectoryObject(object_id=GROUP, kind=PrincipalKind.SECURITY_GROUP),
            DirectoryObject(object_id=USER, kind=PrincipalKind.USER, display_name="Avery Demo"),
            DirectoryObject(object_id=WORKLOAD, kind=PrincipalKind.MANAGED_IDENTITY),
        ],
        tenant_id=TENANT,
        application_permissions=permissions,
    )
    return directory, FakeGroupManagement(directory, service_principal_object_id=API_OBJECT_ID)


@pytest.mark.asyncio
async def test_fake_writes_are_visible_to_complete_eligibility_lookup() -> None:
    directory, adapter = _fake()
    await adapter.add_member(GROUP, USER, member_kind=PrincipalKind.USER)
    await adapter.add_member(GROUP, WORKLOAD, member_kind=PrincipalKind.MANAGED_IDENTITY)
    evidence = await directory.verify_group_members(GROUP, [USER, WORKLOAD])
    assert evidence.member_object_ids == frozenset({USER, WORKLOAD})
    assert await directory.member_groups(USER, [GROUP]) == {GROUP}
    assert {item.object_id for item in (await directory.group_members(GROUP)).members} == {
        USER,
        WORKLOAD,
    }
    await adapter.remove_member(GROUP, USER)
    evidence = await directory.verify_group_members(GROUP, [USER, WORKLOAD])
    assert evidence.member_object_ids == frozenset({WORKLOAD})
    assert await directory.get_object(USER) is not None
    assert await directory.member_groups(USER, [GROUP]) == set()
    assert len(adapter.writes) == 3


@pytest.mark.asyncio
async def test_fake_create_is_self_owned_and_composes_with_a_second_read_lookup() -> None:
    directory, adapter = _fake()
    second = FakeDirectoryLookup(
        [], tenant_id=TENANT, state=directory.state, application_permissions=BASELINE
    )
    assert isinstance(directory.state, FakeDirectoryState)
    created = await adapter.create_group("Fictional new center")
    assert adapter.owners[created.group_object_id] == (API_OBJECT_ID,)
    assert await second.get_object(created.group_object_id) is not None
    await adapter.add_member(created.group_object_id, USER, member_kind=PrincipalKind.USER)
    evidence = await second.verify_group_members(created.group_object_id, [USER])
    assert evidence.member_object_ids == frozenset({USER})


@pytest.mark.asyncio
async def test_fake_direct_paging_does_not_flatten_inherited_memberships() -> None:
    directory, _ = _fake()
    directory.state.objects[NESTED] = DirectoryObject(
        object_id=NESTED, kind=PrincipalKind.SECURITY_GROUP
    )
    directory.state.group_members.update({GROUP: [WORKLOAD], NESTED: [USER]})
    adapter = FakeGroupManagement(directory)
    await adapter.add_member(GROUP, NESTED, member_kind=PrincipalKind.SECURITY_GROUP)
    first = await adapter.list_members(GROUP, page_size=1)
    second = await adapter.list_members(GROUP, next_link=first.next_link)
    assert {item.object_id for item in (*first.members, *second.members)} == {NESTED, WORKLOAD}
    assert second.next_link is None
    evidence = await directory.verify_group_members(GROUP, [USER])
    assert evidence.member_object_ids == frozenset({USER})
    with pytest.raises(GroupManagementError) as error:
        await adapter.remove_member(GROUP, USER)
    assert error.value.kind == GroupFailureKind.NOT_FOUND
    await adapter.remove_member(GROUP, NESTED)
    assert (await directory.verify_group_members(GROUP, [USER])).member_object_ids == frozenset()
    assert await directory.get_object(NESTED) is not None


@pytest.mark.asyncio
async def test_fake_nested_groups_cannot_create_cycles() -> None:
    directory, adapter = _fake()
    with pytest.raises(GroupManagementError) as error:
        await adapter.add_member(GROUP, GROUP, member_kind=PrincipalKind.SECURITY_GROUP)
    assert error.value.kind == GroupFailureKind.REJECTED
    directory.state.objects[NESTED] = DirectoryObject(
        object_id=NESTED, kind=PrincipalKind.SECURITY_GROUP
    )
    directory.state.group_members[NESTED] = [GROUP]
    with pytest.raises(GroupManagementError):
        await adapter.add_member(GROUP, NESTED, member_kind=PrincipalKind.SECURITY_GROUP)
    assert adapter.writes == []


@pytest.mark.asyncio
async def test_fake_permission_and_injected_failure_never_mutate_local_roster() -> None:
    directory, adapter = _fake(permissions=BASELINE)
    with pytest.raises(GroupManagementError) as error:
        await adapter.add_member(GROUP, USER, member_kind=PrincipalKind.USER)
    assert error.value.kind == GroupFailureKind.FORBIDDEN
    assert directory.state.group_members == {}
    directory, adapter = _fake()
    adapter.failures["add_member"] = GroupManagementError(
        "Fictional Graph response lost",
        kind=GroupFailureKind.UNKNOWN_OUTCOME,
        outcome=GroupWriteOutcome.UNKNOWN,
    )
    with pytest.raises(GroupManagementError) as error:
        await adapter.add_member(GROUP, USER, member_kind=PrincipalKind.USER)
    assert error.value.outcome == GroupWriteOutcome.UNKNOWN
    assert adapter.writes == [] and directory.state.group_members == {}


@pytest.mark.asyncio
async def test_fake_unsupported_metadata_is_not_treated_as_an_editable_group() -> None:
    directory, _ = _fake()
    adapter = FakeGroupManagement(
        directory, groups=[replace(GraphGroup(TENANT, GROUP), group_types=("DynamicMembership",))]
    )
    with pytest.raises(GroupManagementError) as error:
        await adapter.add_member(GROUP, USER, member_kind=PrincipalKind.USER)
    assert error.value.kind == GroupFailureKind.UNSUPPORTED_GROUP
    assert adapter.writes == []
