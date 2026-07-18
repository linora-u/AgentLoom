from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import src.tui_bridge.bridge as bridge_module
from src.lib.runtime.context import RuntimeRunLease
from src.tui_bridge.bridge import BridgeError, TuiBridge

SYSTEM_ID = "applications/demo/workflows/demo.yaml"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _project(tmp_path: Path) -> TuiBridge:
    _write(
        tmp_path / "config/system.yaml",
        "runtime:\n  root_dir: .runtime-live\n",
    )
    _write(
        tmp_path / "config/llm.yaml",
        "model:\n  default_model_type: test\n  test:\n    model: openai/test\n",
    )
    _write(
        tmp_path / SYSTEM_ID,
        """\
name: demo
description: Cached Agent identity
model_type: test
worker_agents: []
workflow: Run the task.
""",
    )
    _write(
        tmp_path / "applications/demo/skills/reader/SKILL.md",
        "---\nname: reader\ndescription: Reads evidence\n---\n",
    )
    return TuiBridge(tmp_path)


def _completed_run(tmp_path: Path) -> None:
    run_id = "run-live"
    task_id = "task-live"
    run_dir = tmp_path / ".runtime-live/runs/demo" / run_id
    _write(
        run_dir / "manifest.json",
        json.dumps(
            {
                "application_id": "demo",
                "task_id": task_id,
                "run_id": run_id,
                "agent_name": "demo",
                "yaml_path": SYSTEM_ID,
                "status": "completed",
                "started_at": "2026-07-18T08:00:00+00:00",
                "ended_at": "2026-07-18T08:01:00+00:00",
            }
        ),
    )
    _write(
        tmp_path / ".runtime-live/checkpoints/demo" / task_id / "task_tree.json",
        json.dumps(
            {
                "run_id": run_id,
                "status": "completed",
                "agent_name": "demo",
                "workers": {
                    "helper": [
                        {
                            "call_index": 2,
                            "status": "succeeded",
                            "step": 3,
                            "started_at": "2026-07-18T08:00:10+00:00",
                            "finished_at": "2026-07-18T08:00:20+00:00",
                            "error": None,
                            "attempt_run_id": run_id,
                        }
                    ]
                },
            }
        ),
    )


def _schedule_document(tmp_path: Path) -> None:
    _write(
        tmp_path / ".agentloom/schedules/jobs.json",
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "job_demo",
                        "name": "Daily demo",
                        "yaml_path": SYSTEM_ID,
                        "schedule": {
                            "kind": "cron",
                            "expression": "0 9 * * *",
                            "timezone": "Asia/Shanghai",
                        },
                        "state": "paused",
                        "next_run_at": None,
                        "last_run_at": None,
                        "last_status": None,
                        "run_count": 0,
                        "claim": None,
                    }
                ],
                "executions": [],
            }
        ),
    )
    _write(
        tmp_path / ".agentloom/schedules/serve-status.json",
        json.dumps(
            {
                "pid": 123,
                "started_at": "2026-07-18T07:00:00+00:00",
                "last_tick_at": "2026-07-18T07:01:00+00:00",
                "stopped_at": "2026-07-18T07:02:00+00:00",
            }
        ),
    )


def _run_with_worker_calls(
    tmp_path: Path,
    *,
    run_id: str,
    task_id: str,
    manifest_status: str,
    task_status: str,
    started_at: str,
    ended_at: str | None,
    workers: list[dict[str, object]],
    additional_workers: dict[str, list[dict[str, object]]] | None = None,
) -> Path:
    run_dir = tmp_path / ".runtime-live/runs/demo" / run_id
    _write(
        run_dir / "manifest.json",
        json.dumps(
            {
                "application_id": "demo",
                "task_id": task_id,
                "run_id": run_id,
                "agent_name": "demo",
                "yaml_path": SYSTEM_ID,
                "status": manifest_status,
                "started_at": started_at,
                "ended_at": ended_at,
            }
        ),
    )
    _write(
        tmp_path / ".runtime-live/checkpoints/demo" / task_id / "task_tree.json",
        json.dumps(
            {
                "run_id": run_id,
                "status": task_status,
                "agent_name": "demo",
                "workers": {"helper": workers, **(additional_workers or {})},
            }
        ),
    )
    return run_dir


def _terminal_run_with_stale_worker(
    tmp_path: Path,
    *,
    manifest_status: str,
    task_status: str,
    worker_status: str,
) -> None:
    run_id = "run-terminal"
    _run_with_worker_calls(
        tmp_path,
        run_id=run_id,
        task_id="task-terminal",
        manifest_status=manifest_status,
        task_status=task_status,
        started_at="2026-07-18T08:00:00+00:00",
        ended_at="2026-07-18T08:01:00+00:00",
        workers=[
            {
                "call_index": 1,
                "status": worker_status,
                "step": 2,
                "started_at": "2026-07-18T08:00:10+00:00",
                "finished_at": None,
                "error": None,
                "attempt_run_id": run_id,
            }
        ],
    )


def test_runtime_summary_requires_bootstrap_and_strict_empty_params(tmp_path: Path) -> None:
    bridge = _project(tmp_path)

    with pytest.raises(BridgeError) as before_bootstrap:
        bridge.dispatch("runtime.summary", {})
    assert before_bootstrap.value.code == "not_ready"
    assert str(before_bootstrap.value) == "runtime.summary requires a successful bootstrap"

    with pytest.raises(BridgeError) as invalid:
        bridge.dispatch("runtime.summary", {"refresh": True})
    assert invalid.value.code == "invalid_params"
    assert str(invalid.value) == "runtime.summary params are invalid (unexpected refresh)"


@pytest.mark.parametrize(
    ("manifest_status", "task_status", "worker_status", "expected_status"),
    [
        ("completed", "completed", "running", "completed"),
        ("failed", "failed", "claimed", "failed"),
        ("interrupted", "interrupted", "claimed", "interrupted"),
        ("running", "running", "in_progress", "crashed"),
    ],
)
def test_terminal_parent_run_reconciles_stale_active_worker_status_in_live_projections(
    tmp_path: Path,
    manifest_status: str,
    task_status: str,
    worker_status: str,
    expected_status: str,
) -> None:
    bridge = _project(tmp_path)
    _terminal_run_with_stale_worker(
        tmp_path,
        manifest_status=manifest_status,
        task_status=task_status,
        worker_status=worker_status,
    )

    bootstrap = bridge.bootstrap()
    live = bridge.dispatch("runtime.summary", {})

    assert bootstrap["runs"][0]["status"] == expected_status
    assert bootstrap["worker_invocations"][0]["status"] == expected_status
    assert live["runs"][0]["status"] == expected_status
    assert live["worker_invocations"][0]["status"] == expected_status


def test_active_worker_beats_a_newer_terminal_invocation_from_another_run(
    tmp_path: Path,
) -> None:
    bridge = _project(tmp_path)
    _run_with_worker_calls(
        tmp_path,
        run_id="run-newer-terminal",
        task_id="task-newer-terminal",
        manifest_status="completed",
        task_status="completed",
        started_at="2026-07-18T09:00:00+00:00",
        ended_at="2026-07-18T09:05:00+00:00",
        workers=[
            {
                "call_index": 2,
                "status": "completed",
                "started_at": "2026-07-18T09:01:00+00:00",
                "finished_at": "2026-07-18T09:02:00+00:00",
                "attempt_run_id": "run-newer-terminal",
            }
        ],
    )
    active_run_dir = _run_with_worker_calls(
        tmp_path,
        run_id="run-older-active",
        task_id="task-older-active",
        manifest_status="running",
        task_status="running",
        started_at="2026-07-18T08:00:00+00:00",
        ended_at=None,
        workers=[
            {
                "call_index": 1,
                "status": "claimed",
                "started_at": "2026-07-18T08:01:00+00:00",
                "finished_at": None,
                "attempt_run_id": "run-older-active",
            }
        ],
    )
    lease = RuntimeRunLease(active_run_dir)
    lease.acquire()
    try:
        bootstrap = bridge.bootstrap()
        live = bridge.dispatch("runtime.summary", {})
    finally:
        lease.release()

    for projection in (bootstrap, live):
        [worker] = projection["worker_invocations"]
        assert worker["run_id"] == "run-older-active"
        assert worker["status"] == "claimed"


def test_active_parallel_invocation_beats_higher_terminal_call_index(
    tmp_path: Path,
) -> None:
    bridge = _project(tmp_path)
    run_dir = _run_with_worker_calls(
        tmp_path,
        run_id="run-parallel",
        task_id="task-parallel",
        manifest_status="running",
        task_status="running",
        started_at="2026-07-18T10:00:00+00:00",
        ended_at=None,
        workers=[
            {
                "call_index": 1,
                "status": "in_progress",
                "started_at": "2026-07-18T10:01:00+00:00",
                "finished_at": None,
                "attempt_run_id": "run-parallel",
            },
            {
                "call_index": 2,
                "status": "completed",
                "started_at": "2026-07-18T10:02:00+00:00",
                "finished_at": "2026-07-18T10:03:00+00:00",
                "attempt_run_id": "run-parallel",
            },
        ],
    )
    lease = RuntimeRunLease(run_dir)
    lease.acquire()
    try:
        projection = bridge.bootstrap()
    finally:
        lease.release()

    [worker] = projection["worker_invocations"]
    assert worker["call_index"] == 1
    assert worker["status"] == "in_progress"


def test_latest_terminal_worker_uses_invocation_time_before_run_start_order(
    tmp_path: Path,
) -> None:
    bridge = _project(tmp_path)
    _run_with_worker_calls(
        tmp_path,
        run_id="run-newer-start",
        task_id="task-newer-start",
        manifest_status="completed",
        task_status="completed",
        started_at="2026-07-18T11:00:00+00:00",
        ended_at="2026-07-18T11:05:00+00:00",
        workers=[
            {
                "call_index": 5,
                "status": "completed",
                "started_at": "2026-07-18T11:00:30+00:00",
                "finished_at": "2026-07-18T11:01:00+00:00",
                "attempt_run_id": "run-newer-start",
            }
        ],
    )
    _run_with_worker_calls(
        tmp_path,
        run_id="run-later-finish",
        task_id="task-later-finish",
        manifest_status="completed",
        task_status="completed",
        started_at="2026-07-18T10:00:00+00:00",
        ended_at="2026-07-18T11:40:00+00:00",
        workers=[
            {
                "call_index": 1,
                "status": "completed",
                "started_at": "2026-07-18T11:20:00+00:00",
                "finished_at": "2026-07-18T11:30:00+00:00",
                "attempt_run_id": "run-later-finish",
            }
        ],
    )

    projection = bridge.bootstrap()

    [worker] = projection["worker_invocations"]
    assert worker["run_id"] == "run-later-finish"
    assert worker["call_index"] == 1


def test_worker_history_cannot_hide_a_later_active_worker_inside_task_budget(
    tmp_path: Path,
) -> None:
    bridge = _project(tmp_path)
    history = [
        {
            "call_index": index,
            "status": "completed",
            "started_at": "2026-07-18T08:00:00+00:00",
            "finished_at": "2026-07-18T08:01:00+00:00",
            "attempt_run_id": "run-bounded",
        }
        for index in range(256)
    ]
    run_dir = _run_with_worker_calls(
        tmp_path,
        run_id="run-bounded",
        task_id="task-bounded",
        manifest_status="running",
        task_status="running",
        started_at="2026-07-18T08:00:00+00:00",
        ended_at=None,
        workers=history,
        additional_workers={
            "current": [
                {
                    "call_index": 0,
                    "status": "running",
                    "started_at": "2026-07-18T09:00:00+00:00",
                    "finished_at": None,
                    "attempt_run_id": "run-bounded",
                }
            ]
        },
    )
    lease = RuntimeRunLease(run_dir)
    lease.acquire()
    try:
        bootstrap = bridge.bootstrap()
        live = bridge.dispatch("runtime.summary", {})
    finally:
        lease.release()

    for projection in (bootstrap, live):
        invocations = {item["agent_name"]: item for item in projection["worker_invocations"]}
        assert invocations["current"]["status"] == "running"
        assert invocations["helper"]["call_index"] == 255
        assert projection["worker_invocations_incomplete"] is False


def test_run_detail_keeps_each_worker_entity_before_filling_call_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bridge_module,
        "RUN_DETAIL_WORKER_MAX_COUNT",
        3,
    )
    bridge = _project(tmp_path)
    _run_with_worker_calls(
        tmp_path,
        run_id="run-detail-budget",
        task_id="task-detail-budget",
        manifest_status="running",
        task_status="running",
        started_at="2026-07-18T08:00:00+00:00",
        ended_at=None,
        workers=[
            {
                "call_index": index,
                "status": "completed",
                "started_at": f"2026-07-18T08:0{index}:00+00:00",
                "finished_at": f"2026-07-18T08:0{index + 1}:00+00:00",
                "attempt_run_id": "run-detail-budget",
            }
            for index in range(3)
        ],
        additional_workers={
            "current": [
                {
                    "call_index": 0,
                    "status": "running",
                    "started_at": "2026-07-18T09:00:00+00:00",
                    "finished_at": None,
                    "attempt_run_id": "run-detail-budget",
                }
            ]
        },
    )

    bootstrap = bridge.bootstrap()
    detail = bridge.dispatch(
        "run.detail",
        {"application_id": "demo", "run_id": "run-detail-budget"},
    )

    assert {(worker["agent_name"], worker["call_index"]) for worker in detail["workers"]} == {
        ("current", 0),
        ("helper", 1),
        ("helper", 2),
    }
    assert detail["limits"]["workers"] == {
        "truncated": True,
        "returned_count": 3,
        "max_count": 3,
    }
    assert {worker["agent_name"] for worker in bootstrap["worker_invocations"]} == {
        "current",
        "helper",
    }
    assert bootstrap["worker_invocations_incomplete"] is False


def test_worker_entity_overflow_is_explicitly_incomplete(tmp_path: Path) -> None:
    bridge = _project(tmp_path)
    workers = {
        f"worker-{index:03d}": [
            {
                "call_index": 0,
                "status": "completed",
                "started_at": "2026-07-18T08:00:00+00:00",
                "finished_at": "2026-07-18T08:01:00+00:00",
                "attempt_run_id": "run-overflow",
            }
        ]
        for index in range(257)
    }
    _run_with_worker_calls(
        tmp_path,
        run_id="run-overflow",
        task_id="task-overflow",
        manifest_status="completed",
        task_status="completed",
        started_at="2026-07-18T08:00:00+00:00",
        ended_at="2026-07-18T08:02:00+00:00",
        workers=[],
        additional_workers=workers,
    )

    bootstrap = bridge.bootstrap()
    live = bridge.dispatch("runtime.summary", {})

    for projection in (bootstrap, live):
        assert len(projection["worker_invocations"]) == 256
        assert projection["worker_invocations_incomplete"] is True


def test_oversized_task_tree_is_explicitly_incomplete_everywhere(
    tmp_path: Path,
) -> None:
    bridge = _project(tmp_path)
    _run_with_worker_calls(
        tmp_path,
        run_id="run-oversized-tree",
        task_id="task-oversized-tree",
        manifest_status="completed",
        task_status="completed",
        started_at="2026-07-18T08:00:00+00:00",
        ended_at="2026-07-18T08:02:00+00:00",
        workers=[],
    )
    tree_path = tmp_path / ".runtime-live/checkpoints/demo/task-oversized-tree/task_tree.json"
    _write(
        tree_path,
        json.dumps(
            {
                "run_id": "run-oversized-tree",
                "status": "completed",
                "agent_name": "demo",
                # Real task trees can exceed the projection budget because
                # task input/result is large even when the Worker catalog is tiny.
                "task_input": "x" * (bridge_module.RUNTIME_TASK_PROJECTION_MAX_BYTES + 1),
                "workers": {
                    "helper": [
                        {
                            "call_index": 0,
                            "status": "completed",
                            "attempt_run_id": "run-oversized-tree",
                        }
                    ]
                },
            }
        ),
    )
    assert tree_path.stat().st_size > bridge_module.RUNTIME_TASK_PROJECTION_MAX_BYTES

    bootstrap = bridge.bootstrap()
    live = bridge.dispatch("runtime.summary", {})
    detail = bridge.dispatch(
        "run.detail",
        {"application_id": "demo", "run_id": "run-oversized-tree"},
    )

    for projection in (bootstrap, live):
        assert projection["worker_invocations"] == []
        assert projection["worker_invocations_incomplete"] is True
    assert detail["workers"] == []
    assert detail["limits"]["workers"]["truncated"] is True


def test_oversized_archived_task_tree_is_explicitly_incomplete(
    tmp_path: Path,
) -> None:
    bridge = _project(tmp_path)
    run_dir = _run_with_worker_calls(
        tmp_path,
        run_id="run-oversized-archive",
        task_id="task-oversized-archive",
        manifest_status="completed",
        task_status="completed",
        started_at="2026-07-18T08:00:00+00:00",
        ended_at="2026-07-18T08:02:00+00:00",
        workers=[],
    )
    archived_tree = run_dir / "audit/task_tree.json"
    _write(
        archived_tree,
        json.dumps(
            {
                "run_id": "run-oversized-archive",
                "task_id": "task-oversized-archive",
                "status": "completed",
                "task_input": "x" * (bridge_module.RUNTIME_TASK_PROJECTION_MAX_BYTES + 1),
                "workers": {},
            }
        ),
    )
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["task_tree_artifact"] = "audit/task_tree.json"
    _write(manifest_path, json.dumps(manifest))
    (tmp_path / ".runtime-live/checkpoints/demo/task-oversized-archive/task_tree.json").unlink()

    bootstrap = bridge.bootstrap()
    detail = bridge.dispatch(
        "run.detail",
        {"application_id": "demo", "run_id": "run-oversized-archive"},
    )

    assert bootstrap["worker_invocations"] == []
    assert bootstrap["worker_invocations_incomplete"] is True
    assert detail["workers"] == []
    assert detail["limits"]["workers"]["truncated"] is True


def test_disabled_task_tree_observation_never_claims_worker_never_ran(
    tmp_path: Path,
) -> None:
    bridge = _project(tmp_path)
    run_dir = _run_with_worker_calls(
        tmp_path,
        run_id="run-observation-disabled",
        task_id="task-observation-disabled",
        manifest_status="completed",
        task_status="completed",
        started_at="2026-07-18T08:00:00+00:00",
        ended_at="2026-07-18T08:02:00+00:00",
        workers=[],
    )
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["task_tree_observation"] = {
        "enabled": False,
        "worker_agents_configured": True,
    }
    _write(manifest_path, json.dumps(manifest))
    (tmp_path / ".runtime-live/checkpoints/demo/task-observation-disabled/task_tree.json").unlink()

    bootstrap = bridge.bootstrap()
    live = bridge.dispatch("runtime.summary", {})
    detail = bridge.dispatch(
        "run.detail",
        {"application_id": "demo", "run_id": "run-observation-disabled"},
    )

    for projection in (bootstrap, live):
        assert projection["worker_invocations"] == []
        assert projection["worker_invocations_incomplete"] is True
    assert detail["workers"] == []
    assert detail["limits"]["workers"]["truncated"] is True


def test_archived_task_tree_preserves_worker_status_after_checkpoint_cleanup(
    tmp_path: Path,
) -> None:
    bridge = _project(tmp_path)
    run_dir = _run_with_worker_calls(
        tmp_path,
        run_id="run-archived-tree",
        task_id="task-archived-tree",
        manifest_status="completed",
        task_status="completed",
        started_at="2026-07-18T08:00:00+00:00",
        ended_at="2026-07-18T08:02:00+00:00",
        workers=[
            {
                "call_index": 2,
                "status": "completed",
                "started_at": "2026-07-18T08:01:00+00:00",
                "finished_at": "2026-07-18T08:01:30+00:00",
                "attempt_run_id": "run-archived-tree",
            }
        ],
    )
    checkpoint_tree = tmp_path / ".runtime-live/checkpoints/demo/task-archived-tree/task_tree.json"
    archived_tree = run_dir / "audit/task_tree.json"
    _write(archived_tree, checkpoint_tree.read_text(encoding="utf-8"))
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["task_tree_artifact"] = "audit/task_tree.json"
    _write(manifest_path, json.dumps(manifest))
    checkpoint_tree.unlink()

    bootstrap = bridge.bootstrap()
    live = bridge.dispatch("runtime.summary", {})
    detail = bridge.dispatch(
        "run.detail",
        {"application_id": "demo", "run_id": "run-archived-tree"},
    )

    for projection in (bootstrap, live):
        [worker] = projection["worker_invocations"]
        assert worker["agent_name"] == "helper"
        assert worker["call_index"] == 2
        assert worker["status"] == "completed"
        assert projection["worker_invocations_incomplete"] is False
    assert detail["workers"] == [
        {
            "agent_name": "helper",
            "call_index": 2,
            "status": "completed",
            "step": None,
            "started_at": "2026-07-18T08:01:00+00:00",
            "ended_at": "2026-07-18T08:01:30+00:00",
            "error": None,
        }
    ]


@pytest.mark.parametrize("artifact_kind", ["symlink", "escape"])
def test_declared_task_tree_artifact_cannot_escape_its_run(
    tmp_path: Path,
    artifact_kind: str,
) -> None:
    bridge = _project(tmp_path)
    run_id = f"run-unsafe-{artifact_kind}"
    task_id = f"task-unsafe-{artifact_kind}"
    run_dir = _run_with_worker_calls(
        tmp_path,
        run_id=run_id,
        task_id=task_id,
        manifest_status="completed",
        task_status="completed",
        started_at="2026-07-18T08:00:00+00:00",
        ended_at="2026-07-18T08:02:00+00:00",
        workers=[],
    )
    checkpoint_tree = tmp_path / f".runtime-live/checkpoints/demo/{task_id}/task_tree.json"
    outside_tree = run_dir.parent / f"outside-{artifact_kind}.json"
    _write(
        outside_tree,
        json.dumps(
            {
                "run_id": run_id,
                "task_id": task_id,
                "status": "completed",
                "workers": {
                    "stolen": [
                        {
                            "call_index": 0,
                            "status": "completed",
                            "attempt_run_id": run_id,
                        }
                    ]
                },
            }
        ),
    )
    if artifact_kind == "symlink":
        artifact_path = run_dir / "audit/task_tree.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.symlink_to(outside_tree)
        artifact = "audit/task_tree.json"
    else:
        artifact = f"../{outside_tree.name}"
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["task_tree_artifact"] = artifact
    _write(manifest_path, json.dumps(manifest))
    checkpoint_tree.unlink()

    bootstrap = bridge.bootstrap()
    detail = bridge.dispatch(
        "run.detail",
        {"application_id": "demo", "run_id": run_id},
    )

    assert bootstrap["worker_invocations"] == []
    assert bootstrap["worker_invocations_incomplete"] is True
    assert detail["workers"] == []
    assert detail["limits"]["workers"]["truncated"] is True


def test_active_attempt_merges_workers_from_an_archived_attempt(
    tmp_path: Path,
) -> None:
    bridge = _project(tmp_path)
    task_id = "task-archived-history"
    old_run_dir = _run_with_worker_calls(
        tmp_path,
        run_id="run-archived-history",
        task_id=task_id,
        manifest_status="completed",
        task_status="completed",
        started_at="2026-07-18T08:00:00+00:00",
        ended_at="2026-07-18T08:02:00+00:00",
        workers=[],
        additional_workers={
            "historical": [
                {
                    "call_index": 1,
                    "status": "completed",
                    "finished_at": "2026-07-18T08:01:00+00:00",
                    "attempt_run_id": "run-archived-history",
                }
            ]
        },
    )
    checkpoint_tree = tmp_path / f".runtime-live/checkpoints/demo/{task_id}/task_tree.json"
    _write(
        old_run_dir / "audit/task_tree.json",
        checkpoint_tree.read_text(encoding="utf-8"),
    )
    old_manifest_path = old_run_dir / "manifest.json"
    old_manifest = json.loads(old_manifest_path.read_text(encoding="utf-8"))
    old_manifest["task_tree_artifact"] = "audit/task_tree.json"
    _write(old_manifest_path, json.dumps(old_manifest))

    current_run_dir = _run_with_worker_calls(
        tmp_path,
        run_id="run-current-history",
        task_id=task_id,
        manifest_status="running",
        task_status="running",
        started_at="2026-07-18T09:00:00+00:00",
        ended_at=None,
        workers=[],
        additional_workers={
            "current": [
                {
                    "call_index": 0,
                    "status": "running",
                    "started_at": "2026-07-18T09:01:00+00:00",
                    "attempt_run_id": "run-current-history",
                }
            ]
        },
    )
    lease = RuntimeRunLease(current_run_dir)
    lease.acquire()
    try:
        bootstrap = bridge.bootstrap()
        live = bridge.dispatch("runtime.summary", {})
    finally:
        lease.release()

    for projection in (bootstrap, live):
        workers = {worker["agent_name"]: worker for worker in projection["worker_invocations"]}
        assert workers["historical"]["run_id"] == "run-archived-history"
        assert workers["historical"]["status"] == "completed"
        assert workers["current"]["run_id"] == "run-current-history"
        assert workers["current"]["status"] == "running"
        assert projection["worker_invocations_incomplete"] is False


def test_live_transition_uses_archive_after_success_cleanup(
    tmp_path: Path,
) -> None:
    bridge = _project(tmp_path)
    run_id = "run-live-archive"
    task_id = "task-live-archive"
    run_dir = _run_with_worker_calls(
        tmp_path,
        run_id=run_id,
        task_id=task_id,
        manifest_status="running",
        task_status="running",
        started_at="2026-07-18T08:00:00+00:00",
        ended_at=None,
        workers=[
            {
                "call_index": 0,
                "status": "running",
                "started_at": "2026-07-18T08:01:00+00:00",
                "attempt_run_id": run_id,
            }
        ],
    )
    lease = RuntimeRunLease(run_dir)
    lease.acquire()
    try:
        bootstrap = bridge.bootstrap()
    finally:
        lease.release()
    [active_worker] = bootstrap["worker_invocations"]
    assert active_worker["status"] == "running"

    checkpoint_tree = tmp_path / f".runtime-live/checkpoints/demo/{task_id}/task_tree.json"
    tree = json.loads(checkpoint_tree.read_text(encoding="utf-8"))
    tree["status"] = "completed"
    tree["workers"]["helper"][0].update(
        status="completed",
        finished_at="2026-07-18T08:02:00+00:00",
    )
    _write(run_dir / "audit/task_tree.json", json.dumps(tree))
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        status="completed",
        ended_at="2026-07-18T08:03:00+00:00",
        task_tree_artifact="audit/task_tree.json",
    )
    _write(manifest_path, json.dumps(manifest))
    checkpoint_tree.unlink()

    live = bridge.dispatch("runtime.summary", {})

    [completed_worker] = live["worker_invocations"]
    assert completed_worker["run_id"] == run_id
    assert completed_worker["status"] == "completed"
    assert completed_worker["ended_at"] == "2026-07-18T08:02:00+00:00"
    assert live["worker_invocations_incomplete"] is False


def test_resumed_task_preserves_latest_worker_from_an_older_attempt(
    tmp_path: Path,
) -> None:
    bridge = _project(tmp_path)
    task_id = "task-resumed-history"
    historical_call = {
        "call_index": 4,
        "status": "completed",
        "started_at": "2026-07-18T08:01:00+00:00",
        "finished_at": "2026-07-18T08:02:00+00:00",
        "attempt_run_id": "run-old-attempt",
    }
    _run_with_worker_calls(
        tmp_path,
        run_id="run-old-attempt",
        task_id=task_id,
        manifest_status="completed",
        task_status="completed",
        started_at="2026-07-18T08:00:00+00:00",
        ended_at="2026-07-18T08:03:00+00:00",
        workers=[historical_call],
    )
    current_run_dir = _run_with_worker_calls(
        tmp_path,
        run_id="run-current-attempt",
        task_id=task_id,
        manifest_status="running",
        task_status="running",
        started_at="2026-07-18T09:00:00+00:00",
        ended_at=None,
        # The cumulative current tree retains the old call, but this attempt
        # has not invoked the Worker again.
        workers=[historical_call],
    )
    lease = RuntimeRunLease(current_run_dir)
    lease.acquire()
    try:
        bootstrap = bridge.bootstrap()
        live = bridge.dispatch("runtime.summary", {})
    finally:
        lease.release()

    for projection in (bootstrap, live):
        [worker] = projection["worker_invocations"]
        assert worker["agent_name"] == "helper"
        assert worker["run_id"] == "run-old-attempt"
        assert worker["status"] == "completed"


def test_worker_scoped_to_a_pruned_run_is_not_reassigned_to_current_run(
    tmp_path: Path,
) -> None:
    bridge = _project(tmp_path)
    current_run_dir = _run_with_worker_calls(
        tmp_path,
        run_id="run-retained-current",
        task_id="task-retained-current",
        manifest_status="running",
        task_status="running",
        started_at="2026-07-18T09:00:00+00:00",
        ended_at=None,
        workers=[
            {
                "call_index": 7,
                "status": "running",
                "started_at": "2026-07-18T08:00:00+00:00",
                # Retention already deleted this historical Run manifest.
                "attempt_run_id": "run-pruned-history",
            }
        ],
    )
    lease = RuntimeRunLease(current_run_dir)
    lease.acquire()
    try:
        bootstrap = bridge.bootstrap()
        live = bridge.dispatch("runtime.summary", {})
    finally:
        lease.release()

    for projection in (bootstrap, live):
        assert projection["worker_invocations"] == []
        assert projection["worker_invocations_incomplete"] is True


def test_runtime_summary_reuses_bootstrap_identity_without_parsing_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _project(tmp_path)
    bootstrap = bridge.bootstrap()
    assert bootstrap["runs"] == []
    assert bootstrap["worker_invocations"] == []
    assert bootstrap["schedules"]["items"] == []

    _completed_run(tmp_path)
    _schedule_document(tmp_path)
    (tmp_path / SYSTEM_ID).unlink()
    loop_path = tmp_path / "applications/demo/workflows/loop.yaml"
    (tmp_path / SYSTEM_ID).symlink_to(loop_path.name)
    loop_path.symlink_to(Path(SYSTEM_ID).name)

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("runtime.summary reparsed project configuration")

    monkeypatch.setattr(bridge, "_scan_systems", forbidden)
    monkeypatch.setattr(bridge, "_read_yaml", forbidden)
    monkeypatch.setattr(yaml, "safe_load", forbidden)

    result = bridge.dispatch("runtime.summary", {})

    assert set(result) == {
        "systems",
        "runs",
        "runs_incomplete",
        "removed_runs",
        "worker_invocations",
        "worker_invocations_incomplete",
        "schedules",
    }
    assert result["runs_incomplete"] is False
    assert result["removed_runs"] == []
    assert result["worker_invocations_incomplete"] is False
    assert result["systems"] == [
        {
            "id": SYSTEM_ID,
            "path": SYSTEM_ID,
            "application_id": "demo",
            "name": "demo",
            "description": "Cached Agent identity",
            "state": "completed",
            "validation": {"valid": True, "errors": []},
            "latest_run": result["runs"][0],
        }
    ]
    assert result["runs"] == [
        {
            "run_id": "run-live",
            "system_id": SYSTEM_ID,
            "application_id": "demo",
            "task_id": "task-live",
            "agent_name": "demo",
            "status": "completed",
            "started_at": "2026-07-18T08:00:00+00:00",
            "ended_at": "2026-07-18T08:01:00+00:00",
        }
    ]
    assert result["worker_invocations"] == [
        {
            "run_id": "run-live",
            "system_id": SYSTEM_ID,
            "application_id": "demo",
            "parent_agent_name": "demo",
            "agent_name": "helper",
            "call_index": 2,
            "status": "succeeded",
            "step": 3,
            "started_at": "2026-07-18T08:00:10+00:00",
            "ended_at": "2026-07-18T08:00:20+00:00",
            "error": None,
        }
    ]
    assert result["schedules"] == {
        "items": [
            {
                "id": "job_demo",
                "name": "Daily demo",
                "enabled": False,
                "state": "paused",
                "yaml_path": SYSTEM_ID,
                "trigger": {
                    "kind": "cron",
                    "expression": "0 9 * * *",
                    "timezone": "Asia/Shanghai",
                },
                "next_run_at": None,
                "last_run_at": None,
                "last_status": None,
                "run_count": 0,
                "last_execution": None,
            }
        ],
        "service": {
            "state": "stopped",
            "pid": 123,
            "started_at": "2026-07-18T07:00:00+00:00",
            "last_tick_at": "2026-07-18T07:01:00+00:00",
            "last_success_at": None,
            "last_error": None,
            "job_count": 1,
            "due_count": 0,
            "claimed_count": 0,
            "execution_count": 0,
        },
    }


def test_runtime_summary_returns_fresh_independent_system_objects(tmp_path: Path) -> None:
    bridge = _project(tmp_path)
    bridge.bootstrap()

    first = bridge.dispatch("runtime.summary", {})
    first["systems"][0]["name"] = "mutated by caller"
    first["systems"][0]["validation"]["errors"].append("mutated")

    second = bridge.dispatch("runtime.summary", {})

    assert second["systems"][0]["name"] == "demo"
    assert second["systems"][0]["validation"] == {"valid": True, "errors": []}
