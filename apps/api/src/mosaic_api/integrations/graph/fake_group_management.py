"""Fictional management adapter; never a fallback for a failing real Graph client.

Construct FakeDirectoryLookup with a fictional tenant GUID and explicit application_permissions,
then pass that same lookup to FakeGroupManagement. Both use lookup.state, so get_object,
member_groups and complete eligibility verification immediately observe actual fake writes.
Existing fictional security-group fixtures default to ordinary assigned cloud groups; supply
GraphGroup metadata to exercise unsupported group types. Production DI must choose one adapter,
not catch a Graph failure and switch to this one.
"""

from collections.abc import Sequence
from dataclasses import replace
from uuid import uuid4

import httpx

from mosaic_api.domain import DirectoryObject, PrincipalKind
from mosaic_api.errors import DirectoryError, DirectoryForbiddenError, ValidationError
from mosaic_api.integrations.graph.fake import FakeDirectoryLookup
from mosaic_api.integrations.graph.group_management import (
    _CREATE_PERMISSIONS,
    _MEMBERSHIP_PERMISSIONS,
    _READ_PERMISSIONS,
    DirectGroupMember,
    GraphGroup,
    GroupFailureKind,
    GroupManagementCapabilities,
    GroupManagementError,
    GroupMemberPage,
    GroupWriteResult,
    _capabilities,
    _create_input,
    _endpoint,
    _member_kind,
    _next_link,
    _object_id,
    _page_size,
    _request_id,
    _require_membership_permission,
    _require_permission,
    _validate_group,
)


class FakeGroupManagement:
    def __init__(
        self,
        directory: FakeDirectoryLookup,
        *,
        groups: Sequence[GraphGroup] = (),
        service_principal_object_id: str | None = "00000000-0000-0000-0000-000000000101",
        endpoint: str = "https://graph.microsoft.com",
    ) -> None:
        self.directory = directory
        self._tenant_id = _object_id(directory.tenant_id, "Configured directory tenant")
        self._endpoint = _endpoint(endpoint)
        self._object_id = (
            _object_id(service_principal_object_id, "API service-principal object ID")
            if service_principal_object_id is not None
            else None
        )
        self._groups = {
            object_id: GraphGroup(self.tenant_id, object_id, item.display_name)
            for object_id, item in directory.state.objects.items()
            if item.kind == PrincipalKind.SECURITY_GROUP
        }
        for group in groups:
            if _object_id(group.tenant_id, "Group tenant") != self.tenant_id:
                raise ValidationError("Fake groups must belong to the configured directory tenant")
            group_id = _object_id(group.object_id, "Group object ID")
            self._groups[group_id] = replace(group, tenant_id=self.tenant_id, object_id=group_id)
            self.directory.state.objects[group_id] = DirectoryObject(
                object_id=group_id,
                kind=PrincipalKind.SECURITY_GROUP,
                display_name=group.display_name,
            )
        self.owners: dict[str, tuple[str, ...]] = {}
        self.writes: list[GroupWriteResult] = []
        self.failures: dict[str, GroupManagementError] = {}
        self.closed = False

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    async def close(self) -> None:
        self.closed = True

    async def _permissions(self, operation: str) -> frozenset[str]:
        if operation in self.failures:
            raise self.failures[operation]
        try:
            return await self.directory.application_permissions()
        except DirectoryForbiddenError as exc:
            raise GroupManagementError(exc.message, kind=GroupFailureKind.FORBIDDEN) from exc
        except DirectoryError as exc:
            raise GroupManagementError(exc.message, kind=GroupFailureKind.UNAVAILABLE) from exc

    async def capabilities(self) -> GroupManagementCapabilities:
        return _capabilities(
            self.tenant_id, await self._permissions("capabilities"), self._object_id
        )

    async def get_group(self, group_object_id: str) -> GraphGroup:
        group_id = _object_id(group_object_id, "Group object ID")
        _require_permission(
            await self._permissions("get_group"), _READ_PERMISSIONS, "GroupMember.Read.All"
        )
        group = self._groups.get(group_id)
        if group is None:
            raise GroupManagementError(
                "Microsoft Graph could not find the fictional group",
                kind=GroupFailureKind.NOT_FOUND,
                http_status=404,
            )
        return group

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
        name, _ = _create_input(display_name, description, mail_nickname)
        trace = _request_id(request_id)
        _require_permission(
            await self._permissions("create_group"), _CREATE_PERMISSIONS, "Group.Create"
        )
        if self._object_id is None:
            raise GroupManagementError(
                "Group creation requires the API service-principal object ID",
                kind=GroupFailureKind.FORBIDDEN,
            )
        group_id = str(uuid4())
        self._groups[group_id] = GraphGroup(self.tenant_id, group_id, name)
        self.directory.state.objects[group_id] = DirectoryObject(
            object_id=group_id, kind=PrincipalKind.SECURITY_GROUP, display_name=name
        )
        self.directory.state.group_members[group_id] = []
        self.owners[group_id] = (self._object_id,)
        result = GroupWriteResult(self.tenant_id, group_id, "create_group", client_request_id=trace)
        self.writes.append(result)
        return result

    async def list_members(
        self, group_object_id: str, *, page_size: int = 100, next_link: str | None = None
    ) -> GroupMemberPage:
        group_id = _object_id(group_object_id, "Group object ID")
        _page_size(page_size)
        await self._permissions("list_members")
        await self.get_group(group_id)
        collection = f"{self._endpoint}/beta/groups/{group_id}/members"
        offset = 0
        if next_link is not None:
            url = httpx.URL(_next_link(next_link, collection))
            try:
                if set(url.params) != {"$skiptoken", "$top"}:
                    raise ValueError("Invalid fictional cursor")
                offset, page_size = int(url.params["$skiptoken"]), int(url.params["$top"])
                _page_size(page_size)
                if offset < 0:
                    raise ValueError("Invalid fictional offset")
            except (ValueError, KeyError) as exc:
                raise ValidationError("Invalid fictional membership paging cursor") from exc
        member_ids = sorted(set(self.directory.state.group_members.get(group_id, [])))
        members = tuple(
            self._member(member_id) for member_id in member_ids[offset : offset + page_size]
        )
        following = (
            str(httpx.URL(collection, params={"$skiptoken": offset + page_size, "$top": page_size}))
            if offset + page_size < len(member_ids)
            else None
        )
        return GroupMemberPage(self.tenant_id, group_id, members, following)

    def _member(self, member_id: str) -> DirectGroupMember:
        normalized = _object_id(member_id, "Member object ID")
        item = self.directory.state.objects.get(normalized)
        if item is None:
            return DirectGroupMember(normalized, "#microsoft.graph.directoryObject", None)
        types = {
            PrincipalKind.USER: "user",
            PrincipalKind.AGENT_USER: "agentUser",
            PrincipalKind.SERVICE_PRINCIPAL: "servicePrincipal",
            PrincipalKind.MANAGED_IDENTITY: "servicePrincipal",
            PrincipalKind.AGENT_IDENTITY: "agentIdentity",
            PrincipalKind.SECURITY_GROUP: "group",
        }
        return DirectGroupMember(
            normalized,
            f"#microsoft.graph.{types[item.kind]}",
            item.kind,
            item.display_name,
            item.detail,
            item.app_id,
        )

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
        permissions = await self._permissions("add_member")
        _require_membership_permission(permissions, member_kind)
        await self.validate_group(group_id)
        item = self.directory.state.objects.get(member_id)
        if item is None:
            raise GroupManagementError(
                "Microsoft Graph could not find the fictional member",
                kind=GroupFailureKind.NOT_FOUND,
                http_status=404,
            )
        _member_kind(item.kind)
        _require_membership_permission(permissions, item.kind)
        if item.kind == PrincipalKind.SECURITY_GROUP and (
            member_id == group_id or await self.directory.member_groups(group_id, [member_id])
        ):
            raise GroupManagementError(
                "The fictional group membership would create a cycle",
                kind=GroupFailureKind.REJECTED,
                http_status=400,
            )
        members = self.directory.state.group_members.setdefault(group_id, [])
        if member_id in members:
            raise GroupManagementError(
                "The fictional object is already a direct member",
                kind=GroupFailureKind.REJECTED,
                http_status=400,
            )
        members.append(member_id)
        result = GroupWriteResult(
            self.tenant_id, group_id, "add_member", member_id, client_request_id=trace
        )
        self.writes.append(result)
        return result

    async def remove_member(
        self, group_object_id: str, member_object_id: str, *, request_id: str | None = None
    ) -> GroupWriteResult:
        group_id = _object_id(group_object_id, "Group object ID")
        member_id = _object_id(member_object_id, "Member object ID")
        trace = _request_id(request_id)
        _require_permission(
            await self._permissions("remove_member"),
            _MEMBERSHIP_PERMISSIONS,
            "GroupMember.ReadWrite.All",
        )
        await self.validate_group(group_id)
        members = self.directory.state.group_members.get(group_id, [])
        if member_id not in members:
            raise GroupManagementError(
                "The fictional direct membership does not exist",
                kind=GroupFailureKind.NOT_FOUND,
                http_status=404,
            )
        members.remove(member_id)
        result = GroupWriteResult(
            self.tenant_id, group_id, "remove_member", member_id, client_request_id=trace
        )
        self.writes.append(result)
        return result
