"""The time an analytics request covers, and which parts of it MOSAIC has figures for."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from mosaic_api.errors import ValidationError
from mosaic_api.services.analytics.models import (
    AnalyticsFilters,
    AnalyticsRange,
    AnalyticsWindow,
    Granularity,
)
from mosaic_api.usage_telemetry import SummaryPeriod, UsageRollupState, month_start

# A custom range may span about five years. Up to about a quarter it is shown by the day, and
# beyond that by whole months, which are also all MOSAIC keeps past daily retention.
MAX_CUSTOM_DAYS = 1830
MAX_DAILY_DAYS = 92
_RANGE_DAYS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90}


def day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def add_months(day: date, months: int) -> date:
    """The first day of the month ``months`` after the one ``day`` falls in."""

    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def split_months(first: date, last: date) -> tuple[list[date], list[tuple[date, date]]]:
    """Split an inclusive day range into whole calendar months and the day ranges left over."""

    months: list[date] = []
    ranges: list[tuple[date, date]] = []
    cursor = first
    while cursor <= last:
        following = add_months(cursor, 1)
        month_last = following - timedelta(days=1)
        if cursor.day == 1 and month_last <= last:
            months.append(cursor)
        elif ranges and ranges[-1][1] + timedelta(days=1) == cursor:
            ranges[-1] = (ranges[-1][0], min(month_last, last))
        else:
            ranges.append((cursor, min(month_last, last)))
        cursor = following
    return months, ranges


@dataclass(frozen=True)
class Window:
    range: AnalyticsRange
    granularity: Granularity
    # Inclusive start and exclusive end. The end can be in the future: the current hour, day or
    # month is a bucket of its own, counted so far.
    start: datetime
    end: datetime
    buckets: tuple[datetime, ...]
    previous_start: datetime
    previous_end: datetime
    today: date

    @property
    def first_day(self) -> date:
        return self.start.date()

    @property
    def last_day(self) -> date:
        return min((self.end - timedelta(microseconds=1)).date(), self.today)

    @property
    def previous_first_day(self) -> date:
        return self.previous_start.date()

    @property
    def previous_last_day(self) -> date:
        return (self.previous_end - timedelta(microseconds=1)).date()

    @property
    def period(self) -> SummaryPeriod:
        return "month" if self.granularity == "month" else "day"

    @property
    def days(self) -> int:
        return (self.last_day - self.first_day).days + 1

    def bucket_end(self, start: datetime) -> datetime:
        if self.granularity == "hour":
            return start + timedelta(hours=1)
        if self.granularity == "day":
            return start + timedelta(days=1)
        return day_start(add_months(start.date(), 1))

    def bucket_index(self, moment: datetime) -> int | None:
        if moment < self.start or moment >= self.end:
            return None
        if self.granularity == "hour":
            return int((moment - self.start) // timedelta(hours=1))
        if self.granularity == "day":
            return (moment.date() - self.first_day).days
        return (moment.year - self.start.year) * 12 + moment.month - self.start.month

    def model(self) -> AnalyticsWindow:
        return AnalyticsWindow(
            range=self.range,
            granularity=self.granularity,
            start=self.start,
            end=self.end,
            breakdown_start=self.first_day,
            breakdown_end=self.last_day,
            previous_start=self.previous_start,
            previous_end=self.previous_end,
        )


def _daily(range_: AnalyticsRange, first: date, last: date, today: date) -> Window:
    count = (last - first).days + 1
    start = day_start(first)
    return Window(
        range=range_,
        granularity="day",
        start=start,
        end=day_start(last + timedelta(days=1)),
        buckets=tuple(start + timedelta(days=index) for index in range(count)),
        previous_start=start - timedelta(days=count),
        previous_end=start,
        today=today,
    )


def _monthly(range_: AnalyticsRange, first: date, last: date, today: date) -> Window:
    count = (last.year - first.year) * 12 + last.month - first.month + 1
    buckets = tuple(day_start(add_months(first, index)) for index in range(count))
    return Window(
        range=range_,
        granularity="month",
        start=buckets[0],
        end=day_start(add_months(first, count)),
        buckets=buckets,
        previous_start=day_start(add_months(first, -count)),
        previous_end=buckets[0],
        today=today,
    )


def resolve_window(filters: AnalyticsFilters, now: datetime) -> Window:
    current = now.astimezone(UTC)
    today = current.date()
    if filters.range == "24h":
        hour = current.replace(minute=0, second=0, microsecond=0)
        start = hour - timedelta(hours=23)
        return Window(
            range="24h",
            granularity="hour",
            start=start,
            end=hour + timedelta(hours=1),
            buckets=tuple(start + timedelta(hours=index) for index in range(24)),
            previous_start=start - timedelta(hours=24),
            previous_end=start,
            today=today,
        )
    if filters.range in _RANGE_DAYS:
        count = _RANGE_DAYS[filters.range]
        return _daily(filters.range, today - timedelta(days=count - 1), today, today)
    if filters.range == "12m":
        return _monthly("12m", add_months(today, -11), month_start(today), today)
    if filters.start is None or filters.end is None:
        raise ValidationError("A custom range needs a start date and an end date")
    # A browser east of UTC is already on tomorrow's date, so a day of skew means UTC's today.
    tomorrow = today + timedelta(days=1)
    if filters.start > tomorrow:
        raise ValidationError(
            "A custom range can't start in the future", details={"start": filters.start.isoformat()}
        )
    if filters.end > tomorrow:
        raise ValidationError(
            "A custom range can't end in the future", details={"end": filters.end.isoformat()}
        )
    if filters.start > filters.end:
        raise ValidationError(
            "A custom range's start date must be on or before its end date",
            details={"start": filters.start.isoformat(), "end": filters.end.isoformat()},
        )
    first_day = min(filters.start, today)
    last_day = min(filters.end, today)
    span = (last_day - first_day).days + 1
    if span > MAX_CUSTOM_DAYS:
        raise ValidationError(
            f"A custom range can span at most {MAX_CUSTOM_DAYS} days",
            details={"days": span},
        )
    if span <= MAX_DAILY_DAYS:
        return _daily("custom", first_day, last_day, today)
    return _monthly("custom", month_start(first_day), month_start(last_day), today)


@dataclass(frozen=True)
class Coverage:
    """The span every gateway in scope has rolled-up figures for."""

    first_day: date
    through: datetime

    @property
    def first_start(self) -> datetime:
        return day_start(self.first_day)

    def known(self, start: datetime, end: datetime) -> bool:
        """Whether MOSAIC has figures for any of the span."""

        return start < self.through and end > self.first_start

    def covers(self, start: datetime, end: datetime) -> bool:
        """Whether MOSAIC has figures for the whole span."""

        return self.first_start <= start and end <= self.through


def coverage_of(states: Iterable[UsageRollupState]) -> Coverage | None:
    """What every gateway that has rolled up successfully has figures for.

    A gateway still waiting for its first rollup is left out, and freshness reports it as
    delayed, so one new gateway doesn't blank every chart.
    """

    firsts: list[date] = []
    throughs: list[datetime] = []
    for state in states:
        if (
            state.last_success_at is None
            or state.data_available_from is None
            or state.queried_through is None
        ):
            continue
        firsts.append(date.fromisoformat(state.data_available_from))
        throughs.append(state.queried_through)
    if not firsts:
        return None
    return Coverage(first_day=max(firsts), through=min(throughs))


def bucket_known(window: Window, coverage: Coverage | None, start: datetime) -> bool:
    return coverage is not None and coverage.known(start, window.bucket_end(start))
