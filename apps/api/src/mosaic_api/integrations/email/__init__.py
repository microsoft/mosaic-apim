"""Email through Azure Communication Services, over REST, as MOSAIC's managed identity.

MOSAIC keeps no secret for this: it signs in to Communication Services as itself, so the resource
grants its identity a role, and Settings holds only the endpoint and the sender address. See
ADR 0023.
"""

from .acs import (
    EMAIL_API_VERSION,
    AcsEmailClient,
    EmailMessage,
    EmailSender,
    EmailSendResult,
    email_scope,
)

__all__ = [
    "EMAIL_API_VERSION",
    "AcsEmailClient",
    "EmailMessage",
    "EmailSendResult",
    "EmailSender",
    "email_scope",
]
