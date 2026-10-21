"""Email through Microsoft Graph using mailbox-scoped application authorization."""

from .contracts import (
    EmailAccessResult,
    EmailMessage,
    EmailSender,
    EmailSendResult,
)
from .graph import GraphEmailClient

__all__ = [
    "EmailAccessResult",
    "EmailMessage",
    "EmailSendResult",
    "EmailSender",
    "GraphEmailClient",
]
