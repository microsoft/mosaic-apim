"""Check that an Azure AI resource accepts an API key, without a billed call (ADR 0018).

MOSAIC reaches a key-authenticated resource only to confirm the key it holds works. It lists the
Azure OpenAI models the resource offers, a metadata read that runs no model and costs nothing, and
reads only the status code. A key can't list the resource's deployments: Foundry lists them only to
a Microsoft Entra token, and the Azure OpenAI deployments listing left the data plane after
2022-12-01. So this confirms the key and the resource, never that a deployment exists.

The key is passed in for one request and goes nowhere else. It is never logged, and a failure is
described by its status code alone, because a response body is not MOSAIC's to repeat.
"""

from dataclasses import dataclass
from enum import StrEnum

import httpx
import structlog

from mosaic_api.domain import azure_ai_account_subdomain

logger = structlog.get_logger()

# The GA version of Models - List. The v1 route answers the same question, but this one is the
# contract Microsoft documents for key authentication across Azure OpenAI and Foundry resources.
MODELS_LIST_API_VERSION = "2024-10-21"


class KeyCheckOutcome(StrEnum):
    ACCEPTED = "accepted"
    REFUSED = "refused"
    UNREACHABLE = "unreachable"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class KeyCheckResult:
    outcome: KeyCheckOutcome
    status_code: int | None = None


class EndpointKeyProbe:
    """One unbilled, authenticated read against an Azure AI resource's own host."""

    def __init__(self, client: httpx.AsyncClient | None = None, *, timeout: float = 15.0) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def check(self, origin: str, key: str) -> KeyCheckResult:
        # Registration already admits only these hosts. Checking again here means a key can only
        # ever be sent to the resource it belongs to, whatever a stored record says.
        if azure_ai_account_subdomain(origin) is None or not origin.startswith("https://"):
            return KeyCheckResult(KeyCheckOutcome.INCONCLUSIVE)
        try:
            response = await self._client.get(
                f"{origin.rstrip('/')}/openai/models",
                params={"api-version": MODELS_LIST_API_VERSION},
                headers={"api-key": key, "Accept": "application/json"},
            )
        except httpx.HTTPError as error:
            logger.warning("endpoint_key_check_unreachable", error_type=type(error).__name__)
            return KeyCheckResult(KeyCheckOutcome.UNREACHABLE)
        status = response.status_code
        if 200 <= status < 300:
            return KeyCheckResult(KeyCheckOutcome.ACCEPTED, status)
        if status in {401, 403}:
            return KeyCheckResult(KeyCheckOutcome.REFUSED, status)
        logger.warning("endpoint_key_check_inconclusive", status_code=status)
        return KeyCheckResult(KeyCheckOutcome.INCONCLUSIVE, status)
