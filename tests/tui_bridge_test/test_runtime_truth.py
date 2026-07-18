from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import src.lib.runtime.context as runtime_context_module
import src.tui_bridge.bridge as bridge_module
from src.lib.runtime.context import RuntimeRunLease
from src.tui_bridge.bridge import BridgeError, TuiBridge


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _workflow(project_root: Path, application_id: str = "reports") -> Path:
    return _write(
        project_root / f"applications/{application_id}/workflows/report.yaml",
        "name: report\ndescription: report agent\nworkflow: write a report\n",
    )


def _run(
    project_root: Path,
    *,
    application_id: str = "reports",
    run_id: str = "run-1",
    task_id: str = "task-1",
    status: str,
    started_at: str = "2026-07-17T10:00:00+00:00",
    manifest_extra: dict[str, Any] | None = None,
) -> Path:
    workflow = _workflow(project_root, application_id)
    run_dir = project_root / f".agentloom/runs/{application_id}/{run_id}"
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "application_id": application_id,
        "task_id": task_id,
        "run_id": run_id,
        "yaml_path": str(workflow),
        "agent_name": "report",
        "status": status,
        "started_at": started_at,
    }
    manifest.update(manifest_extra or {})
    _write(run_dir / "manifest.json", json.dumps(manifest))
    return run_dir


def _events(project_root: Path, application_id: str, task_id: str, events: list[dict[str, Any]]) -> None:
    _write(
        project_root / f".agentloom/checkpoints/{application_id}/{task_id}/task_events.jsonl",
        "".join(json.dumps(event) + "\n" for event in events),
    )


def test_run_scan_does_not_suppress_unrelated_process_logs(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    _run(tmp_path, status="completed")
    _write(
        tmp_path / ".agentloom/checkpoints/reports/task-1/task_tree.json",
        json.dumps({"task_id": "task-1", "run_id": "run-1", "status": "completed", "workers": {}}),
    )
    audit_logger = logging.getLogger("agentloom.test.builder-audit")
    original_projection = TuiBridge._task_projection

    def projection_with_concurrent_audit(bridge: TuiBridge, *args: Any, **kwargs: Any) -> dict[str, Any] | None:
        audit_logger.warning("builder tool audit remains visible")
        return original_projection(bridge, *args, **kwargs)

    monkeypatch.setattr(TuiBridge, "_task_projection", projection_with_concurrent_audit)

    with caplog.at_level(logging.WARNING, logger=audit_logger.name):
        TuiBridge(tmp_path)._scan_runs([])

    assert "builder tool audit remains visible" in caplog.messages


def test_bounded_json_read_distinguishes_missing_from_invalid_source(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()

    assert TuiBridge._read_json_object_bounded_secure_with_status(
        runtime_root,
        Path("missing.json"),
        max_bytes=128,
    ) == (None, False)

    _write(runtime_root / "invalid.json", "{not-json")
    assert TuiBridge._read_json_object_bounded_secure_with_status(
        runtime_root,
        Path("invalid.json"),
        max_bytes=128,
    ) == (None, True)


def test_run_scan_never_replays_the_unbounded_checkpoint_event_catalog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _run(tmp_path, status="completed")
    _write(
        tmp_path / ".agentloom/checkpoints/reports/task-1/task_tree.json",
        json.dumps({"task_id": "task-1", "run_id": "run-1", "status": "completed", "workers": {}}),
    )
    _events(
        tmp_path,
        "reports",
        "task-1",
        [{"type": "run_started", "run_id": "run-1"}],
    )

    def fail_if_events_are_replayed(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("TUI status scan must not replay cumulative task events")

    monkeypatch.setattr(
        "src.lib.checkpoint.checkpoint_manager.CheckpointManager._read_task_events_from_path",
        fail_if_events_are_replayed,
    )

    bootstrap = TuiBridge(tmp_path).bootstrap()

    assert bootstrap["runs"][0]["status"] == "completed"


def test_catalog_preserves_same_id_unlinked_runs_from_different_applications(
    tmp_path: Path,
) -> None:
    for application_id in ("alpha", "beta"):
        _run(
            tmp_path,
            application_id=application_id,
            run_id="shared-run",
            task_id=f"task-{application_id}",
            status="completed",
            manifest_extra={"yaml_path": "missing/workflow.yaml"},
        )
        _write(
            tmp_path / f"applications/{application_id}/workflows/second.yaml",
            "name: second\ndescription: second candidate\nworkflow: answer\n",
        )

    bridge = TuiBridge(tmp_path)
    bootstrap = bridge.bootstrap()

    assert {(run["application_id"], run["run_id"], run["system_id"]) for run in bootstrap["runs"]} == {
        ("alpha", "shared-run", None),
        ("beta", "shared-run", None),
    }
    for application_id in ("alpha", "beta"):
        detail = bridge.dispatch(
            "run.detail",
            {"application_id": application_id, "run_id": "shared-run"},
        )
        assert detail["summary"]["application_id"] == application_id


def test_run_detail_directly_addresses_one_run_without_scanning_the_catalog(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _run(tmp_path, status="completed")
    bridge = TuiBridge(tmp_path)

    def fail_if_snapshot_is_scanned() -> None:
        raise AssertionError("run.detail must not scan unrelated systems or runs")

    monkeypatch.setattr(bridge, "_snapshot", fail_if_snapshot_is_scanned)

    detail = bridge.dispatch(
        "run.detail",
        {"run_id": "run-1", "application_id": "reports"},
    )

    assert detail["summary"]["run_id"] == "run-1"


def test_system_detail_directly_addresses_one_definition_without_full_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workflow = _workflow(tmp_path)
    bridge = TuiBridge(tmp_path)

    def fail_if_snapshot_is_scanned() -> None:
        raise AssertionError("system.detail must not rescan the project catalog")

    monkeypatch.setattr(bridge, "_snapshot", fail_if_snapshot_is_scanned)

    detail = bridge.system_detail(workflow.relative_to(tmp_path).as_posix())

    assert detail["definition"]["name"] == "report"


def test_run_detail_rejects_an_oversized_manifest_without_parsing_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(bridge_module, "RUN_MANIFEST_MAX_BYTES", 128, raising=False)
    run_dir = _run(tmp_path, status="completed")
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["padding"] = "x" * 512
    _write(manifest_path, json.dumps(manifest))

    with pytest.raises(BridgeError, match="run not found") as error:
        TuiBridge(tmp_path).dispatch(
            "run.detail",
            {"run_id": "run-1", "application_id": "reports"},
        )

    assert error.value.code == "not_found"


def test_failed_run_does_not_expose_worker_result_as_final_result(tmp_path: Path) -> None:
    _run(tmp_path, status="failed")
    _events(
        tmp_path,
        "reports",
        "task-1",
        [
            {"type": "run_started", "run_id": "run-1"},
            {
                "type": "worker_call_finished",
                "agent_name": "researcher",
                "call_index": 0,
                "status": "completed",
                "result": "intermediate evidence",
            },
            {"type": "task_status_changed", "status": "failed", "error": "boom"},
        ],
    )

    detail = TuiBridge(tmp_path).dispatch("run.detail", {"run_id": "run-1", "application_id": "reports"})

    assert detail["summary"]["status"] == "failed"
    assert detail["result_state"] == "unavailable"
    assert detail["result"] is None


def test_failed_run_detail_exposes_the_manifest_failure_reason(tmp_path: Path) -> None:
    _run(
        tmp_path,
        status="failed",
        manifest_extra={"error": "provider timed out after 30 seconds"},
    )

    detail = TuiBridge(tmp_path).dispatch("run.detail", {"run_id": "run-1", "application_id": "reports"})

    assert detail["error"] == "provider timed out after 30 seconds"


def test_nonterminal_task_tree_result_is_not_a_final_result(tmp_path: Path) -> None:
    _run(tmp_path, status="completed")
    _events(
        tmp_path,
        "reports",
        "task-1",
        [
            {"type": "run_started", "run_id": "run-1"},
            {
                "type": "task_tree_replaced",
                "tree": {
                    "run_id": "run-1",
                    "status": "running",
                    "result": "result retained while a resume is still active",
                },
            },
        ],
    )

    detail = TuiBridge(tmp_path).dispatch("run.detail", {"run_id": "run-1", "application_id": "reports"})

    assert detail["result_state"] == "unavailable"
    assert detail["result"] is None


def test_only_latest_matching_task_tree_can_supply_final_result(tmp_path: Path) -> None:
    _run(tmp_path, status="completed")
    _events(
        tmp_path,
        "reports",
        "task-1",
        [
            {"type": "run_started", "run_id": "run-1"},
            {
                "type": "task_tree_replaced",
                "tree": {
                    "run_id": "run-1",
                    "status": "completed",
                    "result": "obsolete result",
                },
            },
            {
                "type": "task_tree_replaced",
                "tree": {
                    "run_id": "run-1",
                    "status": "running",
                    "result": "retained during resume",
                },
            },
        ],
    )

    detail = TuiBridge(tmp_path).dispatch("run.detail", {"run_id": "run-1", "application_id": "reports"})

    assert detail["result_state"] == "unavailable"
    assert detail["result"] is None


def test_matching_completed_task_tree_supplies_final_result(tmp_path: Path) -> None:
    _run(tmp_path, status="completed")
    _events(
        tmp_path,
        "reports",
        "task-1",
        [
            {"type": "run_started", "run_id": "run-1"},
            {
                "type": "worker_call_finished",
                "agent_name": "researcher",
                "call_index": 0,
                "status": "completed",
                "result": "intermediate evidence",
            },
            {
                "type": "task_tree_replaced",
                "tree": {
                    "run_id": "run-1",
                    "status": "completed",
                    "result": {"answer": "final report"},
                },
            },
        ],
    )

    detail = TuiBridge(tmp_path).dispatch("run.detail", {"run_id": "run-1", "application_id": "reports"})

    assert detail["result_state"] == "available"
    assert detail["result"] == '{"answer": "final report"}'


def test_run_detail_projects_cumulative_task_events_to_the_selected_run(tmp_path: Path) -> None:
    _run(
        tmp_path,
        run_id="run-1",
        task_id="task-shared",
        status="completed",
        started_at="2026-07-17T10:00:00+00:00",
    )
    _run(
        tmp_path,
        run_id="run-2",
        task_id="task-shared",
        status="completed",
        started_at="2026-07-17T11:00:00+00:00",
    )
    events = [
        {"type": "task_created", "task_id": "task-shared"},
        {"type": "run_started", "run_id": "run-1"},
        {"type": "worker_call_started", "agent_name": "first", "call_index": 0},
        {"type": "task_status_changed", "status": "completed", "result": "first result"},
        {"type": "run_resumed", "run_id": "run-2"},
        {"type": "worker_call_started", "agent_name": "second", "call_index": 0},
        {"type": "task_status_changed", "status": "completed", "result": "second result"},
    ]
    _events(tmp_path, "reports", "task-shared", events)

    first = TuiBridge(tmp_path).dispatch("run.detail", {"run_id": "run-1", "application_id": "reports"})
    second = TuiBridge(tmp_path).dispatch("run.detail", {"run_id": "run-2", "application_id": "reports"})

    assert first["events"] == events[:4]
    assert [worker["agent_name"] for worker in first["workers"]] == ["first"]
    assert first["result"] == "first result"
    assert second["events"] == events[4:]
    assert [worker["agent_name"] for worker in second["workers"]] == ["second"]
    assert second["result"] == "second result"


def test_resumed_run_never_inherits_unmarked_legacy_worker_or_result_events(tmp_path: Path) -> None:
    _run(tmp_path, run_id="run-2", task_id="task-shared", status="completed")
    events = [
        {"type": "worker_call_finished", "agent_name": "old", "call_index": 0},
        {"type": "task_status_changed", "status": "completed", "result": "old result"},
        {"type": "run_resumed", "run_id": "run-2"},
        {"type": "worker_call_started", "agent_name": "new", "call_index": 0},
        {"type": "task_status_changed", "status": "completed", "result": "new result"},
    ]
    _events(tmp_path, "reports", "task-shared", events)

    detail = TuiBridge(tmp_path).dispatch("run.detail", {"run_id": "run-2", "application_id": "reports"})

    assert detail["events"] == events[2:]
    assert [worker["agent_name"] for worker in detail["workers"]] == ["new"]
    assert detail["result"] == "new result"


def test_run_detail_projects_resume_claim_as_running_worker_for_selected_run(
    tmp_path: Path,
) -> None:
    _run(
        tmp_path,
        run_id="run-2",
        task_id="task-shared",
        status="completed",
        started_at="2026-07-17T11:00:00+00:00",
    )
    _events(
        tmp_path,
        "reports",
        "task-shared",
        [
            {"type": "run_started", "run_id": "run-1"},
            {
                "type": "worker_call_started",
                "agent_name": "old-worker",
                "call_index": 0,
                "started_at": "2026-07-17T10:00:10+00:00",
            },
            {"type": "run_resumed", "run_id": "run-2"},
            {
                "type": "worker_call_resume_claimed",
                "agent_name": "researcher",
                "call_index": 3,
                "run_id": "run-2",
                "claimed_at": "2026-07-17T11:00:10+00:00",
            },
        ],
    )

    detail = TuiBridge(tmp_path).dispatch("run.detail", {"run_id": "run-2", "application_id": "reports"})

    assert detail["workers"] == [
        {
            "agent_name": "researcher",
            "call_index": 3,
            "status": "running",
            "step": None,
            "started_at": "2026-07-17T11:00:10+00:00",
            "ended_at": None,
            "error": None,
        }
    ]


def test_run_detail_projects_cached_claim_as_cached_worker_for_selected_run(
    tmp_path: Path,
) -> None:
    _run(
        tmp_path,
        run_id="run-2",
        task_id="task-shared",
        status="completed",
        started_at="2026-07-17T11:00:00+00:00",
    )
    _events(
        tmp_path,
        "reports",
        "task-shared",
        [
            {"type": "run_started", "run_id": "run-1"},
            {
                "type": "worker_call_cached_result_claimed",
                "agent_name": "old-worker",
                "call_index": 0,
                "run_id": "run-1",
                "claimed_at": "2026-07-17T10:00:10+00:00",
            },
            {"type": "run_resumed", "run_id": "run-2"},
            {
                "type": "worker_call_cached_result_claimed",
                "agent_name": "researcher",
                "call_index": 2,
                "run_id": "run-2",
                "claimed_at": "2026-07-17T11:00:10+00:00",
            },
        ],
    )

    detail = TuiBridge(tmp_path).dispatch("run.detail", {"run_id": "run-2", "application_id": "reports"})

    assert detail["workers"] == [
        {
            "agent_name": "researcher",
            "call_index": 2,
            "status": "cached",
            "step": None,
            "started_at": None,
            "ended_at": "2026-07-17T11:00:10+00:00",
            "error": None,
        }
    ]


def test_held_run_lease_is_running_even_with_stale_heartbeat(tmp_path: Path) -> None:
    run_dir = _run(tmp_path, status="running")
    checkpoint_dir = tmp_path / ".agentloom/checkpoints/reports/task-1"
    _write(
        checkpoint_dir / "task_tree.json",
        json.dumps(
            {
                "task_id": "task-1",
                "run_id": "run-1",
                "agent_name": "report",
                "status": "running",
                "created_at": "2026-07-17T10:00:00+00:00",
                "workers": {},
            }
        ),
    )
    _write(
        checkpoint_dir / "heartbeat.json",
        json.dumps(
            {
                "pid": os.getpid(),
                "timestamp": time.time() - 3600,
                "status": "running",
                "agent_name": "report",
                "run_id": "run-1",
            }
        ),
    )
    lease = RuntimeRunLease(run_dir)
    lease.acquire()
    try:
        bootstrap = TuiBridge(tmp_path).bootstrap()
    finally:
        lease.release()

    run = next(item for item in bootstrap["runs"] if item["run_id"] == "run-1")
    assert run["status"] == "running"


def test_released_run_lease_is_crashed_even_with_live_pid_and_fresh_heartbeat(
    tmp_path: Path,
) -> None:
    _run(tmp_path, status="running")
    checkpoint_dir = tmp_path / ".agentloom/checkpoints/reports/task-1"
    _write(
        checkpoint_dir / "task_tree.json",
        json.dumps(
            {
                "task_id": "task-1",
                "run_id": "run-1",
                "agent_name": "report",
                "status": "running",
                "created_at": "2026-07-17T10:00:00+00:00",
                "workers": {},
            }
        ),
    )
    _write(
        checkpoint_dir / "heartbeat.json",
        json.dumps(
            {
                "pid": os.getpid(),
                "timestamp": time.time(),
                "status": "running",
                "agent_name": "report",
                "run_id": "run-1",
            }
        ),
    )

    bootstrap = TuiBridge(tmp_path).bootstrap()

    run = next(item for item in bootstrap["runs"] if item["run_id"] == "run-1")
    assert run["status"] == "crashed"


def test_concurrent_read_only_scans_do_not_make_an_orphaned_run_look_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _run(tmp_path, status="running")
    first_probe_acquired = threading.Event()
    release_first_probe = threading.Event()
    real_flock = runtime_context_module.fcntl.flock
    probe_count = 0
    probe_count_lock = threading.Lock()

    def overlap_first_probe(fd: int, operation: int) -> None:
        nonlocal probe_count
        real_flock(fd, operation)
        if not operation & runtime_context_module.fcntl.LOCK_NB:
            return
        if not operation & (runtime_context_module.fcntl.LOCK_SH | runtime_context_module.fcntl.LOCK_EX):
            return
        with probe_count_lock:
            probe_count += 1
            is_first_probe = probe_count == 1
        if is_first_probe:
            first_probe_acquired.set()
            assert release_first_probe.wait(timeout=2)

    monkeypatch.setattr(runtime_context_module.fcntl, "flock", overlap_first_probe)
    first_result: list[dict[str, Any]] = []

    def first_scan() -> None:
        first_result.append(TuiBridge(tmp_path).bootstrap())

    first_thread = threading.Thread(target=first_scan)
    first_thread.start()
    assert first_probe_acquired.wait(timeout=1)
    second_result = TuiBridge(tmp_path).bootstrap()
    release_first_probe.set()
    first_thread.join(timeout=2)

    assert first_thread.is_alive() is False
    assert [item["status"] for item in first_result[0]["runs"]] == ["crashed"]
    assert [item["status"] for item in second_result["runs"]] == ["crashed"]


def test_released_run_lease_uses_terminal_task_state(tmp_path: Path) -> None:
    expected = {
        "task-completed": "completed",
        "task-failed": "failed",
        "task-crashed": "crashed",
    }
    for task_id, terminal_status in expected.items():
        application_id = terminal_status
        run_id = f"run-{terminal_status}"
        _run(
            tmp_path,
            application_id=application_id,
            run_id=run_id,
            task_id=task_id,
            status="running",
        )
        _write(
            tmp_path / f".agentloom/checkpoints/{application_id}/{task_id}/task_tree.json",
            json.dumps(
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "agent_name": "report",
                    "status": terminal_status,
                    "created_at": "2026-07-17T10:00:00+00:00",
                    "workers": {},
                }
            ),
        )

    bootstrap = TuiBridge(tmp_path).bootstrap()

    statuses = {run["task_id"]: run["status"] for run in bootstrap["runs"]}
    assert statuses == expected


def test_external_runtime_uses_stable_run_relative_log_and_artifact_paths(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    runtime_root = tmp_path / "runtime"
    workflow = _workflow(project_root)
    _write(
        project_root / "config/system.yaml",
        f"runtime:\n  root_dir: {runtime_root}\n",
    )
    run_dir = runtime_root / "runs/reports/run-1"
    _write(
        run_dir / "manifest.json",
        json.dumps(
            {
                "schema_version": 1,
                "application_id": "reports",
                "task_id": "task-1",
                "run_id": "run-1",
                "yaml_path": str(workflow),
                "agent_name": "report",
                "status": "completed",
                "started_at": "2026-07-17T10:00:00+00:00",
            }
        ),
    )
    log = _write(run_dir / "logs/runtime.log", "done\n")
    artifact = _write(run_dir / "artifacts/report.txt", "report\n")

    detail = TuiBridge(project_root).dispatch("run.detail", {"run_id": "run-1", "application_id": "reports"})

    assert detail["logs"] == [
        {
            "path": "logs/runtime.log",
            "size": log.stat().st_size,
            "tail": "done\n",
            "tail_truncated": False,
        }
    ]
    assert detail["artifacts"] == [{"path": "artifacts/report.txt", "size": artifact.stat().st_size}]


def test_run_detail_enforces_explicit_event_log_artifact_and_result_budgets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(bridge_module, "RUN_DETAIL_EVENT_SCAN_MAX_BYTES", 4_096, raising=False)
    monkeypatch.setattr(bridge_module, "RUN_DETAIL_EVENT_MAX_COUNT", 3, raising=False)
    monkeypatch.setattr(bridge_module, "RUN_DETAIL_EVENT_MAX_BYTES", 512, raising=False)
    monkeypatch.setattr(bridge_module, "RUN_DETAIL_LOG_MAX_FILES", 2, raising=False)
    monkeypatch.setattr(bridge_module, "RUN_DETAIL_LOG_MAX_TOTAL_BYTES", 10, raising=False)
    monkeypatch.setattr(bridge_module, "RUN_DETAIL_LOG_MAX_BYTES_PER_FILE", 8, raising=False)
    monkeypatch.setattr(bridge_module, "RUN_DETAIL_ARTIFACT_MAX_FILES", 2, raising=False)
    monkeypatch.setattr(bridge_module, "RUN_DETAIL_FILE_SCAN_MAX_ENTRIES", 20, raising=False)
    monkeypatch.setattr(bridge_module, "RUN_DETAIL_RESULT_MAX_BYTES", 16, raising=False)

    run_dir = _run(
        tmp_path,
        status="completed",
        manifest_extra={
            "result_artifact": "artifacts/result.txt",
            "task_events_artifact": "audit/task_events.jsonl",
        },
    )
    events = [
        {"type": "run_started", "run_id": "run-1"},
        *[
            {
                "type": "worker_call_finished",
                "agent_name": f"worker-{index}",
                "call_index": index,
                "status": "completed",
            }
            for index in range(8)
        ],
        {"type": "task_status_changed", "status": "completed", "result": "event result"},
    ]
    _write(
        run_dir / "audit/task_events.jsonl",
        "".join(json.dumps(event) + "\n" for event in events),
    )
    _write(run_dir / "artifacts/result.txt", "R" * 64)
    for index in range(5):
        _write(run_dir / f"logs/worker-{index}.log", f"log-{index}-" * 8)
        _write(run_dir / f"artifacts/report-{index}.txt", f"artifact-{index}")

    detail = TuiBridge(tmp_path).dispatch("run.detail", {"run_id": "run-1", "application_id": "reports"})

    assert len(detail["events"]) <= 3
    assert (
        sum(len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()) for event in detail["events"])
        <= 512
    )
    assert len(detail["logs"]) <= 2
    assert sum(len(log["tail"].encode()) for log in detail["logs"]) <= 10
    assert all("tail_truncated" in log for log in detail["logs"])
    assert len(detail["artifacts"]) <= 2
    assert detail["result"] == "R" * 16
    assert detail["limits"] == {
        "workers": {
            "truncated": True,
            "returned_count": len(detail["workers"]),
            "max_count": bridge_module.RUN_DETAIL_WORKER_MAX_COUNT,
        },
        "events": {
            "truncated": True,
            "source_incomplete": False,
            "returned_count": len(detail["events"]),
            "returned_bytes": sum(
                len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()) for event in detail["events"]
            ),
            "max_count": 3,
            "max_bytes": 512,
            "max_scan_bytes": 4_096,
        },
        "logs": {
            "truncated": True,
            "returned_count": len(detail["logs"]),
            "returned_bytes": sum(len(log["tail"].encode()) for log in detail["logs"]),
            "max_count": 2,
            "max_bytes": 10,
            "max_bytes_per_file": 8,
            "max_scanned_entries": 20,
        },
        "artifacts": {
            "truncated": True,
            "returned_count": len(detail["artifacts"]),
            "max_count": 2,
            "max_scanned_entries": 20,
        },
        "result": {
            "truncated": True,
            "source_incomplete": False,
            "returned_bytes": 16,
            "max_bytes": 16,
        },
    }


def test_truncated_legacy_event_source_never_claims_that_no_result_was_saved(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(bridge_module, "RUN_DETAIL_EVENT_SCAN_MAX_BYTES", 128, raising=False)
    _run(tmp_path, status="completed")
    _events(
        tmp_path,
        "reports",
        "task-1",
        [
            {"type": "run_started", "run_id": "run-1"},
            {
                "type": "task_status_changed",
                "status": "completed",
                "result": "R" * 512,
            },
        ],
    )

    detail = TuiBridge(tmp_path).dispatch(
        "run.detail",
        {"run_id": "run-1", "application_id": "reports"},
    )

    assert detail["result_state"] == "unavailable"
    assert detail["result"] is None
    assert detail["limits"]["events"]["source_incomplete"] is True
    assert detail["limits"]["result"] == {
        "truncated": True,
        "source_incomplete": True,
        "returned_bytes": 0,
        "max_bytes": bridge_module.RUN_DETAIL_RESULT_MAX_BYTES,
    }


def test_bounded_cumulative_events_never_attribute_a_newer_resume_tail_to_an_old_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(bridge_module, "RUN_DETAIL_EVENT_SCAN_MAX_BYTES", 256, raising=False)
    monkeypatch.setattr(bridge_module, "RUN_DETAIL_EVENT_MAX_COUNT", 4, raising=False)
    monkeypatch.setattr(bridge_module, "RUN_DETAIL_EVENT_MAX_BYTES", 256, raising=False)

    _run(tmp_path, run_id="run-1", task_id="task-shared", status="completed")
    _run(
        tmp_path,
        run_id="run-2",
        task_id="task-shared",
        status="completed",
        started_at="2026-07-17T11:00:00+00:00",
    )
    cumulative = [
        {"type": "run_started", "run_id": "run-1"},
        {"type": "worker_call_finished", "agent_name": "old", "call_index": 0},
        {"type": "run_resumed", "run_id": "run-2"},
        *[
            {
                "type": "worker_call_finished",
                "agent_name": f"new-{index}",
                "call_index": index,
                "status": "completed",
            }
            for index in range(12)
        ],
    ]
    _events(tmp_path, "reports", "task-shared", cumulative)
    _write(
        tmp_path / ".agentloom/checkpoints/reports/task-shared/task_tree.json",
        json.dumps(
            {
                "task_id": "task-shared",
                "run_id": "run-2",
                "status": "completed",
                "workers": {},
            }
        ),
    )

    old_detail = TuiBridge(tmp_path).dispatch("run.detail", {"run_id": "run-1", "application_id": "reports"})
    latest_detail = TuiBridge(tmp_path).dispatch("run.detail", {"run_id": "run-2", "application_id": "reports"})

    assert old_detail["events"] == []
    assert old_detail["workers"] == []
    assert old_detail["limits"]["events"]["truncated"] is True
    assert latest_detail["events"]
    assert len(latest_detail["events"]) <= 4
    assert all(event.get("agent_name", "").startswith("new-") for event in latest_detail["events"])
    assert latest_detail["limits"]["events"]["truncated"] is True


def test_system_state_is_running_when_any_linked_run_is_active(tmp_path: Path) -> None:
    active_run_dir = _run(
        tmp_path,
        run_id="run-active",
        task_id="task-active",
        status="running",
        started_at="2026-07-17T10:00:00+00:00",
    )
    _run(
        tmp_path,
        run_id="run-latest",
        task_id="task-latest",
        status="completed",
        started_at="2026-07-17T11:00:00+00:00",
    )
    lease = RuntimeRunLease(active_run_dir)
    bridge = TuiBridge(tmp_path)
    lease.acquire()
    try:
        bootstrap = bridge.bootstrap()
        system_id = bootstrap["systems"][0]["id"]
        detail = bridge.system_detail(system_id)
    finally:
        lease.release()

    [system] = bootstrap["systems"]
    assert system["state"] == "running"
    assert system["latest_run"]["run_id"] == "run-latest"
    assert system["latest_run"]["status"] == "completed"
    assert detail["summary"]["state"] == system["state"]
    assert detail["summary"]["latest_run"] == system["latest_run"]


def test_system_catalog_rejects_symlinked_yaml_files(tmp_path: Path) -> None:
    external = _write(
        tmp_path / "outside/secret.yaml",
        "name: secret\ndescription: outside project\nworkflow: exfiltrate\n",
    )
    linked = tmp_path / "project/applications/linked/workflows/linked.yaml"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(external)

    bootstrap = TuiBridge(tmp_path / "project").bootstrap()

    assert bootstrap["systems"] == []


def test_system_catalog_rejects_symlinked_applications_root(tmp_path: Path) -> None:
    external_root = tmp_path / "outside/applications"
    _write(
        external_root / "secret/workflows/secret.yaml",
        "name: secret\ndescription: outside project\nworkflow: exfiltrate\n",
    )
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "applications").symlink_to(external_root, target_is_directory=True)

    bootstrap = TuiBridge(project_root).bootstrap()

    assert bootstrap["systems"] == []
