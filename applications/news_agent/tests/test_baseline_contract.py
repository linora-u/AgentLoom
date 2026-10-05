"""Reject unsupported model claims before they become completed results."""

from __future__ import annotations

from news_agent.tests.source_evidence import verified_sources

from datetime import date
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from news_agent import baseline
from news_agent.processing import prepare
from news_agent.processing.results import mark_refined_complete
from news_agent.settings import Settings


def _event() -> dict:
    return {"event_id": "E1", "record_ids": ["N1"], "summary": "产业需求增加",
            "etf_code": "510300.SH", "direction": "利好",
            "reason": "新闻指出需求增长"}


@pytest.mark.parametrize("change", [
    {"record_ids": ["unknown"]},
    {"record_ids": ["N1", "N1"]},
    {"etf_code": "999999.SH"},
    {"direction": "中性"},
    {"level": "旧档位"},
])
def test_refined_contract_rejects_invalid_claims(change: dict) -> None:
    event = {**_event(), **change}
    with pytest.raises(ValueError):
        baseline._checked_events([event], {"N1"}, {"N1": {"record_id": "N1"}},
                                 {"510300.SH": "宽基"})


def test_refined_contract_accepts_empty_and_rejects_duplicate_event_etf() -> None:
    records = {"N1": {"record_id": "N1"}}
    universe = {"510300.SH": "宽基"}
    assert baseline._checked_events([], {"N1"}, records, universe) == []
    with pytest.raises(ValueError, match="重复"):
        baseline._checked_events([_event(), _event()], {"N1"}, records, universe)


def test_bad_model_output_has_no_whole_batch_correction_or_success_cache(tmp_path: Path, monkeypatch) -> None:
    calls = 0

    def bad_worker(definition: Path, query: str, **_kwargs) -> dict:
        nonlocal calls
        calls += 1
        return {"reviews": "not a list"}

    monkeypatch.setattr(baseline, "_call_worker", bad_worker)
    result_path = tmp_path / "chunk.json"
    with pytest.raises(ValueError, match="reviews 数组"):
        baseline._refined_worker_rows(result_path, "fingerprint", tmp_path / "refined.yaml",
                                      '证据截止=2025-08-17T23:59:59+08:00；fixture query', ["N1"], {"N1": {"record_id": "N1"}},
                                      {"510300.SH": "宽基"}, '证据截止=2025-08-17T23:59:59+08:00', {})
    assert calls == 1
    assert not result_path.exists() and not result_path.with_suffix(".meta.json").exists()


def test_refined_number_restores_primary_id_and_keeps_all_source_members(tmp_path: Path, monkeypatch) -> None:
    primary_id = "N20250731-6b5a3793382f628de3fda584"
    alias_id = "N20250731-028413c131637f3687e05263"
    members = [{"record_id": primary_id, "source": "原文"},
               {"record_id": alias_id, "source": "转载"}]
    record = {"record_id": primary_id, "record_ids": [primary_id, alias_id],
              "members": members}
    records = {primary_id: record, alias_id: record}
    calls = []

    def worker(definition: Path, query: str, **_kwargs) -> dict:
        calls.append(query)
        return {
                "reviews": [{"event_id": "E1", "etf_code": "510300.SH",
                             "record_numbers": [1], "summary": "产业需求增加",
                              "decision": "利好",
                             "reason": "核对原文与同事实转载", "sources": verified_sources()}]}

    monkeypatch.setattr(baseline, "_call_worker", worker)
    path = tmp_path / "chunk.json"
    rows = baseline._refined_worker_rows(
        path, "fingerprint", tmp_path / "refined.yaml", '证据截止=2025-08-17T23:59:59+08:00；fixture query', [primary_id],
        records, {"510300.SH": "宽基"}, '证据截止=2025-08-17T23:59:59+08:00', {})
    assert len(calls) == 1
    assert rows[0]["record_ids"] == [primary_id]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["reviews"][0]["record_ids"] == [primary_id]
    assert saved["reviews"][0]["record_ids"] == [primary_id]
    assert record["members"] == members
    assert record["record_ids"] == [primary_id, alias_id]


def test_selected_news_cannot_silently_lose_analysis_or_abstention_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(baseline, "_call_worker", lambda *_, **_kwargs: { "reviews": []})
    path = tmp_path / "chunk.json"
    with pytest.raises(ValueError, match="遗漏候选新闻判断及放弃原因"):
        baseline._refined_worker_rows(path, "fingerprint", tmp_path / "refined.yaml",
                                      '证据截止=2025-08-17T23:59:59+08:00；完整候选新闻', ["N1"],
                                      {"N1": {"record_id": "N1", "record_ids": ["N1"], "members": [], "metadata": {},
                                               "category": "新闻快讯", "source": "fixture", "published_at": "2025-08-17",
                                               "time_precision": "date", "title": "N1", "text": "完整正文", "url": ""}},
                                      {"510300.SH": "宽基"}, '证据截止=2025-08-17T23:59:59+08:00', {})
    assert not path.exists()


@pytest.mark.parametrize(("category", "retryable"), [
    ("provider", True), ("provider", False),
    ("output_validation", True), ("output_validation", False),
    ("invalid_output", False),
])
def test_failed_refinement_never_triggers_project_split_or_retry(tmp_path: Path, monkeypatch,
                                                               category: str, retryable: bool) -> None:
    from agentloom.execution.agent_runtime import AgentRuntimeError
    from news_agent.settings import load_baseline_settings

    day = date(2025, 7, 31)
    ids = ["N1", "N2"]
    records = {
        rid: {"record_id": rid, "record_ids": [rid], "members": [], "metadata": {},
              "category": "新闻快讯", "source": "fixture", "published_at": str(day),
              "time_precision": "date", "title": rid,
              "text": f"{rid}正文起点" + "完整正文。" * 200 + f"{rid}正文终点", "url": ""}
        for rid in ids
    }
    calls = []

    def worker(_definition: Path, query: str, **_kwargs) -> dict:
        calls.append(query)
        if len(calls) == 1:
            if category == "invalid_output":
                return {"reviews": "not a list"}
            message = "Pi model request timed out" if retryable else "Pi Codex provider request failed"
            raise AgentRuntimeError(message, category=category, retryable=retryable)
        current = [rid for rid in ids if records[rid]["text"] in query]
        return { "reviews": [
            {"event_id": None, "etf_code": None, "record_numbers": [number], "summary": "观点回顾",
              "decision": "放弃",  "reason": "没有新增事实", "sources": []}
            for number, rid in enumerate(current, 1)]}

    monkeypatch.setattr(baseline, "_call_worker", worker)
    monkeypatch.setattr(baseline, "historical_candidates", lambda universe, *_: universe)
    settings = replace(load_baseline_settings(), exposure_root=None, refined_workers=1)
    args = SimpleNamespace(hold_days=settings.hold_days, sell_at=settings.sell_at, **settings.thresholds)
    with pytest.raises(RuntimeError, match="精筛块失败"):
        baseline._analyze_selected(day, tmp_path, ids, records, {"510300.SH": "宽基"},
                                   args, "method", settings)
    assert len(calls) == 1
    assert all(records[rid]["text"] in calls[0] for rid in ids)
    assert not (tmp_path / "results/reviews" / f"{day}.json").exists()
    directory = tmp_path / "results/refined_chunks" / str(day)
    assert not list(directory.glob("*.json"))


def test_interrupted_coverage_update_keeps_previous_state(tmp_path: Path, monkeypatch) -> None:
    day = date(2025, 8, 17)
    coverage = tmp_path / "days" / day.isoformat() / "coverage.json"
    coverage.parent.mkdir(parents=True)
    coverage.write_text('{"refined_result_state":"pending"}\n', encoding="utf-8")
    original = coverage.read_bytes()
    result = tmp_path / "results" / f"{day.isoformat()}.jsonl"
    result.parent.mkdir(parents=True)
    result.write_text("", encoding="utf-8")
    settings = Settings(news_root=tmp_path, etf_root=tmp_path, etf_config=tmp_path,
                        output_root=tmp_path, mode="baseline", day=day, year=2025)
    original_replace = prepare.os.replace

    def interrupted_replace(source: Path, destination: Path) -> None:
        if destination == coverage:
            raise OSError("simulated interruption")
        original_replace(source, destination)

    monkeypatch.setattr(prepare.os, "replace", interrupted_replace)
    with pytest.raises(OSError, match="simulated interruption"):
        mark_refined_complete(settings, day, 0)
    assert coverage.read_bytes() == original
    assert json.loads(coverage.read_text(encoding="utf-8"))["refined_result_state"] == "pending"
