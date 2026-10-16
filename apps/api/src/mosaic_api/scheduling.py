"""Shared scheduling values. Local wall times schedule work; UTC instants identify it."""

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, AwareDatetime, Field, field_validator, model_validator

from mosaic_api.domain import Entity, MosaicModel


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


def _time_zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("Choose an IANA time zone, such as UTC or America/New_York") from error
    return value


UtcInstant = Annotated[AwareDatetime, AfterValidator(_utc)]
IanaTimeZone = Annotated[str, AfterValidator(_time_zone)]
LocalTime = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]
Weekday = Annotated[int, Field(strict=True, ge=0, le=6)]


class WeeklyTimeWindow(MosaicModel):
    """Half-open window, attributed to its starting weekday (Monday=0), including overnight."""

    days_of_week: list[Weekday] = Field(min_length=1, max_length=7)
    start_time: LocalTime
    end_time: LocalTime

    @field_validator("days_of_week")
    @classmethod
    def _unique_days(cls, value: list[int]) -> list[int]:
        return sorted(set(value))

    @model_validator(mode="after")
    def _nonempty_window(self) -> Self:
        if self.start_time == self.end_time:
            raise ValueError("A window needs different start and end times")
        return self

    @property
    def crosses_midnight(self) -> bool:
        return self.end_time < self.start_time


class WeeklySchedule(MosaicModel):
    time_zone: IanaTimeZone = "UTC"
    windows: list[WeeklyTimeWindow] = Field(min_length=1, max_length=28)


class SchedulerLease(Entity):
    """CAS-protected lease: only its current token may renew, finish, or release work."""

    entity_type: Literal["schedulerLease"] = "schedulerLease"
    scope: str = Field(min_length=1, max_length=256)
    owner: str = Field(min_length=1, max_length=128)
    token: str = Field(min_length=1, max_length=128)
    expires_at: UtcInstant
