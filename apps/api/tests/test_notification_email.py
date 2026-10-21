"""Graph adapter preserves audience boundaries and reports acceptance, not receipt."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import cast

import httpx
import pytest
from azure.core.credentials_async import AsyncTokenCredential
from mosaic_api.budgets import EmailSettings
from mosaic_api.domain import AuditEvent
from mosaic_api.integrations.email import EmailMessage, EmailSendResult, GraphEmailClient
from mosaic_api.notifications import NotificationDelivery, NotificationSubject
from mosaic_api.repositories.memory_budgets import InMemoryBudgetRepository
from mosaic_api.services.notification_email import GraphNotificationDispatcher
from test_email_client import MAILBOX, MESSAGE, SENDER, TENANT, Credential
from test_notification_repository import event


class Sender:
    def __init__(self, result: EmailSendResult) -> None:
        self.result = result
        self.messages: list[EmailMessage] = []
        self.operations: list[str] = []

    async def send(
        self, sender_mailbox_id: str, message: EmailMessage, *, operation_id: str
    ) -> EmailSendResult:
        assert sender_mailbox_id == MAILBOX
        self.messages.append(message)
        self.operations.append(operation_id)
        return self.result

    async def check_access(self):
        from mosaic_api.integrations.email import EmailAccessResult

        return EmailAccessResult(True)

    async def close(self) -> None:
        return None


async def email_settings(repository: InMemoryBudgetRepository, *, enabled: bool = True) -> None:
    await repository.save_email_settings(
        EmailSettings(
            id="email",
            tenant_id="tenant",
            enabled=enabled,
            provider="graph",
            sender_mailbox_id=MAILBOX,
            sender=SENDER,
        ),
        AuditEvent(
            id="audit",
            tenant_id="tenant",
            action="email.updated",
            resource_type="emailSettings",
            resource_id="email",
            actor_object_id="admin",
        ),
    )


def delivery() -> NotificationDelivery:
    return NotificationDelivery(
        id="delivery",
        tenant_id="tenant",
        event_ids=[event().id],
        channel="email",
        persona="portal",
        recipient_reference="user",
    )


async def mailbox(_delivery: NotificationDelivery) -> str:
    return "fictional-user@example.test"


@pytest.mark.parametrize(
    ("response", "outcome"),
    [
        (EmailSendResult(True, "operation", 202), "providerAccepted"),
        (EmailSendResult(True, "operation", 409), "providerAccepted"),
        (EmailSendResult(False, "operation", 429, retry_after_seconds=120), "retryableFailure"),
        (EmailSendResult(False, "operation", 503), "retryableFailure"),
        (EmailSendResult(False, "operation", 400, error="sensitive recipient"), "permanentFailure"),
        (EmailSendResult(False, "operation", 403), "permanentFailure"),
        (EmailSendResult(False, "operation", outcome_unknown=True), "unknownOutcome"),
    ],
)
async def test_adapter_classifies_acceptance_failure_and_ambiguity_without_leaking_errors(
    response: EmailSendResult,
    outcome: str,
) -> None:
    repository = InMemoryBudgetRepository()
    await email_settings(repository)
    sender = Sender(response)
    dispatcher = GraphNotificationDispatcher(
        sender, repository, mailbox_resolver=mailbox, portal_url="https://portal.example.test"
    )
    first = await dispatcher.send(delivery(), [event()])
    second = await dispatcher.send(delivery(), [event()])
    assert first.outcome == second.outcome == outcome
    assert first.retry_after_seconds == response.retry_after_seconds
    assert "sensitive recipient" not in (first.safe_error or "")
    assert sender.operations[0] == sender.operations[1]
    assert all(message.to == ["fictional-user@example.test"] for message in sender.messages)
    assert "/notifications" in sender.messages[0].plain_text


async def test_general_delivery_does_not_depend_on_the_budget_email_switch() -> None:
    repository = InMemoryBudgetRepository()
    await email_settings(repository, enabled=False)
    sender = Sender(EmailSendResult(True, "operation"))
    dispatcher = GraphNotificationDispatcher(sender, repository, mailbox_resolver=mailbox)
    assert (await dispatcher.send(delivery(), [event()])).outcome == "providerAccepted"
    sender.messages.clear()

    async def no_mailbox(_delivery: NotificationDelivery) -> None:
        return None

    enabled_repository = InMemoryBudgetRepository()
    await email_settings(enabled_repository)
    dispatcher = GraphNotificationDispatcher(
        sender, enabled_repository, mailbox_resolver=no_mailbox
    )
    assert (await dispatcher.send(delivery(), [event()])).outcome == "suppressed"
    assert sender.messages == []


@pytest.mark.parametrize("header", ["120", "future-http-date", "invalid", "-1"])
async def test_graph_exposes_retry_after(header: str) -> None:
    if header == "future-http-date":
        header = format_datetime(datetime.now(UTC) + timedelta(hours=1))
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(429, headers={"Retry-After": header})
        )
    )
    sender = GraphEmailClient(
        cast(AsyncTokenCredential, Credential()), tenant_id=TENANT, client=client
    )
    response = await sender.send(MAILBOX, MESSAGE, operation_id="operation")
    assert not response.accepted and response.status_code == 429
    if header == "120":
        assert response.retry_after_seconds == 120
    elif header == "invalid":
        assert response.retry_after_seconds is None
    elif header == "-1":
        assert response.retry_after_seconds == 0
    else:
        assert response.retry_after_seconds and 3500 < response.retry_after_seconds <= 3600
    await client.aclose()


async def test_graph_transport_timeout_exposes_unknown_outcome() -> None:
    def timed_out(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("after acceptance")

    client = httpx.AsyncClient(transport=httpx.MockTransport(timed_out))
    sender = GraphEmailClient(
        cast(AsyncTokenCredential, Credential()), tenant_id=TENANT, client=client
    )
    result = await sender.send(MAILBOX, MESSAGE, operation_id="operation")
    assert not result.accepted and result.outcome_unknown
    await client.aclose()


def test_message_links_only_use_trusted_https_mosaic_origins() -> None:
    sender = Sender(EmailSendResult(True, "operation"))
    for origin in (
        "http://mosaic.test",
        "https://user:password@mosaic.test",
        "https://mosaic/?sig=key",
    ):
        with pytest.raises(ValueError, match="HTTPS"):
            GraphNotificationDispatcher(
                sender, InMemoryBudgetRepository(), mailbox_resolver=mailbox, portal_url=origin
            )


@pytest.mark.parametrize(
    ("persona", "expected_link", "expected_preferences"),
    [
        ("portal", "/requests?request=request", "/notification-preferences"),
        ("admin", "/entitlements?tab=requests&selected=request", "/profile"),
    ],
)
async def test_messages_link_to_exact_authenticated_request_and_personal_preferences(
    persona: str,
    expected_link: str,
    expected_preferences: str,
) -> None:
    repository = InMemoryBudgetRepository()
    await email_settings(repository)
    sender = Sender(EmailSendResult(True, "operation"))
    dispatcher = GraphNotificationDispatcher(
        sender,
        repository,
        mailbox_resolver=mailbox,
        frontend_url="https://admin.example.test",
        portal_url="https://portal.example.test",
    )
    await dispatcher.send(delivery().model_copy(update={"persona": persona}), [event()])
    assert expected_link in sender.messages[0].plain_text
    assert expected_preferences in sender.messages[0].plain_text


@pytest.mark.parametrize(
    ("subject", "path"),
    [
        (
            {"publication_id": "item/a", "publication_target": "model"},
            "/models?tab=published&selected=item%2Fa&runId=run%3F1",
        ),
        (
            {"publication_id": "item/a", "publication_target": "mcp"},
            "/mcps?tab=published&selected=item%2Fa&runId=run%3F1",
        ),
        (
            {"publication_id": "item/a", "publication_target": "pool"},
            "/pools/item%2Fa?tab=plans&runId=run%3F1",
        ),
        (
            {"endpoint_id": "item/a", "endpoint_kind": "model"},
            "/models?tab=endpoints&selected=item%2Fa&syncRun=run%3F1",
        ),
        (
            {"endpoint_id": "item/a", "endpoint_kind": "mcp"},
            "/mcps?tab=registered&selected=item%2Fa&syncRun=run%3F1",
        ),
        (
            {"gateway_id": "item/a", "integration": "gatewayInventory"},
            "/gateways/item%2Fa?syncRun=run%3F1",
        ),
        ({"gateway_id": "item/a", "integration": "gatewayApply"}, "/gateways/item%2Fa"),
    ],
)
async def test_admin_source_links_preserve_exact_historical_runs(
    subject: dict[str, str], path: str
) -> None:
    repository = InMemoryBudgetRepository()
    await email_settings(repository)
    sender = Sender(EmailSendResult(True, "operation"))
    dispatcher = GraphNotificationDispatcher(
        sender, repository, mailbox_resolver=mailbox, frontend_url="https://admin.example.test"
    )
    source = event().model_copy(
        update={"subject": NotificationSubject.model_validate({**subject, "run_id": "run?1"})}
    )
    await dispatcher.send(delivery().model_copy(update={"persona": "admin"}), [source])
    assert "https://admin.example.test" + path in sender.messages[0].plain_text
