"""Keep an API key an administrator gave MOSAIC in MOSAIC's own Key Vault (ADR 0021).

An administrator who can't store a resource's key in Key Vault gives it to MOSAIC once. MOSAIC
writes it as a secret in the vault deployed with it, using its managed identity, and keeps only the
secret's identifier. From then on the key is read exactly like one the administrator stored
(ADR 0018): MOSAIC reads it to check it, and API Management reads it through a named value.

The value is in one request body here and nowhere else. It isn't logged, returned or put into an
error, the response, which repeats it, is never read, and an error raised here doesn't chain the
HTTP failure whose frames held it.
"""

import secrets
from typing import Protocol

import httpx
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError
from pydantic import SecretStr

from mosaic_api.domain import KeyVaultSecretId
from mosaic_api.errors import ConflictError, UpstreamAuthorizationError, UpstreamError
from mosaic_api.integrations.mcp.credentials import KEY_VAULT_API_VERSION, KEY_VAULT_SCOPE

MANAGED_BY_TAG = "managedBy"
MANAGED_BY = "MOSAIC"
MODEL_ENDPOINT_TAG = "mosaicModelEndpoint"
STORED_KEY_PREFIX = "mosaic-apikey-"


def stored_key_name(subdomain: str) -> str:
    """A new secret name for a resource's key, never one used before.

    The environment's vault has purge protection, so a deleted secret's name stays taken for as
    long as the vault keeps it. A random part lets an endpoint that was removed be registered again.
    """

    return f"{STORED_KEY_PREFIX}{subdomain}-{secrets.token_hex(4)}"


class KeyStore(Protocol):
    """Where MOSAIC writes a key it was given, and deletes it from again."""

    def new_secret(self, name: str) -> KeyVaultSecretId: ...

    async def put(
        self, secret: KeyVaultSecretId, value: SecretStr, *, tags: dict[str, str]
    ) -> None: ...

    async def delete(self, secret: KeyVaultSecretId) -> bool: ...


class KeyVaultSecretWriter:
    """Writes and deletes secrets over the Key Vault REST API with MOSAIC's identity.

    New secrets go to the vault MOSAIC was deployed with. A secret MOSAIC already keeps is written
    and deleted where its identifier says, so changing the configured vault strands nothing.
    """

    def __init__(
        self,
        credential: AsyncTokenCredential,
        vault_uri: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        try:
            self._vault = KeyVaultSecretId.parse(f"{vault_uri.rstrip('/')}/secrets/mosaic")
        except ValueError as error:
            raise ValueError(f"MOSAIC_KEY_VAULT_URI isn't a Key Vault URI: {error}") from None
        self._credential = credential
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False
        )
        self._owns_client = client is None

    @property
    def vault_name(self) -> str:
        return self._vault.vault_name

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def new_secret(self, name: str) -> KeyVaultSecretId:
        return KeyVaultSecretId.parse(f"{self._vault.vault_uri}/secrets/{name}")

    async def put(
        self, secret: KeyVaultSecretId, value: SecretStr, *, tags: dict[str, str]
    ) -> None:
        """Write the value as the secret's newest version, creating the secret if it's new."""

        token = await self._token()
        try:
            response = await self._client.put(
                f"{secret.vault_uri}/secrets/{secret.secret_name}",
                params={"api-version": KEY_VAULT_API_VERSION},
                headers={"Authorization": f"Bearer {token}"},
                json={"value": value.get_secret_value(), "tags": tags},
            )
        except httpx.HTTPError:
            raise UpstreamError(
                f"MOSAIC couldn't reach Key Vault {secret.vault_name} to store the API key. "
                "Nothing was stored. Try again.",
                details={"vault": secret.vault_name},
            ) from None
        # The response repeats the value, so nothing is read from it but its status.
        _raise_for_status(response.status_code, secret, storing=True)

    async def delete(self, secret: KeyVaultSecretId) -> bool:
        """Delete the secret. Returns False if it was already gone."""

        token = await self._token()
        try:
            response = await self._client.delete(
                f"{secret.vault_uri}/secrets/{secret.secret_name}",
                params={"api-version": KEY_VAULT_API_VERSION},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError:
            raise UpstreamError(
                f"MOSAIC couldn't reach Key Vault {secret.vault_name} to delete the API key it "
                "stored. Try again.",
                details={"vault": secret.vault_name},
            ) from None
        if response.status_code == 404:
            return False
        _raise_for_status(response.status_code, secret, storing=False)
        return True

    async def _token(self) -> str:
        try:
            token = await self._credential.get_token(KEY_VAULT_SCOPE)
        except ClientAuthenticationError:
            raise UpstreamAuthorizationError(
                "MOSAIC couldn't get a token for Key Vault with its managed identity."
            ) from None
        return token.token


def _raise_for_status(status: int, secret: KeyVaultSecretId, *, storing: bool) -> None:
    if 200 <= status < 300:
        return
    vault = secret.vault_name
    details = {"status": status, "vault": vault}
    if 300 <= status < 400:
        action = "stored" if storing else "deleted"
        raise UpstreamError(
            f"Key Vault {vault} answered with a redirect (HTTP {status}), and MOSAIC follows "
            f"none, so it couldn't confirm the API key was {action}.",
            details=details,
        )
    if status in {401, 403}:
        verb = "store" if storing else "delete"
        raise UpstreamAuthorizationError(
            f"MOSAIC isn't allowed to {verb} secrets in Key Vault {vault}. Grant its identity Key "
            "Vault Secrets Officer on the vault.",
            details=details,
        )
    if status == 409:
        raise ConflictError(
            f"Key Vault {vault} still keeps a deleted secret of that name, so MOSAIC couldn't "
            "write it. Recover the deleted secret in Key Vault, or remove the endpoint and "
            "register it again so MOSAIC uses a new secret.",
            details=details,
        )
    action = "store" if storing else "delete"
    raise UpstreamError(
        f"Key Vault {vault} refused to {action} the API key (HTTP {status}).", details=details
    )
