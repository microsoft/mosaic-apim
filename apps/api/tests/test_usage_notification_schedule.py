from datetime import UTC, datetime, timedelta

import pytest
from mosaic_api.notifications import DigestSchedule
from mosaic_api.services.usage_notification_schedule import (
    completed_window,
    scheduled_delivery,
)


def test_daily_spring_forward_is_completed_23_hour_local_day() -> None:
    schedule = DigestSchedule(period="daily")
    due = datetime(2026, 3, 9, 13, tzinfo=UTC)
    window = completed_window(due, schedule, "America/New_York")
    assert window.start_at == datetime(2026, 3, 8, 5, tzinfo=UTC)
    assert window.end_at == datetime(2026, 3, 9, 4, tzinfo=UTC)
    assert window.end_at - window.start_at == timedelta(hours=23)


def test_weekly_is_completed_calendar_week_not_last_seven_instants() -> None:
    schedule = DigestSchedule(period="weekly", day_of_week=2)
    due = datetime(2026, 3, 11, 13, tzinfo=UTC)
    window = completed_window(due, schedule, "America/New_York")
    assert window.start_at == datetime(2026, 3, 2, 5, tzinfo=UTC)
    assert window.end_at == datetime(2026, 3, 9, 4, tzinfo=UTC)
    assert window.end_at - window.start_at == timedelta(hours=167)


def test_monthly_is_previous_completed_month_even_with_later_delivery_day() -> None:
    schedule = DigestSchedule(period="monthly", day_of_month=5)
    due = datetime(2026, 3, 5, 14, tzinfo=UTC)
    window = completed_window(due, schedule, "America/New_York")
    assert window.start_at == datetime(2026, 2, 1, 5, tzinfo=UTC)
    assert window.end_at == datetime(2026, 3, 1, 5, tzinfo=UTC)


@pytest.mark.parametrize("period", ["daily", "weekly", "monthly"])
def test_next_delivery_is_strict_and_previous_is_inclusive(period: str) -> None:
    schedule = DigestSchedule(period=period)
    now = datetime(2026, 6, 1, 9, tzinfo=UTC)  # Monday and first of month.
    assert scheduled_delivery(now, schedule, "UTC", following=False) == now
    assert scheduled_delivery(now, schedule, "UTC", following=True) > now


def test_gap_is_shifted_to_first_valid_minute_and_fold_runs_only_once() -> None:
    gap = DigestSchedule(period="daily", delivery_time="02:30")
    assert scheduled_delivery(
        datetime(2026, 3, 8, 5, tzinfo=UTC), gap, "America/New_York", following=True
    ) == datetime(2026, 3, 8, 7, tzinfo=UTC)
    fold = DigestSchedule(period="daily", delivery_time="01:30")
    first_fold = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert scheduled_delivery(first_fold, fold, "America/New_York", following=True) == datetime(
        2026, 11, 2, 6, 30, tzinfo=UTC
    )
    assert (
        scheduled_delivery(
            datetime(2026, 11, 1, 6, 45, tzinfo=UTC), fold, "America/New_York", following=False
        )
        == first_fold
    )
