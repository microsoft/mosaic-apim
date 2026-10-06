from datetime import UTC, datetime, timedelta, timezone

import pytest

import tools


@pytest.mark.parametrize("text", ["mosaic", "", "  spaced  ", "line one\nline two", "é ✓ 漢字"])
def test_echo_returns_the_text_unchanged(text: str) -> None:
    assert tools.echo(text) == text


@pytest.mark.parametrize("value", [None, 5, ["text"], {"text": "x"}])
def test_echo_takes_only_text(value: object) -> None:
    with pytest.raises(tools.ToolInputError):
        tools.echo(value)


def test_format_utc_writes_iso_8601_in_utc_to_the_second() -> None:
    eastern = timezone(timedelta(hours=-4))
    assert tools.format_utc(datetime(2026, 10, 5, 14, 6, 46, 818000, tzinfo=eastern)) == (
        "2026-10-05T18:06:46Z"
    )


def test_utc_now_returns_the_current_utc_time() -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    moment = datetime.fromisoformat(tools.utc_now())
    after = datetime.now(UTC)
    assert moment.utcoffset() == timedelta(0)
    assert before <= moment <= after


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [(2, 3, 5), (-7, 2, -5), (1.5, 2, 3.5), (10**20, 1, 100000000000000000001)],
)
def test_add_returns_the_sum(a: float, b: float, expected: float) -> None:
    total = tools.add(a, b)
    assert total == expected
    assert type(total) is type(expected)


@pytest.mark.parametrize(
    ("a", "b"),
    [(True, 1), (1, False), ("2", 3), (None, 1), ([1], 2), (float("inf"), 1), (float("nan"), 1)],
)
def test_add_refuses_anything_but_two_finite_numbers(a: object, b: object) -> None:
    with pytest.raises(tools.ToolInputError):
        tools.add(a, b)


def test_add_refuses_a_sum_that_overflows() -> None:
    with pytest.raises(tools.ToolInputError, match="finite"):
        tools.add(1e308, 1e308)
    with pytest.raises(tools.ToolInputError, match="too large"):
        tools.add(10**400, 1.0)
