from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from agentloom.schedules.schedule import (
    cron_schedule,
    interval_schedule,
    next_run,
    once_schedule,
    parse_datetime,
)
from agentloom.schedules.schema import MAX_INTERVAL_SECONDS


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


def test_cron_preserves_preexisting_croniter_six_field_grammar() -> None:
    schedule = cron_schedule("*/5 * * * * *", timezone="UTC")

    assert schedule["expression"] == "*/5 * * * * *"


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


def test_interval_schedule_rejects_seconds_above_javascript_safe_integer() -> None:
    with pytest.raises(ValueError, match="supported range"):
        interval_schedule(MAX_INTERVAL_SECONDS + 1)


def test_next_run_reports_datetime_overflow_as_value_error() -> None:
    schedule = interval_schedule(MAX_INTERVAL_SECONDS)

    with pytest.raises(ValueError, match="valid next run"):
        next_run(
            schedule,
            after=datetime.max.replace(tzinfo=UTC),
        )


def test_parse_datetime_reports_utc_conversion_overflow_as_value_error() -> None:
    with pytest.raises(ValueError, match="Invalid ISO timestamp"):
        parse_datetime("0001-01-01T00:00:00+14:00")


def test_once_is_consumed_after_its_previous_fire() -> None:
    schedule = once_schedule("2026-07-19T09:30:00Z", timezone="UTC")
    fire = datetime(2026, 7, 19, 9, 30, tzinfo=UTC)

    assert next_run(schedule, after=fire + timedelta(seconds=1), previous=fire) is None
