"""Only unfinished news enters a supplemental screening task."""

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from news_agent import baseline
from news_agent.processing.prepare import _record_input
from news_agent.settings import load_baseline_settings

CONTEXT = ("mode=historical_replay；历史日期=2025-07-31；"
           "证据截止=2025-07-31T23:59:59+08:00；ETF 候选：510300.SH、512000.SH。\n")
UNIVERSE = {"510300.SH": "宽基", "512000.SH": "行业"}


def records(count=4):
    return {f"N{i}": {"record_id": f"N{i}", "record_ids": [f"N{i}"],
                       "members": [], "metadata": {}, "category": "新闻快讯",
                       "source": "fixture", "published_at": "2025-07-31T18:00:00+08:00",
                       "time_precision": "datetime", "title": f"新闻{i}",
                       "text": f"正文起点{i}" + "完整正文。" * 1000 + f"正文终点{i}",
                       "url": f"https://example.org/{i}"} for i in range(count)}


def query_for(data):
    ids = list(data)
    return CONTEXT + "\n" + baseline._indexed_news(
        "".join(_record_input(data[rid]) for rid in ids), ids)


def review(numbers, *, event=None, etf=None, decision="放弃"):
    return {"event_id": event, "etf_code": etf, "record_numbers": numbers,
            "summary": "已有结论", "decision": decision,
            "reason": "原始判断理由", "sources": []}


def test_initial_sends_only_missing_full_text_and_keeps_existing_codes(tmp_path, monkeypatch):
    data = records()
    calls = []

    def worker(definition, query, **kwargs):
        calls.append(query)
        assert definition.name == "initial.yaml"
        if len(calls) == 1:
            return {"classifications": [[1, 0], [4, 6]]}
        assert data["N1"]["text"] in query and data["N2"]["text"] in query
        assert data["N0"]["text"] not in query and data["N3"]["text"] not in query
        assert CONTEXT in query
        return {"classifications": [[1, 3], [2, 0]]}

    monkeypatch.setattr(baseline, "_call_worker", worker)
    result = baseline._classify_initial(query_for(data), {"file": "batch.txt", "ids": list(data)},
                                       data, tmp_path / "batch.json", "fingerprint", CONTEXT)
    assert len(calls) == 2
    assert result["selected_ids"] == ["N0", "N2"]
    rejected = {row["reason_code"]: row["record_ids"] for row in result["rejected"]}
    assert rejected == {"旧闻无增量": ["N1"], "非财经": ["N3"]}


def test_refined_sends_only_missing_text_and_preserves_multiple_etfs(tmp_path, monkeypatch):
    data = records()
    calls = []
    original = [review([1], event="e1", etf="510300.SH", decision="利好"), review([4])]

    def worker(definition, query, **kwargs):
        calls.append(query)
        if len(calls) == 1:
            return {"reviews": original}
        assert data["N1"]["text"] in query and data["N2"]["text"] in query
        assert data["N0"]["text"] not in query and data["N3"]["text"] not in query
        assert CONTEXT in query
        assert '"record_number":1,"reported_holdings":[{"fund":"512000.SH"}]' in query
        assert '"fund":"510300.SH"' not in query
        return {"reviews": [review([1], event="e1", etf="510300.SH", decision="利空"),
                            review([2], event="e2", etf="510300.SH", decision="利好"),
                            review([2], event="e2", etf="512000.SH", decision="利好")]}

    monkeypatch.setattr(baseline, "_call_worker", worker)
    path = tmp_path / "chunk.json"
    rows = baseline._refined_worker_rows(path, "fingerprint", Path("refined.yaml"),
                                         query_for(data), list(data), data, UNIVERSE, CONTEXT,
                                         {"N0": [{"fund": "510300.SH"}], "N1": [{"fund": "512000.SH"}]})
    assert len(calls) == 2 and len(rows) == 4
    saved = json.loads(path.read_text())["reviews"]
    assert saved[:2] == baseline._refined_wire_result({"reviews": original}, list(data))["reviews"]
    assert {rid for row in saved for rid in row["record_ids"]} == set(data)
    assert {row["etf_code"] for row in saved if row["record_ids"] == ["N2"]} == set(UNIVERSE)
    assert len({row["event_id"] for row in rows if row["record_ids"] == ["N0"] or row["record_ids"] == ["N1"]}) == 2


@pytest.mark.parametrize("stage", ["initial", "refined"])
def test_incomplete_supplement_saves_progress_and_resumes_only_remaining(stage, tmp_path, monkeypatch):
    data = records()
    path = tmp_path / "result.json"
    calls = []

    def worker(definition, query, **kwargs):
        calls.append(query)
        if len(calls) == 1:
            return {"classifications": [[1, 0], [4, 6]]} if stage == "initial" else {"reviews": [review([1]), review([4])]}
        assert data["N0"]["text"] not in query and data["N3"]["text"] not in query
        if len(calls) == 2:
            assert data["N1"]["text"] in query and data["N2"]["text"] in query
        else:
            assert data["N1"]["text"] not in query and data["N2"]["text"] in query
        return {"classifications": [[1, 3]]} if stage == "initial" else {"reviews": [review([1])]}

    def run(fingerprint="fingerprint"):
        if stage == "initial":
            return baseline._classify_initial(query_for(data), {"file": "batch.txt", "ids": list(data)},
                                               data, path, fingerprint, CONTEXT)
        return baseline._refined_worker_rows(path, fingerprint, Path("refined.yaml"),
                                             query_for(data), list(data), data, UNIVERSE, CONTEXT, {})

    monkeypatch.setattr(baseline, "_call_worker", worker)
    with pytest.raises(baseline.IncompleteNewsResults):
        run()
    assert len(calls) == 2 and not path.exists()
    partial = path.with_name("result-partial.json")
    assert baseline._result_is_current(partial, "fingerprint")
    run()
    assert len(calls) == 3 and path.exists() and not partial.exists()


@pytest.mark.parametrize("stage", ["initial", "refined"])
def test_failed_supplement_does_not_trigger_whole_batch_split(stage, tmp_path, monkeypatch):
    from agentloom.execution.agent_runtime import AgentRuntimeError

    data = records()
    path = tmp_path / "result.json"
    calls = []

    def worker(definition, query, **kwargs):
        calls.append(query)
        if len(calls) == 1:
            return {"classifications": [[1, 0], [4, 6]]} if stage == "initial" else {"reviews": [review([1]), review([4])]}
        raise AgentRuntimeError("provider failure", category="provider", retryable=True)

    monkeypatch.setattr(baseline, "_call_worker", worker)
    with pytest.raises(baseline.IncompleteNewsResults):
        if stage == "initial":
            baseline._classify_initial(query_for(data), {"file": "batch.txt", "ids": list(data)},
                                       data, path, "fingerprint", CONTEXT)
        else:
            baseline._refined_worker_rows(path, "fingerprint", Path("refined.yaml"),
                                         query_for(data), list(data), data, UNIVERSE, CONTEXT, {})
    assert len(calls) == 2 and not path.exists()
    assert baseline._result_is_current(path.with_name("result-partial.json"), "fingerprint")


@pytest.mark.parametrize("stage", ["initial", "refined"])
def test_partial_cache_requires_matching_version_and_input(stage, tmp_path, monkeypatch):
    data = records()
    path = tmp_path / "result.json"
    partial = path.with_name("result-partial.json")
    value = {"classifications": [[1, 0]]} if stage == "initial" else baseline._refined_wire_result(
        {"reviews": [review([1])]}, list(data))
    baseline._record_result(partial, "old-input-or-version", json.dumps(value))
    calls = []

    def worker(definition, query, **kwargs):
        calls.append(query)
        assert all(row["text"] in query for row in data.values())
        return {"classifications": [[n, 0] for n in range(1, 5)]} if stage == "initial" else {"reviews": [review([1, 2, 3, 4])]}

    monkeypatch.setattr(baseline, "_call_worker", worker)
    if stage == "initial":
        baseline._classify_initial(query_for(data), {"file": "batch.txt", "ids": list(data)},
                                   data, path, "new-input-and-version", CONTEXT)
    else:
        baseline._refined_worker_rows(path, "new-input-and-version", Path("refined.yaml"),
                                     query_for(data), list(data), data, UNIVERSE, CONTEXT, {})
    assert len(calls) == 1 and not partial.exists()


@pytest.mark.parametrize("failure", ["missing", "provider"])
def test_refined_pipeline_preserves_partial_without_splitting(failure, tmp_path, monkeypatch):
    from agentloom.execution.agent_runtime import AgentRuntimeError

    data = records()
    day = date(2025, 7, 31)
    calls = []

    def worker(definition, query, **kwargs):
        calls.append(query)
        if len(calls) == 1:
            return {"reviews": [review([1]), review([4])]}
        assert data["N0"]["text"] not in query and data["N3"]["text"] not in query
        if len(calls) == 2 and failure == "provider":
            raise AgentRuntimeError("provider failure", category="provider", retryable=True)
        return {"reviews": [review([1])] if len(calls) == 2 else [
            review([number]) for number in range(1, 3 if failure == "provider" else 2)]}

    monkeypatch.setattr(baseline, "_call_worker", worker)
    monkeypatch.setattr(baseline, "historical_candidates", lambda universe, *_: universe)
    monkeypatch.setattr(baseline, "_entry_news_window", lambda *_: (day, day, date(2025, 8, 1)))
    settings = replace(load_baseline_settings(), exposure_root=None, refined_workers=1)
    args = SimpleNamespace(hold_days=settings.hold_days, sell_at=settings.sell_at, **settings.thresholds)

    def run():
        return baseline._analyze_selected(day, tmp_path, list(data), data, UNIVERSE,
                                           args, "method", settings)

    with pytest.raises(RuntimeError, match="精筛块失败"):
        run()
    assert len(calls) == 2
    directory = tmp_path / "results" / "refined_chunks" / str(day)
    assert (directory / "chunk-0001-partial.json").exists()
    assert not (directory / "chunk-0001.json").exists()
    assert not list(directory.glob("*-split-*.json"))
    assert not (tmp_path / "results" / "reviews" / f"{day}.json").exists()
    assert run() == []
    assert len(calls) == 3
    saved = json.loads((tmp_path / "results" / "reviews" / f"{day}.json").read_text())
    assert {rid for row in saved["reviews"] for rid in row["record_ids"]} == set(data)
