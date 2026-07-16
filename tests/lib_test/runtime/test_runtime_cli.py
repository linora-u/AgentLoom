from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from src.__main__ import main
from src.lib.checkpoint import CheckpointManager
from src.lib.runtime import RuntimeHome


def test_clean_runtime_command_applies_configured_retention(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = RuntimeHome(tmp_path / ".agentloom")
    context = home.context(application_id="app", task_id="task", run_id="run")
    context.prepare_run()
    old = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    context.manifest_path.write_text(
        json.dumps(
            {
                "application_id": "app",
                "task_id": "task",
                "run_id": "run",
                "status": "completed",
                "ended_at": old,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.__main__._configured_runtime_home", lambda: home)

    result = CliRunner().invoke(main, ["clean-runtime"])

    assert result.exit_code == 0, result.output
    assert "runs=1" in result.output
    assert not context.run_dir.exists()


def test_clean_runtime_command_never_removes_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = RuntimeHome(tmp_path / ".agentloom")
    context = home.context(application_id="app", task_id="expired", run_id="run_old")
    manager = CheckpointManager("supervisor", checkpoint_dir=context.checkpoint_dir)
    manager.save_task_tree(
        context.task_id,
        {
            "task_id": context.task_id,
            "status": "interrupted",
            "created_at": (datetime.now(UTC) - timedelta(days=8)).isoformat(),
            "workers": {},
        },
    )
    monkeypatch.setattr("src.__main__._configured_runtime_home", lambda: home)

    result = CliRunner().invoke(main, ["clean-runtime"])

    assert result.exit_code == 0, result.output
    assert "checkpoints=" not in result.output
    assert context.checkpoint_dir.exists()


def test_clean_runtime_command_reports_lock_contention_as_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = RuntimeHome(tmp_path / ".agentloom")
    home.root_dir.mkdir(parents=True)
    monkeypatch.setattr("src.__main__._configured_runtime_home", lambda: home)
    lock_fd = os.open(home.root_dir, os.O_RDONLY)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = CliRunner().invoke(main, ["clean-runtime"])
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert result.exit_code != 0
    assert "cleanup skipped" in result.output
    assert "already in progress" in result.output
    assert "Cleaned runtime" not in result.output


def test_migrate_runtime_command_defaults_to_dry_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = RuntimeHome(tmp_path / ".agentloom")
    observed = {}

    def _migrate(legacy_logs_dir, runtime_root, **kwargs):
        observed.update(
            legacy_logs_dir=Path(legacy_logs_dir),
            runtime_root=Path(runtime_root),
            **kwargs,
        )
        return SimpleNamespace(
            dry_run=kwargs["dry_run"],
            plan=SimpleNamespace(candidate_count=0, skipped_count=0, candidates=[], skipped=[]),
            migrated_count=0,
            already_migrated_count=0,
            archive_dir=None,
        )

    monkeypatch.setattr("src.__main__._configured_runtime_home", lambda: home)
    monkeypatch.setattr("src.lib.runtime.migration.migrate_runtime", _migrate)

    result = CliRunner().invoke(main, ["migrate-runtime", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert observed["dry_run"] is True
    assert observed["archive_legacy"] is False
    assert "candidates=0" in result.output


def test_migrate_runtime_apply_requests_atomic_legacy_archive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = RuntimeHome(tmp_path / ".agentloom")
    observed = {}

    def _migrate(_legacy_logs_dir, _runtime_root, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(
            dry_run=False,
            plan=SimpleNamespace(candidate_count=1, skipped_count=0, candidates=[], skipped=[]),
            migrated_count=1,
            already_migrated_count=0,
            archive_dir=home.root_dir / "legacy" / "logs-v1-now",
        )

    monkeypatch.setattr("src.__main__._configured_runtime_home", lambda: home)
    monkeypatch.setattr("src.lib.runtime.migration.migrate_runtime", _migrate)

    result = CliRunner().invoke(main, ["migrate-runtime", "--apply"])

    assert result.exit_code == 0, result.output
    assert observed["dry_run"] is False
    assert observed["archive_legacy"] is True
    assert "migrated=1" in result.output
