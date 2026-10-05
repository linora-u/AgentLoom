"""Exclude historical article bodies that contradict their recorded availability time."""

from __future__ import annotations

from datetime import date, datetime, timezone, timedelta
import re
from typing import Any


SHANGHAI = timezone(timedelta(hours=8))
_LIVE_US_TITLE = re.compile(r"美股.{0,20}(?:涨跌互现|高开|盘中)")
_US_CLOSE_BODY = re.compile(r"美股三大指数.{0,20}(?:收涨|收跌|收低|收高|收盘|均告收|集体收)")


def inaccessible_us_close_body(day: date, record: dict[str, Any]) -> bool:
    """Detect an updated US-market recap stored under an earlier evening timestamp.

    The source-specific rule is deliberately narrow. At 21:00–23:59 Beijing time,
    the same day's US equity session has not closed. A live-session headline paired
    with final index closing results cannot be the article body available then.
    Other sources and article patterns need separate evidence before exclusion.
    """
    if (record.get("source") != "华尔街见闻"
            or record.get("category") != "长篇新闻"
            or record.get("time_precision") != "datetime"
            or day.weekday() >= 5):
        return False
    try:
        published = datetime.fromisoformat(record["published_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return False
    if published.tzinfo is None:
        return False
    local = published.astimezone(SHANGHAI)
    return (local.date() == day and 21 <= local.hour <= 23
            and bool(_LIVE_US_TITLE.search(record.get("title") or ""))
            and bool(_US_CLOSE_BODY.search(record.get("text") or "")))

