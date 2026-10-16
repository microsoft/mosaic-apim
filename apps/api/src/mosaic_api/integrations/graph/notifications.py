"""Notification-only directory capabilities; incumbent lookup/group semantics are unchanged."""

import re
from collections.abc import Sequence
from datetime import timedelta
from types import TracebackType
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import jwt
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError
from pydantic import Field

from mosaic_api.domain import MosaicModel, utc_now
from mosaic_api.errors import DirectoryError, DirectoryForbiddenError
from mosaic_api.integrations.graph.client import GraphDirectoryLookup
from mosaic_api.scheduling import UtcInstant

_MAILBOX = re.compile(r"^[^@\s<>,;:\x00-\x1f]+@[^@\s<>,;:\x00-\x1f]+\.[^@\s<>,;:\x00-\x1f]+$")


def usable_mailbox(value: object) -> str | None:
    """Only a directory mailbox, never a constructed address or a guest sign-in UPN."""
    if not isinstance(value, str):
        return None
    address = value.strip()
    if len(address) > 254 or "#ext#" in address.casefold() or not _MAILBOX.fullmatch(address):
        return None
    return address


class NotificationContact(MosaicModel):
    object_id: str
    display_name: str | None = None
    mailbox: str | None = None
    available: bool = False
    lookup_complete: bool = True
    reason: str | None = None


class AdministratorDiscovery(MosaicModel):
    administrators: list[NotificationContact] = Field(default_factory=list)
    complete: bool = False
    checked_at: UtcInstant = Field(default_factory=utc_now)
    fresh_until: UtcInstant | None = None
    reason: str | None = None


class NotificationDirectory(Protocol):
    async def discover_administrators(self, tenant_id: str) -> AdministratorDiscovery: ...

    async def contact(self, tenant_id: str, object_id: str) -> NotificationContact: ...


def _guid(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise DirectoryError("Microsoft Graph returned a missing or invalid object ID") from exc


class _TenantNotificationCredential(AsyncTokenCredential):
    """Bind every read, not only the initial permission probe, to the configured API tenant."""

    def __init__(self, credential: AsyncTokenCredential, tenant_id: str | None) -> None:
        self._credential = credential
        self._tenant_id = tenant_id

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        kwargs["tenant_id"] = self._tenant_id
        token = await self._credential.get_token(*scopes, **kwargs)
        try:
            claims = jwt.decode(token.token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise DirectoryForbiddenError("The Graph notification credential is invalid.") from exc
        if (
            not self._tenant_id
            or str(claims.get("tid", "")).casefold() != self._tenant_id
            or claims.get("scp")
            or claims.get("idtyp") not in {None, "app"}
            or not isinstance(claims.get("roles"), list)
        ):
            raise DirectoryForbiddenError(
                "Notification discovery requires the API's app-only credential in this tenant."
            )
        return token

    async def close(self) -> None:
        # The host owns this shared Azure credential.
        return None

    async def __aenter__(self) -> "_TenantNotificationCredential":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        return None


class GraphNotificationDirectory:
    """Resolve actual API app-role assignments, never MOSAIC's onboarded principals.

    No roster cache is used: delivery rechecks observe current Graph results. Graph itself is
    eventually consistent, so the returned freshness is evidence of a read, not an access token.
    """

    def __init__(
        self,
        lookup: GraphDirectoryLookup,
        *,
        api_client_id: str,
        admin_role: str = "Admin",
        max_objects: int = 10000,
    ) -> None:
        if lookup._client.follow_redirects:
            raise ValueError("Notification Graph clients must disable redirects.")
        self._lookup = GraphDirectoryLookup(
            _TenantNotificationCredential(lookup._credential, lookup._tenant_id),
            endpoint=lookup._endpoint,
            client=lookup._client,
            sleep=lookup._sleep,
            tenant_id=lookup._tenant_id,
        )
        self._api_client_id = api_client_id
        self._admin_role = admin_role
        self._max_objects = min(max(max_objects, 1), 10000)

    async def _permissions(self, tenant_id: str, *, administrators: bool) -> None:
        if not self._lookup._tenant_id or tenant_id.casefold() != self._lookup._tenant_id:
            raise DirectoryForbiddenError(
                "Notification directory reads are restricted to this tenant"
            )
        permissions = await self._lookup.application_permissions()
        directory_read = "Directory.Read.All" in permissions
        if not directory_read and not permissions & {"User.ReadBasic.All", "User.Read.All"}:
            raise DirectoryForbiddenError(
                "Grant User.ReadBasic.All for directory-backed mailboxes."
            )
        if administrators and not ("Application.Read.All" in permissions or directory_read):
            raise DirectoryForbiddenError(
                "Grant Application.Read.All to discover API app-role admins."
            )
        if administrators and not (
            "GroupMember.Read.All" in permissions
            or "Group.Read.All" in permissions
            or directory_read
        ):
            raise DirectoryForbiddenError(
                "Grant GroupMember.Read.All to resolve direct users of app-role assigned groups."
            )

    async def _collection(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        next_url: str | None = path
        visited: set[str] = set()
        while next_url:
            if next_url in visited:
                raise DirectoryError("Microsoft Graph returned a repeated notification paging URL")
            visited.add(next_url)
            if next_url.startswith("https://"):
                expected = urlsplit(f"{self._lookup._endpoint}/v1.0/")
                actual = urlsplit(next_url)
                if (
                    actual.scheme != expected.scheme
                    or actual.netloc != expected.netloc
                    or not actual.path.startswith(expected.path)
                    or actual.fragment
                    or actual.username
                ):
                    raise DirectoryError(
                        "Microsoft Graph returned an unsafe notification paging URL"
                    )
            data = await self._lookup._json(
                "notification_recipients",
                "GET",
                next_url,
                params=params,
                permission="Application.Read.All, User.ReadBasic.All and GroupMember.Read.All",
            )
            params = None
            page = data.get("value")
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise DirectoryError(
                    "Microsoft Graph returned incomplete notification discovery data"
                )
            values.extend(page)
            if len(values) > self._max_objects:
                raise DirectoryError(
                    "Administrator discovery exceeded its limit; narrow the route."
                )
            raw_next = data.get("@odata.nextLink")
            if raw_next is not None and (
                not isinstance(raw_next, str) or not raw_next.startswith("https://")
            ):
                raise DirectoryError("Microsoft Graph returned an invalid notification paging URL")
            next_url = raw_next
        return values

    async def _contact(self, object_id: str) -> NotificationContact | None:
        data = await self._lookup._json(
            "notification_contact",
            "GET",
            f"/users/{_guid(object_id)}",
            params={"$select": "id,displayName,mail"},
            permission="User.ReadBasic.All",
        )
        if not data:
            return None
        if _guid(data.get("id")) != _guid(object_id):
            raise DirectoryError("Microsoft Graph returned a different notification user")
        if data.get("@odata.type", "#microsoft.graph.user") != "#microsoft.graph.user":
            return None
        mailbox = usable_mailbox(data.get("mail"))
        name = data.get("displayName")
        return NotificationContact(
            object_id=_guid(object_id),
            display_name=name[:256] if isinstance(name, str) else None,
            mailbox=mailbox,
            available=mailbox is not None,
            reason=None
            if mailbox
            else "No usable directory mailbox; sign-in UPN is not a mailbox.",
        )

    async def contact(self, tenant_id: str, object_id: str) -> NotificationContact:
        try:
            await self._permissions(tenant_id, administrators=False)
            contact = await self._contact(object_id)
            return contact or NotificationContact(
                object_id=object_id, reason="The directory user no longer exists."
            )
        except (DirectoryError, DirectoryForbiddenError, AzureError, TimeoutError) as exc:
            return NotificationContact(
                object_id=object_id, lookup_complete=False, reason=_safe_reason(exc)
            )

    async def discover_administrators(self, tenant_id: str) -> AdministratorDiscovery:
        return await self._discover(tenant_id)

    async def selected_administrators(
        self, tenant_id: str, object_ids: Sequence[str]
    ) -> AdministratorDiscovery:
        """Verify an explicit smaller audience without enumerating every assigned group member."""
        try:
            selected = {_guid(item) for item in object_ids}
        except DirectoryError as exc:
            return AdministratorDiscovery(reason=exc.message)
        return await self._discover(tenant_id, selected=selected)

    async def _discover(
        self, tenant_id: str, selected: set[str] | None = None
    ) -> AdministratorDiscovery:
        now = utc_now()
        try:
            await self._permissions(tenant_id, administrators=True)
            app_id = _guid(self._api_client_id)
            principals = await self._collection(
                "/servicePrincipals",
                params={
                    "$filter": f"appId eq '{app_id}'",
                    "$select": "id,appId,appRoles",
                },
            )
            if len(principals) != 1 or _guid(principals[0].get("appId")) != app_id:
                raise DirectoryError(
                    "The API enterprise application could not be uniquely resolved."
                )
            principal = principals[0]
            roles = principal.get("appRoles")
            if not isinstance(roles, list) or not all(isinstance(role, dict) for role in roles):
                raise DirectoryError("The API enterprise application's app roles are unavailable.")
            if any(not isinstance(role.get("allowedMemberTypes"), list) for role in roles):
                raise DirectoryError(
                    "The API enterprise application's app-role metadata is incomplete."
                )
            role_ids = {
                _guid(role.get("id"))
                for role in roles
                if role.get("value") == self._admin_role
                and role.get("isEnabled") is True
                and "User" in role.get("allowedMemberTypes", [])
            }
            if not role_ids:
                raise DirectoryError(
                    "The enabled API administrator app role could not be resolved."
                )
            assignments = await self._collection(
                f"/servicePrincipals/{_guid(principal.get('id'))}/appRoleAssignedTo",
                params={"$select": "principalId,principalType,appRoleId,resourceId"},
            )
            users: set[str] = set()
            groups: set[str] = set()
            for assignment in assignments:
                if _guid(assignment.get("resourceId")) != _guid(principal.get("id")):
                    raise DirectoryError("Microsoft Graph returned an assignment for another API.")
                if _guid(assignment.get("appRoleId")) not in role_ids:
                    continue
                principal_id = _guid(assignment.get("principalId"))
                kind = assignment.get("principalType")
                if kind == "User":
                    users.add(principal_id)
                elif kind == "Group":
                    groups.add(principal_id)
                elif kind != "ServicePrincipal":
                    raise DirectoryError(
                        "Microsoft Graph returned an unknown app-role principal type."
                    )
            for group_id in sorted(groups):
                # Enterprise-app group assignments do not propagate through nested groups.
                if selected is not None:
                    for user_id in sorted(selected - users):
                        member = await self._lookup._json(
                            "notification_selected_administrator",
                            "GET",
                            f"/groups/{group_id}/members/{user_id}",
                            permission="GroupMember.Read.All",
                            params={"$select": "id"},
                        )
                        if member and _guid(member.get("id")) == user_id:
                            users.add(user_id)
                    continue
                members = await self._collection(
                    f"/groups/{group_id}/members/microsoft.graph.user",
                    params={"$select": "id", "$top": "999"},
                )
                for member in members:
                    if (
                        member.get("@odata.type", "#microsoft.graph.user")
                        == "#microsoft.graph.user"
                    ):
                        users.add(_guid(member.get("id")))
                if len(users) > self._max_objects:
                    raise DirectoryError("Administrator discovery exceeded its limit.")
            contacts = []
            if selected is not None:
                users &= selected
            for user_id in sorted(users):
                contact = await self._contact(user_id)
                if contact is not None:
                    contacts.append(contact)
            return AdministratorDiscovery(
                administrators=contacts,
                complete=True,
                checked_at=now,
                fresh_until=now + timedelta(minutes=2),
                reason=("Graph is eventually consistent; changed assignments may replicate later."),
            )
        except (DirectoryError, DirectoryForbiddenError, AzureError, TimeoutError) as exc:
            # Never present successfully read pages as a complete all-admin roster.
            return AdministratorDiscovery(checked_at=now, reason=_safe_reason(exc))


def _safe_reason(exc: Exception) -> str:
    if isinstance(exc, (DirectoryError, DirectoryForbiddenError)):
        return exc.message[:1000]
    return "Microsoft Graph recipient discovery is unavailable; check API identity permissions."
