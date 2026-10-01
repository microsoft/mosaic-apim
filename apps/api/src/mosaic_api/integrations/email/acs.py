"""The Communication Services Email ``emails:send`` operation, with a managed identity token.

Each send carries an ``Operation-Id`` the caller derives from the notification it sends, so a send
tried again after an answer that never came is the same operation, not a second email.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError

from mosaic_api.budgets import EMAIL_HOST_SUFFIXES

EMAIL_API_VERSION = "2023-03-31"
_COMMERCIAL_SCOPE = "https://communication.azure.com/.default"
_GOVERNMENT_SCOPE = "https://communication.azure.us/.default"
_ERROR_LENGTH = 300


@dataclass(frozen=True)
class EmailMessage:
    subject: str
    plain_text: str
    html: str
    to: Sequence[str]


@dataclass(frozen=True)
class EmailSendResult:
    """Whether Communication Services accepted the email for delivery, and why not if it didn't."""

    accepted: bool
    operation_id: str
    status_code: int | None = None
    error: str | None = None


class EmailSender(Protocol):
    async def send(
        self, endpoint: str, sender: str, message: EmailMessage, *, operation_id: str
    ) -> EmailSendResult: ...

    async def close(self) -> None: ...


def email_scope(endpoint: str) -> str:
    """The token scope for a Communication Services endpoint, by the cloud its host is in."""

    host = (urlsplit(endpoint).hostname or "").casefold()
    return _GOVERNMENT_SCOPE if host.endswith(".communication.azure.us") else _COMMERCIAL_SCOPE


def _allowed(endpoint: str) -> bool:
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").casefold()
    return parts.scheme == "https" and host.endswith(EMAIL_HOST_SUFFIXES)


def _error(payload: Any, status: int) -> str:
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    text = " ".join(part for part in (code, message) if isinstance(part, str) and part.strip())
    return (text or f"Communication Services answered HTTP {status}.")[:_ERROR_LENGTH]


class AcsEmailClient:
    """Queues email with Communication Services. A refusal is returned, never raised."""

    def __init__(
        self,
        credential: AsyncTokenCredential,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._credential = credential
        # The token goes only to a Communication Services host, and a redirect isn't followed to
        # take it anywhere else.
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=False
        )
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(
        self, endpoint: str, sender: str, message: EmailMessage, *, operation_id: str
    ) -> EmailSendResult:
        if not _allowed(endpoint):
            return EmailSendResult(
                False,
                operation_id,
                error="The endpoint isn't a Communication Services endpoint.",
            )
        if not message.to:
            return EmailSendResult(False, operation_id, error="The email names no recipient.")
        try:
            token = await self._credential.get_token(email_scope(endpoint))
        except ClientAuthenticationError:
            return EmailSendResult(
                False,
                operation_id,
                error="MOSAIC couldn't get a Communication Services token with its identity.",
            )
        body = {
            "senderAddress": sender,
            "recipients": {"to": [{"address": address} for address in message.to]},
            "content": {
                "subject": message.subject,
                "plainText": message.plain_text,
                "html": message.html,
            },
            "userEngagementTrackingDisabled": True,
        }
        try:
            response = await self._client.post(
                f"{endpoint.rstrip('/')}/emails:send",
                params={"api-version": EMAIL_API_VERSION},
                headers={
                    "Authorization": f"Bearer {token.token}",
                    "Operation-Id": operation_id,
                    "x-ms-client-request-id": str(uuid4()),
                },
                json=body,
            )
        except httpx.HTTPError as failure:
            return EmailSendResult(
                False,
                operation_id,
                error=f"Communication Services didn't answer ({type(failure).__name__}).",
            )
        if response.status_code == 202:
            return EmailSendResult(True, operation_id, 202)
        if response.status_code == 409:
            # The operation ID is already Communication Services': an earlier try of this same
            # email was accepted, so it isn't sent again.
            return EmailSendResult(True, operation_id, 409)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return EmailSendResult(
            False, operation_id, response.status_code, _error(payload, response.status_code)
        )
