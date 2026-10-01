"""What MOSAIC's budget emails say. Plain words, the figures, and what happens next. ADR 0023."""

from __future__ import annotations

from datetime import date
from html import escape

from mosaic_api.budgets import Budget, BudgetNotification, BudgetState
from mosaic_api.domain import CostCenterRef
from mosaic_api.integrations.email import EmailMessage

_UNBLOCKED_WHY: dict[str, str] = {
    "newMonth": "A new month has started, so its budget starts again.",
    "budgetRaised": "An administrator raised its budget above what it has spent.",
    "blockingOff": "An administrator set its budget to let calls continue.",
    "budgetRemoved": "An administrator removed its budget.",
}


def money(value: float | None) -> str:
    return "an amount MOSAIC can't price" if value is None else f"${value:,.2f}"


def _month(month: str | None) -> str:
    if not month:
        return "this month"
    year, number = (int(part) for part in month.split("-"))
    return date(year, number, 1).strftime("%B %Y")


def _subject_name(budget: Budget, cost_center: CostCenterRef | None) -> str:
    if budget.scope == "organization" or cost_center is None:
        return "Your organization"
    return f"{cost_center.name} ({cost_center.code})"


def _html(paragraphs: list[str]) -> str:
    body = "".join(f"<p>{escape(paragraph)}</p>" for paragraph in paragraphs)
    return f"<html><body style=\"font-family:Segoe UI,Arial,sans-serif\">{body}</body></html>"


def _message(subject: str, paragraphs: list[str], to: list[str]) -> EmailMessage:
    footer = (
        "MOSAIC sent this because you're a recipient of this budget. An administrator can change "
        "who it emails on the cost center's page in MOSAIC's console."
    )
    lines = [*paragraphs, footer]
    return EmailMessage(subject=subject, plain_text="\n\n".join(lines), html=_html(lines), to=to)


def budget_email(
    notice: BudgetNotification,
    budget: Budget,
    state: BudgetState,
    cost_center: CostCenterRef | None,
    to: list[str],
) -> EmailMessage:
    who = _subject_name(budget, cost_center)
    month = _month(state.month)
    spent = money(state.month_to_date)
    amount = money(budget.amount)
    forecast = (
        f"At this pace, MOSAIC projects {money(state.forecast)} by the end of the month."
        if state.forecast is not None
        else "MOSAIC projects the month's end once it has a day of figures."
    )
    estimate = (
        "Figures are list-price estimates from gateway usage, before any discount, and trail "
        "calls by up to about half an hour."
    )
    if notice.kind == "threshold":
        threshold = notice.threshold or 100
        if budget.scope == "organization":
            next_step = "The organization's budget only warns, so calls continue."
        elif budget.blocks:
            next_step = (
                "Calls charged to it are blocked at the gateway once it reaches 100% of its "
                "budget, until an administrator raises the budget or the month ends."
            )
        else:
            next_step = "Its calls continue past 100%: this budget only warns."
        return _message(
            f"MOSAIC budget: {who} has reached {threshold}% of its {month} budget",
            [
                f"{who} has spent {spent} of its {amount} budget for {month}, which is "
                f"{threshold}% or more.",
                forecast,
                next_step,
                estimate,
            ],
            to,
        )
    if notice.kind == "blocked":
        return _message(
            f"MOSAIC budget: calls charged to {who} are now blocked",
            [
                f"{who} has spent {spent}, all of its {amount} budget for {month}. Its budget is "
                "set to block calls at 100%, so the gateway now refuses calls charged to it with "
                "403, naming the budget.",
                "The block lifts when an administrator raises the budget above what's been spent, "
                "sets it to let calls continue, or when the month ends.",
                estimate,
            ],
            to,
        )
    why = _UNBLOCKED_WHY.get(notice.reason or "", "Its budget allows calls again.")
    return _message(
        f"MOSAIC budget: calls charged to {who} are allowed again",
        [
            f"The gateway no longer blocks calls charged to {who}. {why}",
            f"It has spent {spent} of its {amount} budget for {month}.",
        ],
        to,
    )


def trial_email(to: str) -> EmailMessage:
    return EmailMessage(
        subject="MOSAIC test email",
        plain_text=(
            "This is a test from MOSAIC. Budget emails will come from this address when a cost "
            "center reaches a threshold, and when calls are blocked or allowed again."
        ),
        html=_html(
            [
                "This is a test from MOSAIC. Budget emails will come from this address when a "
                "cost center reaches a threshold, and when calls are blocked or allowed again."
            ]
        ),
        to=[to],
    )
