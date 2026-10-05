"""The simplified pipeline preserves final decisions and phase cache isolation."""

from pathlib import Path
import shutil

import yaml

from news_agent import baseline


def test_refinement_changes_do_not_rerun_initial_screening(tmp_path, monkeypatch):
    source = baseline.ROOT
    for relative in ("config/model.yaml", "config/system.yaml", "workflows/initial.yaml",
                     "workflows/refined.yaml", "agent/初筛.md", "agent/精筛.md",
                     "skill_reference.py", "baseline.py"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    reference = tmp_path / "skills/news-catalyst/references/disclosure.md"
    reference.parent.mkdir(parents=True)
    reference.write_text("current judgment method")
    monkeypatch.setattr(baseline, "ROOT", tmp_path)
    initial = baseline._stage_signature("initial")
    refined = baseline._stage_signature("refined")
    reference.write_text("improved judgment method")
    (tmp_path / "agent/精筛.md").write_text("improved refinement task")
    (tmp_path / "report.py").write_text("presentation change")
    assert baseline._stage_signature("initial") == initial
    assert baseline._stage_signature("refined") != refined
    (tmp_path / "agent/初筛.md").write_text("changed initial task")
    assert baseline._stage_signature("initial") != initial


def test_only_two_workflows_and_original_model_reasoning_remain():
    assert {path.stem for path in (baseline.ROOT / "workflows").glob("*.yaml")} == {
        "initial", "refined"}
    profiles = yaml.safe_load((baseline.ROOT / "config/model.yaml").read_text())["model"]
    assert len(profiles) == 2
    assert all(profile["model"] == "gpt-6-luna" and profile["reasoning_effort"] == "xhigh"
               for profile in profiles.values())


def test_bearish_priority_is_independent_of_input_order_and_other_etfs():
    def row(code, direction, source):
        return dict(event_id=source, record_ids=[source], summary=source,
                    etf_code=code, direction=direction, reason="final refinement decision")
    rows = [row("510300.SH", "利好", "good"), row("510300.SH", "利空", "bad"),
            row("512880.SH", "利好", "other")]
    result = baseline.merge_etf_decisions(rows)
    assert result == baseline.merge_etf_decisions(list(reversed(rows)))
    assert {item["etf_code"]: item["direction"] for item in result} == {
        "510300.SH": "利空", "512880.SH": "利好"}
    assert result[0]["record_ids"] == ["bad"]


def test_one_review_ledger_derives_signal_without_source_action_gate(tmp_path, monkeypatch):
    calls = []
    answer = {"reviews": [dict(event_id="event", etf_code="510300.SH", record_numbers=[1],
        summary="final judgment", decision="利空", reason="Agent checked the material",
        sources=[])]}
    monkeypatch.setattr(baseline, "_call_worker", lambda *args, **kwargs: calls.append(args) or answer)
    result = baseline._refined_worker_rows(tmp_path / "chunk.json", "method", Path("refined.yaml"),
        "news input", ["N1"], {"N1": {"record_id": "N1"}}, {"510300.SH": "ETF"}, "news context", {})
    assert result[0]["direction"] == "利空"
    assert len(calls) == 1
    assert not hasattr(baseline, "_review_events")
    assert not hasattr(baseline, "_require_refined_trace")
