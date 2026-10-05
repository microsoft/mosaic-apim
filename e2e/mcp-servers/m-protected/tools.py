"""M-protected's tools: the same three deterministic tools as M-tools, with the same results."""

import math
from datetime import UTC, datetime
from typing import Any


class ToolInputError(ValueError):
    """The tool's arguments aren't what it takes."""


def echo(text: Any) -> str:
    if not isinstance(text, str):
        raise ToolInputError("echo takes text.")
    return text


def format_utc(moment: datetime) -> str:
    """ISO 8601 in UTC, to the second, such as 2026-10-05T18:06:46Z."""

    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now() -> str:
    return format_utc(datetime.now(UTC))


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def add(a: Any, b: Any) -> int | float:
    if not (_is_number(a) and _is_number(b)):
        raise ToolInputError("add takes two numbers.")
    try:
        total: int | float = a + b
    except OverflowError:
        raise ToolInputError("The sum is too large.") from None
    if isinstance(total, float) and not math.isfinite(total):
        raise ToolInputError("add takes finite numbers.")
    return total
