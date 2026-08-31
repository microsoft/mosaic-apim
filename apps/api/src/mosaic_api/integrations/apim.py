from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from azure.core.credentials_async import AsyncTokenCredential


@dataclass(frozen=True)
class ObservedApi:
    name: str
    path: str
    revision: str | None


class ApimObserver(Protocol):
    async def list_apis(self) -> list[ObservedApi]: ...


class AzureApimObserver:
    def __init__(
        self,
        credential: AsyncTokenCredential,
        subscription_id: str,
        resource_group: str,
        service_name: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._credential = credential
        self._base_url = (
            "https://management.azure.com/subscriptions/"
            f"{subscription_id}/resourceGroups/{resource_group}/providers/"
            f"Microsoft.ApiManagement/service/{service_name}"
        )
        self._client = client or httpx.AsyncClient(timeout=20)
        self._owns_client = client is None

    async def list_apis(self) -> list[ObservedApi]:
        token = await self._credential.get_token("https://management.azure.com/.default")
        response = await self._client.get(
            f"{self._base_url}/apis",
            params={"api-version": "2024-05-01"},
            headers={"Authorization": f"Bearer {token.token}"},
        )
        response.raise_for_status()
        return [
            ObservedApi(
                name=item["name"],
                path=item.get("properties", {}).get("path", ""),
                revision=item.get("properties", {}).get("apiRevision"),
            )
            for item in response.json().get("value", [])
        ]

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def api_fingerprint(api: ObservedApi) -> tuple[str, str, str | None]:
    return api.name, api.path, api.revision


ObservedPayload = dict[str, Any]
