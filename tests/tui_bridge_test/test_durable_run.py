from __future__ import annotations

import json
from pathlib import Path

from src.tui_bridge.bridge import TuiBridge


def test_run_detail_uses_durable_events_after_success_checkpoint_cleanup(tmp_path: Path) -> None:
    workflow = tmp_path / "applications/reports/workflows/report.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: report\ndescription: report\nworkflow: write a report\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / ".agentloom/runs/reports/run-1"
    (run_dir / "audit").mkdir(parents=True)
    (run_dir / "artifacts").mkdir()
    manifest = {
        "schema_version": 1,
        "application_id": "reports",
        "task_id": "task-1",
        "run_id": "run-1",
        "yaml_path": str(workflow),
        "agent_name": "report",
        "status": "completed",
        "started_at": "2026-07-17T10:00:00+00:00",
        "ended_at": "2026-07-17T10:01:00+00:00",
        "result_artifact": "artifacts/result.txt",
        "task_events_artifact": "audit/task_events.jsonl",
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (run_dir / "artifacts/result.txt").write_text("final report", encoding="utf-8")
    events = [
        {"type": "run_started", "run_id": "run-1"},
        {"type": "worker_call_started", "agent_name": "researcher", "call_index": 0},
        {
            "type": "worker_call_finished",
            "agent_name": "researcher",
            "call_index": 0,
            "status": "completed",
        },
        {"type": "task_status_changed", "status": "completed", "result": "final report"},
    ]
    (run_dir / "audit/task_events.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    resumed_events = tmp_path / ".agentloom/checkpoints/reports/task-1/task_events.jsonl"
    resumed_events.parent.mkdir(parents=True)
    resumed_events.write_text(
        json.dumps({"type": "run_resumed", "run_id": "run-2"}) + "\n",
        encoding="utf-8",
    )

    result = TuiBridge(tmp_path).dispatch(
        "run.detail",
        {
            "run_id": "run-1",
            "application_id": "reports",
            "system_id": "applications/reports/workflows/report.yaml",
        },
    )

    assert result["result_state"] == "available"
    assert result["result"] == "final report"
    assert result["events"] == events
    assert result["workers"] == [
        {
            "agent_name": "researcher",
            "call_index": 0,
            "status": "completed",
            "step": None,
            "started_at": None,
            "ended_at": None,
            "error": None,
        }
    ]
