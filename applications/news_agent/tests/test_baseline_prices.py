"""Price and grade boundaries used by the historical baseline."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from news_agent.evaluation import baseline as evaluation_baseline
from news_agent.evaluation.baseline import (LEVELS, _observation, actual_level,
                                            verify_evaluation_artifacts, write_evaluation)
from news_agent.evaluation.evaluate import PriceStore
from news_agent.settings import load_baseline_settings


@pytest.mark.parametrize(("realized", "expected"), [
    ("-0.0151", "利空"), ("-0.015", "利空"),
    ("-0.0149", None), ("0", None), ("0.0199", None),
    ("0.02", "利好"), ("0.0201", "利好"),
])
def test_actual_grade_uses_exact_unrounded_boundaries(realized: str, expected: str | None) -> None:
    assert actual_level(Decimal(realized), load_baseline_settings().thresholds) == expected


def test_adjusted_return_and_invalid_price_windows(tmp_path: Path) -> None:
    def write(code: str, dates: list[str], opens: list[float], closes: list[float],
              volumes: list[float], factors: list[float]) -> None:
        path = tmp_path / f"ts_code={code}" / "data.parquet"
        path.parent.mkdir(parents=True)
        pq.write_table(pa.table({"trade_date": dates, "open": opens, "close": closes,
                                 "vol": volumes, "adj_factor": factors}), path)

    dates = ["20250818", "20250819", "20250820"]
    write("510300.SH", dates, [100, 50, 52], [100, 55, 52], [1000] * 3, [1, 2, 2])
    write("512880.SH", [dates[0], dates[2]], [100, 100], [100, 100], [1000, 1000], [1, 1])
    write("159919.SZ", dates, [100, 100, 100], [100, 100, 100], [1000, 0, 1000], [1, 1, 1])
    write("159915.SZ", dates, [100, 100, 100], [100, 100, 100], [1000] * 3, [1, 0, 1])
    store = PriceStore(tmp_path)
    assert store.calendar[:3] == [date(2025, 8, 18), date(2025, 8, 19), date(2025, 8, 20)]
    opening, drawdown, reason = _observation(store, "510300.SH", 0, 1, "open")
    assert (opening, drawdown, reason) == (Decimal(0), Decimal(0), None)
    closing, drawdown, reason = _observation(store, "510300.SH", 0, 1, "close")
    assert (closing, drawdown, reason) == (Decimal("0.1"), Decimal(0), None)
    missing = _observation(store, "512880.SH", 0, 1, "open")[2]
    zero_volume = _observation(store, "159919.SZ", 0, 1, "open")[2]
    bad_factor = _observation(store, "159915.SZ", 0, 1, "open")[2]
    assert isinstance(missing, str) and "缺少交易日" in missing
    assert isinstance(zero_volume, str) and "无效" in zero_volume
    assert isinstance(bad_factor, str) and "无效" in bad_factor


def test_empty_evaluation_explains_undefined_rates(tmp_path: Path, monkeypatch) -> None:
    summary = {
        "news_records": 1, "initial_batches": 1, "total_batches": 1,
        "selected_records": 0, "total_event_etf_rows": 0,
        "entry_date": "2025-08-18", "exit_date": "2025-08-25",
        "sell_at": "open", "hold_days": 5,
        "direction_hit_rate": None, "direction_hits": 0, "direction_total": 0,
        "market_reference": {"etf_code": "510300.SH", "return": None,
                             "positive_outperformance": {"hits": 0, "total": 0},
                             "negative_underperformance": {"hits": 0, "total": 0}},
        "directions": [{"direction": level, "samples": 0, "mean_return": None,
                    "max_price_drawdown": None} for level in LEVELS],
        "excluded": {},
    }
    path = write_evaluation(tmp_path, date(2025, 8, 17), summary, [])
    report = path.read_text(encoding="utf-8")
    assert "方向命中率：不可计算（0/0）" in report
    assert "没有利好或利空判断" in report
    verify_evaluation_artifacts(tmp_path, date(2025, 8, 17))
    marker = path.parent / "2025-08-17-manifest.json"
    assert marker.is_file()
    path.write_text("incomplete report", encoding="utf-8")
    with pytest.raises(ValueError, match="文件不一致"):
        verify_evaluation_artifacts(tmp_path, date(2025, 8, 17))
    path.write_text(report, encoding="utf-8")

    original_replace = evaluation_baseline.os.replace

    def interrupted_replace(source: Path, destination: Path) -> None:
        if destination.name == "2025-08-17.json":
            raise OSError("simulated interruption")
        original_replace(source, destination)

    monkeypatch.setattr(evaluation_baseline.os, "replace", interrupted_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        write_evaluation(tmp_path, date(2025, 8, 17), {**summary, "news_records": 2}, [])
    assert not marker.exists()
