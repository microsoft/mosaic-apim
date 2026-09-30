import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
import structlog
from azure.core.credentials_async import AsyncTokenCredential

from mosaic_api.domain import (
    DirectoryMemberPage,
    DirectoryObject,
    DirectorySearchKind,
    PrincipalKind,
)
from mosaic_api.errors import DirectoryError, DirectoryForbiddenError, ValidationError

logger = structlog.get_logger()

_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_MAX_TEXT = 256


class _GraphNotFound(Exception):
    pass


Sleep = Callable[[float], Awaitable[None]]


class GraphDirectoryLookup:
    def __init__(
        self,
        credential: AsyncTokenCredential,
        *,
        endpoint: str,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._credential = credential
        self._endpoint = str(endpoint).rstrip("/")
        self._scope = f"{self._endpoint}/.default"
        self._client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self._owns_client = client is None
        self._sleep = sleep

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(
        self, kind: DirectorySearchKind, query: str, *, limit: int = 20
    ) -> list[DirectoryObject]:
        limit = min(max(limit, 1), 25)
        term = query.strip()
        if not term:
            raise ValidationError("Directory search query cannot be empty")
        term = term[:120]
        if _is_guid(term):
            item = await self.get_object(term)
            if item and _matches_search_kind(kind, item.kind):
                return [item]
            return []
        if kind == DirectorySearchKind.USER:
            users, agent_users = await asyncio.gather(
                self._search_users(term, limit),
                self._search_agent_users(term, limit),
            )
            return _dedupe(agent_users + users)[:limit]
        if kind == DirectorySearchKind.GROUP:
            return (await self._search_groups(term, limit))[:limit]
        if kind == DirectorySearchKind.AGENT:
            agent_identities, agent_users = await asyncio.gather(
                self._search_agent_identities(term, limit),
                self._search_agent_users(term, limit),
            )
            return _dedupe(agent_identities + agent_users)[:limit]
        raise ValidationError("Unsupported directory search kind")

    async def get_object(self, object_id: str) -> DirectoryObject | None:
        if not _is_guid(object_id):
            return None
        normalized = object_id.lower()
        try:
            user = await self._get_user(normalized)
            if user:
                return user
        except _GraphNotFound:
            pass
        try:
            group = await self._get_group(normalized)
            if group:
                return group
        except _GraphNotFound:
            pass
        try:
            return await self._get_agent_identity(normalized)
        except _GraphNotFound:
            return None

    async def member_groups(self, object_id: str, group_object_ids: Sequence[str]) -> set[str]:
        wanted = [item.lower() for item in group_object_ids if _is_guid(item)]
        if not wanted or not _is_guid(object_id):
            return set()
        item = await self.get_object(object_id)
        if item and item.kind in {PrincipalKind.USER, PrincipalKind.AGENT_USER}:
            found: set[str] = set()
            for batch_start in range(0, len(wanted), 20):
                batch = wanted[batch_start : batch_start + 20]
                response = await self._json(
                    "member_groups",
                    "POST",
                    f"/users/{object_id.lower()}/checkMemberGroups",
                    permission="User.ReadBasic.All and GroupMember.Read.All",
                    json={"groupIds": batch},
                )
                values = response.get("value", [])
                if isinstance(values, list):
                    found.update(str(value).lower() for value in values)
            return found & set(wanted)
        checks = await asyncio.gather(
            *(self._group_contains_member(group_id, object_id.lower()) for group_id in wanted)
        )
        return {group_id for group_id, contains in zip(wanted, checks, strict=True) if contains}

    async def group_members(
        self, group_object_id: str, *, limit: int = 200
    ) -> DirectoryMemberPage:
        if not _is_guid(group_object_id):
            raise ValidationError("Group object ID must be a GUID")
        capped_limit = max(limit, 0)
        members: list[DirectoryObject] = []
        truncated = False
        next_url: str | None = (
            f"{self._endpoint}/v1.0/groups/{group_object_id.lower()}/transitiveMembers"
        )
        params: dict[str, str | int] | None = {
            "$select": "id,displayName,userPrincipalName,mail,appId,servicePrincipalType,"
            "securityEnabled",
            "$top": min(max(capped_limit, 1), 999),
        }
        while next_url and len(members) < capped_limit:
            data = await self._json(
                "group_members",
                "GET",
                next_url,
                permission="GroupMember.Read.All",
                params=params,
            )
            params = None
            for raw in _items(data):
                member = _object_from_member(raw)
                if member:
                    members.append(member)
                    if len(members) >= capped_limit:
                        break
            raw_next = data.get("@odata.nextLink")
            next_url = str(raw_next) if raw_next else None
            if next_url and not next_url.startswith(self._endpoint):
                raise DirectoryError("Microsoft Graph returned an unexpected paging URL")
            truncated = bool(next_url and len(members) >= capped_limit)
        return DirectoryMemberPage(
            group_object_id=group_object_id.lower(),
            members=members,
            truncated=truncated,
        )

    async def _search_users(self, query: str, limit: int) -> list[DirectoryObject]:
        data = await self._search_collection(
            "search_users",
            "/users",
            query,
            limit,
            permission="User.ReadBasic.All",
            select="id,displayName,userPrincipalName,mail",
        )
        return [_object_from_user(item, agent_user=False) for item in _items(data)]

    async def _search_agent_users(self, query: str, limit: int) -> list[DirectoryObject]:
        data = await self._search_collection(
            "search_agent_users",
            "/users/microsoft.graph.agentUser",
            query,
            limit,
            permission="User.ReadBasic.All",
            select="id,displayName,userPrincipalName,mail,identityParentId",
        )
        return [_object_from_user(item, agent_user=True) for item in _items(data)]

    async def _search_groups(self, query: str, limit: int) -> list[DirectoryObject]:
        data = await self._search_collection(
            "search_groups",
            "/groups",
            query,
            limit,
            permission="GroupMember.Read.All",
            select="id,displayName,mailNickname,description,securityEnabled",
            extra={"$filter": "securityEnabled eq true"},
        )
        return [
            item
            for raw in _items(data)
            if (item := _object_from_group(raw, validate_security=False)) is not None
        ]

    async def _search_agent_identities(self, query: str, limit: int) -> list[DirectoryObject]:
        data = await self._search_collection(
            "search_agent_identities",
            "/servicePrincipals/microsoft.graph.agentIdentity",
            query,
            limit,
            permission="AgentIdentity.Read.All",
            select="id,displayName,appId,agentIdentityBlueprintId",
        )
        return [_object_from_agent_identity(item) for item in _items(data)]

    async def _search_collection(
        self,
        operation: str,
        path: str,
        query: str,
        limit: int,
        *,
        permission: str,
        select: str,
        extra: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str | int | bool] = {
            "$search": _search_expression(query),
            "$count": "true",
            "$top": limit,
            "$select": select,
        }
        if extra:
            params.update(extra)
        return await self._json(
            operation,
            "GET",
            path,
            permission=permission,
            params=params,
            headers={"ConsistencyLevel": "eventual"},
        )

    async def _get_user(self, object_id: str) -> DirectoryObject | None:
        data = await self._json(
            "get_user",
            "GET",
            f"/users/{object_id}",
            permission="User.ReadBasic.All",
            params={"$select": "id,displayName,userPrincipalName,mail"},
            not_found_none=False,
        )
        odata_type = str(data.get("@odata.type", "")).casefold()
        if odata_type.endswith("agentuser"):
            return await self._get_agent_user(object_id, fallback=data)
        return _object_from_user(data, agent_user=False)

    async def _get_agent_user(
        self, object_id: str, *, fallback: dict[str, Any] | None = None
    ) -> DirectoryObject:
        try:
            data = await self._json(
                "get_agent_user",
                "GET",
                f"/users/{object_id}/microsoft.graph.agentUser",
                permission="User.ReadBasic.All",
                params={"$select": "id,displayName,userPrincipalName,mail,identityParentId"},
                not_found_none=False,
            )
        except _GraphNotFound:
            data = fallback or {}
        return _object_from_user(data, agent_user=True)

    async def _get_group(self, object_id: str) -> DirectoryObject | None:
        data = await self._json(
            "get_group",
            "GET",
            f"/groups/{object_id}",
            permission="GroupMember.Read.All",
            params={"$select": "id,displayName,mailNickname,description,securityEnabled"},
            not_found_none=False,
        )
        return _object_from_group(data, validate_security=True)

    async def _get_agent_identity(self, object_id: str) -> DirectoryObject:
        data = await self._json(
            "get_agent_identity",
            "GET",
            f"/servicePrincipals/{object_id}/microsoft.graph.agentIdentity",
            permission="AgentIdentity.Read.All",
            params={"$select": "id,displayName,appId,agentIdentityBlueprintId"},
            not_found_none=False,
        )
        return _object_from_agent_identity(data)

    async def _group_contains_member(self, group_id: str, object_id: str) -> bool:
        try:
            data = await self._json(
                "group_member_check",
                "GET",
                f"/groups/{group_id}/transitiveMembers",
                permission="GroupMember.Read.All",
                params={"$select": "id", "$filter": f"id eq '{object_id}'", "$top": 1},
                headers={"ConsistencyLevel": "eventual"},
                fallback_on_bad_request=True,
            )
            return any(str(item.get("id", "")).casefold() == object_id for item in _items(data))
        except DirectoryError:
            page = await self.group_members(group_id, limit=5000)
            return any(member.object_id.casefold() == object_id for member in page.members)

    async def _json(
        self,
        operation: str,
        method: str,
        url: str,
        *,
        permission: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        not_found_none: bool = True,
        fallback_on_bad_request: bool = False,
    ) -> dict[str, Any]:
        request_headers = dict(headers or {})
        token = await self._credential.get_token(self._scope)
        request_headers["Authorization"] = f"Bearer {token.token}"
        request_url = url if url.startswith("https://") else f"{self._endpoint}/v1.0{url}"
        for attempt in range(2):
            try:
                response = await self._client.request(
                    method,
                    request_url,
                    params=params,
                    headers=request_headers,
                    json=json,
                )
            except httpx.HTTPError as exc:
                raise DirectoryError("Microsoft Graph could not be reached") from exc
            if response.status_code in {429, 503} and attempt == 0:
                await self._sleep(_retry_after(response))
                continue
            if response.status_code in {401, 403}:
                _log_graph_status(operation, response)
                raise DirectoryForbiddenError(
                    f"Microsoft Graph refused the directory read; grant {permission}."
                )
            if response.status_code == 404:
                _log_graph_status(operation, response)
                if not_found_none:
                    return {}
                raise _GraphNotFound()
            if response.status_code == 400 and fallback_on_bad_request:
                _log_graph_status(operation, response)
                raise DirectoryError("Microsoft Graph did not accept the optimized directory read")
            if response.is_error:
                _log_graph_status(operation, response)
                raise DirectoryError("Microsoft Graph directory read failed")
            try:
                value = response.json()
            except ValueError as exc:
                _log_graph_status(operation, response)
                raise DirectoryError("Microsoft Graph returned malformed JSON") from exc
            if not isinstance(value, dict):
                raise DirectoryError("Microsoft Graph returned an unexpected response")
            return value
        raise DirectoryError("Microsoft Graph directory read failed after retry")


def _is_guid(value: str) -> bool:
    return bool(_GUID_RE.match(value.strip()))


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:_MAX_TEXT] if text else None


def _items(data: dict[str, Any]) -> list[dict[str, Any]]:
    value = data.get("value", [])
    if not isinstance(value, list):
        raise DirectoryError("Microsoft Graph returned an unexpected response")
    return [item for item in value if isinstance(item, dict)]


def _search_expression(query: str) -> str:
    escaped = query.replace("\\", " ").replace('"', " ").strip()
    return (
        f'"displayName:{escaped}" OR "userPrincipalName:{escaped}" OR "mail:{escaped}" '
        f'OR "appId:{escaped}"'
    )


def _matches_search_kind(kind: DirectorySearchKind, object_kind: PrincipalKind) -> bool:
    if kind == DirectorySearchKind.USER:
        return object_kind in {PrincipalKind.USER, PrincipalKind.AGENT_USER}
    if kind == DirectorySearchKind.GROUP:
        return object_kind == PrincipalKind.SECURITY_GROUP
    if kind == DirectorySearchKind.AGENT:
        return object_kind in {PrincipalKind.AGENT_IDENTITY, PrincipalKind.AGENT_USER}
    return False


def _dedupe(items: list[DirectoryObject]) -> list[DirectoryObject]:
    result: dict[str, DirectoryObject] = {}
    for item in items:
        key = item.object_id.casefold()
        if key not in result:
            result[key] = item
    return list(result.values())


def _object_from_user(data: dict[str, Any], *, agent_user: bool) -> DirectoryObject:
    return DirectoryObject(
        object_id=str(data.get("id", "")).lower(),
        kind=PrincipalKind.AGENT_USER if agent_user else PrincipalKind.USER,
        display_name=_text(data.get("displayName")),
        detail=_text(data.get("userPrincipalName")) or _text(data.get("mail")),
        identity_parent_id=_text(data.get("identityParentId")),
    )


def _object_from_group(data: dict[str, Any], *, validate_security: bool) -> DirectoryObject | None:
    if data.get("securityEnabled") is not True:
        if validate_security:
            raise ValidationError(
                "That group is not a security-enabled group, so it never appears in token groups."
            )
        return None
    return DirectoryObject(
        object_id=str(data.get("id", "")).lower(),
        kind=PrincipalKind.SECURITY_GROUP,
        display_name=_text(data.get("displayName")),
        detail=_text(data.get("mailNickname")) or _text(data.get("description")),
    )


def _object_from_agent_identity(data: dict[str, Any]) -> DirectoryObject:
    app_id = _text(data.get("appId"))
    return DirectoryObject(
        object_id=str(data.get("id", "")).lower(),
        kind=PrincipalKind.AGENT_IDENTITY,
        display_name=_text(data.get("displayName")),
        detail=app_id,
        app_id=app_id,
        blueprint_id=_text(data.get("agentIdentityBlueprintId")),
    )


def _object_from_member(data: dict[str, Any]) -> DirectoryObject | None:
    odata_type = str(data.get("@odata.type", "")).casefold()
    if odata_type.endswith("agentuser"):
        return _object_from_user(data, agent_user=True)
    if odata_type.endswith("agentidentity"):
        return _object_from_agent_identity(data)
    if odata_type.endswith("user"):
        return _object_from_user(data, agent_user=False)
    if odata_type.endswith("serviceprincipal"):
        service_principal_type = str(data.get("servicePrincipalType", ""))
        app_id = _text(data.get("appId"))
        return DirectoryObject(
            object_id=str(data.get("id", "")).lower(),
            kind=(
                PrincipalKind.MANAGED_IDENTITY
                if service_principal_type == "ManagedIdentity"
                else PrincipalKind.SERVICE_PRINCIPAL
            ),
            display_name=_text(data.get("displayName")),
            detail=app_id,
            app_id=app_id,
        )
    if odata_type.endswith("group"):
        return _object_from_group(data, validate_security=False)
    return None


def _retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After", "0")
    try:
        return min(max(float(raw), 0.0), 5.0)
    except ValueError:
        return 0.0


def _log_graph_status(operation: str, response: httpx.Response) -> None:
    logger.warning(
        "graph_directory_read_failed",
        operation=operation,
        status_code=response.status_code,
        request_id=response.headers.get("request-id"),
    )
