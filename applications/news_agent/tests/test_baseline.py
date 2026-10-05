from __future__ import annotations

from news_agent.tests.source_evidence import verified_sources

from argparse import Namespace
from dataclasses import replace
from functools import partial
import hashlib
import json
import os
import shutil
from datetime import date
from pathlib import Path
import subprocess
import sys
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from news_agent import baseline
from news_agent.settings import load_baseline_settings


APPLICATIONS = Path(__file__).resolve().parents[2]


def test_initial_ids_validate_complete_results_and_reject_duplicates_or_unknown_ids() -> None:
    batch = {"file": "batch-0001.txt", "ids": ["N1", "N2", "N3"]}
    assert baseline._checked_initial_ids({"selected_ids": ["N3", "N1"], "rejected": [{"reason_code": "行情复述", "record_ids": ["N2"]}]}, batch) == ["N1", "N3"]
    with pytest.raises(ValueError, match="重复"):
        baseline._checked_initial_ids({"selected_ids": ["N3", "N1", "N3"], "rejected": []}, batch)
    with pytest.raises(ValueError, match="非本批次"):
        baseline._checked_initial_ids({"selected_ids": ["N4"], "rejected": []}, batch)


def test_baseline_settings_read_values_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "settings.yaml"
    raw: dict[str, Any] = {
        "evaluation_output_root": str(tmp_path / "results"),
        "batching": {"token_encoding": "o200k_base", "initial_max_tokens": 15000,
                     "refined_max_tokens": 12000},
        "etf_root": str(tmp_path / "prices"),
        "baseline": {
            "initial_workers": 2, "refined_workers": 2,
            "hold_days": 7, "sell_at": "close",
            "thresholds": {"positive": "0.02", "negative": "-0.02"},
        },
    }
    path.write_text(json.dumps(raw), encoding="utf-8")

    settings = load_baseline_settings(path)
    assert (settings.refined_workers, settings.refined_max_tokens, settings.hold_days,
            settings.sell_at) == (2, 12000, 7, "close")
    assert settings.initial_max_tokens == 15000
    assert settings.candidates == tmp_path / "results" / "etf_candidates.txt"
    assert str(settings.thresholds["positive"]) == "0.02"

    raw["baseline"]["refined_workers"] = 0
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="refined_workers"):
        load_baseline_settings(path)


def test_retry_policy_invalidates_full_version_cache_identity(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "system.yaml").write_text("system: test\n")
    common = {"adapter": "openai_codex_responses", "model": "gpt-6-luna", "timeout": 900}
    models = {name: {**common, "web_search": search, "num_retries": 0}
              for name, search in (("codex_luna_news_initial", "off"),
                                   ("codex_luna_news_replay", "auto"))}
    path = config / "model.yaml"
    path.write_text(json.dumps({"model": models}))
    for name in ("baseline.py", "settings.py", "run.py", "report.py", "skill_reference.py"):
        (tmp_path / name).write_text("fixture implementation")
    monkeypatch.setattr(baseline, "ROOT", tmp_path)
    original = baseline._model_signature()
    for profile in models.values():
        profile.update(num_retries=2, retry_delay=15, max_retry_delay=30)
    path.write_text(json.dumps({"model": models}))
    assert baseline._model_signature() != original


def test_cross_chunk_decision_merge_preserves_sources_and_cache(
    tmp_path: Path, monkeypatch,
) -> None:
    from news_agent.processing import prepare
    from news_agent.evaluation import prepare_etf_exposure
    from news_agent.settings import load_baseline_settings

    (tmp_path / "agent").mkdir()
    (tmp_path / "workflows").mkdir()
    (tmp_path / "agent" / "精筛.md").write_text("test prompt", encoding="utf-8")
    for name in ("refined",):
        (tmp_path / "workflows" / f"{name}.yaml").write_text(name, encoding="utf-8")
    shutil.copyfile(baseline.ROOT / "skill_reference.py", tmp_path / "skill_reference.py")
    shutil.copytree(baseline.ROOT / "skills" / "news-catalyst", tmp_path / "skills" / "news-catalyst")
    candidates = tmp_path / "etfs.txt"
    candidates.write_text("510300.SH\t沪深300\n", encoding="utf-8")
    monkeypatch.setattr(baseline, "ROOT", tmp_path)
    monkeypatch.setattr(baseline, "_stage_signature", lambda _: "phase-test")
    records = {
        key: {"record_id": key, "record_ids": [key], "members": [], "metadata": {},
              "category": "test", "source": "test", "published_at": "2025-08-17",
              "time_precision": "date", "title": key, "text": key, "url": ""}
        for key in ("N1", "N2")
    }
    monkeypatch.setattr(prepare, "chunk_selected_records", lambda selected, records, max_tokens, encoding, **kwargs: [
        (["N1"], prepare._record_input(records["N1"])), (["N2"], prepare._record_input(records["N2"]))])
    monkeypatch.setattr(prepare_etf_exposure, "holding_mentions_as_of",
                        lambda *_: {"N1": [{"fund": "510300.SH", "stock": "688012.SH",
                                              "stock_name": "中微公司", "reported_rank": 1,
                                              "stk_mkv_ratio": 15.28,
                                              "weight_basis": "percent_of_stock_market_value_not_fund_nav",
                                              "portfolio_ann_date": "2025-08-01"}]})
    calls: list[str] = []

    def row(event_id: str, source: str) -> dict:
        return {"event_id": event_id, "record_ids": [source], "summary": source,
                "etf_code": "510300.SH", "direction": "利好",
                "reason": "新闻显示需求改善"}

    def wire(row: dict) -> dict:
        return {**{key: value for key, value in row.items() if key != "record_ids"}, "record_numbers": [1]}

    def worker(definition: Path, query: str, **_kwargs) -> dict:
        calls.append(definition.name)
        if "第 1/2 块" in query:
            assert '"reported_holdings"' in query and '"stk_mkv_ratio":15.28' in query
        def review(source: str) -> dict:
            return {"event_id": "E1" if source == "N1" else "E2", "etf_code": "510300.SH", "record_ids": [source], "summary": source,
                    "decision": "利好",  "reason": "新需求覆盖行业", "sources": verified_sources()}
        if "第 1/2 块" in query:
            return { "reviews": [wire(review("N1"))]}
        if "第 2/2 块" in query:
            return { "reviews": [wire(review("N2"))]}
        raise AssertionError("unexpected redundant event reanalysis")

    monkeypatch.setattr(baseline, "_call_worker", worker)
    prices = tmp_path / "prices"
    reference = prices / "ts_code=510300.SH" / "data.parquet"
    reference.parent.mkdir(parents=True)
    pq.write_table(pa.table({"trade_date": ["20250815", "20250818"]}), reference)
    settings = replace(load_baseline_settings(), prices=prices, exposure_root=tmp_path / "exposure")
    args = Namespace(hold_days=5, sell_at="open", candidates=candidates,
                     **settings.thresholds)
    result = baseline._analyze_selected(date(2025, 8, 17), tmp_path, ["N1", "N2"],
                                        records, {"510300.SH": "沪深300"}, args, "test-model", settings)
    expected_sources = {("N1", "N2")}
    assert {tuple(item["record_ids"]) for item in result} == expected_sources
    assert calls.count("refined.yaml") == 2

    monkeypatch.setattr(baseline, "_call_worker", lambda *_, **_kwargs: (_ for _ in ()).throw(AssertionError("cache missed")))
    cached = baseline._analyze_selected(date(2025, 8, 17), tmp_path, ["N1", "N2"],
                                        records, {"510300.SH": "沪深300"}, args, "test-model", settings)
    assert cached == result







def _invoke(*args: str, config_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(APPLICATIONS) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    command = [sys.executable, "-m", "news_agent.baseline", *args]
    if config_path is not None:
        # Run the real CLI with a test-owned config and normal __main__ metadata.
        bootstrap = (
            "import runpy,sys; from functools import partial; from pathlib import Path; "
            "from news_agent import settings; "
            "settings.load_baseline_settings = partial(settings.load_baseline_settings, Path(sys.argv.pop(1))); "
            "runpy.run_module('news_agent.baseline', run_name='__main__', alter_sys=True)"
        )
        command = [sys.executable, "-c", bootstrap, str(config_path), *args]
    return subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_copy_prices_creates_verified_local_snapshot(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "local"
    price_file = source / "ts_code=510300.SH" / "data.parquet"
    price_file.parent.mkdir(parents=True)
    pq.write_table(pa.table({
        "trade_date": ["20250818", "20250819"],
        "open": [100.0, 102.0], "close": [101.0, 103.0],
        "vol": [1000.0, 1200.0], "adj_factor": [1.0, 1.0],
        "extra_column": ["kept", "kept"],
    }), price_file)
    candidates = tmp_path / "etf_candidates.txt"
    candidates.write_text("ETF 候选池\n510300.SH\t沪深300\n", encoding="utf-8")

    result = _invoke("copy-prices", "--source", str(source), "--target", str(destination),
                     "--candidates", str(candidates))

    assert result.returncode == 0, result.stderr
    copied = destination / "ts_code=510300.SH" / "data.parquet"
    assert copied.read_bytes() == price_file.read_bytes()
    manifest = json.loads((destination / "copy_manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"]["510300.SH"]["sha256"] == hashlib.sha256(price_file.read_bytes()).hexdigest()
    assert pq.read_table(copied, columns=["extra_column"]).column(0).to_pylist() == ["kept", "kept"]
    assert price_file.exists()


def test_copy_prices_rejects_missing_fields_and_unreadable_pages(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    price_file = source / "ts_code=510300.SH" / "data.parquet"
    price_file.parent.mkdir(parents=True)
    candidates = tmp_path / "etfs.txt"
    candidates.write_text("510300.SH\t沪深300\n", encoding="utf-8")
    target = tmp_path / "local"
    with pytest.raises(FileNotFoundError, match="行情缺失"):
        baseline.copy_prices(source, target, candidates)

    pq.write_table(pa.table({"trade_date": ["20250818"], "open": [100.0]}), price_file)
    with pytest.raises(ValueError, match="字段缺失"):
        baseline.copy_prices(source, target, candidates)

    pq.write_table(pa.table({"trade_date": ["20250818"], "open": [100.0],
                             "close": [100.0], "vol": [1000.0],
                             "adj_factor": [1.0]}), price_file)
    original = pq.ParquetFile

    class UnreadableDataPage:
        def __init__(self, path: Path) -> None:
            self.parquet = original(path)
            self.schema_arrow = self.parquet.schema_arrow

        def iter_batches(self, **kwargs):
            raise pa.ArrowInvalid("corrupt data page")

    monkeypatch.setattr(baseline.pq, "ParquetFile", UnreadableDataPage)
    with pytest.raises(ValueError, match="数据无法读取"):
        baseline.copy_prices(source, target, candidates)
    assert not (target / "copy_manifest.json").exists()


def test_evaluate_day_scores_grade_and_excludes_post_exit_close(tmp_path: Path, monkeypatch) -> None:
    import yaml
    from news_agent.evaluation import baseline as evaluation_baseline

    config = yaml.safe_load((baseline.ROOT / "config/settings.yaml").read_text())
    config["baseline"]["exposure_root"] = None
    config_path = tmp_path / "settings.yaml"
    config_path.write_text(yaml.safe_dump(config))
    settings = load_baseline_settings(config_path)
    monkeypatch.setattr(evaluation_baseline, "load_baseline_settings", lambda: settings)
    invoke = partial(_invoke, config_path=config_path)
    source = tmp_path / "source"
    price_file = source / "ts_code=510300.SH" / "data.parquet"
    price_file.parent.mkdir(parents=True)
    pq.write_table(pa.table({
        "trade_date": ["20250818", "20250819", "20250820"],
        "open": [100.0, 105.0, 104.0],
        "close": [110.0, 90.0, 50.0],
        "vol": [1000.0, 1000.0, 1000.0],
        "adj_factor": [1.0, 1.0, 1.0],
    }), price_file)
    candidates = tmp_path / "etf_candidates.txt"
    candidates.write_text("ETF 候选池\n510300.SH\t沪深300\n", encoding="utf-8")
    local_prices = tmp_path / "prices"
    copied = invoke("copy-prices", "--source", str(source), "--target", str(local_prices),
                     "--candidates", str(candidates))
    assert copied.returncode == 0, copied.stderr
    price_file.unlink()  # Evaluation must be independent of the source after copying.

    root = tmp_path / "evaluation"
    expected = ("--expect-records", "1", "--expect-batches", "1")
    day = root / "days" / "2025-08-17"
    day.mkdir(parents=True)
    (day / "batch-0001.txt").write_text("记录ID: N1\n标题: 政策新闻\n", encoding="utf-8")
    (day / "manifest.json").write_text(json.dumps({
        "prepared_count": 1, "batches": [{"file": "batch-0001.txt", "ids": ["N1"]}],
    }), encoding="utf-8")
    (day / "records.jsonl").write_text(json.dumps({
        "record_id": "N1", "record_ids": ["N1"], "category": "新闻快讯",
        "members": [{"record_id": "N1", "source": "fixture", "published_at": "2025-08-17"}],
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    initial = root / "results" / "initial" / "2025-08-17"
    initial.mkdir(parents=True)
    (initial / "batch-0001.json").write_text('{"selected_ids":["N1"],"rejected":[]}\n', encoding="utf-8")
    results = root / "results" / "2025-08-17.jsonl"
    results.write_text("\n".join(json.dumps({
        "event_id": event_id, "record_ids": ["N1"], "summary": "政策消息",
        "etf_code": "510300.SH", "direction": direction, "reason": "需求变化",
    }, ensure_ascii=False) for event_id, direction in [
        ("e1", "利好"), ("e2", "利空"),
    ]) + "\n", encoding="utf-8")

    manifest_path = day / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["prepared_count"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    inconsistent = invoke("run", "--day", "2025-08-17", "--input-root", str(root),
                           *expected,
                           "--prices", str(local_prices), "--candidates", str(candidates),
                           "--max-initial-batches", "0")
    assert inconsistent.returncode != 0
    assert "准备记录数不一致" in inconsistent.stderr
    manifest["prepared_count"] = 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    pending = invoke("run", "--day", "2025-08-17", "--input-root", str(root),
                      *expected,
                      "--prices", str(local_prices), "--candidates", str(candidates),
                      "--max-initial-batches", "0")
    assert pending.returncode == 0, pending.stderr
    assert json.loads(pending.stdout)["status"] == "pending_initial"
    assert not (root / "reports" / "baseline" / "2025-08-17.json").exists()

    initial_file = initial / "batch-0001.json"
    initial_file.unlink()
    incomplete = invoke("evaluate", "--day", "2025-08-17", "--input-root", str(root),
                         *expected,
                         "--prices", str(local_prices), "--candidates", str(candidates))
    assert incomplete.returncode != 0
    assert "初筛尚未完成" in incomplete.stderr
    assert not (root / "reports" / "baseline" / "2025-08-17.json").exists()
    initial_file.write_text('{"selected_ids":["N1"],"rejected":[]}\n', encoding="utf-8")
    results.write_text(results.read_text(encoding="utf-8"), encoding="utf-8")
    review_path = root / "results/reviews/2025-08-17.json"
    review_path.parent.mkdir(parents=True)
    review_path.write_text(json.dumps({"selected_records": 1, "reviews": [],
        "mapping_signals": [json.loads(line) for line in results.read_text().splitlines()]}))
    prediction = {"hold_days": 2, "sell_at": "open", "limits": {
        "positive": "0.02", "negative": "-0.015",
    }}
    metadata = {"sha256": hashlib.sha256(results.read_bytes()).hexdigest(), "prediction": prediction,
                "analysis_fingerprint_version": 9, "reviews_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest()}
    from news_agent.baseline import _settings
    from news_agent.evaluation.baseline import analysis_fingerprint
    metadata["analysis_fingerprint"] = analysis_fingerprint(
        _settings(root, local_prices, candidates, date(2025, 8, 17)), date(2025, 8, 17))
    results.with_suffix(".meta.json").write_text(json.dumps(metadata), encoding="utf-8")

    evaluated = invoke("evaluate", "--day", "2025-08-17", "--input-root", str(root),
                        *expected,
                        "--prices", str(local_prices), "--candidates", str(candidates),
                        "--hold-days", "2", "--sell-at", "open")

    assert evaluated.returncode == 0, evaluated.stderr
    summary = json.loads((root / "reports" / "baseline" / "2025-08-17.json").read_text())
    assert (summary["news_records"], summary["initial_batches"], summary["selected_records"]) == (1, 1, 1)
    assert summary["direction_hits"] == 1
    assert summary["direction_total"] == 2
    details = [json.loads(line) for line in (root / "reports" / "baseline" / "2025-08-17-details.jsonl").read_text().splitlines()]
    assert len(details) == 2
    assert [row["direction_hit"] for row in details] == [True, False]
    assert all(row["entry_date"] == "2025-08-18" and row["exit_date"] == "2025-08-20" for row in details)
    assert all(abs(row["return"] - 0.04) < 1e-12 for row in details)
    assert all(abs(row["max_drawdown"] - 20 / 110) < 1e-12 for row in details)

    closing = invoke("evaluate", "--day", "2025-08-17", "--input-root", str(root),
                      *expected,
                      "--prices", str(local_prices), "--candidates", str(candidates),
                      "--hold-days", "2", "--sell-at", "close")
    assert closing.returncode != 0
    assert "请先用 run 重新预测" in closing.stderr
    assert json.loads((root / "reports" / "baseline" / "2025-08-17.json").read_text())["sell_at"] == "open"
    prediction["sell_at"] = "close"  # Represents a fresh prediction for the other exit time.
    results.with_suffix(".meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    closing = invoke("evaluate", "--day", "2025-08-17", "--input-root", str(root),
                      *expected,
                      "--prices", str(local_prices), "--candidates", str(candidates),
                      "--hold-days", "2", "--sell-at", "close")
    assert closing.returncode == 0, closing.stderr
    closed = [json.loads(line) for line in (root / "reports" / "baseline" / "2025-08-17-details.jsonl").read_text().splitlines()]
    assert all(abs(row["return"] + 0.5) < 1e-12 for row in closed)
    assert all(abs(row["max_drawdown"] - 60 / 110) < 1e-12 for row in closed)

    candidates.write_text("ETF 候选池\n510300.SH\t沪深300指数\n", encoding="utf-8")
    stale = invoke("evaluate", "--day", "2025-08-17", "--input-root", str(root),
                    *expected, "--prices", str(local_prices), "--candidates", str(candidates),
                    "--hold-days", "2", "--sell-at", "close")
    assert stale.returncode != 0
    assert "当前新闻、提示词、模型或预测口径不一致" in stale.stderr
    assert json.loads((day / "coverage.json").read_text())["refined_result_state"] == "stale"

    candidates.write_text("ETF 候选池\n510300.SH\t沪深300\n", encoding="utf-8")
    prediction["hold_days"] = 5
    metadata["analysis_fingerprint"] = analysis_fingerprint(
        _settings(root, local_prices, candidates, date(2025, 8, 17)), date(2025, 8, 17))
    results.with_suffix(".meta.json").write_text(json.dumps(metadata), encoding="utf-8")
    insufficient = invoke("evaluate", "--day", "2025-08-17", "--input-root", str(root),
                           *expected, "--prices", str(local_prices), "--candidates", str(candidates),
                           "--hold-days", "5", "--sell-at", "close")
    assert insufficient.returncode == 0, insufficient.stderr
    missing = json.loads((root / "reports" / "baseline" / "2025-08-17.json").read_text())
    assert missing["excluded"] == {"观察期不足": 2}
    assert missing["direction_hit_rate"] == 0 and not missing["scoring_complete"]
