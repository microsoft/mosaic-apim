"""Offline real app-role enumeration; never reuse transitive grant membership for app roles."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from mosaic_api.integrations.graph.notifications import GraphNotificationDirectory, usable_mailbox
from test_graph_client import FakeCredential, _client

APP = "10000000-0000-0000-0000-000000000001"
SP = "10000000-0000-0000-0000-000000000002"
ROLE = "10000000-0000-0000-0000-000000000003"
USER = "10000000-0000-0000-0000-000000000004"
MEMBER = "10000000-0000-0000-0000-000000000005"
GROUP = "10000000-0000-0000-0000-000000000006"
NESTED = "10000000-0000-0000-0000-000000000007"
PERMISSIONS = ["Application.Read.All", "User.ReadBasic.All", "GroupMember.Read.All"]


def principal() -> dict[str, Any]:
    return {
        "id": SP,
        "appId": APP,
        "appRoles": [
            {"id": ROLE, "value": "Admin", "isEnabled": True, "allowedMemberTypes": ["User"]}
        ],
    }


def assignment(identifier: str, kind: str) -> dict[str, str]:
    return {"principalId": identifier, "principalType": kind, "appRoleId": ROLE, "resourceId": SP}


def directory(
    handler: Callable[[httpx.Request], httpx.Response], *, permissions: list[str] | None = None
) -> GraphNotificationDirectory:
    credential = FakeCredential(
        {
            "tid": "tenant-test",
            "idtyp": "app",
            "roles": PERMISSIONS if permissions is None else permissions,
        }
    )
    return GraphNotificationDirectory(_client(handler, credential=credential), api_client_id=APP)


async def test_actual_admins_are_direct_app_role_users_and_direct_assigned_group_users_only() -> (
    None
):
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        assert "transitive" not in path
        if path == "/v1.0/servicePrincipals":
            assert request.url.params["$filter"] == f"appId eq '{APP}'"
            return httpx.Response(200, json={"value": [principal()]})
        if path.endswith("/appRoleAssignedTo"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        assignment(USER, "User"),
                        assignment(GROUP, "Group"),
                        {**assignment(NESTED, "User"), "appRoleId": APP},
                    ]
                },
            )
        if path == f"/v1.0/groups/{GROUP}/members/microsoft.graph.user":
            if request.url.params.get("$skiptoken") == "page2":
                return httpx.Response(200, json={"value": [{"id": MEMBER}]})
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": USER},
                        {"id": NESTED, "@odata.type": "#microsoft.graph.group"},
                    ],
                    "@odata.nextLink": (
                        f"https://graph.microsoft.com/v1.0/groups/{GROUP}"
                        "/members/microsoft.graph.user?$skiptoken=page2"
                    ),
                },
            )
        if path in {f"/v1.0/users/{USER}", f"/v1.0/users/{MEMBER}"}:
            return httpx.Response(
                200,
                json={
                    "id": path.rsplit("/", 1)[1],
                    "displayName": "Fictional administrator",
                    "mail": "admin@example.test",
                },
            )
        pytest.fail(f"Unexpected directory request {request.url}")

    result = await directory(handler).discover_administrators("tenant-test")
    assert result.complete
    assert {contact.object_id for contact in result.administrators} == {USER, MEMBER}
    assert result.checked_at and result.fresh_until
    assert len([path for path in paths if path.startswith("/v1.0/users/")]) == 2
    assert all(contact.available for contact in result.administrators)


@pytest.mark.parametrize("failure", ["secondPageDenied", "malformed", "unsafePage", "missingRole"])
async def test_incomplete_admin_discovery_never_reports_partial_success(failure: str) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/v1.0/servicePrincipals":
            app = principal()
            if failure == "missingRole":
                app["appRoles"][0]["isEnabled"] = False
            return httpx.Response(200, json={"value": [app]})
        if request.url.path.endswith("/appRoleAssignedTo"):
            if failure == "malformed":
                return httpx.Response(200, json={"value": [{"principalId": USER}]})
            return httpx.Response(
                200,
                json={
                    "value": [assignment(USER, "User")],
                    "@odata.nextLink": "https://graph.microsoft.com.evil.test/v1.0/roles"
                    if failure == "unsafePage"
                    else "https://graph.microsoft.com/v1.0/roles?page=2",
                },
            )
        return httpx.Response(403, json={"error": {"message": "not authorized"}})

    result = await directory(handler).discover_administrators("tenant-test")
    assert not result.complete
    assert result.administrators == []
    assert result.reason
    assert all("evil.test" not in url for url in calls)


@pytest.mark.parametrize(
    "permissions",
    [
        [],
        ["Application.Read.All"],
        ["User.ReadBasic.All", "GroupMember.Read.All"],
    ],
)
async def test_missing_graph_permissions_are_explicit_and_do_not_probe_rosters(
    permissions: list[str],
) -> None:
    reader = directory(
        lambda _: pytest.fail("No directory request is allowed with missing permissions"),
        permissions=permissions,
    )
    result = await reader.discover_administrators("tenant-test")
    assert not result.complete and result.reason


async def test_directory_contact_uses_mail_not_guest_upn_or_display_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": USER,
                "displayName": "Guest Person",
                "userPrincipalName": "guest_example.com#EXT#@tenant.onmicrosoft.com",
                "mail": None,
            },
        )

    reader = directory(handler)
    result = await reader.contact("tenant-test", USER)
    assert not result.available and result.mailbox is None
    assert "UPN" in (result.reason or "")
    wrong_tenant = await reader.contact("other-tenant", USER)
    assert not wrong_tenant.available and "tenant" in (wrong_tenant.reason or "")


async def test_selected_administrators_use_direct_membership_not_an_incomplete_large_roster() -> (
    None
):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1.0/servicePrincipals":
            return httpx.Response(200, json={"value": [principal()]})
        if path.endswith("/appRoleAssignedTo"):
            return httpx.Response(200, json={"value": [assignment(GROUP, "Group")]})
        if path == f"/v1.0/groups/{GROUP}/members/{USER}":
            return httpx.Response(200, json={"id": USER})
        if path == f"/v1.0/groups/{GROUP}/members/{MEMBER}":
            return httpx.Response(404)
        if path == f"/v1.0/users/{USER}":
            return httpx.Response(200, json={"id": USER, "mail": "admin@example.test"})
        pytest.fail(f"Selected verification must not enumerate group members: {path}")

    result = await directory(handler).selected_administrators("tenant-test", [USER, MEMBER])
    assert result.complete
    assert [contact.object_id for contact in result.administrators] == [USER]


async def test_contact_read_failure_is_unavailable_not_evidence_of_a_missing_mailbox() -> None:
    reader = directory(lambda _: httpx.Response(503))
    contact = await reader.contact("tenant-test", USER)
    assert not contact.available
    assert not contact.lookup_complete
    assert contact.reason


async def test_every_graph_read_uses_same_tenant_app_credential_not_only_permission_probe() -> None:
    credential = FakeCredential({"tid": "tenant-test", "idtyp": "app", "roles": PERMISSIONS})
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        credential.claims = {"tid": "another-tenant", "idtyp": "app", "roles": PERMISSIONS}
        return httpx.Response(200, json={"value": [principal()]})

    reader = GraphNotificationDirectory(_client(handler, credential=credential), api_client_id=APP)
    result = await reader.discover_administrators("tenant-test")
    assert not result.complete and "tenant" in (result.reason or "")
    assert calls == ["/v1.0/servicePrincipals"]
    assert all(tenant == "tenant-test" for tenant in credential.tenants)


@pytest.mark.parametrize(
    "mail",
    [
        "Guest Person",
        "a@example.test\r\nBcc:b@example.test",
        "a#EXT#@tenant.example",
        "a@example.test,b@example.test",
        "a@example.test; other@example.test",
        None,
    ],
)
def test_non_mailbox_directory_values_are_unavailable(mail: object) -> None:
    assert usable_mailbox(mail) is None


def test_usable_directory_mail_is_preserved_without_constructing_an_address() -> None:
    assert usable_mailbox(" Fictional.Admin@example.test ") == "Fictional.Admin@example.test"
