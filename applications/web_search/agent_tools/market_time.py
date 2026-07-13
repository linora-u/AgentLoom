from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_NEW_YORK = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
_NOW_OVERRIDE_ENV = "AGENTLOOM_WEB_SEARCH_NOW_UTC"


def _observed_fixed_holiday(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    return current + timedelta(days=7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year, 12, 31)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _us_market_holidays_around(year: int) -> set[date]:
    holidays: set[date] = set()
    for y in (year - 1, year, year + 1):
        holidays.update(
            {
                _observed_fixed_holiday(date(y, 1, 1)),
                _nth_weekday(y, 1, 0, 3),  # Martin Luther King Jr. Day
                _nth_weekday(y, 2, 0, 3),  # Washington's Birthday
                _easter_sunday(y) - timedelta(days=2),  # Good Friday
                _last_weekday(y, 5, 0),  # Memorial Day
                _observed_fixed_holiday(date(y, 6, 19)),  # Juneteenth
                _observed_fixed_holiday(date(y, 7, 4)),
                _nth_weekday(y, 9, 0, 1),  # Labor Day
                _nth_weekday(y, 11, 3, 4),  # Thanksgiving
                _observed_fixed_holiday(date(y, 12, 25)),
            }
        )
    return holidays


def _is_us_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in _us_market_holidays_around(day.year)


def _previous_us_trading_day(day: date) -> date:
    current = day
    while not _is_us_trading_day(current):
        current -= timedelta(days=1)
    return current


def _next_china_weekday(day: date) -> date:
    current = day
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def _previous_china_weekday(day: date) -> date:
    current = day
    while current.weekday() >= 5:
        current -= timedelta(days=1)
    return current


def _next_a_share_prediction_date(now_shanghai: datetime) -> date:
    candidate = _next_china_weekday(now_shanghai.date())
    cutoff = datetime.combine(candidate, time(9, 30), tzinfo=_SHANGHAI)
    if now_shanghai >= cutoff:
        return _next_china_weekday(candidate + timedelta(days=1))
    return candidate


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(tz=_UTC)

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    return parsed.astimezone(_UTC)


def _market_context(now: datetime) -> dict[str, object]:
    now_shanghai = now.astimezone(_SHANGHAI)
    now_ny = now.astimezone(_NEW_YORK)
    today_ny = now_ny.date()
    close_time = time(16, 0)

    if _is_us_trading_day(today_ny) and now_ny.time() >= close_time:
        trading_day = today_ny
        market_status = "after_regular_close"
    elif _is_us_trading_day(today_ny):
        trading_day = _previous_us_trading_day(today_ny - timedelta(days=1))
        market_status = "before_regular_close"
    else:
        trading_day = _previous_us_trading_day(today_ny - timedelta(days=1))
        market_status = "weekend_or_us_market_holiday"

    close_dt_ny = datetime.combine(trading_day, close_time, tzinfo=_NEW_YORK)
    a_share_date = _next_a_share_prediction_date(now_shanghai)
    a_share_cutoff = datetime.combine(a_share_date, time(9, 30), tzinfo=_SHANGHAI)
    previous_a_share_date = _previous_china_weekday(a_share_date - timedelta(days=1))
    previous_a_share_cutoff = datetime.combine(previous_a_share_date, time(9, 30), tzinfo=_SHANGHAI)
    news_start_ny = previous_a_share_cutoff.astimezone(_NEW_YORK)
    news_end_ny = min(now_ny, a_share_cutoff.astimezone(_NEW_YORK))

    return {
        "generated_at": {
            "asia_shanghai": now_shanghai.isoformat(),
            "america_new_york": now_ny.isoformat(),
            "utc": now.astimezone(_UTC).isoformat(),
        },
        "us_market": {
            "status": market_status,
            "last_completed_regular_trading_day": trading_day.isoformat(),
            "regular_close_et": close_dt_ny.isoformat(),
            "calendar_policy": (
                "NYSE weekday plus standard US market holidays; special one-off "
                "closures are not encoded."
            ),
        },
        "news_window": {
            "start_et": news_start_ny.isoformat(),
            "end_et": news_end_ny.isoformat(),
            "start_asia_shanghai": news_start_ny.astimezone(_SHANGHAI).isoformat(),
            "end_asia_shanghai": news_end_ny.astimezone(_SHANGHAI).isoformat(),
            "hard_cutoff": (
                "Use facts published no later than this end time. The window starts "
                "at the previous A-share 09:30 cutoff and ends at the earlier of "
                "current runtime and the prediction-date 09:30 cutoff."
            ),
        },
        "a_share": {
            "prediction_date": a_share_date.isoformat(),
            "previous_info_cutoff_asia_shanghai": previous_a_share_cutoff.isoformat(),
            "future_info_cutoff_asia_shanghai": a_share_cutoff.isoformat(),
            "run_is_after_cutoff": now_shanghai > a_share_cutoff,
            "run_is_after_current_a_share_open": now_shanghai.time() >= time(9, 30)
            and now_shanghai.weekday() < 5,
            "prediction_policy": (
                "Predict the next China weekday whose 09:30 information cutoff "
                "has not passed at runtime."
            ),
            "calendar_policy": "Weekends are skipped; exchange holidays should be verified near holiday periods.",
        },
        "query_terms": {
            "us_trading_day": f"{trading_day.strftime('%B')} {trading_day.day}, {trading_day.year}",
            "us_trading_day_iso": trading_day.isoformat(),
            "a_share_prediction_date_iso": a_share_date.isoformat(),
        },
    }


def get_market_time_context(now_iso: str | None = None) -> str:
    """Return current market-time context for the web_search workflow.

    Call this tool before any news or market search. It returns the current
    Asia/Shanghai, America/New_York, and UTC timestamps; the most recent
    completed US regular trading day; the +/- 24 hour news window around that
    US close; and the A-share prediction date plus future-information cutoff.
    Use the returned exact dates in search queries instead of words like
    "today" or hard-coded calendar dates.

    Args:
        now_iso: Optional ISO-8601 UTC or timezone-aware timestamp used only for
            deterministic validation/backtests. Leave empty in normal workflow
            runs so the tool uses the current runtime.
    """
    now = _parse_now(now_iso or os.environ.get(_NOW_OVERRIDE_ENV))
    return json.dumps(_market_context(now), ensure_ascii=False, indent=2)
