from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.schedules.schedule import (
    cron_schedule,
    interval_schedule,
    next_run,
    once_schedule,
)


def test_once_normalizes_a_naive_wall_clock_in_the_requested_timezone() -> None:
    schedule = once_schedule("2026-07-19T09:30:00", timezone="Asia/Shanghai")

    assert schedule == {
        "kind": "once",
        "at": "2026-07-19T01:30:00+00:00",
        "timezone": "Asia/Shanghai",
    }
    assert next_run(schedule, after=datetime(2026, 7, 18, tzinfo=UTC)) == datetime(2026, 7, 19, 1, 30, tzinfo=UTC)


def test_interval_skips_missed_slots_without_drifting_from_the_previous_fire() -> None:
    schedule = interval_schedule("15m", timezone="UTC")
    previous = datetime(2026, 7, 18, 1, 0, tzinfo=UTC)
    after = datetime(2026, 7, 18, 1, 47, tzinfo=UTC)

    assert next_run(schedule, after=after, previous=previous) == datetime(2026, 7, 18, 2, 0, tzinfo=UTC)


def test_cron_uses_iana_timezone_wall_clock_and_returns_utc() -> None:
    schedule = cron_schedule("0 9 * * *", timezone="Asia/Shanghai")

    result = next_run(
        schedule,
        after=datetime(2026, 7, 18, 2, 0, tzinfo=UTC),
    )

    assert result == datetime(2026, 7, 19, 1, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("factory", "value", "timezone"),
    [
        (interval_schedule, "0m", "UTC"),
        (cron_schedule, "not a cron", "UTC"),
        (once_schedule, "2026-07-19T09:30:00", "Mars/Olympus"),
    ],
)
def test_invalid_schedule_inputs_fail_at_creation(factory, value, timezone) -> None:
    with pytest.raises(ValueError):
        factory(value, timezone=timezone)


def test_once_is_consumed_after_its_previous_fire() -> None:
    schedule = once_schedule("2026-07-19T09:30:00Z", timezone="UTC")
    fire = datetime(2026, 7, 19, 9, 30, tzinfo=UTC)

    assert next_run(schedule, after=fire + timedelta(seconds=1), previous=fire) is None
