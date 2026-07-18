from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from src.lib.runtime.context import RuntimeRunLease
from src.tui_bridge.bridge import TuiBridge

SYSTEM_ID = "applications/demo/workflows/demo.yaml"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _bridge(tmp_path: Path) -> TuiBridge:
    _write(tmp_path / "config/system.yaml", "runtime:\n  root_dir: .runtime-live\n")
    _write(
        tmp_path / "config/llm.yaml",
        "model:\n  default_model_type: test\n  test:\n    model: openai/test\n",
    )
    _write(
        tmp_path / SYSTEM_ID,
        """\
name: demo
description: Live refresh index fixture
model_type: test
worker_agents: []
workflow: Run the task.
""",
    )
    return TuiBridge(tmp_path)


def _run(
    tmp_path: Path,
    index: int,
    *,
    status: str = "completed",
    with_task: bool = False,
) -> Path:
    run_id = f"run_{index:04d}"
    task_id = f"task_{index:04d}"
    run_dir = tmp_path / ".runtime-live/runs/demo" / run_id
    ended_at = f"2026-07-18T10:{index % 60:02d}:30+00:00" if status != "running" else None
    _write(
        run_dir / "manifest.json",
        json.dumps(
            {
                "application_id": "demo",
                "task_id": task_id,
                "run_id": run_id,
                "agent_name": "demo",
                "yaml_path": SYSTEM_ID,
                "status": status,
                "started_at": f"2026-07-18T10:{index % 60:02d}:00+00:00",
                "ended_at": ended_at,
            }
        ),
    )
    if with_task:
        _write(
            tmp_path / ".runtime-live/checkpoints/demo" / task_id / "task_tree.json",
            json.dumps(
                {
                    "run_id": run_id,
                    "status": status,
                    "agent_name": "demo",
                    "workers": {},
                }
            ),
        )
    return run_dir


def _count_runtime_reads(
    bridge: TuiBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[int], Callable[[], None]]:
    original = bridge._read_json_object_bounded_secure
    original_with_status = bridge._read_json_object_bounded_secure_with_status
    reads: list[str] = []
    budgets: list[int] = []

    def counted(root: Path, relative: Path, max_bytes: int) -> dict[str, Any] | None:
        if relative.name in {"manifest.json", "task_tree.json"}:
            reads.append(relative.as_posix())
            budgets.append(max_bytes)
        return original(root, relative, max_bytes=max_bytes)

    def counted_with_status(
        root: Path,
        relative: Path,
        max_bytes: int,
    ) -> tuple[dict[str, Any] | None, bool]:
        if relative.name in {"manifest.json", "task_tree.json"}:
            reads.append(relative.as_posix())
            budgets.append(max_bytes)
        return original_with_status(root, relative, max_bytes=max_bytes)

    def clear() -> None:
        reads.clear()
        budgets.clear()

    monkeypatch.setattr(bridge, "_read_json_object_bounded_secure", counted)
    monkeypatch.setattr(
        bridge,
        "_read_json_object_bounded_secure_with_status",
        counted_with_status,
    )
    return reads, budgets, clear


def test_unchanged_live_refresh_reuses_bootstrap_index_and_bounds_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _bridge(tmp_path)
    for index in range(300):
        _run(tmp_path, index)

    bootstrap = bridge.bootstrap()
    assert len(bootstrap["runs"]) == 300
    reads, _, _ = _count_runtime_reads(bridge, monkeypatch)

    live = bridge.dispatch("runtime.summary", {})

    assert reads == []
    assert live["runs_incomplete"] is True
    assert len(live["runs"]) <= 256
    assert live["systems"][0]["latest_run"]["run_id"] == "run_0299"


def test_live_refresh_discovers_and_finishes_one_active_run_without_history_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _bridge(tmp_path)
    for index in range(40):
        _run(tmp_path, index)
    bridge.bootstrap()
    reads, _, clear_reads = _count_runtime_reads(bridge, monkeypatch)

    run_dir = _run(tmp_path, 1000, status="running", with_task=True)
    lease = RuntimeRunLease(run_dir)
    lease.acquire()
    try:
        running = bridge.dispatch("runtime.summary", {})
        assert running["systems"][0]["state"] == "running"
        assert running["systems"][0]["latest_run"]["run_id"] == "run_1000"
        assert next(run for run in running["runs"] if run["run_id"] == "run_1000")["status"] == "running"
        assert len(reads) <= 2

        clear_reads()
        _run(tmp_path, 1000, status="completed", with_task=True)
    finally:
        lease.release()

    completed = bridge.dispatch("runtime.summary", {})
    assert completed["systems"][0]["state"] == "completed"
    assert completed["systems"][0]["latest_run"]["run_id"] == "run_1000"
    assert next(run for run in completed["runs"] if run["run_id"] == "run_1000")["status"] == "completed"
    assert len(reads) <= 2

    clear_reads()
    bridge.dispatch("runtime.summary", {})
    assert reads == []


def test_new_run_burst_has_a_per_refresh_manifest_and_task_read_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _bridge(tmp_path)
    bridge.bootstrap()
    reads, budgets, _ = _count_runtime_reads(bridge, monkeypatch)
    for index in range(300):
        _run(tmp_path, index)

    live = bridge.dispatch("runtime.summary", {})

    # One bounded refresh may defer part of a pathological burst, but it must
    # still prioritize the lexically newest timestamped Run and say so.
    assert len(reads) <= 48
    assert sum(budgets) <= 8 * 1024 * 1024
    assert live["runs_incomplete"] is True
    assert live["systems"][0]["latest_run"]["run_id"] == "run_0299"


def test_orphan_run_directories_do_not_starve_a_later_valid_run(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    bridge.bootstrap()
    application_dir = tmp_path / ".runtime-live/runs/demo"
    # These sort ahead of the valid Run, so newest-first selection alone would
    # permanently starve it once the pending queue reaches its bound.
    for index in range(256):
        (application_dir / f"run_z_orphan_{index:04d}").mkdir(parents=True)
    _run(tmp_path, 9000)

    observed = None
    for _ in range(30):
        observed = bridge.dispatch("runtime.summary", {})
        latest = observed["systems"][0]["latest_run"]
        if latest is not None and latest["run_id"] == "run_9000":
            break

    assert observed is not None
    assert observed["systems"][0]["latest_run"]["run_id"] == "run_9000"


def test_run_directory_remains_pending_when_manifest_arrives_after_directory_cursor(
    tmp_path: Path,
) -> None:
    bridge = _bridge(tmp_path)
    bridge.bootstrap()
    run_dir = tmp_path / ".runtime-live/runs/demo/run_2000"
    run_dir.mkdir(parents=True)

    before_manifest = bridge.dispatch("runtime.summary", {})
    assert before_manifest["runs"] == []
    assert before_manifest["runs_incomplete"] is True

    _run(tmp_path, 2000)
    after_manifest = bridge.dispatch("runtime.summary", {})

    assert after_manifest["systems"][0]["latest_run"]["run_id"] == "run_2000"
    assert after_manifest["runs"][0]["run_id"] == "run_2000"


def test_live_discovery_uses_the_exact_nested_application_directory(tmp_path: Path) -> None:
    bridge = _bridge(tmp_path)
    nested_system_id = "applications/org/demo/workflows/nested.yaml"
    _write(
        tmp_path / nested_system_id,
        """\
name: nested
description: Nested application fixture
model_type: test
worker_agents: []
workflow: Run nested task.
""",
    )
    bootstrap = bridge.bootstrap()
    assert {system["application_id"] for system in bootstrap["systems"]} == {"demo", "org/demo"}

    run_dir = tmp_path / ".runtime-live/runs/org/demo/run_3000"
    _write(
        run_dir / "manifest.json",
        json.dumps(
            {
                "application_id": "org/demo",
                "task_id": "task_3000",
                "run_id": "run_3000",
                "agent_name": "nested",
                "yaml_path": nested_system_id,
                "status": "completed",
                "started_at": "2026-07-18T12:00:00+00:00",
                "ended_at": "2026-07-18T12:01:00+00:00",
            }
        ),
    )

    live = bridge.dispatch("runtime.summary", {})

    nested = next(system for system in live["systems"] if system["id"] == nested_system_id)
    assert nested["state"] == "completed"
    assert nested["latest_run"]["run_id"] == "run_3000"


def test_stable_directory_reconciliation_removes_deleted_run_and_worker_truth(
    tmp_path: Path,
) -> None:
    bridge = _bridge(tmp_path)
    run_dir = _run(tmp_path, 4000, with_task=True)
    _write(
        tmp_path / ".runtime-live/checkpoints/demo/task_4000/task_tree.json",
        json.dumps(
            {
                "run_id": "run_4000",
                "status": "completed",
                "agent_name": "demo",
                "workers": {
                    "helper": [
                        {
                            "call_index": 1,
                            "status": "completed",
                            "attempt_run_id": "run_4000",
                        }
                    ]
                },
            }
        ),
    )
    bootstrap = bridge.bootstrap()
    assert bootstrap["runs"][0]["run_id"] == "run_4000"
    assert bootstrap["worker_invocations"][0]["run_id"] == "run_4000"

    shutil.rmtree(run_dir)
    live = bridge.dispatch("runtime.summary", {})

    assert live["runs"] == []
    assert live["runs_incomplete"] is False
    assert live["removed_runs"] == [{"application_id": "demo", "run_id": "run_4000"}]
    assert live["systems"][0]["state"] == "never_run"
    assert live["systems"][0]["latest_run"] is None
    assert live["worker_invocations"] == []


def test_missing_application_run_directory_is_a_complete_empty_snapshot(
    tmp_path: Path,
) -> None:
    bridge = _bridge(tmp_path)
    run_dir = _run(tmp_path, 4100)
    bridge.bootstrap()

    shutil.rmtree(run_dir.parent)
    live = bridge.dispatch("runtime.summary", {})

    assert live["runs"] == []
    assert live["runs_incomplete"] is False
    assert live["removed_runs"] == [{"application_id": "demo", "run_id": "run_4100"}]
    assert live["systems"][0]["state"] == "never_run"


@pytest.mark.parametrize("replacement", ["symlink", "file"])
def test_unsafe_application_run_directory_does_not_delete_cached_truth(
    tmp_path: Path,
    replacement: str,
) -> None:
    bridge = _bridge(tmp_path)
    run_dir = _run(tmp_path, 4200)
    bridge.bootstrap()
    application_dir = run_dir.parent
    shutil.rmtree(application_dir)
    if replacement == "symlink":
        external = tmp_path / "external-runs"
        external.mkdir()
        application_dir.symlink_to(external, target_is_directory=True)
    else:
        application_dir.write_text("not a directory", encoding="utf-8")

    live = bridge.dispatch("runtime.summary", {})

    assert live["runs_incomplete"] is True
    assert live["removed_runs"] == []
    assert live["runs"][0]["run_id"] == "run_4200"
    assert live["systems"][0]["state"] == "completed"
