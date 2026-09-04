"""Credential resolution for outbound MCP calls.

MOSAIC stores a Key Vault *secret identifier*, never a secret value. This module is where that
URI is finally exchanged for a value, at call time, and the value never leaves the request that
needed it: it is not persisted, not cached, not logged, and not returned to an API caller.

ADR 0006 described this path and did not implement it. This is the first place it runs.
"""

from typing import Any
from urllib.parse import urlsplit

import httpx
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError

from mosaic_api.errors import UpstreamAuthorizationError, UpstreamError, ValidationError

KEY_VAULT_API_VERSION = "7.4"
KEY_VAULT_SCOPE = "https://vault.azure.net/.default"
_KEY_VAULT_HOST_SUFFIXES: tuple[str, ...] = (
    ".vault.azure.net",
    ".vault.azure.cn",
    ".vault.usgovcloudapi.net",
    ".vault.microsoftazure.de",
)


def scope_for_audience(audience: str) -> str:
    """Turn an operator-supplied audience into an Entra scope.

    Accepts either a bare audience (``api://contoso-mcp``) or a full scope already ending in
    ``/.default``, because operators copy both out of the portal.
    """

    trimmed = audience.strip()
    if not trimmed:
        raise ValidationError("A managed-identity MCP server needs a non-empty audience.")
    if trimmed.endswith("/.default"):
        return trimmed
    return f"{trimmed.rstrip('/')}/.default"


class EntraTokenProvider:
    """Issues a managed-identity token for an audience the operator explicitly named."""

    def __init__(self, credential: AsyncTokenCredential) -> None:
        self._credential = credential

    async def token_for(self, audience: str) -> str:
        scope = scope_for_audience(audience)
        try:
            token = await self._credential.get_token(scope)
        except ClientAuthenticationError as error:
            raise UpstreamAuthorizationError(
                "MOSAIC could not acquire a token for this MCP server's audience.",
                details={"audience": audience, "reason": str(error)},
            ) from error
        return token.token


class KeyVaultSecretReader:
    """Reads one secret value over the Key Vault REST API.

    Deliberately not ``azure-keyvault-secrets``: a single authenticated GET does not justify a new
    dependency when the credential and an httpx client are already in hand.
    """

    def __init__(
        self,
        credential: AsyncTokenCredential,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._credential = credential
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _validate_secret_uri(secret_uri: str) -> str:
        parts = urlsplit(secret_uri)
        host = (parts.hostname or "").casefold()
        if parts.scheme.casefold() != "https" or not host:
            raise ValidationError(
                "A credential reference must be an https Key Vault secret identifier.",
                details={"uri": secret_uri},
            )
        if not any(host.endswith(suffix) for suffix in _KEY_VAULT_HOST_SUFFIXES):
            raise ValidationError(
                "A credential reference must point at an Azure Key Vault.",
                details={"host": host},
            )
        if not parts.path.strip("/").startswith("secrets/"):
            raise ValidationError(
                "A credential reference must be a Key Vault *secret* identifier.",
                details={"uri": secret_uri},
            )
        return f"https://{host}{parts.path}"

    async def read(self, secret_uri: str) -> str:
        url = self._validate_secret_uri(secret_uri)
        try:
            token = await self._credential.get_token(KEY_VAULT_SCOPE)
        except ClientAuthenticationError as error:
            raise UpstreamAuthorizationError(
                "MOSAIC could not acquire a Key Vault token.",
                details={"reason": str(error)},
            ) from error
        try:
            response = await self._client.get(
                url,
                params={"api-version": KEY_VAULT_API_VERSION},
                headers={"Authorization": f"Bearer {token.token}"},
            )
        except httpx.HTTPError as error:
            # A transient vault outage must arrive as a DomainError, so callers can record it
            # rather than let it escape a request handler as a 500.
            raise UpstreamError(
                "MOSAIC could not reach Key Vault to read this server's credential.",
                details={"reason": str(error)},
            ) from error
        if response.status_code in {401, 403}:
            raise UpstreamAuthorizationError(
                "MOSAIC is not allowed to read this Key Vault secret. Grant it the Key Vault "
                "Secrets User role on the vault.",
                # The URI is deliberately not echoed. It identifies a secret, and a secret
                # identifier must not reach an API response or a log line.
                details={"status": response.status_code},
            )
        if response.status_code == 404:
            raise ValidationError("That Key Vault secret does not exist.")
        if response.status_code >= 400:
            raise UpstreamAuthorizationError(
                "MOSAIC could not read this Key Vault secret.",
                details={"status": response.status_code},
            )
        payload: Any = response.json()
        value = payload.get("value") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value:
            raise ValidationError("That Key Vault secret has no value.")
        return value
