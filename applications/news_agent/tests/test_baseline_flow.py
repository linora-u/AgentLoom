"""Observable baseline behavior with controlled model responses and local prices."""

from __future__ import annotations

from news_agent.tests.source_evidence import verified_sources

from datetime import date
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from news_agent import baseline
from news_agent.evaluation.baseline import verify_evaluation_artifacts
from news_agent.processing.prepare import _record_input, count_tokens


def test_single_initial_pass_and_final_bearish_decision_preserve_all_mappings(
    tmp_path: Path, monkeypatch,
) -> None:
    day = date(2025, 9, 30)
    stem = day.isoformat()
    root = tmp_path / "evaluation"
    day_root = root / "days" / day.isoformat()
    day_root.mkdir(parents=True)
    records = []
    batches = []
    for index in (1, 2, 3):
        record_id = f"N{index}"
        record = {
            "record_id": record_id, "record_ids": [record_id], "members": [{
                "record_id": record_id, "source": "fixture", "published_at": stem,
                "time_precision": "date",
            }],
            "metadata": {}, "category": "test", "source": "fixture",
            "published_at": stem, "time_precision": "date",
            "title": f"两项独立消息 {index}", "text": "需求消息。" * 10000,
            "url": "",
        }
        records.append(record)
        filename = f"batch-{index:04d}.txt"
        content = _record_input(record)
        (day_root / filename).write_text(content, encoding="utf-8")
        batches.append({"file": filename, "ids": [record_id],
                        "sha256": hashlib.sha256(content.encode()).hexdigest()})
    records_text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records)
    (day_root / "records.jsonl").write_text(records_text, encoding="utf-8")
    (day_root / "manifest.json").write_text(json.dumps({
        "prepared_count": 3, "records_sha256": hashlib.sha256(records_text.encode()).hexdigest(),
        "batches": batches,
    }), encoding="utf-8")

    candidates = tmp_path / "etfs.txt"
    candidates.write_text("ETF 候选池\n510300.SH\t宽基\n512880.SH\t券商\n", encoding="utf-8")
    source = tmp_path / "source"
    prices = tmp_path / "prices"
    for code, second_open in (("510300.SH", 102.5), ("512880.SH", 98.0)):
        path = source / f"ts_code={code}" / "data.parquet"
        path.parent.mkdir(parents=True)
        pq.write_table(pa.table({
            "trade_date": ["20250930", "20251009", "20251010"],
            "open": [100.0, 100.0, second_open], "close": [100.0, 100.0, second_open],
            "vol": [1000.0, 1000.0, 1000.0], "adj_factor": [1.0, 1.0, 1.0],
        }), path)
    baseline.copy_prices(source, prices, candidates)

    from news_agent.evaluation import baseline as evaluation_baseline

    configured = replace(baseline.load_baseline_settings(), exposure_root=None)
    token_limit = max(count_tokens(_record_input(row), configured.token_encoding) for row in records)
    configured = replace(configured, refined_max_tokens=token_limit)
    monkeypatch.setattr(baseline, "load_baseline_settings", lambda: configured)
    monkeypatch.setattr(evaluation_baseline, "load_baseline_settings", lambda: configured)

    calls: list[str] = []

    def wire(row: dict) -> dict:
        return {**{key: value for key, value in row.items() if key != "record_ids"},
                "record_numbers": [1]}

    def worker(definition: Path, query: str, **_kwargs) -> dict:
        calls.append(definition.name)
        assert stem in query
        assert "102.5" not in query and "98.0" not in query
        if definition.name == "initial.yaml":
            if "两项独立消息 3" in query:
                return {"classifications": [[1, 6]]}
            return {"classifications": [[1, 0]]}
        def review(ids: list[str], summary: str) -> dict:
            return {"event_id": ("E3" if summary == "独立事件" else "E1" if ids == ["N1"] else "E2"), "etf_code": "510300.SH", "record_ids": ids, "summary": summary,
                    "decision": "利好",  "reason": "新增行业需求", "sources": verified_sources()}
        if "第 1/2 块" in query:
            return { "reviews": [wire(review(["N1"], "重复事件")), wire(review(["N1"], "独立事件"))]}
        if "第 2/2 块" in query:
            return {
                    "reviews": [wire({**review(["N2"], "利空事件"), "decision": "利空"})]}
        raise AssertionError("unexpected redundant event reanalysis")

    monkeypatch.setattr(baseline, "_call_worker", worker)
    argv = ["news_agent.baseline", "run", "--day", day.isoformat(),
            "--input-root", str(root), "--prices", str(prices),
            "--candidates", str(candidates), "--expect-records", "3",
            "--expect-batches", "3", "--hold-days", "1"]
    monkeypatch.setattr(sys, "argv", argv)
    original_atomic_json = baseline._atomic_json

    def interrupted_prediction_metadata(path: Path, value: dict) -> None:
        if "prediction" in value:
            raise OSError("simulated prediction metadata failure")
        original_atomic_json(path, value)

    monkeypatch.setattr(baseline, "_atomic_json", interrupted_prediction_metadata)
    with pytest.raises(OSError, match="prediction metadata failure"):
        baseline.main()
    assert not (root / "results" / f"{stem}.jsonl").exists()
    coverage = json.loads((day_root / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["refined_result_state"] != "complete"
    completed_calls = len(calls)
    monkeypatch.setattr(baseline, "_atomic_json", original_atomic_json)
    baseline.main()
    assert len(calls) == completed_calls  # Successful worker caches survive the interrupted publication.

    result_file = root / "results" / f"{stem}.jsonl"
    events = [json.loads(line) for line in result_file.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 1
    assert len({row["event_id"] for row in events}) == 1
    assert events[0]["direction"] == "利空"
    assert events[0]["record_ids"] == ["N2"]
    ledger = json.loads((root / "results/reviews" / f"{stem}.json").read_text())
    assert len(ledger["mapping_signals"]) == 3
    assert all("N3" not in row["record_ids"] for row in ledger["reviews"])
    assert {row["etf_code"] for row in events} == {"510300.SH"}
    assert all(set(row) == {"event_id", "record_ids", "summary", "etf_code",
                                "direction", "reason"} for row in events)
    report = root / "reports" / "baseline" / f"{stem}.json"
    summary = json.loads(report.read_text(encoding="utf-8"))
    assert (summary["direction_hits"], summary["direction_total"]) == (2, 3)
    reference = summary["market_reference"]
    assert reference["return"] == pytest.approx(0.025)
    assert reference["positive_outperformance"] == {"hits": 0, "total": 0}
    assert reference["negative_underperformance"] == {"hits": 0, "total": 0}
    assert (summary["entry_date"], summary["exit_date"]) == ("2025-10-09", "2025-10-10")
    details = (root / "reports" / "baseline" / f"{stem}-details.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(details) == 3
    assert "方向命中率：66.67%" in (root / "reports" / "baseline" / f"{stem}.md").read_text(encoding="utf-8")
    assert "利好跑赢沪深300 ETF" in (root / "reports" / "baseline" / f"{stem}.md").read_text(encoding="utf-8")
    assert calls.count("initial.yaml") == 3  # Each prepared batch is screened once.
    assert calls.count("refined.yaml") == 2
    assert set(calls) == {"initial.yaml", "refined.yaml"}

    monkeypatch.setattr(baseline, "_call_worker", lambda *_, **_kwargs: (_ for _ in ()).throw(AssertionError("cache missed")))
    baseline.main()
    assert json.loads(report.read_text(encoding="utf-8")) == summary

    for code, second_open in (("510300.SH", 102.5), ("512880.SH", 98.0)):
        pq.write_table(pa.table({
            "trade_date": ["20251009", "20251010"],
            "open": [100.0, second_open], "close": [100.0, second_open],
            "vol": [1000.0, 0.0], "adj_factor": [1.0, 1.0],
        }), source / f"ts_code={code}" / "data.parquet")
    evaluation_argv = [*argv]
    evaluation_argv[1] = "evaluate"
    monkeypatch.setattr(sys, "argv", evaluation_argv)
    baseline.main()  # Updating the source does not change the local price snapshot.
    assert json.loads(report.read_text(encoding="utf-8")) == summary

    baseline.copy_prices(source, prices, candidates)
    with pytest.raises(ValueError, match="行情副本已变化"):
        verify_evaluation_artifacts(root, day)
    baseline.main()
    verify_evaluation_artifacts(root, day)
    updated = json.loads(report.read_text(encoding="utf-8"))
    assert updated["direction_hit_rate"] == 0 and not updated["scoring_complete"]
    assert sum(updated["excluded"].values()) == 3
    assert "缺行情或观察期不足：3" in (root / "reports" / "baseline" / f"{stem}.md").read_text(encoding="utf-8")
