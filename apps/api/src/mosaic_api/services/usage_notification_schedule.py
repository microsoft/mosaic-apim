"""Completed local-calendar usage windows with UTC durable identities."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from mosaic_api.notifications import DigestPeriod, DigestSchedule, DigestWindow


def wall_instant(local: datetime, zone: ZoneInfo) -> datetime:
    """First fold on overlap, first valid wall minute after a gap (including skipped dates)."""

    for _ in range(2881):
        instant = local.replace(tzinfo=zone, fold=0).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == local:
            return instant
        local += timedelta(minutes=1)
    raise ValueError("Unable to resolve a delivery time in this IANA zone")


def calendar_boundary(day: date, period: DigestPeriod) -> date:
    if period == "weekly":
        return day - timedelta(days=day.weekday())
    if period == "monthly":
        return day.replace(day=1)
    return day


def preceding_boundary(day: date, period: DigestPeriod) -> date:
    if period == "monthly":
        return (day.replace(day=1) - timedelta(days=1)).replace(day=1)
    return day - timedelta(days=7 if period == "weekly" else 1)


def scheduled_delivery(
    reference: datetime, schedule: DigestSchedule, time_zone: str, *, following: bool
) -> datetime:
    """Strictly next or inclusively previous scheduled instant."""

    zone = ZoneInfo(time_zone)
    local = reference.astimezone(zone)
    hour, minute = map(int, schedule.delivery_time.split(":"))
    for offset in range(64):
        day = local.date() + timedelta(days=offset if following else -offset)
        if schedule.period == "weekly" and day.weekday() != schedule.day_of_week:
            continue
        if schedule.period == "monthly" and day.day != schedule.day_of_month:
            continue
        candidate = wall_instant(datetime.combine(day, time(hour, minute)), zone)
        if candidate > reference if following else candidate <= reference:
            return candidate
    raise ValueError("No delivery date found")


def completed_window(due_at: datetime, schedule: DigestSchedule, time_zone: str) -> DigestWindow:
    zone = ZoneInfo(time_zone)
    end = calendar_boundary(due_at.astimezone(zone).date(), schedule.period)
    start = preceding_boundary(end, schedule.period)
    return DigestWindow(
        period=schedule.period,
        time_zone=time_zone,
        start_at=wall_instant(datetime.combine(start, time.min), zone),
        end_at=wall_instant(datetime.combine(end, time.min), zone),
        due_at=due_at,
    )
