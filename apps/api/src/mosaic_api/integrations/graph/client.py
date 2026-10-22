import asyncio
import json
import math
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
import jwt
import structlog
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError

from mosaic_api.domain import (
    DirectoryMemberPage,
    DirectoryObject,
    DirectorySearchKind,
    PrincipalKind,
    utc_now,
)
from mosaic_api.errors import (
    DirectoryError,
    DirectoryForbiddenError,
    DirectoryThrottledError,
    ValidationError,
)
from mosaic_api.integrations.graph.membership import (
    GroupMembershipEvidence,
    parse_object_id,
    validate_verification_timeout,
    verification_scope,
)
from mosaic_api.integrations.graph.photos import PHOTO_MAX_BYTES, DirectoryPhoto, validate_photo

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
        tenant_id: str | None = None,
        photo_cache_entries: int = 128,
        photo_cache_seconds: float = 120,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credential = credential
        self._endpoint = str(endpoint).rstrip("/")
        self._scope = f"{self._endpoint}/.default"
        self._client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self._owns_client = client is None
        self._sleep = sleep
        self._tenant_id = tenant_id.casefold() if tenant_id else None
        self._photo_cache_entries = min(max(photo_cache_entries, 0), 128)
        self._photo_cache_seconds = min(max(photo_cache_seconds, 0), 120)
        self._photo_clock = clock
        self._photo_cache: OrderedDict[
            tuple[str, str], tuple[float, DirectoryPhoto | None]
        ] = OrderedDict()
        # Fixed striped locks coalesce identical requests and cap concurrent Graph photo reads.
        self._photo_locks = tuple(asyncio.Lock() for _ in range(4))

    async def close(self) -> None:
        self._photo_cache.clear()
        if self._owns_client:
            await self._client.aclose()

    async def application_permissions(self) -> frozenset[str]:
        _, permissions = await self._application_token()
        return permissions

    async def _application_token(self) -> tuple[AccessToken, frozenset[str]]:
        try:
            token = await self._credential.get_token(
                self._scope, tenant_id=self._tenant_id
            )
        except (AzureError, TimeoutError) as exc:
            raise DirectoryError(
                "The API identity could not get a Microsoft Graph token"
            ) from exc
        try:
            claims = jwt.decode(token.token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise DirectoryForbiddenError(
                "The API identity's Microsoft Graph token could not be verified"
            ) from exc
        tenant_id = str(claims.get("tid", "")).casefold()
        roles = claims.get("roles")
        if "roles" not in claims and claims.get("idtyp") == "app":
            roles = []
        if (
            (self._tenant_id and tenant_id != self._tenant_id)
            or "scp" in claims
            or claims.get("idtyp") not in (None, "app")
            or not isinstance(roles, list)
            or not all(isinstance(role, str) and role for role in roles)
        ):
            raise DirectoryForbiddenError(
                "Directory lookup requires the API identity's app-only Microsoft Graph token"
            )
        return token, frozenset(roles)

    async def user_photo(self, tenant_id: str, object_id: str) -> DirectoryPhoto | None:
        if not self._tenant_id or tenant_id.casefold() != self._tenant_id:
            raise DirectoryForbiddenError("Directory photos are restricted to this tenant")
        if not _is_guid(object_id):
            raise ValidationError("Photo object ID must be a GUID")
        key = (tenant_id.casefold(), object_id.strip().lower())
        async with self._photo_locks[hash(key) % len(self._photo_locks)]:
            now = self._photo_clock()
            for expired_key, (expires_at, _) in list(self._photo_cache.items()):
                if expires_at <= now:
                    del self._photo_cache[expired_key]
            if key in self._photo_cache:
                self._photo_cache.move_to_end(key)
                return self._photo_cache[key][1]
            try:
                async with asyncio.timeout(30):
                    photo = await self._fetch_user_photo(*key)
            except (TimeoutError, AzureError, httpx.HTTPError) as exc:
                raise DirectoryError("Microsoft Graph photo read could not be completed") from exc
            if self._photo_cache_entries:
                ttl = self._photo_cache_seconds if photo else min(self._photo_cache_seconds, 30)
                self._photo_cache[key] = (self._photo_clock() + ttl, photo)
                while len(self._photo_cache) > self._photo_cache_entries:
                    self._photo_cache.popitem(last=False)
            return photo

    async def _fetch_user_photo(self, tenant_id: str, object_id: str) -> DirectoryPhoto | None:
        token = await self._credential.get_token(self._scope, tenant_id=tenant_id)
        # This is a defensive check on a token from our trusted Azure credential, not caller
        # authentication. Never send a developer's wrong-tenant or delegated token to Graph.
        try:
            claims = jwt.decode(token.token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise DirectoryForbiddenError(
                "The Graph photo credential could not be verified"
            ) from exc
        if (
            str(claims.get("tid", "")).casefold() != tenant_id
            or claims.get("scp")
            or claims.get("idtyp") not in {None, "app"}
            or not (claims.get("idtyp") == "app" or isinstance(claims.get("roles"), list))
        ):
            raise DirectoryForbiddenError(
                "Directory photos require MOSAIC's app-only Graph credential in this tenant"
            )
        headers = {
            "Authorization": f"Bearer {token.token}",
            "Accept-Encoding": "identity",
        }
        # /users verifies a person without probing groups/applications or needing their consent.
        user = await self._photo_resource(
            "photo_user",
            f"/users/{object_id}",
            headers=headers,
            permission="User.ReadBasic.All",
            params={"$select": "id"},
        )
        if user is None:
            return None
        try:
            data = json.loads(user[0])
        except (ValueError, UnicodeDecodeError) as exc:
            raise DirectoryError("Microsoft Graph returned malformed photo user data") from exc
        if not isinstance(data, dict) or str(data.get("id", "")).casefold() != object_id:
            raise DirectoryError("Microsoft Graph returned unexpected photo user data")
        odata_type = str(data.get("@odata.type", "")).casefold()
        if odata_type not in {"", "#microsoft.graph.user"}:
            return None
        result = await self._photo_resource(
            "user_photo",
            f"/users/{object_id}/photos/96x96/$value",
            headers=headers,
            permission="ProfilePhoto.Read.All",
        )
        return validate_photo(*result) if result else None

    async def _photo_resource(
        self,
        operation: str,
        path: str,
        *,
        headers: dict[str, str],
        permission: str,
        params: dict[str, str] | None = None,
    ) -> tuple[bytes, str] | None:
        for attempt in range(2):
            async with self._client.stream(
                "GET",
                f"{self._endpoint}/v1.0{path}",
                headers=headers,
                params=params,
                follow_redirects=False,
                timeout=10,
            ) as response:
                if response.status_code in {429, 503} and attempt == 0:
                    await self._sleep(min(_graph_retry_after(response), 5))
                    continue
                if response.status_code != 200:
                    _log_graph_status(operation, response)
                if response.status_code in {401, 403}:
                    raise DirectoryForbiddenError(
                        f"Microsoft Graph refused the photo read; grant {permission}."
                    )
                if response.status_code == 404:
                    return None
                if response.status_code == 429:
                    raise DirectoryThrottledError(
                        "Microsoft Graph throttled the photo read",
                        retry_after=_graph_retry_after(response),
                    )
                if response.status_code != 200:
                    raise DirectoryError("Microsoft Graph photo read failed")
                if response.headers.get("Content-Encoding", "identity").casefold() != "identity":
                    raise DirectoryError("Microsoft Graph returned an encoded photo response")
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        if not 0 <= int(length) <= PHOTO_MAX_BYTES:
                            raise ValueError("Oversized response")
                    except ValueError as exc:
                        raise DirectoryError(
                            "Microsoft Graph returned an invalid or oversized photo response"
                        ) from exc
                content = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if len(content) + len(chunk) > PHOTO_MAX_BYTES:
                        raise DirectoryError("Microsoft Graph returned an oversized photo response")
                    content.extend(chunk)
                return bytes(content), response.headers.get("Content-Type", "")
        raise DirectoryError("Microsoft Graph photo read failed after retry")

    async def search(
        self, kind: DirectorySearchKind, query: str, *, limit: int = 20
    ) -> list[DirectoryObject]:
        limit = min(max(limit, 1), 25)
        term = query.strip()
        if not term:
            raise ValidationError("Directory search query cannot be empty")
        term = term[:120]
        if kind == DirectorySearchKind.APPLICATION:
            return await self._search_applications(term, limit)
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

    async def _search_applications(self, query: str, limit: int) -> list[DirectoryObject]:
        select = "id,displayName,appId,servicePrincipalType"
        if _is_guid(query):
            data = await self._json(
                "get_application",
                "GET",
                f"/servicePrincipals/{query.lower()}",
                permission="Application.Read.All",
                params={"$select": select},
            )
            if data:
                item = _application_from_service_principal(data)
                return [item] if item else []
            expression = f"appId eq '{query.lower()}'"
        else:
            escaped = query.replace("'", "''")
            expression = f"startswith(displayName,'{escaped}')"
        data = await self._json(
            "search_applications",
            "GET",
            "/servicePrincipals",
            permission="Application.Read.All",
            params={
                "$filter": f"({expression}) and (servicePrincipalType eq 'Application' "
                "or servicePrincipalType eq 'ManagedIdentity')",
                "$select": select,
                "$top": limit,
                "$count": "true",
            },
            headers={"ConsistencyLevel": "eventual"},
        )
        return [
            item
            for raw in _items(data)[:limit]
            if (item := _application_from_service_principal(raw)) is not None
        ]

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
        collection_url = (
            f"{self._endpoint}/v1.0/groups/{group_object_id.lower()}/transitiveMembers"
        )
        next_url: str | None = collection_url
        params: dict[str, str | int] | None = {
            "$select": "id,displayName,userPrincipalName,mail,appId,servicePrincipalType,"
            "securityEnabled",
            "$top": min(max(capped_limit, 1), 999),
        }
        seen: set[str] = set()
        while next_url and len(members) < capped_limit:
            if next_url in seen:
                raise DirectoryError("Microsoft Graph repeated a membership paging URL")
            seen.add(next_url)
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
            next_url = _membership_next_url(data, collection_url)
            truncated = bool(next_url and len(members) >= capped_limit)
        return DirectoryMemberPage(
            group_object_id=group_object_id.lower(),
            members=members,
            truncated=truncated,
        )

    async def verify_group_members(
        self,
        group_object_id: str,
        candidate_object_ids: Sequence[str],
        *,
        timeout_seconds: float = 60,
    ) -> GroupMembershipEvidence:
        group_id, candidates = verification_scope(group_object_id, candidate_object_ids)
        validate_verification_timeout(timeout_seconds)
        if not self._tenant_id:
            raise DirectoryForbiddenError("Membership verification requires a configured tenant")
        checked_at = utc_now()
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        try:
            async with asyncio.timeout(timeout_seconds):
                members = await self._verified_group_members(group_id, candidates, deadline)
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError
        except _GraphNotFound as exc:
            raise DirectoryError(
                "Microsoft Graph could not read the referenced security group"
            ) from exc
        except TimeoutError as exc:
            raise DirectoryError(
                "Microsoft Graph complete membership verification timed out"
            ) from exc
        return GroupMembershipEvidence(
            tenant_id=self._tenant_id,
            group_object_id=group_id,
            candidate_object_ids=candidates,
            member_object_ids=members,
            checked_at=checked_at,
        )

    async def _verified_group_members(
        self, group_id: str, candidates: frozenset[str], deadline: float
    ) -> frozenset[str]:
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError
        group = await self._json(
            "verify_security_group",
            "GET",
            f"/groups/{group_id}",
            permission="GroupMember.Read.All",
            params={"$select": "id,securityEnabled"},
            not_found_none=False,
            app_only=True,
        )
        if parse_object_id(group.get("id")) != group_id or group.get("securityEnabled") is not True:
            raise DirectoryError("Microsoft Graph did not verify the referenced security group")
        collection_url = f"{self._endpoint}/v1.0/groups/{group_id}/transitiveMembers"
        next_url: str | None = collection_url
        params: dict[str, Any] | None = {"$select": "id", "$top": 999}
        seen: set[str] = set()
        members: set[str] = set()
        while next_url:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            if next_url in seen:
                raise DirectoryError("Microsoft Graph repeated a membership paging URL")
            seen.add(next_url)
            data = await self._json(
                "verify_group_members",
                "GET",
                next_url,
                permission="GroupMember.Read.All",
                params=params,
                not_found_none=False,
                app_only=True,
            )
            params = None
            values = data.get("value")
            if not isinstance(values, list) or len(values) > 999:
                raise DirectoryError("Microsoft Graph returned an invalid membership page")
            for value in values:
                member_id = parse_object_id(value.get("id")) if isinstance(value, dict) else None
                if member_id is None:
                    raise DirectoryError("Microsoft Graph returned an invalid member object ID")
                if member_id in candidates:
                    members.add(member_id)
            next_url = _membership_next_url(data, collection_url)
        return frozenset(members)

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
            evidence = await self.verify_group_members(group_id, [object_id])
            return object_id in evidence.member_object_ids

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
        app_only: bool = False,
    ) -> dict[str, Any]:
        request_headers = dict(headers or {})
        if app_only:
            token, _ = await self._application_token()
        else:
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
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                raise DirectoryError("Microsoft Graph could not be reached") from exc
            if response.status_code == 429 and app_only:
                _log_graph_status(operation, response)
                raise DirectoryThrottledError(
                    "Microsoft Graph membership verification is throttled",
                    retry_after=_graph_retry_after(response),
                )
            if response.status_code in {429, 503} and attempt == 0:
                delay = min(_graph_retry_after(response), 5) if app_only else _retry_after(response)
                await self._sleep(delay)
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
            if response.status_code != 200:
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


def _membership_next_url(data: dict[str, Any], collection_url: str) -> str | None:
    if "@odata.nextLink" not in data:
        return None
    raw = data["@odata.nextLink"]
    if not isinstance(raw, str) or not raw or raw.strip() != raw:
        raise DirectoryError("Microsoft Graph returned an invalid membership paging URL")
    try:
        candidate = httpx.URL(raw)
    except httpx.InvalidURL as exc:
        raise DirectoryError("Microsoft Graph returned an invalid membership paging URL") from exc
    expected = httpx.URL(collection_url)
    if (
        candidate.scheme != "https"
        or candidate.host != expected.host
        or candidate.port != expected.port
        or candidate.path != expected.path
        or candidate.username
        or candidate.password
        or candidate.fragment
    ):
        raise DirectoryError("Microsoft Graph returned an unexpected paging URL")
    return str(candidate)


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


def _application_from_service_principal(data: dict[str, Any]) -> DirectoryObject | None:
    odata_type = str(data.get("@odata.type", "")).casefold()
    if odata_type and odata_type != "#microsoft.graph.serviceprincipal":
        return None
    principal_type = data.get("servicePrincipalType")
    if principal_type not in {"Application", "ManagedIdentity"} or not _is_guid(
        str(data.get("id", ""))
    ):
        return None
    app_id = _text(data.get("appId"))
    return DirectoryObject(
        object_id=str(data["id"]).lower(),
        kind=(
            PrincipalKind.MANAGED_IDENTITY
            if principal_type == "ManagedIdentity"
            else PrincipalKind.SERVICE_PRINCIPAL
        ),
        display_name=_text(data.get("displayName")),
        detail=app_id,
        app_id=app_id,
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


def _graph_retry_after(response: httpx.Response) -> int:
    raw = response.headers.get("Retry-After", "1")
    try:
        seconds = float(raw)
        if not math.isfinite(seconds):
            return 1
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 1
    return min(max(math.ceil(seconds), 1), 300)


def _log_graph_status(operation: str, response: httpx.Response) -> None:
    logger.warning(
        "graph_directory_read_failed",
        operation=operation,
        status_code=response.status_code,
        request_id=response.headers.get("request-id"),
    )
