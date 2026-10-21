"""Provider-neutral email contracts. Acceptance is not a delivery receipt."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmailMessage:
    subject: str
    plain_text: str
    html: str
    to: Sequence[str]


@dataclass(frozen=True)
class EmailSendResult:
    accepted: bool
    operation_id: str
    status_code: int | None = None
    error: str | None = None
    retry_after_seconds: int | None = None
    outcome_unknown: bool = False
    error_code: str | None = None
    request_id: str | None = None

    @property
    def retryable(self) -> bool:
        return (
            self.outcome_unknown
            or self.status_code in {408, 429, 500, 502, 503, 504}
            or self.error_code in {"graphNetworkUnavailable", "graphIdentityUnavailable"}
        )


@dataclass(frozen=True)
class EmailAccessResult:
    authenticated: bool
    identity_object_id: str | None = None
    error: str | None = None
    error_code: str | None = None


class EmailSender(Protocol):
    async def send(
        self, sender_mailbox_id: str, message: EmailMessage, *, operation_id: str
    ) -> EmailSendResult: ...

    async def check_access(self) -> EmailAccessResult: ...

    async def close(self) -> None: ...
