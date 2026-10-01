"""Budgets from end to end: the check, its emails, the gateway lists, and who may see what.

Built on ``test_cost``'s estate, where Research's grants carry $4,320 of provisioned capacity this
month, through 18 March 2026. See ADR 0023.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from mosaic_api.budgets import (
    BLOCKED_COST_CENTERS_NAMED_VALUE,
    NONE_BLOCKED,
    Budget,
    budget_id,
    budget_key,
)
from mosaic_api.domain import ManagementMode
from mosaic_api.errors import UpstreamError
from mosaic_api.integrations.email import EmailMessage, EmailSendResult
from mosaic_api.services.budget_gate import BlockedListGate
from mosaic_api.services.budgets import (
    CHECKS_OFF,
    EMAIL_OFF,
    MONTH_CHANGE_RETRY_SECONDS,
    NOBODY,
    BudgetCheck,
    BudgetService,
)
from test_cost import (
    ALICE,
    ANALYSTS,
    BOB,
    CAROL,
    GATEWAY,
    MARCH_SO_FAR,
    TENANT,
    Harness,
    _audit,
    harness,
    settings,
)
from test_cost_center_analytics import GENERAL, RESEARCH, _charge_research

__all__ = ["harness", "settings"]

ENDPOINT = "https://contoso-mosaic.communication.azure.com"
SENDER = "DoNotReply@contoso.com"
OWNERS = ["research-lead@contoso.com"]


class FakeEmail:
    """Communication Services, as far as MOSAIC can tell: it accepts or refuses each send."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, EmailMessage, str]] = []
        self.refusals = 0

    async def send(
        self, endpoint: str, sender: str, message: EmailMessage, *, operation_id: str
    ) -> EmailSendResult:
        self.sent.append((endpoint, sender, message, operation_id))
        if self.refusals:
            self.refusals -= 1
            return EmailSendResult(False, operation_id, 503, "ServiceUnavailable Try later.")
        return EmailSendResult(True, operation_id, 202)

    async def close(self) -> None:
        return None

    def subjects(self) -> list[str]:
        return [message.subject for _, _, message, _ in self.sent]


class NamedValues:
    """A managed gateway's named values, read and written as MOSAIC's ARM clients would."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.writes: list[str] = []
        self.failing = False

    async def get_named_value(self, name: str) -> dict[str, Any] | None:
        if self.failing:
            raise UpstreamError("The gateway didn't answer")
        if name not in self.values:
            return None
        return {"name": name, "properties": {"value": self.values[name], "secret": False}}

    async def put_plain_named_value(self, name: str, value: str) -> dict[str, Any]:
        if self.failing:
            raise UpstreamError("The gateway didn't answer")
        self.values[name] = value
        self.writes.append(value)
        return {"name": name}

    @property
    def blocked(self) -> str | None:
        return self.values.get(BLOCKED_COST_CENTERS_NAMED_VALUE)


class Budgets:
    def __init__(self, harness: Harness, *, enabled: bool = True) -> None:
        self.harness = harness
        self.email = FakeEmail()
        self.gateway = NamedValues()
        state = harness.state
        self.repository = state.budget_repository
        self.gate = BlockedListGate(
            self.repository,
            gateway_repository=state.gateway_repository,
            clock=lambda: harness.now,
            enabled=enabled,
        )
        self.service = BudgetService(
            self.repository,
            cost_center_repository=state.cost_center_repository,
            gateway_repository=state.gateway_repository,
            gate=self.gate,
            client_factory=lambda _resource: cast(Any, self.gateway),
            writer_factory=lambda _resource: cast(Any, self.gateway),
            analytics=state.analytics_service,
            email=self.email,
            portal=state.portal_service,
            tenant_id=TENANT,
            clock=lambda: harness.now,
            enabled=enabled,
        )
        state.budget_service = self.service

    async def manage_gateway(self) -> None:
        repository = self.harness.state.gateway_repository
        gateway = await repository.get_gateway(TENANT, GATEWAY)
        assert gateway is not None
        await repository.save_gateway(
            gateway.model_copy(update={"management_mode": ManagementMode.MANAGE}),
            _audit("gateway"),
        )

    def put(self, path: str, payload: dict[str, Any], status: int = 200) -> Any:
        response = self.harness.client.put(path, json=payload)
        assert response.status_code == status, response.text
        return response.json()

    def budget(self, amount: float, **values: Any) -> Any:
        return self.put(f"/api/v1/cost-centers/{RESEARCH}/budget", {"amount": amount, **values})

    def email_on(self) -> None:
        self.put(
            "/api/v1/settings/email",
            {"enabled": True, "endpoint": ENDPOINT, "sender": SENDER},
        )

    async def check(self) -> None:
        assert await self.service.run_check(full=True) is not None


@pytest.fixture
async def budgets(harness: Harness) -> Budgets:
    await _charge_research(harness)
    cost_centers = harness.state.cost_center_repository
    research = await cost_centers.get_cost_center(TENANT, RESEARCH)
    assert research is not None
    await cost_centers.save_cost_center(
        research.model_copy(update={"owners": OWNERS}), _audit("costCenter")
    )
    return Budgets(harness)


# -- what a check finds ----------------------------------------------------------------------


async def test_a_cost_centers_budget_is_judged_on_exactly_its_cost_tab_spend(
    budgets: Budgets,
) -> None:
    view = budgets.budget(5000)

    spend = await budgets.harness.state.analytics_service.cost_center_spend(TENANT, RESEARCH)
    assert spend is not None
    status = view["status"]
    assert status["monthToDate"] == pytest.approx(MARCH_SO_FAR)
    assert status["monthToDate"] == pytest.approx(spend.month_to_date)
    assert status["forecast"] == pytest.approx(spend.forecast)
    assert status["used"] == pytest.approx(MARCH_SO_FAR / 5000, abs=1e-4)
    assert status["level"] == "warning"
    assert status["crossed"] == [80]
    assert status["month"] == "2026-03"


async def test_one_read_prices_every_budget_as_each_would_be_priced_alone(
    budgets: Budgets,
) -> None:
    analytics = budgets.harness.state.analytics_service

    batch = await analytics.budget_spend(TENANT, [RESEARCH, GENERAL])

    assert batch is not None
    for cost_center in (RESEARCH, GENERAL):
        alone = await analytics.cost_center_spend(TENANT, cost_center)
        assert alone is not None
        assert batch.cost_centers[cost_center] == alone
    overview = budgets.harness.get("/api/v1/analytics/overview", range="30d")
    assert batch.organization is not None
    assert batch.organization.month_to_date == pytest.approx(overview["spend"]["monthToDate"])


async def test_the_organizations_budget_counts_every_call_and_only_warns(
    budgets: Budgets,
) -> None:
    budgets.email_on()
    budgets.put("/api/v1/budgets/organization", {"amount": 4000, "recipients": ["cfo@contoso.com"]})

    view = budgets.harness.get("/api/v1/budgets/organization")

    # Research's capacity and Alice's chat: more than Research alone.
    assert view["status"]["monthToDate"] > MARCH_SO_FAR
    assert view["status"]["level"] == "exceeded"
    assert view["status"]["blocked"] is False
    [(_, _, message, _)] = budgets.email.sent
    assert message.to == ["cfo@contoso.com"]
    assert "Your organization" in message.subject
    response = budgets.harness.client.put(
        "/api/v1/budgets/organization", json={"amount": 4000, "action": "block"}
    )
    assert response.status_code == 422


# -- thresholds and their emails -------------------------------------------------------------


async def test_without_email_set_up_a_threshold_is_recorded_and_no_one_is_emailed(
    budgets: Budgets,
) -> None:
    view = budgets.budget(5000, recipients=["finance@fabrikam.com"])

    [notice] = view["status"]["notifications"]
    assert (notice["threshold"], notice["status"], notice["error"]) == (80, "skipped", EMAIL_OFF)
    assert budgets.email.sent == []
    overview = budgets.harness.get("/api/v1/budgets")
    assert any("Email is off" in note for note in overview["notes"])


async def test_each_threshold_emails_its_recipients_once_a_month(budgets: Budgets) -> None:
    budgets.email_on()

    budgets.budget(5000, recipients=["finance@fabrikam.com"])
    await budgets.check()
    await budgets.check()

    [(endpoint, sender, message, operation_id)] = budgets.email.sent
    assert (endpoint, sender) == (ENDPOINT, SENDER)
    # The cost center's owners and anyone the budget names, who needn't use MOSAIC.
    assert message.to == [*OWNERS, "finance@fabrikam.com"]
    assert message.subject == (
        "MOSAIC budget: Research (RES) has reached 80% of its March 2026 budget"
    )
    assert "$4,320.00 of its $5,000.00 budget" in message.plain_text
    view = budgets.harness.get(f"/api/v1/cost-centers/{RESEARCH}/budget")
    [notice] = view["status"]["notifications"]
    assert (notice["status"], notice["recipients"], notice["attempts"]) == ("sent", 2, 1)
    assert operation_id  # the same notification is always the same operation


async def test_a_refused_email_is_tried_again_at_the_next_check(budgets: Budgets) -> None:
    budgets.email_on()
    budgets.email.refusals = 1

    budgets.budget(5000)
    first = budgets.harness.get(f"/api/v1/cost-centers/{RESEARCH}/budget")
    await budgets.check()

    [refused] = first["status"]["notifications"]
    assert (refused["status"], refused["attempts"]) == ("failed", 1)
    assert "ServiceUnavailable" in refused["error"]
    view = budgets.harness.get(f"/api/v1/cost-centers/{RESEARCH}/budget")
    [sent] = view["status"]["notifications"]
    assert (sent["status"], sent["attempts"]) == ("sent", 2)
    # Both tries were the same Communication Services operation.
    assert len({operation for _, _, _, operation in budgets.email.sent}) == 1


async def test_a_budget_with_no_one_to_email_says_so(budgets: Budgets) -> None:
    budgets.email_on()

    view = budgets.budget(5000, notifyOwners=False)

    [notice] = view["status"]["notifications"]
    assert (notice["status"], notice["error"]) == ("skipped", NOBODY)
    assert budgets.email.sent == []


# -- blocking --------------------------------------------------------------------------------


async def test_a_budget_used_up_blocks_its_cost_center_at_every_managed_gateway(
    budgets: Budgets,
) -> None:
    await budgets.manage_gateway()
    budgets.email_on()

    view = budgets.budget(4000, action="block")

    status = view["status"]
    assert status["level"] == "blocked"
    assert status["blocked"] is True
    assert budgets.gateway.blocked == budget_key(RESEARCH)
    [gateway] = status["gateways"]
    assert (gateway["gatewayId"], gateway["enforcing"]) == (GATEWAY, True)
    # The 80% email is covered by the 100% one, and the block has its own.
    assert budgets.email.subjects() == [
        "MOSAIC budget: Research (RES) has reached 100% of its March 2026 budget",
        "MOSAIC budget: calls charged to Research (RES) are now blocked",
    ]
    assert "Every gateway MOSAIC manages refuses them now." in budgets.email.sent[-1][2].plain_text
    # Nothing new at the next check: the list already says so, and the emails went.
    writes = len(budgets.gateway.writes)
    await budgets.check()
    assert len(budgets.gateway.writes) == writes
    assert len(budgets.email.sent) == 2


async def test_raising_the_budget_lifts_the_block_at_once(budgets: Budgets) -> None:
    await budgets.manage_gateway()
    budgets.email_on()
    budgets.budget(4000, action="block")

    view = budgets.budget(10_000, action="block")

    assert view["status"]["blocked"] is False
    assert view["status"]["unblockReason"] == "budgetRaised"
    assert budgets.gateway.blocked == NONE_BLOCKED
    assert budgets.email.subjects()[-1] == (
        "MOSAIC budget: calls charged to Research (RES) are allowed again"
    )


async def test_turning_blocking_off_or_removing_the_budget_lifts_the_block(
    budgets: Budgets,
) -> None:
    await budgets.manage_gateway()
    budgets.budget(4000, action="block")
    assert budgets.gateway.blocked == budget_key(RESEARCH)

    budgets.budget(4000, action="continue")
    assert budgets.gateway.blocked == NONE_BLOCKED

    budgets.budget(4000, action="block")
    assert budgets.gateway.blocked == budget_key(RESEARCH)
    response = budgets.harness.client.delete(f"/api/v1/cost-centers/{RESEARCH}/budget")
    assert response.status_code == 204, response.text
    assert budgets.gateway.blocked == NONE_BLOCKED
    assert await budgets.repository.get_budget_state(TENANT, budget_id(TENANT, RESEARCH)) is None


async def test_a_budget_set_again_while_the_old_one_is_removed_keeps_its_own_state(
    budgets: Budgets,
) -> None:
    await budgets.manage_gateway()
    budgets.budget(4000, action="block")
    budgets.email_on()
    repository = budgets.repository
    identifier = budget_id(TENANT, RESEARCH)
    remove = repository.delete_budget_state

    async def set_again_first(tenant_id: str, budget: str, *, etag: str | None = None) -> bool:
        # Another administrator sets the budget again, and its check saves its state first.
        await repository.save_budget(
            Budget(
                id=budget,
                tenant_id=tenant_id,
                cost_center_id=RESEARCH,
                amount=3000,
                action="block",
            ),
            _audit("budget"),
        )
        await repository.update_budget_state(
            tenant_id,
            budget,
            lambda state: state.model_copy(update={"blocked": True, "crossed": [50]}),
        )
        return await remove(tenant_id, budget, etag=etag)

    cast(Any, repository).delete_budget_state = set_again_first
    response = budgets.harness.client.delete(f"/api/v1/cost-centers/{RESEARCH}/budget")

    assert response.status_code == 204, response.text
    kept = await repository.get_budget_state(TENANT, identifier)
    assert kept is not None
    assert kept.crossed == [50]
    assert kept.blocked is True
    # The gateways are written from the budgets as saved, so the new budget's block stays, and
    # the removed one's recipients aren't told calls are allowed while the new one blocks them.
    assert budgets.gateway.blocked == budget_key(RESEARCH)
    assert budgets.email.sent == []


async def test_removing_a_blocked_budget_lifts_its_block_even_if_its_state_changed_meanwhile(
    budgets: Budgets,
) -> None:
    await budgets.manage_gateway()
    budgets.budget(4000, action="block")
    budgets.email_on()
    repository = budgets.repository
    remove = repository.delete_budget_state

    async def saved_again_first(tenant_id: str, budget: str, *, etag: str | None = None) -> bool:
        # A check records an email's outcome between the lift and the delete.
        await repository.update_budget_state(tenant_id, budget, lambda state: state)
        return await remove(tenant_id, budget, etag=etag)

    cast(Any, repository).delete_budget_state = saved_again_first
    response = budgets.harness.client.delete(f"/api/v1/cost-centers/{RESEARCH}/budget")

    assert response.status_code == 204, response.text
    # The state stays for the next full check to remove, but the block lifts at once.
    assert budgets.gateway.blocked == NONE_BLOCKED
    [(_, _, message, _)] = budgets.email.sent
    assert message.subject.endswith("are allowed again")
    assert "Every gateway MOSAIC manages allows them again." in message.plain_text


async def test_a_rollup_during_a_full_check_gets_a_full_check_of_its_own(
    budgets: Budgets,
) -> None:
    service = budgets.service
    check = service.run_check
    fulls: list[bool] = []

    async def rollup_meanwhile(
        *, full: bool = True, only: Collection[str] | None = None
    ) -> BudgetCheck | None:
        fulls.append(full)
        if len(fulls) == 1:
            service.rollups_changed()
        return await check(full=full, only=only)

    cast(Any, service).run_check = rollup_meanwhile
    await service._tick()
    await service._tick()

    assert fulls == [True, True]


async def test_a_full_check_that_fails_is_tried_again_after_the_fast_interval(
    budgets: Budgets,
) -> None:
    service = budgets.service

    async def failing(*, full: bool = True, only: Collection[str] | None = None) -> None:
        raise RuntimeError("Cosmos is unavailable")

    cast(Any, service).run_check = failing
    budgets.harness.now += timedelta(hours=1)
    await service._tick()

    # Asked for again, and not retried every second while the store is down.
    assert service._full_requested
    assert service._delay(budgets.harness.now) == pytest.approx(300)


async def test_the_block_lifts_when_the_utc_month_ends(budgets: Budgets) -> None:
    await budgets.manage_gateway()
    budgets.email_on()
    budgets.budget(4000, action="block")

    budgets.harness.now = datetime(2026, 4, 1, 0, 0, 5, tzinfo=UTC)
    await budgets.check()

    view = budgets.harness.get(f"/api/v1/cost-centers/{RESEARCH}/budget")
    assert view["status"]["blocked"] is False
    assert view["status"]["month"] == "2026-04"
    assert budgets.gateway.blocked == NONE_BLOCKED
    assert budgets.email.subjects()[-1].endswith("are allowed again")
    assert "A new month has started" in budgets.email.sent[-1][2].plain_text


async def test_a_check_that_ran_across_the_months_end_judges_the_new_month_at_once(
    budgets: Budgets,
) -> None:
    budgets.budget(4000, action="block")
    await budgets.check()
    started = budgets.harness.now

    after_midnight = datetime(2026, 4, 1, 0, 0, 2, tzinfo=UTC)

    assert started.month == 3
    assert budgets.service._delay(after_midnight) == MONTH_CHANGE_RETRY_SECONDS
    assert budgets.service._delay(started + timedelta(minutes=1)) > MONTH_CHANGE_RETRY_SECONDS


async def test_with_budget_checks_off_a_budget_is_kept_but_never_judged(
    harness: Harness,
) -> None:
    await _charge_research(harness)
    budgets = Budgets(harness, enabled=False)
    await budgets.manage_gateway()
    budgets.email_on()

    view = budgets.budget(4000, action="block")

    assert view["amount"] == 4000
    assert view["status"]["blocked"] is False
    assert view["status"]["evaluatedAt"] is None
    assert budgets.email.sent == []
    assert budgets.gateway.writes == []
    response = harness.client.post("/api/v1/budgets/check")
    assert response.status_code == 409, response.text
    overview = harness.get("/api/v1/budgets")
    assert CHECKS_OFF in overview["notes"]
    # A list written while checks are off, such as for a new publication, blocks no one.
    assert await budgets.gate.value(TENANT) == (NONE_BLOCKED, [])


async def test_a_check_that_cant_price_the_month_never_lifts_a_block(budgets: Budgets) -> None:
    await budgets.manage_gateway()
    budgets.budget(4000, action="block")

    async def unreadable(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("Cosmos is unavailable")

    budgets.service._analytics = cast(Any, type("Broken", (), {"budget_spend": unreadable})())
    budgets.harness.now += timedelta(hours=1)
    await budgets.check()

    view = budgets.harness.get(f"/api/v1/cost-centers/{RESEARCH}/budget")
    assert view["status"]["blocked"] is True
    assert "couldn't read" in view["status"]["error"]
    assert budgets.gateway.blocked == budget_key(RESEARCH)


async def test_a_gateway_that_cant_be_written_keeps_its_list_and_is_tried_again(
    budgets: Budgets,
) -> None:
    await budgets.manage_gateway()
    budgets.email_on()
    budgets.gateway.failing = True

    view = budgets.budget(4000, action="block")

    assert view["status"]["blocked"] is True
    [gateway] = view["status"]["gateways"]
    assert gateway["enforcing"] is False
    assert "didn't answer" in gateway["error"]
    # The block email says the gateway doesn't refuse the calls yet, rather than that it does.
    block = budgets.email.sent[-1][2]
    assert block.subject.endswith("are now blocked")
    assert "0 of 1 gateways MOSAIC manages refuse them so far" in block.plain_text
    budgets.gateway.failing = False
    await budgets.check()
    assert budgets.gateway.blocked == budget_key(RESEARCH)


async def test_a_list_someone_changed_is_written_again(budgets: Budgets) -> None:
    await budgets.manage_gateway()
    budgets.budget(4000, action="block")
    budgets.gateway.values[BLOCKED_COST_CENTERS_NAMED_VALUE] = "*"

    await budgets.check()

    assert budgets.gateway.blocked == budget_key(RESEARCH)


async def test_the_overview_says_when_blocked_cost_centers_dont_fit_the_list(
    budgets: Budgets,
) -> None:
    await budgets.manage_gateway()
    budgets.budget(4000, action="block")
    await budgets.repository.update_gate_state(
        TENANT, GATEWAY, lambda state: state.model_copy(update={"left_out": ["cc-1", "cc-2"]})
    )

    overview = budgets.harness.get("/api/v1/budgets")

    assert any(
        note.startswith("2 blocked cost centers don't fit in the gateways' list")
        for note in overview["notes"]
    )


async def test_a_removed_cost_centers_budget_goes_with_it(budgets: Budgets) -> None:
    budgets.budget(5000)
    repository = budgets.harness.state.cost_center_repository
    research = await repository.get_cost_center(TENANT, RESEARCH)
    assert research is not None
    await repository.delete_cost_center(research, _audit("costCenter"))

    await budgets.check()

    assert await budgets.repository.get_budget(TENANT, budget_id(TENANT, RESEARCH)) is None


# -- the portal ------------------------------------------------------------------------------


async def test_a_member_sees_their_cost_centers_budget_and_no_one_elses_use(
    budgets: Budgets,
) -> None:
    budgets.budget(4000, action="block")
    budgets.harness.sign_in(CAROL, ["User"], frozenset({ANALYSTS}))

    alerts = budgets.harness.get("/api/v1/me/budgets")

    [alert] = alerts
    assert alert["costCenter"]["code"] == "RES"
    assert (alert["level"], alert["action"], alert["month"]) == ("blocked", "block", "2026-03")
    assert alert["used"] == pytest.approx(MARCH_SO_FAR / 4000, abs=1e-4)
    text = json.dumps(alerts)
    for identity in (BOB, "Bob", ALICE, "Alice", "grant-", "principal-", *OWNERS):
        assert identity not in text
    assert set(alert) == {"costCenter", "level", "month", "used", "action"}

    # Alice's grants are under General, which has no budget.
    budgets.harness.sign_in(ALICE, ["User"], frozenset())
    assert budgets.harness.get("/api/v1/me/budgets") == []


async def test_a_budget_well_within_its_amount_raises_no_alert(budgets: Budgets) -> None:
    budgets.budget(50_000)
    budgets.harness.sign_in(BOB, ["User"], frozenset())

    assert budgets.harness.get("/api/v1/me/budgets") == []


# -- email settings --------------------------------------------------------------------------


async def test_email_is_off_until_settings_turn_it_on(budgets: Budgets) -> None:
    settings = budgets.harness.get("/api/v1/settings/email")

    assert (settings["enabled"], settings["ready"]) == (False, False)
    saved = budgets.put(
        "/api/v1/settings/email",
        {"enabled": False, "endpoint": ENDPOINT, "sender": SENDER},
    )
    assert (saved["enabled"], saved["ready"], saved["endpoint"]) == (False, False, ENDPOINT)
    response = budgets.harness.client.put(
        "/api/v1/settings/email",
        json={"enabled": True, "endpoint": "https://mail.example.com", "sender": SENDER},
    )
    assert response.status_code == 422
    audits = [
        event.action
        for event in budgets.repository.audit_events.values()
        if event.action.startswith("email.")
    ]
    assert audits == ["email.settingsUpdated"]


async def test_a_test_email_goes_through_the_saved_settings(budgets: Budgets) -> None:
    response = budgets.harness.client.post(
        "/api/v1/settings/email/test", json={"to": "me@contoso.com"}
    )
    assert response.status_code == 409
    assert response.json()["details"]["reason"] == "notConfigured"

    budgets.put(
        "/api/v1/settings/email", {"enabled": False, "endpoint": ENDPOINT, "sender": SENDER}
    )
    sent = budgets.harness.client.post("/api/v1/settings/email/test", json={"to": "me@contoso.com"})

    assert sent.status_code == 200, sent.text
    assert sent.json()["sent"] is True
    [(_, _, message, _)] = budgets.email.sent
    assert (message.subject, message.to) == ("MOSAIC test email", ["me@contoso.com"])
    assert budgets.harness.get("/api/v1/settings/email")["lastTestAt"] is not None
    again = budgets.harness.client.post(
        "/api/v1/settings/email/test", json={"to": "me@contoso.com"}
    )
    assert again.status_code == 429


# -- who may do what -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/budgets", None),
        ("POST", "/api/v1/budgets/check", None),
        ("GET", "/api/v1/budgets/organization", None),
        ("PUT", "/api/v1/budgets/organization", {"amount": 10}),
        ("DELETE", "/api/v1/budgets/organization", None),
        ("GET", f"/api/v1/cost-centers/{RESEARCH}/budget", None),
        ("PUT", f"/api/v1/cost-centers/{RESEARCH}/budget", {"amount": 10}),
        ("DELETE", f"/api/v1/cost-centers/{RESEARCH}/budget", None),
        ("GET", "/api/v1/settings/email", None),
        ("PUT", "/api/v1/settings/email", {"enabled": False}),
        ("POST", "/api/v1/settings/email/test", {"to": "me@contoso.com"}),
    ],
)
async def test_only_administrators_see_or_change_budgets_and_email(
    budgets: Budgets, method: str, path: str, body: dict[str, Any] | None
) -> None:
    budgets.harness.sign_in(BOB, ["User"], frozenset())

    response = budgets.harness.client.request(method, path, json=body)

    assert response.status_code == 403


async def test_an_unknown_cost_center_has_no_budget(budgets: Budgets) -> None:
    response = budgets.harness.client.put(
        "/api/v1/cost-centers/costCenter_missing/budget", json={"amount": 100}
    )

    assert response.status_code == 404
    assert budgets.harness.get(f"/api/v1/cost-centers/{GENERAL}/budget") is None


async def test_the_overview_lists_every_budget_worst_first(budgets: Budgets) -> None:
    budgets.budget(4000, action="block")
    budgets.put(f"/api/v1/cost-centers/{GENERAL}/budget", {"amount": 100_000})
    budgets.put("/api/v1/budgets/organization", {"amount": 1_000_000})

    overview = budgets.harness.get("/api/v1/budgets")

    assert [view["costCenter"]["code"] for view in overview["costCenters"]] == ["RES", "general"]
    assert [view["status"]["level"] for view in overview["costCenters"]] == ["blocked", "ok"]
    assert overview["organization"]["scope"] == "organization"
    assert overview["unbudgeted"] == 0
    assert overview["month"] == "2026-03"
    audits = sorted(
        event.action
        for event in budgets.repository.audit_events.values()
        if event.action.startswith("budget.")
    )
    assert audits == [
        "budget.blocked",
        "budget.created",
        "budget.created",
        "budget.created",
        "budget.thresholdReached",
    ]
