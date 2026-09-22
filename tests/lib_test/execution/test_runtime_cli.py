from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentloom.__main__ import main
from agentloom.execution import RuntimeHome
from agentloom.execution.checkpoint import CheckpointManager
from click.testing import CliRunner


def test_runtime_cli_exposes_only_canonical_storage_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0, result.output
    assert "clean-runtime" in result.output
    assert "migrate-runtime" not in result.output


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
    monkeypatch.setattr("agentloom.execution.cli._configured_runtime_home", lambda: home)

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
    monkeypatch.setattr("agentloom.execution.cli._configured_runtime_home", lambda: home)

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
    monkeypatch.setattr("agentloom.execution.cli._configured_runtime_home", lambda: home)
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
