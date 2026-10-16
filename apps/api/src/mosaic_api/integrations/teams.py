"""Teams REST adapters. URLs, OAuth tokens and provider response bodies never become errors."""

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
import jwt
from azure.core.credentials_async import AsyncTokenCredential
from jwt.algorithms import RSAAlgorithm
from opentelemetry.instrumentation.utils import suppress_http_instrumentation

from mosaic_api.domain import KeyVaultSecretId
from mosaic_api.errors import ValidationError
from mosaic_api.services.notifications import NotificationDispatchResult

BOT_ISSUER = "https://api.botframework.com"
BOT_KEYS_URL = "https://login.botframework.com/v1/.well-known/keys"
BOT_METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
BOT_SCOPE = f"{BOT_ISSUER}/.default"


class _TeamsHttpLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if any(
            host in message
            for host in (".logic.azure.com", ".api.powerplatform.com", "smba.trafficmanager.net")
        ):
            record.msg = "Teams HTTP exchange (URL and provider details redacted)"
            record.args = ()
        return True


class SecretReader(Protocol):
    async def read(self, secret_uri: str) -> str: ...


class BotTokenSource(Protocol):
    @property
    def bot_app_id(self) -> str: ...

    async def token(self) -> str: ...


class TeamsAuthenticationError(Exception):
    """No upstream exception text or activity content may cross the callback boundary."""


def application_id(value: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValidationError("Teams registration IDs must be UUIDs.") from None


@dataclass(frozen=True)
class WorkflowEndpointPolicy:
    """An operator-owned exact-host allowlist, intersected with public Workflows URL shapes."""

    allowed_hosts: frozenset[str] = frozenset()

    def validate(self, value: str) -> str:
        try:
            parts = urlsplit(value)
            host = parts.hostname or ""
            microsoft_host = bool(
                re.fullmatch(r"[a-z0-9.-]+\.logic\.azure\.com", host)
                or re.fullmatch(r"[a-z0-9.-]+\.api\.powerplatform\.com", host)
            )
            path = re.fullmatch(
                r"(?:/powerautomate/automations/direct)?/workflows/[a-zA-Z0-9-]+"
                r"/triggers/[a-zA-Z0-9_-]+/paths/invoke",
                parts.path,
            )
            valid = (
                0 < len(value) <= 4096
                and all(32 < ord(char) < 127 for char in value)
                and "\\" not in value
                and parts.scheme == "https"
                and host in self.allowed_hosts
                and microsoft_host
                and path is not None
                and parts.port in {None, 443}
                and parts.username is None
                and parts.password is None
                and not parts.fragment
                and bool(parts.query)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValidationError(
                "Use an HTTPS Teams Workflows URL on the deployment's exact-host allowlist."
            ) from None
        return value


def validate_service_url(value: str) -> str:
    """Public Teams only. A signed serviceUrl must also be a known Connector host/path."""
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        valid = (
            parts.scheme == "https"
            and host == "smba.trafficmanager.net"
            and re.fullmatch(r"/[a-zA-Z0-9/_-]+/?", parts.path) is not None
            and parts.port in {None, 443}
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
            and "\\" not in value
        )
    except ValueError:
        valid = False
    if not valid:
        raise TeamsAuthenticationError("Unsupported Teams Connector service URL.") from None
    return value.rstrip("/")


class TeamsLifecycleAuthenticator:
    """Connector JWT validation, including Teams key endorsement and signed serviceUrl.

    The Connector token identifies Microsoft, not a user. Tenant/user/conversation identity
    comes only from the authenticated activity and is constrained by the lifecycle service.
    Emulator and skill tokens are deliberately unsupported.
    """

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._keys: dict[str, tuple[Any, frozenset[str]]] = {}
        self._expires = 0.0
        self._last_refresh = float("-inf")
        self._lock = asyncio.Lock()

    async def _refresh(self) -> None:
        metadata = await self._client.get(BOT_METADATA_URL, follow_redirects=False)
        if metadata.status_code != 200:
            raise TeamsAuthenticationError("Microsoft signing metadata is unavailable.")
        document = metadata.json()
        if (
            document.get("issuer") != BOT_ISSUER
            or document.get("jwks_uri") != BOT_KEYS_URL
            or "RS256" not in document.get("id_token_signing_alg_values_supported", [])
        ):
            raise TeamsAuthenticationError("Microsoft signing metadata is invalid.")
        response = await self._client.get(BOT_KEYS_URL, follow_redirects=False)
        if response.status_code != 200:
            raise TeamsAuthenticationError("Microsoft signing keys are unavailable.")
        self._keys = {
            key["kid"]: (
                RSAAlgorithm.from_jwk(key),
                frozenset(key.get("endorsements", [])),
            )
            for key in response.json().get("keys", [])
            if key.get("kty") == "RSA"
            and key.get("use", "sig") == "sig"
            and isinstance(key.get("kid"), str)
        }
        self._expires = time.monotonic() + 3600

    async def authenticate(
        self, authorization: str, *, bot_app_id: str, service_url: str
    ) -> Mapping[str, Any]:
        scheme, _, token = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not token or len(token) > 16384:
            raise TeamsAuthenticationError("A Microsoft Connector bearer token is required.")
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if header.get("alg") != "RS256" or not isinstance(kid, str) or not 0 < len(kid) <= 256:
                raise TeamsAuthenticationError("The Connector signing key is invalid.")
            async with self._lock:
                now = time.monotonic()
                if now >= self._expires or kid not in self._keys:
                    if now - self._last_refresh >= 60:
                        self._last_refresh = now
                        await self._refresh()
                if time.monotonic() >= self._expires:
                    raise TeamsAuthenticationError("Microsoft signing keys must be refreshed.")
            key = self._keys.get(kid)
            if not key or "msteams" not in key[1]:
                raise TeamsAuthenticationError("The signing key is not endorsed for Teams.")
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key[0],
                algorithms=["RS256"],
                audience=bot_app_id,
                issuer=BOT_ISSUER,
                leeway=60,
                options={"require": ["iss", "aud", "exp", "nbf", "serviceurl"]},
            )
            if claims.get("serviceurl", "").rstrip("/") != service_url.rstrip("/"):
                raise TeamsAuthenticationError("The signed Connector service URL does not match.")
            validate_service_url(service_url)
            return claims
        except TeamsAuthenticationError:
            raise
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            raise TeamsAuthenticationError("Microsoft Connector authentication failed.") from None


class ManagedIdentityBotTokenSource:
    """The injected credential MUST be the Azure Bot's user-assigned managed identity."""

    def __init__(self, credential: AsyncTokenCredential, *, bot_app_id: str) -> None:
        self._credential = credential
        self._bot_app_id = application_id(bot_app_id)

    @property
    def bot_app_id(self) -> str:
        return self._bot_app_id

    async def token(self) -> str:
        return (await self._credential.get_token(BOT_SCOPE)).token


class SecretBotTokenSource:
    """Single-tenant client credentials; only a Key Vault reference is configured."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        reader: SecretReader,
        *,
        tenant_id: str,
        bot_app_id: str,
        secret_reference: str,
    ) -> None:
        self._client = client
        self._reader = reader
        self._tenant_id = application_id(tenant_id)
        self._bot_app_id = application_id(bot_app_id)
        secret = KeyVaultSecretId.parse(secret_reference)
        self._secret_reference = secret.versionless + (
            f"/{secret.version}" if secret.version else ""
        )

    @property
    def bot_app_id(self) -> str:
        return self._bot_app_id

    async def token(self) -> str:
        try:
            secret = await self._reader.read(self._secret_reference)
            response = await self._client.post(
                f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._bot_app_id,
                    "client_secret": secret,
                    "scope": BOT_SCOPE,
                },
                follow_redirects=False,
            )
            if response.status_code != 200:
                raise TeamsAuthenticationError("Teams bot OAuth credential was not accepted.")
            payload = response.json()
            token = payload.get("access_token")
            if not isinstance(token, str) or not token or payload.get("token_type") != "Bearer":
                raise TeamsAuthenticationError("Teams bot OAuth response is invalid.")
            return token
        except TeamsAuthenticationError:
            raise
        except Exception:
            raise TeamsAuthenticationError("Teams bot OAuth acquisition failed.") from None


def _retry_after(value: str) -> int:
    try:
        seconds = (
            int(value)
            if re.fullmatch(r"[0-9]{1,9}", value)
            else int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        )
        return min(max(seconds, 1), 86400)
    except (ValueError, TypeError, OverflowError):
        return 60


def provider_result(
    response: httpx.Response, *, personal: bool = False
) -> NotificationDispatchResult:
    status = response.status_code
    if 200 <= status < 300:
        # Workflow success means the trigger accepted the request, NOT that its actions ran.
        message_id = None
        if personal:
            try:
                payload = response.json()
                candidate = payload.get("id") if isinstance(payload, dict) else None
                if isinstance(candidate, str) and re.fullmatch(
                    r"[A-Za-z0-9._:-]{1,256}", candidate
                ):
                    message_id = candidate
            except ValueError:
                pass
        return NotificationDispatchResult("providerAccepted", provider_message_id=message_id)
    if status == 429:
        retry = response.headers.get("Retry-After", "")
        seconds = _retry_after(retry)
        return NotificationDispatchResult(
            "retryableFailure",
            retry_after_seconds=seconds,
            error_code="teamsThrottled",
            safe_error="Teams throttled the request.",
        )
    if status >= 500 or status == 408:
        return NotificationDispatchResult(
            "unknownOutcome",
            error_code="teamsUnknownOutcome",
            safe_error="Teams did not confirm acceptance; retry may duplicate a message.",
        )
    return NotificationDispatchResult(
        "permanentFailure",
        error_code=f"teamsHttp{status}",
        safe_error="Teams rejected the request. Check app installation, credentials or workflow.",
    )


class TeamsHttpTransport:
    def __init__(self, client: httpx.AsyncClient, policy: WorkflowEndpointPolicy) -> None:
        self._client = client
        self.policy = policy
        logger = logging.getLogger("httpx")
        if not any(isinstance(item, _TeamsHttpLogFilter) for item in logger.filters):
            logger.addFilter(_TeamsHttpLogFilter())

    async def workflow(self, url: str, card: Mapping[str, Any]) -> NotificationDispatchResult:
        self.policy.validate(url)
        return await self._post(
            url,
            {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "contentUrl": None,
                        "content": dict(card),
                    }
                ],
            },
        )

    async def personal(
        self, *, service_url: str, conversation_id: str, token: str, card: Mapping[str, Any]
    ) -> NotificationDispatchResult:
        root = validate_service_url(service_url)
        return await self._post(
            f"{root}/v3/conversations/{quote(conversation_id, safe='')}/activities",
            {
                "type": "message",
                "attachments": [
                    {
                        "contentType": "application/vnd.microsoft.card.adaptive",
                        "content": dict(card),
                    }
                ],
            },
            token=token,
        )

    async def _post(
        self, url: str, payload: Mapping[str, Any], *, token: str | None = None
    ) -> NotificationDispatchResult:
        if len(json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")) > 24 * 1024:
            return NotificationDispatchResult(
                "permanentFailure",
                error_code="teamsPayloadTooLarge",
                safe_error="The notification exceeds the bounded Teams card size. Open MOSAIC.",
            )
        try:
            # Workflows encode a credential in the URL; never create an auto-instrumented
            # HTTP span with it, nor one containing a person's conversation ID.
            with suppress_http_instrumentation():
                response = await self._client.post(
                    url,
                    json=dict(payload),
                    headers={"Authorization": f"Bearer {token}"} if token else {},
                    follow_redirects=False,
                )
        except httpx.ConnectError:
            return NotificationDispatchResult(
                "retryableFailure",
                error_code="teamsConnectFailed",
                safe_error="Teams connection failed before a response.",
            )
        except httpx.HTTPError:
            return NotificationDispatchResult(
                "unknownOutcome",
                error_code="teamsUnknownOutcome",
                safe_error="Teams acceptance is unknown; retry may duplicate a message.",
            )
        return provider_result(response, personal=token is not None)
