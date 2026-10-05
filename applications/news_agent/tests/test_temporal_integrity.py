from datetime import date

from news_agent.processing.temporal_integrity import (
    inaccessible_us_close_body,
)


def _record(title: str, text: str, published_at: str) -> dict:
    return {"source": "华尔街见闻", "category": "长篇新闻",
            "time_precision": "datetime", "published_at": published_at,
            "title": title, "text": text}


def test_us_close_in_live_evening_article_is_inaccessible() -> None:
    day = date(2026, 8, 14)
    stale = _record("美股涨跌互现，今夜看点", "美股三大指数小幅收跌。", "2026-08-14T21:42:30+08:00")
    next_morning = _record("美股涨跌互现，今夜看点", "美股三大指数小幅收跌。", "2026-08-15T07:06:37+08:00")
    earlier_session = _record("美股涨跌互现，今夜看点", "周四美股收跌。", "2026-08-14T21:42:30+08:00")
    records = {"stale": stale, "morning": next_morning, "earlier": earlier_session}
    assert inaccessible_us_close_body(day, stale)
    assert not inaccessible_us_close_body(day, next_morning)
    assert not inaccessible_us_close_body(day, earlier_session)
    assert not inaccessible_us_close_body(day, {**stale, "source": "另一来源"})
    assert not inaccessible_us_close_body(day, {**stale, "time_precision": "date"})
