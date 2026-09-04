import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import jwt
from fastapi import HTTPException, Request, status
from jwt.algorithms import RSAAlgorithm

from mosaic_api.config import Settings


@dataclass(frozen=True)
class AuthContext:
    """The authenticated caller.

    Authentication establishes *who* is calling and which app roles Entra issued them.
    Deciding whether those roles are sufficient for a given route is authorization, and lives in
    the ``require_*`` dependencies below rather than here, so one API can serve both the
    administrator console and the end-user portal.
    """

    object_id: str
    tenant_id: str
    roles: frozenset[str]

    def has_any_role(self, *roles: str) -> bool:
        return any(role in self.roles for role in roles)


class Authenticator(Protocol):
    async def authenticate(self, request: Request) -> AuthContext: ...

    async def close(self) -> None: ...


class LocalAuthenticator:
    def __init__(self, tenant_id: str, roles: Iterable[str] = ("Admin",)) -> None:
        self._context = AuthContext(
            object_id="local-admin",
            tenant_id=tenant_id,
            roles=frozenset(roles),
        )

    async def authenticate(self, _request: Request) -> AuthContext:
        return self._context

    async def close(self) -> None:
        return None


class EntraAuthenticator:
    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        cache_seconds: int = 3600,
        minimum_refresh_seconds: int = 60,
        unknown_key_cache_seconds: int = 300,
        maximum_unknown_keys: int = 256,
        maximum_kid_length: int = 256,
    ) -> None:
        if not settings.api_client_id:
            raise ValueError("API client ID is required")
        self._tenant_id = settings.tenant_id
        self._audience = settings.api_client_id
        self._issuer = settings.issuer.rstrip("/")
        self._discovery_url = settings.discovery_url
        self._accepted_roles = frozenset({settings.required_role, settings.portal_role})
        self._client = client or httpx.AsyncClient(timeout=10)
        self._owns_client = client is None
        self._cache_seconds = cache_seconds
        self._minimum_refresh_seconds = minimum_refresh_seconds
        self._unknown_key_cache_seconds = unknown_key_cache_seconds
        self._maximum_unknown_keys = maximum_unknown_keys
        self._maximum_kid_length = maximum_kid_length
        self._jwks_uri: str | None = None
        self._keys: dict[str, Any] = {}
        self._unknown_keys: dict[str, float] = {}
        self._expires_at = 0.0
        self._last_refresh_at = 0.0
        self._refresh_lock = asyncio.Lock()

    @staticmethod
    def _unauthorized(message: str) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=message,
            headers={"WWW-Authenticate": "Bearer"},
        )

    async def _refresh_keys(self) -> None:
        discovery = await self._client.get(self._discovery_url)
        discovery.raise_for_status()
        metadata = discovery.json()
        issuer = str(metadata.get("issuer", "")).rstrip("/")
        if issuer != self._issuer:
            raise self._unauthorized("Identity provider issuer does not match configuration")
        self._jwks_uri = str(metadata["jwks_uri"])
        response = await self._client.get(self._jwks_uri)
        response.raise_for_status()
        self._keys = {
            item["kid"]: RSAAlgorithm.from_jwk(item)
            for item in response.json().get("keys", [])
            if item.get("kid") and item.get("kty") == "RSA"
        }
        now = time.monotonic()
        self._expires_at = now + self._cache_seconds
        self._last_refresh_at = now
        self._unknown_keys = {
            kid: expires_at for kid, expires_at in self._unknown_keys.items() if expires_at > now
        }

    async def _ensure_keys(self, *, force: bool = False) -> None:
        async with self._refresh_lock:
            now = time.monotonic()
            if not force and now < self._expires_at:
                return
            if force and now - self._last_refresh_at < self._minimum_refresh_seconds:
                return
            await self._refresh_keys()

    async def _key(self, token: str) -> Any:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise self._unauthorized("Access token header is invalid") from exc
        if header.get("alg") != "RS256" or not header.get("kid"):
            raise self._unauthorized("Access token must use an approved signing algorithm")
        raw_kid = header["kid"]
        if not isinstance(raw_kid, str) or len(raw_kid) > self._maximum_kid_length:
            raise self._unauthorized("Access token signing key is invalid")
        kid = raw_kid
        now = time.monotonic()
        if self._unknown_keys.get(kid, 0.0) > now:
            raise self._unauthorized("Access token signing key is unknown")
        try:
            await self._ensure_keys()
            if kid not in self._keys:
                await self._ensure_keys(force=True)
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise self._unauthorized("Unable to resolve identity provider signing keys") from exc
        key = self._keys.get(kid)
        if not key:
            now = time.monotonic()
            self._unknown_keys = {
                cached_kid: expires_at
                for cached_kid, expires_at in self._unknown_keys.items()
                if expires_at > now
            }
            if (
                kid not in self._unknown_keys
                and len(self._unknown_keys) >= self._maximum_unknown_keys
            ):
                self._unknown_keys.pop(next(iter(self._unknown_keys)))
            self._unknown_keys[kid] = now + self._unknown_key_cache_seconds
            raise self._unauthorized("Access token signing key is unknown")
        return key

    async def authenticate(self, request: Request) -> AuthContext:
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not token:
            raise self._unauthorized("A bearer access token is required")
        key = await self._key(token)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "iss", "aud", "tid", "oid"]},
            )
        except jwt.PyJWTError as exc:
            raise self._unauthorized("Access token validation failed") from exc
        if claims.get("tid") != self._tenant_id:
            raise self._unauthorized("Access token tenant is not allowed")
        roles = frozenset(str(role) for role in claims.get("roles", []))
        if not roles & self._accepted_roles:
            # Fail closed at the edge: a tenant token carrying none of MOSAIC's app roles never
            # reaches a route. Which of the accepted roles a given route needs is decided by the
            # ``require_*`` dependencies.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "A MOSAIC app role is required: "
                    f"{', '.join(sorted(self._accepted_roles))}"
                ),
            )
        return AuthContext(
            object_id=str(claims["oid"]),
            tenant_id=str(claims["tid"]),
            roles=roles,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _forbidden(role: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"The {role} app role is required",
    )


async def _authenticate(request: Request) -> AuthContext:
    authenticator: Authenticator = request.app.state.authenticator
    return await authenticator.authenticate(request)


async def require_admin(request: Request) -> AuthContext:
    """Administrator console access. Nothing else satisfies it."""

    settings: Settings = request.app.state.settings
    context = await _authenticate(request)
    if settings.required_role not in context.roles:
        raise _forbidden(settings.required_role)
    return context


async def require_portal_user(request: Request) -> AuthContext:
    """End-user portal access.

    The administrator role also satisfies this, so an administrator can open the portal to see
    what a user sees without holding a second role assignment.
    """

    settings: Settings = request.app.state.settings
    context = await _authenticate(request)
    if not context.has_any_role(settings.portal_role, settings.required_role):
        raise _forbidden(settings.portal_role)
    return context
