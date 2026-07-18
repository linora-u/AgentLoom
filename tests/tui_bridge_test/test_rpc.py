from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from src.lib.runtime.context import RuntimeRunLease

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_project_file(project_root: Path, relative_path: str, content: str) -> Path:
    path = project_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    _write_project_file(
        tmp_path,
        "config/llm.yaml",
        """
model:
  default_model_type: powerful
  powerful:
    model: openai/secret-endpoint
    api_key: must-not-cross-the-bridge
    description: Primary builder model
  summary:
    model: openai/summary-endpoint
    api_key: also-secret
    description: Summary model
""".strip(),
    )
    _write_project_file(
        tmp_path,
        "config/system.yaml",
        """
runtime:
  root_dir: .agentloom
checkpoint:
  enabled: true
""".strip(),
    )
    _write_project_file(
        tmp_path,
        "applications/never_run/workflows/never_run_agent.yaml",
        """
name: never_run_agent
description: A system that has never run.
model_type: powerful
worker_agents:
  - path: applications/never_run/workflows/worker_agents/researcher.yaml
workflow: |
  Ask the researcher for evidence, then summarize it.
""".strip(),
    )
    _write_project_file(
        tmp_path,
        "applications/never_run/workflows/worker_agents/researcher.yaml",
        """
name: researcher
description: Finds evidence.
agent_function_schema:
  description: Research one question.
  inputs:
    query:
      description: Question to research.
      required: true
  output:
    description: Evidence summary.
workflow: |
  Research the supplied query.
""".strip(),
    )
    return tmp_path


def _rpc(project_root: Path, *requests: dict) -> list[dict]:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(REPO_ROOT), existing_pythonpath) if part)
    payload = "".join(json.dumps(request) + "\n" for request in requests)
    completed = subprocess.run(
        [sys.executable, "-m", "src.tui_bridge"],
        cwd=project_root,
        env=env,
        input=payload,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    responses = [json.loads(line) for line in completed.stdout.splitlines() if line]
    responses_by_id = {response["id"]: response for response in responses}
    assert len(responses_by_id) == len(requests)
    return [responses_by_id[request.get("id")] for request in requests]


def test_bootstrap_lists_never_run_systems_and_redacts_model_config(
    project_root: Path,
) -> None:
    [response] = _rpc(
        project_root,
        {"id": "bootstrap-1", "method": "bootstrap", "params": {}},
    )

    assert response["id"] == "bootstrap-1"
    assert response["ok"] is True
    result = response["result"]
    assert result["project"] == {
        "root": str(project_root),
        "name": project_root.name,
    }
    assert result["models"] == {
        "default": "powerful",
        "configured": True,
        "items": [
            {
                "type": "powerful",
                "description": "Primary builder model",
                "default": True,
                "configured": True,
            },
            {
                "type": "summary",
                "description": "Summary model",
                "default": False,
                "configured": True,
            },
        ],
    }
    assert result["systems"] == [
        {
            "id": "applications/never_run/workflows/never_run_agent.yaml",
            "path": "applications/never_run/workflows/never_run_agent.yaml",
            "application_id": "never_run",
            "name": "never_run_agent",
            "description": "A system that has never run.",
            "state": "never_run",
            "validation": {"valid": True, "errors": []},
            "latest_run": None,
        }
    ]
    assert result["runs"] == []
    serialized = json.dumps(result)
    assert "secret-endpoint" not in serialized
    assert "must-not-cross-the-bridge" not in serialized


def test_system_detail_exposes_definition_files_topology_and_never_run_state(
    project_root: Path,
) -> None:
    system_id = "applications/never_run/workflows/never_run_agent.yaml"
    [response] = _rpc(
        project_root,
        {
            "id": "system-detail-1",
            "method": "system.detail",
            "params": {"system_id": system_id},
        },
    )

    assert response["id"] == "system-detail-1"
    assert response["ok"] is True
    result = response["result"]
    assert result["summary"] == {
        "id": system_id,
        "path": system_id,
        "application_id": "never_run",
        "name": "never_run_agent",
        "description": "A system that has never run.",
        "state": "never_run",
        "validation": {"valid": True, "errors": []},
        "latest_run": None,
    }
    assert result["definition"] == {
        "name": "never_run_agent",
        "description": "A system that has never run.",
        "workflow": "Ask the researcher for evidence, then summarize it.",
        "model_type": "powerful",
        "path": system_id,
    }
    assert result["files"] == [
        {
            "path": system_id,
            "kind": "supervisor",
            "size": (project_root / system_id).stat().st_size,
        },
        {
            "path": "applications/never_run/workflows/worker_agents/researcher.yaml",
            "kind": "worker",
            "size": (project_root / "applications/never_run/workflows/worker_agents/researcher.yaml").stat().st_size,
        },
    ]
    assert result["topology"] == {
        "supervisor": {"name": "never_run_agent", "path": system_id},
        "workers": [
            {
                "name": "researcher",
                "path": "applications/never_run/workflows/worker_agents/researcher.yaml",
                "description": "Finds evidence.",
            }
        ],
    }
    assert result["execution"] == {"state": "never_run", "latest_run": None}
    assert result["result_state"] == "never_run"


def test_runtime_state_merge_and_run_detail_use_canonical_runtime_data(
    project_root: Path,
) -> None:
    run_specs = {
        "completed": {
            "run_id": "run-completed",
            "task_id": "task-completed",
            "status": "completed",
            "started_at": "2026-07-17T10:00:00+00:00",
            "ended_at": "2026-07-17T10:01:00+00:00",
        },
        "failed": {
            "run_id": "run-failed",
            "task_id": "task-failed",
            "status": "failed",
            "started_at": "2026-07-17T11:00:00+00:00",
            "ended_at": "2026-07-17T11:01:00+00:00",
        },
        "crashed": {
            "run_id": "run-crashed",
            "task_id": "task-crashed",
            "status": "running",
            "started_at": "2026-07-17T12:00:00+00:00",
        },
        "running": {
            "run_id": "run-running",
            "task_id": "task-running",
            "status": "running",
            "started_at": "2026-07-17T13:00:00+00:00",
        },
    }
    for application_id, spec in run_specs.items():
        workflow_path = _write_project_file(
            project_root,
            f"applications/{application_id}/workflows/{application_id}_agent.yaml",
            f"""
name: {application_id}_agent
description: {application_id} system
model_type: powerful
worker_agents: []
workflow: |
  Execute the {application_id} task.
""".strip(),
        )
        manifest = {
            "schema_version": 1,
            "application_id": application_id,
            "task_id": spec["task_id"],
            "run_id": spec["run_id"],
            "yaml_path": str(workflow_path),
            "agent_name": f"{application_id}_agent",
            "mode": "new",
            "status": spec["status"],
            "started_at": spec["started_at"],
        }
        if "ended_at" in spec:
            manifest["ended_at"] = spec["ended_at"]
        _write_project_file(
            project_root,
            f".agentloom/runs/{application_id}/{spec['run_id']}/manifest.json",
            json.dumps(manifest),
        )

    completed_events = [
        {
            "type": "task_created",
            "task_id": "task-completed",
            "agent_name": "completed_agent",
            "status": "running",
            "created_at": "2026-07-17T10:00:00+00:00",
        },
        {
            "type": "run_started",
            "run_id": "run-completed",
            "timestamp": "2026-07-17T10:00:00+00:00",
        },
        {
            "type": "worker_call_started",
            "agent_name": "researcher",
            "call_index": 0,
            "started_at": "2026-07-17T10:00:10+00:00",
        },
        {
            "type": "worker_call_finished",
            "agent_name": "researcher",
            "call_index": 0,
            "status": "completed",
            "finished_at": "2026-07-17T10:00:20+00:00",
            "result": "evidence",
        },
        {
            "type": "task_status_changed",
            "status": "completed",
            "result": "final answer",
            "timestamp": "2026-07-17T10:01:00+00:00",
        },
    ]
    _write_project_file(
        project_root,
        ".agentloom/checkpoints/completed/task-completed/task_events.jsonl",
        "".join(json.dumps(event) + "\n" for event in completed_events),
    )
    for application_id in ("running", "crashed"):
        spec = run_specs[application_id]
        _write_project_file(
            project_root,
            f".agentloom/checkpoints/{application_id}/{spec['task_id']}/task_tree.json",
            json.dumps(
                {
                    "task_id": spec["task_id"],
                    "run_id": spec["run_id"],
                    "agent_name": f"{application_id}_agent",
                    "status": "running",
                    "created_at": spec["started_at"],
                    "workers": {},
                }
            ),
        )
        _write_project_file(
            project_root,
            f".agentloom/checkpoints/{application_id}/{spec['task_id']}/heartbeat.json",
            json.dumps(
                {
                    "pid": os.getpid(),
                    "timestamp": time.time() if application_id == "running" else 0,
                    "status": "running",
                    "agent_name": f"{application_id}_agent",
                    "run_id": spec["run_id"],
                }
            ),
        )

    log_path = _write_project_file(
        project_root,
        ".agentloom/runs/completed/run-completed/logs/runtime.log",
        "first line\nlast line\n",
    )
    artifact_path = _write_project_file(
        project_root,
        ".agentloom/runs/completed/run-completed/artifacts/report.txt",
        "report body",
    )

    running_lease = RuntimeRunLease(project_root / ".agentloom/runs/running/run-running")
    running_lease.acquire()
    try:
        bootstrap, detail, running_detail, failed_detail = _rpc(
            project_root,
            {"id": "bootstrap-runtime", "method": "bootstrap", "params": {}},
            {
                "id": "run-detail-completed",
                "method": "run.detail",
                "params": {
                    "run_id": "run-completed",
                    "application_id": "completed",
                    "system_id": "applications/completed/workflows/completed_agent.yaml",
                },
            },
            {
                "id": "run-detail-running",
                "method": "run.detail",
                "params": {"run_id": "run-running", "application_id": "running"},
            },
            {
                "id": "run-detail-failed",
                "method": "run.detail",
                "params": {"run_id": "run-failed", "application_id": "failed"},
            },
        )
    finally:
        running_lease.release()

    assert bootstrap["ok"] is True
    systems = {item["application_id"]: item for item in bootstrap["result"]["systems"]}
    assert {application_id: summary["state"] for application_id, summary in systems.items()} == {
        "completed": "completed",
        "crashed": "crashed",
        "failed": "failed",
        "never_run": "never_run",
        "running": "running",
    }
    runs = {item["run_id"]: item for item in bootstrap["result"]["runs"]}
    assert {run_id: summary["status"] for run_id, summary in runs.items()} == {
        "run-completed": "completed",
        "run-crashed": "crashed",
        "run-failed": "failed",
        "run-running": "running",
    }

    assert detail["ok"] is True
    result = detail["result"]
    assert result["summary"] == {
        "run_id": "run-completed",
        "system_id": "applications/completed/workflows/completed_agent.yaml",
        "application_id": "completed",
        "task_id": "task-completed",
        "agent_name": "completed_agent",
        "status": "completed",
        "started_at": "2026-07-17T10:00:00+00:00",
        "ended_at": "2026-07-17T10:01:00+00:00",
    }
    assert result["workers"] == [
        {
            "agent_name": "researcher",
            "call_index": 0,
            "status": "completed",
            "step": None,
            "started_at": "2026-07-17T10:00:10+00:00",
            "ended_at": "2026-07-17T10:00:20+00:00",
            "error": None,
        }
    ]
    assert result["events"] == completed_events
    assert result["logs"] == [
        {
            "path": log_path.relative_to(project_root).as_posix(),
            "size": log_path.stat().st_size,
            "tail": "first line\nlast line\n",
            "tail_truncated": False,
        }
    ]
    assert result["artifacts"] == [
        {
            "path": artifact_path.relative_to(project_root).as_posix(),
            "size": artifact_path.stat().st_size,
        }
    ]
    assert result["result_state"] == "available"
    assert result["result"] == "final answer"
    assert running_detail["result"]["summary"]["status"] == "running"
    assert running_detail["result"]["result_state"] == "running"
    assert running_detail["result"]["result"] is None
    assert failed_detail["result"]["summary"]["status"] == "failed"
    assert failed_detail["result"]["result_state"] == "unavailable"
    assert failed_detail["result"]["result"] is None


def test_rpc_returns_stable_errors_without_terminating_the_process(
    project_root: Path,
) -> None:
    unknown, invalid, missing, bootstrap = _rpc(
        project_root,
        {"id": "unknown", "method": "unknown.method", "params": {}},
        {"id": "invalid", "method": "system.detail", "params": {}},
        {
            "id": "missing",
            "method": "run.detail",
            "params": {"run_id": "missing-run", "application_id": "missing"},
        },
        {"id": "after-errors", "method": "bootstrap", "params": {}},
    )

    assert unknown == {
        "id": "unknown",
        "ok": False,
        "error": {"code": "method_not_found", "message": "unknown method: unknown.method"},
    }
    assert invalid == {
        "id": "invalid",
        "ok": False,
        "error": {
            "code": "invalid_params",
            "message": "system_id must be a non-empty string",
        },
    }
    assert missing == {
        "id": "missing",
        "ok": False,
        "error": {"code": "not_found", "message": "run not found: missing-run"},
    }
    assert bootstrap["id"] == "after-errors"
    assert bootstrap["ok"] is True
