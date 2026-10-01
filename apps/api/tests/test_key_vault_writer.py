"""What the Key Vault writer accepts as proof that Key Vault stored or deleted a key (ADR 0021).

MOSAIC follows no redirects, so only a 2xx answer confirms the operation. Treating a 3xx as success
would register an endpoint whose secret was never written, or forget one whose secret is still live.
"""

import json
from typing import Any

import httpx
import pytest
from apim_double import FakeCredential
from mosaic_api.errors import UpstreamError
from mosaic_api.integrations.key_vault import KeyVaultSecretWriter
from pydantic import SecretStr

KEY = "fictional-key-VALUE-never-logged-1234"
VAULT_URI = "https://kv-contoso-ai.vault.azure.net"


class TestWriter:
    def writer(self, handler: Any) -> KeyVaultSecretWriter:
        return KeyVaultSecretWriter(
            FakeCredential(),  # type: ignore[arg-type]
            f"{VAULT_URI}/",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    @pytest.mark.parametrize("status", [301, 302, 307])
    async def test_a_redirect_never_counts_as_a_stored_key(self, status: int) -> None:
        writer = self.writer(lambda _: httpx.Response(status, json={"value": KEY}))

        with pytest.raises(UpstreamError) as refused:
            await writer.put(writer.new_secret("mosaic-apikey-x-0a1b2c3d"), SecretStr(KEY), tags={})

        assert "redirect" in str(refused.value)
        assert "stored" in str(refused.value)
        assert KEY not in str(refused.value)
        assert KEY not in json.dumps(refused.value.details)

    async def test_a_successful_put_still_succeeds(self) -> None:
        writer = self.writer(lambda _: httpx.Response(200, json={"value": KEY}))

        await writer.put(writer.new_secret("mosaic-apikey-x-0a1b2c3d"), SecretStr(KEY), tags={})

    @pytest.mark.parametrize("status", [301, 302, 307])
    async def test_a_redirect_never_counts_as_a_deleted_key(self, status: int) -> None:
        writer = self.writer(lambda _: httpx.Response(status, json={"value": KEY}))

        with pytest.raises(UpstreamError) as refused:
            await writer.delete(writer.new_secret("mosaic-apikey-x-0a1b2c3d"))

        assert "redirect" in str(refused.value)
        assert "deleted" in str(refused.value)
        assert KEY not in str(refused.value)
        assert KEY not in json.dumps(refused.value.details)

    @pytest.mark.parametrize(("status", "deleted"), [(200, True), (404, False)])
    async def test_delete_keeps_success_and_already_gone_behaviour(
        self, status: int, deleted: bool
    ) -> None:
        writer = self.writer(lambda _: httpx.Response(status, json={}))

        assert await writer.delete(writer.new_secret("mosaic-apikey-x-0a1b2c3d")) is deleted
