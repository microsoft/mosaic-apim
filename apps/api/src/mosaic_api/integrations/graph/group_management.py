"""Opt-in app-only group management, separate from the read-only DirectoryLookup.

The service must authorize the administrator and the center's bound tenant/group before calling
this adapter. A confirmed directory write does not prove propagation, token refresh, or key
revocation. Persist write intent before calling; client-request-id is tracing, NOT idempotency.

Direct-member paging deliberately uses Graph's documented beta workaround for the v1.0 service
principal omission. Beta is subject to change and not supported for production applications:
https://learn.microsoft.com/graph/api/group-list-members?view=graph-rest-1.0
https://learn.microsoft.com/graph/api/group-list-members?view=graph-rest-beta
All group creation, verification and membership writes use v1.0.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import uuid4

import httpx
import jwt
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError

from mosaic_api.domain import PrincipalKind
from mosaic_api.errors import DirectoryError, ValidationError
from mosaic_api.integrations.graph.client import _graph_retry_after
from mosaic_api.integrations.graph.membership import parse_object_id

_GROUP_SELECT = (
    "id,displayName,securityEnabled,mailEnabled,groupTypes,isAssignableToRole,"
    "onPremisesSyncEnabled,onPremisesSecurityIdentifier,membershipRule"
)
_READ_PERMISSIONS = frozenset(
    {
        "GroupMember.Read.All",
        "GroupMember.ReadWrite.All",
        "Group.Read.All",
        "Group.ReadWrite.All",
        "Directory.Read.All",
        "Directory.ReadWrite.All",
    }
)
_MEMBERSHIP_PERMISSIONS = frozenset(
    {"GroupMember.ReadWrite.All", "Group.ReadWrite.All", "Directory.ReadWrite.All"}
)
_CREATE_PERMISSIONS = frozenset({"Group.Create", "Group.ReadWrite.All", "Directory.ReadWrite.All"})
_USER_KINDS = frozenset({PrincipalKind.USER, PrincipalKind.AGENT_USER})
_WORKLOAD_KINDS = frozenset(
    {
        PrincipalKind.SERVICE_PRINCIPAL,
        PrincipalKind.MANAGED_IDENTITY,
        PrincipalKind.AGENT_IDENTITY,
    }
)
_MAIL_NICKNAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_REPLICATION_MESSAGE = (
    "the source resource object or one of the objects being referenced don't exist"
)


class GroupFailureKind(StrEnum):
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    REPLICATION_PENDING = "replication_pending"
    THROTTLED = "throttled"
    REJECTED = "rejected"
    UNSUPPORTED_GROUP = "unsupported_group"
    INVALID_RESPONSE = "invalid_response"
    UNAVAILABLE = "unavailable"
    UNKNOWN_OUTCOME = "unknown_outcome"


class GroupWriteOutcome(StrEnum):
    NOT_APPLIED = "not_applied"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


class GroupManagementError(DirectoryError):
    """Structured failure suitable for a durable directory-operation record.

    UNKNOWN must be reconciled, never interpreted as permission to repeat group creation.
    Graph's error code/message are diagnostic only, never evidence of a successful write.
    """

    code = "directory_group_management_failed"

    def __init__(
        self,
        message: str,
        *,
        kind: GroupFailureKind,
        outcome: GroupWriteOutcome = GroupWriteOutcome.NOT_APPLIED,
        http_status: int | None = None,
        graph_code: str | None = None,
        graph_message: str | None = None,
        request_id: str | None = None,
        client_request_id: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        self.kind = kind
        self.outcome = outcome
        self.http_status = http_status
        self.graph_code = graph_code
        self.graph_message = graph_message
        self.request_id = request_id
        self.client_request_id = client_request_id
        self.retry_after = retry_after
        self.status_code = {
            GroupFailureKind.FORBIDDEN: 403,
            GroupFailureKind.NOT_FOUND: 404,
            GroupFailureKind.THROTTLED: 429,
            GroupFailureKind.REJECTED: 422,
            GroupFailureKind.UNSUPPORTED_GROUP: 422,
            GroupFailureKind.REPLICATION_PENDING: 503,
        }.get(kind, 502)
        super().__init__(
            message,
            details={
                "kind": kind.value,
                "outcome": outcome.value,
                "httpStatus": http_status,
                "graphCode": graph_code,
                "graphMessage": graph_message,
                "requestId": request_id,
                "clientRequestId": client_request_id,
                "retryAfter": retry_after,
            },
        )


@dataclass(frozen=True)
class GraphGroup:
    tenant_id: str
    object_id: str
    display_name: str | None = None
    security_enabled: bool = True
    mail_enabled: bool = False
    group_types: tuple[str, ...] = ()
    is_assignable_to_role: bool | None = False
    on_premises_sync_enabled: bool | None = None
    on_premises_security_identifier: str | None = None
    membership_rule: str | None = None

    @property
    def management_unsupported_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.security_enabled:
            reasons.append("The group must be security-enabled")
        if self.mail_enabled:
            reasons.append("Mail-enabled groups are not supported")
        if self.group_types or self.membership_rule:
            reasons.append("Only ordinary assigned-membership security groups are supported")
        if self.is_assignable_to_role:
            reasons.append("Role-assignable groups are not supported")
        if self.on_premises_sync_enabled or self.on_premises_security_identifier:
            reasons.append(
                "On-premises-managed or previously synchronized groups are not supported"
            )
        return tuple(reasons)


@dataclass(frozen=True)
class GroupManagementCapabilities:
    tenant_id: str
    service_principal_object_id: str | None
    application_permissions: frozenset[str]
    can_read_groups: bool
    can_create_groups: bool
    can_add_users: bool
    can_add_service_principals: bool
    can_remove_members: bool
    # Token consent is not proof that Graph will allow a write to a particular group.
    advisory: bool = True
    member_listing_api: Literal["beta"] = "beta"


@dataclass(frozen=True)
class DirectGroupMember:
    object_id: str
    odata_type: str
    kind: PrincipalKind | None
    display_name: str | None = None
    detail: str | None = None
    app_id: str | None = None


@dataclass(frozen=True)
class GroupMemberPage:
    tenant_id: str
    group_object_id: str
    members: tuple[DirectGroupMember, ...]
    next_link: str | None = None
    api_version: Literal["beta"] = "beta"


@dataclass(frozen=True)
class GroupWriteResult:
    tenant_id: str
    group_object_id: str
    operation: Literal["create_group", "add_member", "remove_member"]
    member_object_id: str | None = None
    request_id: str | None = None
    client_request_id: str | None = None
    outcome: GroupWriteOutcome = GroupWriteOutcome.CONFIRMED


class GroupManagement(Protocol):
    @property
    def tenant_id(self) -> str: ...

    async def capabilities(self) -> GroupManagementCapabilities:
        """Inspect the API token only; never use destructive permission probes."""
        ...

    async def get_group(self, group_object_id: str) -> GraphGroup: ...

    async def validate_group(self, group_object_id: str) -> GraphGroup:
        """Read and reject unsupported group types before attaching or editing."""
        ...

    async def create_group(
        self,
        display_name: str,
        *,
        description: str | None = None,
        mail_nickname: str | None = None,
        request_id: str | None = None,
    ) -> GroupWriteResult:
        """Create with the API service principal as owner; result identity is NOT verified.

        Persist this identity and call validate_group separately. Never repeat an unknown create.
        No initial members or human owners are silently assigned.
        """
        ...

    async def list_members(
        self, group_object_id: str, *, page_size: int = 100, next_link: str | None = None
    ) -> GroupMemberPage:
        """One DIRECT page, including limited-info/unsupported objects without dropping rows.

        Follow next_link until absent for a roster; a page is not a complete membership proof
        or an atomic snapshot. Inherited members are deliberately not flattened into this editor.
        """
        ...

    async def add_member(
        self,
        group_object_id: str,
        member_object_id: str,
        *,
        member_kind: PrincipalKind,
        request_id: str | None = None,
    ) -> GroupWriteResult:
        """Add a user, supported workload, or security-group relationship.

        member_kind is a permission preflight hint from server-side directory lookup, not proof
        supplied by a browser. Graph still validates the actual object type and permissions.
        """
        ...

    async def remove_member(
        self, group_object_id: str, member_object_id: str, *, request_id: str | None = None
    ) -> GroupWriteResult: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class _ApplicationToken:
    token: AccessToken
    permissions: frozenset[str]
    object_id: str | None


class GraphGroupManagement:
    def __init__(
        self,
        credential: AsyncTokenCredential,
        *,
        endpoint: str,
        tenant_id: str,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._credential = credential
        self._endpoint = _endpoint(endpoint)
        self._tenant_id = _object_id(tenant_id, "Configured directory tenant")
        self._scope = f"{self._endpoint}/.default"
        self._client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self._owns_client = client is None
        self._sleep = sleep

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _application_token(self) -> _ApplicationToken:
        try:
            token = await self._credential.get_token(self._scope, tenant_id=self.tenant_id)
        except (AzureError, TimeoutError) as exc:
            raise GroupManagementError(
                "The API identity could not get a Microsoft Graph token",
                kind=GroupFailureKind.UNAVAILABLE,
            ) from exc
        # Inspect a token from the trusted server credential, never a browser-supplied token.
        try:
            claims = jwt.decode(token.token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise GroupManagementError(
                "Group management requires the API identity's app-only Graph token",
                kind=GroupFailureKind.FORBIDDEN,
            ) from exc
        roles = claims.get("roles")
        if roles is None and "roles" not in claims and claims.get("idtyp") == "app":
            roles = []
        if (
            parse_object_id(claims.get("tid")) != self.tenant_id
            or "scp" in claims
            or claims.get("idtyp") not in (None, "app")
            or not isinstance(roles, list)
            or not all(isinstance(role, str) and role for role in roles)
        ):
            raise GroupManagementError(
                "Group management requires the API identity's tenant-bound app-only Graph token",
                kind=GroupFailureKind.FORBIDDEN,
            )
        return _ApplicationToken(token, frozenset(roles), parse_object_id(claims.get("oid")))

    async def capabilities(self) -> GroupManagementCapabilities:
        identity = await self._application_token()
        return _capabilities(self.tenant_id, identity.permissions, identity.object_id)

    async def get_group(self, group_object_id: str) -> GraphGroup:
        group_id = _object_id(group_object_id, "Group object ID")
        response = await self._request(
            "GET",
            f"{self._endpoint}/v1.0/groups/{group_id}",
            permission=_READ_PERMISSIONS,
            permission_hint="GroupMember.Read.All",
            params={"$select": _GROUP_SELECT},
        )
        return _group_from_json(
            _response_json(response), tenant_id=self.tenant_id, group_id=group_id
        )

    async def validate_group(self, group_object_id: str) -> GraphGroup:
        group = await self.get_group(group_object_id)
        _validate_group(group)
        return group

    async def create_group(
        self,
        display_name: str,
        *,
        description: str | None = None,
        mail_nickname: str | None = None,
        request_id: str | None = None,
    ) -> GroupWriteResult:
        name, nickname = _create_input(display_name, description, mail_nickname)
        trace = _request_id(request_id)
        identity = await self._application_token()
        _require_permission(identity.permissions, _CREATE_PERMISSIONS, "Group.Create")
        if identity.object_id is None:
            raise GroupManagementError(
                "Group creation requires the API service-principal object ID in its Graph token",
                kind=GroupFailureKind.FORBIDDEN,
                client_request_id=trace,
            )
        body: dict[str, Any] = {
            "displayName": name,
            "mailNickname": nickname,
            "mailEnabled": False,
            "securityEnabled": True,
            "groupTypes": [],
            "owners@odata.bind": [f"{self._endpoint}/v1.0/servicePrincipals/{identity.object_id}"],
        }
        # Do not set isAssignableToRole: setting that property requires extra privileged consent.
        if description is not None:
            body["description"] = description
        response = await self._request(
            "POST",
            f"{self._endpoint}/v1.0/groups",
            permission=_CREATE_PERMISSIONS,
            permission_hint="Group.Create",
            body=body,
            expected_status=201,
            identity=identity,
            request_id=trace,
        )
        group_id = parse_object_id(_response_json(response, write=True).get("id"))
        if group_id is None:
            raise _invalid_response(response, write=True, client_request_id=trace)
        return GroupWriteResult(
            tenant_id=self.tenant_id,
            group_object_id=group_id,
            operation="create_group",
            request_id=_text(response.headers.get("request-id")),
            client_request_id=trace,
        )

    async def list_members(
        self, group_object_id: str, *, page_size: int = 100, next_link: str | None = None
    ) -> GroupMemberPage:
        group_id = _object_id(group_object_id, "Group object ID")
        _page_size(page_size)
        collection = f"{self._endpoint}/beta/groups/{group_id}/members"
        url = _next_link(next_link, collection) if next_link is not None else collection
        response = await self._request(
            "GET",
            url,
            permission=_READ_PERMISSIONS,
            permission_hint="GroupMember.Read.All",
            params=None if next_link is not None else {"$top": str(page_size)},
        )
        data = _response_json(response)
        values = data.get("value")
        if not isinstance(values, list) or len(values) > 999:
            raise _invalid_response(response)
        members = tuple(_member_from_json(item) for item in values)
        following = (
            _next_link(data["@odata.nextLink"], collection) if "@odata.nextLink" in data else None
        )
        if following is not None and following == str(response.request.url):
            raise GroupManagementError(
                "Microsoft Graph repeated a direct-membership paging URL",
                kind=GroupFailureKind.INVALID_RESPONSE,
            )
        return GroupMemberPage(self.tenant_id, group_id, members, following)

    async def add_member(
        self,
        group_object_id: str,
        member_object_id: str,
        *,
        member_kind: PrincipalKind,
        request_id: str | None = None,
    ) -> GroupWriteResult:
        group_id = _object_id(group_object_id, "Group object ID")
        member_id = _object_id(member_object_id, "Member object ID")
        _member_kind(member_kind)
        trace = _request_id(request_id)
        identity = await self._application_token()
        _require_membership_permission(identity.permissions, member_kind)
        await self.validate_group(group_id)
        response = await self._request(
            "POST",
            f"{self._endpoint}/v1.0/groups/{group_id}/members/$ref",
            permission=_MEMBERSHIP_PERMISSIONS,
            permission_hint="GroupMember.ReadWrite.All",
            body={"@odata.id": f"{self._endpoint}/v1.0/directoryObjects/{member_id}"},
            expected_status=204,
            identity=identity,
            request_id=trace,
        )
        return GroupWriteResult(
            self.tenant_id,
            group_id,
            "add_member",
            member_id,
            _text(response.headers.get("request-id")),
            trace,
        )

    async def remove_member(
        self, group_object_id: str, member_object_id: str, *, request_id: str | None = None
    ) -> GroupWriteResult:
        group_id = _object_id(group_object_id, "Group object ID")
        member_id = _object_id(member_object_id, "Member object ID")
        trace = _request_id(request_id)
        identity = await self._application_token()
        _require_permission(
            identity.permissions, _MEMBERSHIP_PERMISSIONS, "GroupMember.ReadWrite.All"
        )
        await self.validate_group(group_id)
        response = await self._request(
            "DELETE",
            f"{self._endpoint}/v1.0/groups/{group_id}/members/{member_id}/$ref",
            permission=_MEMBERSHIP_PERMISSIONS,
            permission_hint="GroupMember.ReadWrite.All",
            expected_status=204,
            identity=identity,
            request_id=trace,
        )
        return GroupWriteResult(
            self.tenant_id,
            group_id,
            "remove_member",
            member_id,
            _text(response.headers.get("request-id")),
            trace,
        )

    async def _request(
        self,
        method: str,
        url: str,
        *,
        permission: frozenset[str],
        permission_hint: str,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        expected_status: int = 200,
        identity: _ApplicationToken | None = None,
        request_id: str | None = None,
    ) -> httpx.Response:
        identity = identity or await self._application_token()
        _require_permission(identity.permissions, permission, permission_hint)
        trace = _request_id(request_id)
        headers = {
            "Authorization": f"Bearer {identity.token.token}",
            "Accept": "application/json",
            "client-request-id": trace,
            "return-client-request-id": "true",
        }
        write = method != "GET"
        for attempt in range(2):
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    json=body,
                    timeout=10,
                    follow_redirects=False,
                )
            except (httpx.HTTPError, TimeoutError) as exc:
                raise GroupManagementError(
                    "Microsoft Graph write outcome is unknown; reconcile before retrying"
                    if write
                    else "Microsoft Graph could not be reached",
                    kind=(
                        GroupFailureKind.UNKNOWN_OUTCOME if write else GroupFailureKind.UNAVAILABLE
                    ),
                    outcome=GroupWriteOutcome.UNKNOWN if write else GroupWriteOutcome.NOT_APPLIED,
                    client_request_id=trace,
                ) from exc
            if response.status_code == expected_status:
                return response
            delay = _graph_retry_after(response)
            # A 429 rejects this attempt. A 5xx write may already have taken effect.
            if attempt == 0 and (
                (response.status_code == 429 and delay <= 5)
                or (not write and response.status_code == 503 and delay <= 5)
            ):
                await self._sleep(delay)
                continue
            raise _response_error(response, write=write, client_request_id=trace)
        raise AssertionError("Unreachable retry state")


def _capabilities(
    tenant_id: str, permissions: frozenset[str], object_id: str | None
) -> GroupManagementCapabilities:
    membership = bool(permissions & _MEMBERSHIP_PERMISSIONS)
    return GroupManagementCapabilities(
        tenant_id=tenant_id,
        service_principal_object_id=object_id,
        application_permissions=permissions,
        can_read_groups=bool(permissions & _READ_PERMISSIONS),
        can_create_groups=bool(permissions & _CREATE_PERMISSIONS) and object_id is not None,
        can_add_users=membership,
        can_add_service_principals=membership and "Application.ReadWrite.All" in permissions,
        can_remove_members=membership,
    )


def _require_permission(permissions: frozenset[str], accepted: frozenset[str], hint: str) -> None:
    if not permissions & accepted:
        raise GroupManagementError(
            f"The API identity needs explicitly consented {hint} application permission",
            kind=GroupFailureKind.FORBIDDEN,
        )


def _require_membership_permission(permissions: frozenset[str], kind: PrincipalKind) -> None:
    # Current Graph contract: adding a service principal needs this additional broad permission.
    # https://learn.microsoft.com/graph/api/group-post-members?view=graph-rest-1.0
    _require_permission(permissions, _MEMBERSHIP_PERMISSIONS, "GroupMember.ReadWrite.All")
    if kind in _WORKLOAD_KINDS:
        _require_permission(
            permissions, frozenset({"Application.ReadWrite.All"}), "Application.ReadWrite.All"
        )


def _member_kind(kind: PrincipalKind) -> None:
    if kind not in _USER_KINDS | _WORKLOAD_KINDS | {PrincipalKind.SECURITY_GROUP}:
        raise ValidationError(
            "This editor only adds users, supported service principals and security groups"
        )


def _validate_group(group: GraphGroup) -> None:
    if group.management_unsupported_reasons:
        raise GroupManagementError(
            "; ".join(group.management_unsupported_reasons),
            kind=GroupFailureKind.UNSUPPORTED_GROUP,
        )


def _object_id(value: str, label: str) -> str:
    normalized = parse_object_id(value)
    if normalized is None:
        raise ValidationError(f"{label} must be a nonzero GUID")
    return normalized


def _request_id(value: str | None) -> str:
    return _object_id(value, "Client request ID") if value is not None else str(uuid4())


def _endpoint(value: str) -> str:
    if not value or value.strip() != value or any(ord(char) < 33 for char in value):
        raise ValidationError("Graph endpoint must be a configured HTTPS origin")
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise ValidationError("Graph endpoint must be a configured HTTPS origin") from exc
    if (
        url.scheme != "https"
        or not url.host
        or url.path not in ("", "/")
        or url.username
        or url.password
        or url.port not in (None, 443)
        or url.query
        or url.fragment
    ):
        raise ValidationError("Graph endpoint must be a configured HTTPS origin")
    return str(url).rstrip("/")


def _page_size(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 999:
        raise ValidationError("Direct membership page size must be between 1 and 999")


def _next_link(value: object, collection: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or any(ord(char) < 33 for char in value)
    ):
        raise GroupManagementError(
            "Microsoft Graph returned an invalid direct-membership paging URL",
            kind=GroupFailureKind.INVALID_RESPONSE,
        )
    try:
        candidate, expected = httpx.URL(value), httpx.URL(collection)
    except httpx.InvalidURL as exc:
        raise GroupManagementError(
            "Microsoft Graph returned an invalid direct-membership paging URL",
            kind=GroupFailureKind.INVALID_RESPONSE,
        ) from exc
    if (
        candidate.scheme != "https"
        or candidate.host != expected.host
        or candidate.port != expected.port
        or candidate.path != expected.path
        or candidate.username
        or candidate.password
        or candidate.fragment
        or not candidate.query
        or candidate.raw_path.split(b"?")[0] != expected.raw_path.split(b"?")[0]
    ):
        raise GroupManagementError(
            "Microsoft Graph returned an unexpected direct-membership paging URL",
            kind=GroupFailureKind.INVALID_RESPONSE,
        )
    return str(candidate)


def _create_input(
    display_name: str, description: str | None, mail_nickname: str | None
) -> tuple[str, str]:
    if (
        not isinstance(display_name, str)
        or not display_name.strip()
        or len(display_name) > 256
        or any(ord(char) < 32 for char in display_name)
    ):
        raise ValidationError("Group display name must be nonempty and at most 256 characters")
    if description is not None and (not isinstance(description, str) or len(description) > 1024):
        raise ValidationError("Group description must be at most 1024 characters")
    nickname = mail_nickname if mail_nickname is not None else f"mosaic-{uuid4().hex}"
    if not isinstance(nickname, str) or not _MAIL_NICKNAME.fullmatch(nickname):
        raise ValidationError("Group mail nickname must contain 1-64 letters, digits, '-' or '_'")
    return display_name.strip(), nickname


def _group_from_json(data: dict[str, Any], *, tenant_id: str, group_id: str) -> GraphGroup:
    nullable_bools = ("isAssignableToRole", "onPremisesSyncEnabled")
    nullable_text = ("onPremisesSecurityIdentifier", "membershipRule")
    if (
        parse_object_id(data.get("id")) != group_id
        or not isinstance(data.get("securityEnabled"), bool)
        or not isinstance(data.get("mailEnabled"), bool)
        or not isinstance(data.get("groupTypes"), list)
        or not all(isinstance(item, str) for item in data["groupTypes"])
        or any(
            key not in data or (data[key] is not None and not isinstance(data[key], bool))
            for key in nullable_bools
        )
        or any(
            key not in data or (data[key] is not None and not isinstance(data[key], str))
            for key in nullable_text
        )
    ):
        raise GroupManagementError(
            "Microsoft Graph did not return verifiable group management properties",
            kind=GroupFailureKind.INVALID_RESPONSE,
        )
    return GraphGroup(
        tenant_id,
        group_id,
        _text(data.get("displayName")),
        data["securityEnabled"],
        data["mailEnabled"],
        tuple(data["groupTypes"]),
        data["isAssignableToRole"],
        data["onPremisesSyncEnabled"],
        data["onPremisesSecurityIdentifier"],
        data["membershipRule"],
    )


def _member_from_json(value: Any) -> DirectGroupMember:
    member_id = parse_object_id(value.get("id")) if isinstance(value, dict) else None
    odata_type = value.get("@odata.type") if isinstance(value, dict) else None
    if (
        member_id is None
        or not isinstance(odata_type, str)
        or not odata_type.startswith("#microsoft.graph.")
    ):
        raise GroupManagementError(
            "Microsoft Graph returned an invalid direct member",
            kind=GroupFailureKind.INVALID_RESPONSE,
        )
    kinds = {
        "#microsoft.graph.user": PrincipalKind.USER,
        "#microsoft.graph.agentuser": PrincipalKind.AGENT_USER,
        "#microsoft.graph.serviceprincipal": PrincipalKind.SERVICE_PRINCIPAL,
        "#microsoft.graph.agentidentity": PrincipalKind.AGENT_IDENTITY,
        "#microsoft.graph.group": PrincipalKind.SECURITY_GROUP,
    }
    kind = kinds.get(odata_type.casefold())
    if (
        kind == PrincipalKind.SERVICE_PRINCIPAL
        and value.get("servicePrincipalType") == "ManagedIdentity"
    ):
        kind = PrincipalKind.MANAGED_IDENTITY
    return DirectGroupMember(
        member_id,
        odata_type,
        kind,
        _text(value.get("displayName")),
        _text(value.get("userPrincipalName")) or _text(value.get("mail")),
        _text(value.get("appId")),
    )


def _text(value: object) -> str | None:
    return value[:1024] if isinstance(value, str) and value else None


def _response_json(response: httpx.Response, *, write: bool = False) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise _invalid_response(response, write=write) from exc
    if not isinstance(data, dict):
        raise _invalid_response(response, write=write)
    return data


def _invalid_response(
    response: httpx.Response, *, write: bool = False, client_request_id: str | None = None
) -> GroupManagementError:
    return GroupManagementError(
        "Microsoft Graph returned an unverifiable write result; reconcile before retrying"
        if write
        else "Microsoft Graph returned malformed directory data",
        kind=GroupFailureKind.UNKNOWN_OUTCOME if write else GroupFailureKind.INVALID_RESPONSE,
        outcome=GroupWriteOutcome.UNKNOWN if write else GroupWriteOutcome.NOT_APPLIED,
        http_status=response.status_code,
        request_id=_text(response.headers.get("request-id")),
        client_request_id=client_request_id or response.request.headers.get("client-request-id"),
    )


def _response_error(
    response: httpx.Response, *, write: bool, client_request_id: str
) -> GroupManagementError:
    try:
        data = response.json()
    except ValueError:
        data = {}
    error = data.get("error", {}) if isinstance(data, dict) else {}
    graph_code = _text(error.get("code")) if isinstance(error, dict) else None
    graph_message = _text(error.get("message")) if isinstance(error, dict) else None
    status = response.status_code
    kind = GroupFailureKind.REJECTED
    outcome = GroupWriteOutcome.NOT_APPLIED
    retry_after = None
    if status in {401, 403}:
        kind = GroupFailureKind.FORBIDDEN
    elif status == 404:
        kind = GroupFailureKind.NOT_FOUND
    elif status == 429:
        kind, retry_after = GroupFailureKind.THROTTLED, _graph_retry_after(response)
    elif status == 400 and graph_message and _REPLICATION_MESSAGE in graph_message.casefold():
        kind, retry_after = GroupFailureKind.REPLICATION_PENDING, 2
    elif write and (status >= 500 or status == 408 or 200 <= status < 300):
        kind, outcome = GroupFailureKind.UNKNOWN_OUTCOME, GroupWriteOutcome.UNKNOWN
    elif status >= 500 or status == 408:
        kind = GroupFailureKind.UNAVAILABLE
    return GroupManagementError(
        "Microsoft Graph group operation failed"
        if outcome != GroupWriteOutcome.UNKNOWN
        else "Microsoft Graph write outcome is unknown; reconcile before retrying",
        kind=kind,
        outcome=outcome,
        http_status=status,
        graph_code=graph_code,
        graph_message=graph_message,
        request_id=_text(response.headers.get("request-id")),
        client_request_id=client_request_id,
        retry_after=retry_after,
    )
