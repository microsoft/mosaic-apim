"""ACS notification adapter; the budget bridge preserves legacy content and operation IDs."""

from collections.abc import Awaitable, Callable, Sequence
from html import escape
from urllib.parse import quote, urlsplit
from uuid import NAMESPACE_URL, uuid5

from mosaic_api.budgets import email_address
from mosaic_api.errors import DirectoryError, DirectoryForbiddenError
from mosaic_api.integrations.email import EmailMessage, EmailSender
from mosaic_api.notifications import NotificationDelivery, NotificationEvent
from mosaic_api.repositories.base import BudgetRepository
from mosaic_api.services.notifications import (
    NotificationDispatchResult,
    notification_summary,
    notification_title,
)

MailboxResolver = Callable[[NotificationDelivery], Awaitable[str | None]]


def _notification_path(delivery: NotificationDelivery, event: NotificationEvent) -> str:
    subject = event.subject
    if delivery.persona == "portal":
        if subject.request_id:
            return "/requests?request=" + quote(subject.request_id, safe="")
        if subject.entitlement_id:
            return "/access?grant=" + quote(subject.entitlement_id, safe="")
        return "/notifications"
    if subject.request_id:
        return "/entitlements?tab=requests&selected=" + quote(subject.request_id, safe="")
    if subject.entitlement_id:
        return "/entitlements?tab=grants&selected=" + quote(subject.entitlement_id, safe="")
    run = quote(subject.run_id, safe="") if subject.run_id else None
    if subject.publication_id and subject.publication_target:
        publication = quote(subject.publication_id, safe="")
        if subject.publication_target == "pool":
            path = f"/pools/{publication}?tab=plans"
        else:
            view = "models" if subject.publication_target == "model" else "mcps"
            path = f"/{view}?tab=published&selected={publication}"
        return path + (f"&runId={run}" if run else "")
    if subject.endpoint_id and subject.endpoint_kind:
        endpoint = quote(subject.endpoint_id, safe="")
        path = (
            f"/models?tab=endpoints&selected={endpoint}" if subject.endpoint_kind == "model"
            else f"/mcps?tab=registered&selected={endpoint}"
        )
        return path + (f"&syncRun={run}" if run else "")
    if subject.gateway_id:
        path = "/gateways/" + quote(subject.gateway_id, safe="")
        return path + (
            f"?syncRun={run}" if run and subject.integration == "gatewayInventory" else ""
        )
    return "/notifications"


class AcsNotificationDispatcher:
    supports_idempotency = True

    def __init__(
        self,
        sender: EmailSender,
        settings_repository: BudgetRepository,
        *,
        mailbox_resolver: MailboxResolver,
        frontend_url: str | None = None,
        portal_url: str | None = None,
    ) -> None:
        for origin in (frontend_url, portal_url):
            if origin is not None:
                parts = urlsplit(origin)
                if (
                    parts.scheme != "https"
                    or not parts.hostname
                    or parts.username
                    or parts.password
                    or parts.query
                    or parts.fragment
                ):
                    raise ValueError("Notification links require configured HTTPS MOSAIC origins")
        self._sender = sender
        self._settings = settings_repository
        self._mailbox = mailbox_resolver
        self._frontend_url = frontend_url
        self._portal_url = portal_url

    async def send(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> NotificationDispatchResult:
        operation_id = str(uuid5(NAMESPACE_URL, f"mosaic-notification:{delivery.id}"))
        settings = await self._settings.get_email_settings(delivery.tenant_id)
        if settings is None or not settings.ready or not settings.endpoint or not settings.sender:
            return NotificationDispatchResult(
                "suppressed",
                provider_operation_id=operation_id,
                safe_error="ACS email is not enabled and configured in Settings.",
            )
        try:
            mailbox = await self._mailbox(delivery)
        except DirectoryForbiddenError:
            return NotificationDispatchResult(
                "permanentFailure",
                provider_operation_id=operation_id,
                error_code="directoryForbidden",
                safe_error="MOSAIC lacks permission to verify the recipient mailbox.",
            )
        except DirectoryError:
            return NotificationDispatchResult(
                "retryableFailure",
                provider_operation_id=operation_id,
                error_code="directoryUnavailable",
                safe_error="The directory could not verify the recipient mailbox.",
            )
        if not mailbox:
            return NotificationDispatchResult(
                "suppressed",
                provider_operation_id=operation_id,
                safe_error="The recipient has no directory-backed usable mailbox.",
            )
        try:
            mailbox = email_address(mailbox)
        except ValueError:
            return NotificationDispatchResult(
                "permanentFailure",
                provider_operation_id=operation_id,
                error_code="invalidRecipient",
                safe_error="The recipient mailbox is invalid.",
            )
        lines = [notification_summary(event) for event in events]
        if delivery.digest_window_id:
            lines.insert(0, "This is a summary of completed notification periods.")
        origin = (
            self._frontend_url
            if delivery.persona == "admin"
            else self._portal_url
            if delivery.persona == "portal"
            else None
        )
        if origin:
            for event in events:
                path = _notification_path(delivery, event)
                lines.append(f"View in MOSAIC: {origin.rstrip('/')}{path}")
            preferences_path = (
                "/profile" if delivery.persona == "admin" else "/notification-preferences"
            )
            lines.append(f"Manage notification preferences: {origin.rstrip('/')}{preferences_path}")
            lines.append(
                f"View your inbox and manage notifications: {origin.rstrip('/')}/notifications"
            )
        else:
            lines.append("Open MOSAIC to view your inbox and manage notification preferences.")
        subject = (
            notification_title(events[0]) if len(events) == 1 else "MOSAIC notification summary"
        )
        message = EmailMessage(
            subject=subject,
            plain_text="\n\n".join(lines),
            html="<html><body>"
            + "".join(f"<p>{escape(line)}</p>" for line in lines)
            + "</body></html>",
            to=[mailbox],
        )
        result = await self._sender.send(
            settings.endpoint, settings.sender, message, operation_id=operation_id
        )
        if result.accepted:
            return NotificationDispatchResult(
                "providerAccepted", provider_operation_id=result.operation_id
            )
        if result.outcome_unknown:
            return NotificationDispatchResult(
                "unknownOutcome",
                provider_operation_id=result.operation_id,
                error_code="acsOutcomeUnknown",
                safe_error="ACS did not return a conclusive acceptance response.",
            )
        retryable = result.status_code in {408, 429, 500, 502, 503, 504}
        return NotificationDispatchResult(
            "retryableFailure" if retryable else "permanentFailure",
            provider_operation_id=result.operation_id,
            retry_after_seconds=result.retry_after_seconds,
            error_code=f"acsHttp{result.status_code}" if result.status_code else "acsConfiguration",
            safe_error=(
                f"ACS refused the notification (HTTP {result.status_code})."
                if result.status_code
                else "ACS authentication or configuration is unavailable."
            ),
        )
