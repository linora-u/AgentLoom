"""Schedule parsing and deterministic next-fire calculation."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, croniter

Schedule = dict[str, Any]

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
}


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(timezone).strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown IANA timezone: {timezone!r}") from exc


def _aware(value: datetime, *, timezone: str = "UTC") -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_zone(timezone))
    return value


def parse_datetime(value: str | datetime, *, timezone: str = "UTC") -> datetime:
    """Parse an instant, interpreting a naive value as wall time in *timezone*."""
    _zone(timezone)
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if raw.endswith(("Z", "z")):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"Invalid ISO timestamp: {value!r}") from exc
    return _aware(parsed, timezone=timezone).astimezone(UTC)


def parse_duration(value: str | int | float) -> int:
    """Return a positive duration in whole seconds."""
    if isinstance(value, bool):
        raise ValueError("A schedule duration must be positive")
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        match = _DURATION_RE.fullmatch(str(value))
        if match is None:
            raise ValueError("Invalid duration; use forms such as 30s, 15m, 2h, or 1d")
        seconds = float(match.group(1)) * _DURATION_SECONDS[match.group(2).lower()]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("A schedule duration must be positive")
    rounded = int(seconds)
    if rounded <= 0:
        raise ValueError("A schedule duration must be at least one second")
    return rounded


def once_schedule(at: str | datetime, *, timezone: str = "UTC") -> Schedule:
    parsed = parse_datetime(at, timezone=timezone)
    return {"kind": "once", "at": parsed.isoformat(), "timezone": timezone}


def interval_schedule(every: str | int | float, *, timezone: str = "UTC") -> Schedule:
    _zone(timezone)
    return {
        "kind": "interval",
        "seconds": parse_duration(every),
        "timezone": timezone,
    }


def cron_schedule(expression: str, *, timezone: str = "UTC") -> Schedule:
    _zone(timezone)
    expression = str(expression).strip()
    try:
        valid = croniter.is_valid(expression)
    except (CroniterBadCronError, KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"Invalid cron expression: {expression!r}")
    return {"kind": "cron", "expression": expression, "timezone": timezone}


def validate_schedule(schedule: Schedule) -> Schedule:
    """Return a normalized copy of a JSON schedule or raise ``ValueError``."""
    if not isinstance(schedule, dict):
        raise ValueError("Schedule must be an object")
    kind = schedule.get("kind")
    timezone = str(schedule.get("timezone") or "UTC")
    if kind == "once":
        return once_schedule(schedule.get("at", ""), timezone=timezone)
    if kind == "interval":
        return interval_schedule(schedule.get("seconds", 0), timezone=timezone)
    if kind == "cron":
        return cron_schedule(schedule.get("expression", ""), timezone=timezone)
    raise ValueError(f"Unknown schedule kind: {kind!r}")


def next_run(
    schedule: Schedule,
    *,
    after: datetime,
    previous: datetime | None = None,
) -> datetime | None:
    """Return the first fire strictly after *after*, except an unconsumed once.

    Recurring schedules skip missed slots. Interval schedules remain anchored to
    the previous scheduled fire, so process runtime does not cause drift.
    Returned datetimes are always UTC.
    """
    normalized = validate_schedule(schedule)
    after_utc = _aware(after).astimezone(UTC)
    previous_utc = _aware(previous).astimezone(UTC) if previous is not None else None
    kind = normalized["kind"]

    if kind == "once":
        if previous_utc is not None:
            return None
        return parse_datetime(normalized["at"], timezone=normalized["timezone"])

    if kind == "interval":
        seconds = int(normalized["seconds"])
        if previous_utc is None:
            return after_utc + timedelta(seconds=seconds)
        elapsed = (after_utc - previous_utc).total_seconds()
        slots = max(1, math.floor(elapsed / seconds) + 1)
        return previous_utc + timedelta(seconds=slots * seconds)

    zone = _zone(normalized["timezone"])
    base_local = after_utc.astimezone(zone)
    try:
        result = croniter(normalized["expression"], base_local).get_next(datetime)
    except (CroniterBadCronError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid cron expression: {normalized['expression']!r}") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=zone)
    return result.astimezone(UTC)
