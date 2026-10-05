from dataclasses import replace
from pathlib import Path
import json

import pytest

from news_agent.settings import load_baseline_settings
from news_agent.evaluation import replay


def _config(tmp_path, monkeypatch):
    config = replace(load_baseline_settings(), evaluation_root=tmp_path,
                     version="test", development_month="2025-01",
                     validation_months=("2025-03", "2025-04", "2025-05"))
    monkeypatch.setattr(replay, "load_baseline_settings", lambda: config)
    return config


def test_scoring_definition_is_fixed_before_results(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    fixed = replay.plan()
    assert fixed["result_review_owner"] == "main_agent"
    monkeypatch.setattr(replay, "load_baseline_settings", lambda: replace(config, initial_max_tokens=100000))
    with pytest.raises(ValueError, match="事前计划"):
        replay.plan()


@pytest.mark.parametrize("hit_rate", [0.0, 1.0])
def test_validation_runs_all_configured_months_for_agent_review(tmp_path, monkeypatch, hit_rate):
    _config(tmp_path, monkeypatch)
    fixed = replay.plan()
    assert fixed["counting_unit"] == "entry_date_x_etf"
    assert fixed["deduplication"] == "entry_date_x_etf; bearish_priority"
    assert "representative_rule" not in fixed
    monkeypatch.setattr(replay, "check_freeze", lambda: {"plan": fixed})
    calls = []
    def run_month(month, development):
        calls.append(month)
        return {"directional_signals": {"signals": 1, "hit_rate": hit_rate}}
    monkeypatch.setattr(replay, "_run_month", run_month)
    def score(roots, _start, _end, **kwargs):
        side = {"signals": 1, "hit_rate": hit_rate}
        return {"directional_signals": {"bullish": side, "bearish": side},
                "result_review_owner": "main_agent", "review_status": "pending"}
    monkeypatch.setattr(replay, "score", score)
    result = replay.validate()
    assert calls == ["2025-03", "2025-04", "2025-05"]
    assert result["execution_complete"]
    assert result["review_status"] == "pending"
    assert "status" not in result


def test_freeze_drift_and_previously_used_months_are_rejected(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    fixed = replay.plan()
    source = tmp_path / "input.txt"
    source.write_text("fixed input")
    marker = replay._version_root() / "freeze.json"
    marker.write_text(json.dumps({"plan": fixed, "paths": {str(source): replay.baseline._sha256(source)}}))
    monkeypatch.setattr(replay, "_snapshot_paths", lambda: [source])
    assert replay.check_freeze()["plan"] == fixed
    source.write_text("changed input")
    with pytest.raises(ValueError, match="发生变化"):
        replay.check_freeze()
    marker.unlink()
    monkeypatch.setattr(replay, "score", lambda *_, **kwargs: {
        "directional_signals": {"signals": 1, "hit_rate": 0.0}})
    replay._usage("2025-03", "development")
    with pytest.raises(ValueError, match="不能重新作为独立"):
        replay.freeze()


def test_snapshot_does_not_freeze_mutable_daily_progress(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    folder = replay.month_root("2025-03") / "days/2025-03-01"
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text("{}")
    (folder / "coverage.json").write_text("pending")
    paths = replay._snapshot_paths()
    assert folder / "manifest.json" in paths
    assert folder / "coverage.json" not in paths


def test_monthly_calendar_is_fixed_before_results(tmp_path, monkeypatch):
    _config(tmp_path, monkeypatch)
    fixed = replay.plan()
    assert replay.replay_days("2025-03")[0].isoformat() == "2025-02-28"
    path = replay._version_root() / "plan.json"
    saved = json.loads(path.read_text())
    saved["entry_dates"]["2025-03"].pop()
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="事前计划"):
        replay.plan()
