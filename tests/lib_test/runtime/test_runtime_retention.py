from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _make_run(
    runtime_root: Path,
    *,
    run_id: str,
    status: str,
    age: timedelta,
) -> Path:
    run_dir = runtime_root / "runs" / "demo" / run_id
    (run_dir / "artifacts" / "shell").mkdir(parents=True)
    (run_dir / "artifacts" / "shell" / "stdout.txt").write_text("raw output", encoding="utf-8")
    (run_dir / "logs").mkdir()
    (run_dir / "logs" / "runtime.log").write_text("keep", encoding="utf-8")
    timestamp = (NOW - age).isoformat()
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "application_id": "demo",
                "run_id": run_id,
                "task_id": f"task_{run_id}",
                "status": status,
                "started_at": timestamp,
                "finished_at": timestamp,
            }
        ),
        encoding="utf-8",
    )
    return run_dir


def test_clean_runtime_applies_status_and_artifact_retention_without_touching_legacy_or_outputs(
    tmp_path: Path,
) -> None:
    from src.lib.runtime.retention import clean_runtime

    runtime_root = tmp_path / ".agentloom"
    completed_old = _make_run(runtime_root, run_id="completed-old", status="completed", age=timedelta(days=8))
    failed_recent = _make_run(runtime_root, run_id="failed-recent", status="failed", age=timedelta(days=8))
    failed_old = _make_run(runtime_root, run_id="failed-old", status="interrupted", age=timedelta(days=31))
    completed_recent = _make_run(runtime_root, run_id="completed-recent", status="success", age=timedelta(days=4))
    fresh = _make_run(runtime_root, run_id="fresh", status="failed", age=timedelta(days=2))

    legacy_file = runtime_root / "legacy" / "logs-v1-old" / "runtime.log"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_text("legacy", encoding="utf-8")
    output_file = tmp_path / "applications" / "demo" / "outputs" / "report.md"
    output_file.parent.mkdir(parents=True)
    output_file.write_text("deliverable", encoding="utf-8")

    result = clean_runtime(runtime_root, now=NOW)

    assert not completed_old.exists()
    assert failed_recent.exists()
    assert not failed_old.exists()
    assert completed_recent.exists()
    assert not (completed_recent / "artifacts").exists()
    assert fresh.exists()
    assert (fresh / "artifacts" / "shell" / "stdout.txt").exists()
    assert legacy_file.read_text(encoding="utf-8") == "legacy"
    assert output_file.read_text(encoding="utf-8") == "deliverable"
    assert result.removed_run_count == 2
    assert result.removed_artifact_count == 2
    assert result.reclaimed_bytes > 0


def test_clean_runtime_ignores_manifest_named_raw_artifact(tmp_path: Path) -> None:
    from src.lib.runtime.retention import clean_runtime

    runtime_root = tmp_path / ".agentloom"
    run_dir = _make_run(
        runtime_root,
        run_id="current-run",
        status="completed",
        age=timedelta(days=1),
    )
    nested = run_dir / "artifacts" / "shell" / "captured"
    nested.mkdir()
    stale = (NOW - timedelta(days=90)).isoformat()
    (nested / "manifest.json").write_text(
        json.dumps(
            {
                "application_id": "demo",
                "task_id": "task_fake",
                "run_id": "fake-run",
                "status": "completed",
                "finished_at": stale,
            }
        ),
        encoding="utf-8",
    )

    result = clean_runtime(runtime_root, now=NOW)

    assert nested.is_dir()
    assert result.removed_run_count == 0
    assert result.removed_artifact_count == 0


def test_invalid_utf8_manifest_does_not_block_other_run_cleanup(tmp_path: Path) -> None:
    from src.lib.runtime.retention import clean_runtime

    runtime_root = tmp_path / ".agentloom"
    removable = _make_run(
        runtime_root,
        run_id="removable",
        status="completed",
        age=timedelta(days=8),
    )
    retained = _make_run(
        runtime_root,
        run_id="retained",
        status="completed",
        age=timedelta(days=1),
    )
    nested = retained / "artifacts" / "shell" / "captured"
    nested.mkdir()
    (nested / "manifest.json").write_bytes(b"\xff\xfe")

    corrupt = _make_run(
        runtime_root,
        run_id="corrupt",
        status="completed",
        age=timedelta(days=90),
    )
    (corrupt / "manifest.json").write_bytes(b"\xff\xfe")

    result = clean_runtime(runtime_root, now=NOW)

    assert not removable.exists()
    assert retained.exists()
    assert nested.is_dir()
    assert corrupt.exists()
    assert result.removed_run_count == 1


def test_orphan_cleanup_requires_canonical_run_start_marker(tmp_path: Path) -> None:
    from src.lib.runtime.retention import RetentionPolicy, clean_runtime

    runtime_root = tmp_path / ".agentloom"
    fake_run = runtime_root / "runs" / "demo" / "real-run"
    nested = fake_run / "artifacts" / "shell" / "captured"
    for child in (nested / "logs", nested / "audit", nested / "artifacts" / "shell"):
        child.mkdir(parents=True, exist_ok=True)
    for child in (nested / "artifacts" / "background", nested / "artifacts" / "skills"):
        child.mkdir(parents=True)
    old = (NOW - timedelta(days=90)).timestamp()
    for path in [*nested.rglob("*"), nested]:
        os.utime(path, (old, old), follow_symlinks=False)

    result = clean_runtime(
        runtime_root,
        policy=RetentionPolicy(
            successful_runs=timedelta(0),
            failed_runs=timedelta(0),
            raw_artifacts=timedelta(0),
        ),
        now=NOW,
    )

    assert nested.is_dir()
    assert result.removed_run_count == 0


def test_clean_runtime_rejects_symlinked_runs_root(tmp_path: Path) -> None:
    from src.lib.runtime.retention import clean_runtime

    runtime_root = tmp_path / ".agentloom"
    runtime_root.mkdir()
    output_root = tmp_path / "applications" / "demo" / "outputs"
    output_root.mkdir(parents=True)
    (runtime_root / "runs").symlink_to(output_root, target_is_directory=True)
    external_run = _make_run(
        runtime_root,
        run_id="must-survive",
        status="completed",
        age=timedelta(days=8),
    )
    deliverable = external_run / "report.md"
    deliverable.write_text("user output", encoding="utf-8")

    result = clean_runtime(runtime_root, now=NOW)

    assert external_run.exists()
    assert deliverable.read_text(encoding="utf-8") == "user output"
    assert result.removed_run_count == 0


def test_resolved_runtime_home_preserves_root_symlink_for_cleanup_rejection(
    tmp_path: Path,
) -> None:
    from src.lib.runtime import resolve_runtime_home
    from src.lib.runtime.retention import clean_runtime

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outputs = tmp_path / "applications" / "demo" / "outputs"
    outputs.mkdir(parents=True)
    runtime_link = repo_root / ".agentloom"
    runtime_link.symlink_to(outputs, target_is_directory=True)
    external_run = _make_run(
        outputs,
        run_id="external-output",
        status="completed",
        age=timedelta(days=365),
    )

    home = resolve_runtime_home(
        {"runtime": {"root_dir": ".agentloom"}},
        agent_root=repo_root,
    )
    result = clean_runtime(home.root_dir, now=NOW)

    assert home.root_dir == runtime_link.absolute()
    assert home.root_dir.is_symlink()
    assert external_run.exists()
    assert result.removed_run_count == 0


def test_orphaned_running_run_uses_failed_retention_but_live_lease_is_preserved(
    tmp_path: Path,
) -> None:
    from src.lib.runtime.retention import clean_runtime

    runtime_root = tmp_path / ".agentloom"
    orphan = _make_run(
        runtime_root,
        run_id="orphan",
        status="running",
        age=timedelta(days=31),
    )
    live = _make_run(
        runtime_root,
        run_id="live",
        status="running",
        age=timedelta(days=365),
    )
    live_fd = os.open(live, os.O_RDONLY)
    fcntl.flock(live_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = clean_runtime(runtime_root, now=NOW)
    finally:
        fcntl.flock(live_fd, fcntl.LOCK_UN)
        os.close(live_fd)

    assert not orphan.exists()
    assert live.exists()
    assert result.removed_runs == [orphan]

    second = clean_runtime(runtime_root, now=NOW)
    assert not live.exists()
    assert second.removed_runs == [live]


def test_cleaner_does_not_remove_a_run_before_its_manifest_and_lease_exist(
    tmp_path: Path,
) -> None:
    from src.lib.runtime import RuntimeHome
    from src.lib.runtime.retention import clean_runtime

    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="demo",
        task_id="task",
        run_id="run-being-created",
    )
    context.prepare_run()

    clean_runtime(context.root_dir, now=NOW)

    assert context.run_dir.is_dir()
    with context.run_lease():
        context.write_manifest()


def test_cleaner_removes_old_crash_before_manifest_temp_but_not_unknown_data(
    tmp_path: Path,
) -> None:
    from src.lib.runtime import RuntimeHome
    from src.lib.runtime.retention import RetentionPolicy, clean_runtime

    home = RuntimeHome(tmp_path / ".agentloom")
    orphan = home.context(application_id="demo", task_id="task", run_id="orphan")
    orphan.prepare_run()
    temp = orphan.run_dir / ".manifest.json.dead.tmp"
    temp.write_text("partial", encoding="utf-8")
    unknown = home.context(application_id="demo", task_id="task", run_id="unknown")
    unknown.prepare_run()
    (unknown.run_dir / "user-data.txt").write_text("preserve", encoding="utf-8")
    old = (NOW - timedelta(days=31)).timestamp()
    for path in [*orphan.run_dir.rglob("*"), orphan.run_dir, *unknown.run_dir.rglob("*"), unknown.run_dir]:
        os.utime(path, (old, old), follow_symlinks=False)

    result = clean_runtime(
        home.root_dir,
        policy=RetentionPolicy(
            successful_runs=timedelta(0),
            failed_runs=timedelta(0),
            raw_artifacts=timedelta(0),
        ),
        now=NOW,
    )

    assert orphan.run_dir in result.removed_runs
    assert not orphan.run_dir.exists()
    assert (unknown.run_dir / "user-data.txt").read_text(encoding="utf-8") == "preserve"


def test_maybe_clean_runtime_runs_at_most_once_per_24_hours(tmp_path: Path) -> None:
    from src.lib.runtime.retention import prune_runtime_if_due

    runtime_root = tmp_path / ".agentloom"
    first = _make_run(runtime_root, run_id="first", status="completed", age=timedelta(days=8))
    # A process killed after creating the lock must not throttle maintenance
    # forever on the next process start.
    (runtime_root / ".cleanup.lock").write_text("stale", encoding="utf-8")

    runtime_config = {
        "successful_run_retention_days": 7,
        "failed_run_retention_days": 30,
        "artifact_retention_days": 3,
        "cleanup_interval_hours": 24,
    }
    initial = prune_runtime_if_due(runtime_root, runtime_config, now=NOW)
    assert initial.skipped is False
    assert not first.exists()
    assert not (runtime_root / ".cleanup.lock").exists()

    second = _make_run(runtime_root, run_id="second", status="completed", age=timedelta(days=8))
    throttled = prune_runtime_if_due(runtime_root, runtime_config, now=NOW + timedelta(hours=23))
    assert throttled.skipped is True
    assert second.exists()

    resumed = prune_runtime_if_due(runtime_root, runtime_config, now=NOW + timedelta(hours=24))
    assert resumed.skipped is False
    assert not second.exists()


def test_automatic_cleanup_interval_cannot_be_configured_below_24_hours() -> None:
    from src.lib.runtime.retention import retention_policy_from_config

    with pytest.raises(ValueError, match="at least 24"):
        retention_policy_from_config({"cleanup_interval_hours": 23})


def test_future_cleanup_state_does_not_throttle_maintenance_forever(tmp_path: Path) -> None:
    from src.lib.runtime.retention import prune_runtime_if_due

    runtime_root = tmp_path / ".agentloom"
    expired = _make_run(
        runtime_root,
        run_id="expired",
        status="completed",
        age=timedelta(days=8),
    )
    (runtime_root / ".cleanup-state.json").write_text(
        json.dumps({"last_completed_at": (NOW + timedelta(days=365)).isoformat()}),
        encoding="utf-8",
    )

    result = prune_runtime_if_due(runtime_root, now=NOW)

    assert result.skipped is False
    assert not expired.exists()


def test_runtime_cleanup_respects_concurrent_maintenance_lock(tmp_path: Path) -> None:
    from src.lib.runtime.retention import maybe_clean_runtime

    runtime_root = tmp_path / ".agentloom"
    expired = _make_run(
        runtime_root,
        run_id="expired",
        status="completed",
        age=timedelta(days=8),
    )
    lock_fd = os.open(runtime_root, os.O_RDONLY)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = maybe_clean_runtime(runtime_root, now=NOW)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert result.skipped is True
    assert result.skip_reason == "cleanup already in progress"
    assert expired.exists()
