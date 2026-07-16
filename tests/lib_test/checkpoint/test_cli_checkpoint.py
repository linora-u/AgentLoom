"""Tests for checkpoint-related CLI commands (list-tasks, clean-tasks)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from src.__main__ import main
from src.lib.checkpoint.checkpoint_manager import CheckpointManager
from src.lib.runtime import RuntimeHome


@pytest.fixture()
def populated_runtime(tmp_path: Path, monkeypatch):
    """Create a fake checkpoint dir with two checkpoints, then point the manager there."""
    runtime_root = tmp_path / ".agentloom"
    ckpt_root = runtime_root / "checkpoints"
    monkeypatch.setattr(
        "src.__main__._configured_runtime_home",
        lambda: RuntimeHome(runtime_root),
    )

    cm1 = CheckpointManager(
        "agent_a",
        checkpoint_dir=ckpt_root / "app_a" / "t1",
        run_id="run_a",
    )
    cm1.save_task_tree("t1", {"task_id": "t1", "agent_name": "agent_a", "status": "interrupted", "created_at": "2026-03-31T10:00:00Z"})
    c0 = cm1.record_worker_started("t1", "worker_a", input_hash="h", task_input="input")
    cm1.record_worker_finished("t1", "worker_a", call_index=c0, status="completed", result="done")

    cm2 = CheckpointManager(
        "agent_b",
        checkpoint_dir=ckpt_root / "app_b" / "t2",
        run_id="run_b",
    )
    cm2.save_task_tree("t2", {"task_id": "t2", "agent_name": "agent_b", "status": "failed", "created_at": "2026-03-31T11:00:00Z"})

    return tmp_path


class TestListTasks:

    def test_empty(self, tmp_path: Path, monkeypatch):
        runtime_root = tmp_path / ".agentloom"
        monkeypatch.setattr(
            "src.__main__._configured_runtime_home",
            lambda: RuntimeHome(runtime_root),
        )
        runner = CliRunner()
        result = runner.invoke(main, ["list-tasks"])
        assert result.exit_code == 0
        assert "No checkpoint tasks" in result.output

    def test_shows_entries(self, populated_runtime):
        runner = CliRunner()
        result = runner.invoke(main, ["list-tasks"])
        assert result.exit_code == 0
        assert "t1" in result.output
        assert "agent_a" in result.output
        assert "t2" in result.output
        assert "agent_b" in result.output

    def test_detail_shows_worker_calls_from_event_projection(self, populated_runtime):
        runner = CliRunner()
        result = runner.invoke(main, ["list-tasks", "--detail"])
        assert result.exit_code == 0
        assert "worker_a #0" in result.output
        assert "completed" in result.output


class TestCleanTasks:

    def test_clean_all(self, populated_runtime):
        runner = CliRunner()
        result = runner.invoke(main, ["clean-tasks", "--all"])
        assert result.exit_code == 0
        assert "Cleaned" in result.output

        # Verify empty
        result2 = runner.invoke(main, ["list-tasks"])
        assert "No checkpoint tasks" in result2.output

    def test_clean_before_preserves_task_with_active_run_lease(self, populated_runtime):
        task_dir = populated_runtime / ".agentloom" / "checkpoints" / "app_a" / "t1"
        manager = CheckpointManager(
            "agent_a",
            checkpoint_dir=task_dir,
            run_id="active-run",
        )

        with manager.task_lease():
            result = CliRunner().invoke(main, ["clean-tasks", "--before", "1"])

        assert result.exit_code == 0
        assert task_dir.exists()
        assert not (
            populated_runtime / ".agentloom" / "checkpoints" / "app_b" / "t2"
        ).exists()


def test_checkpoint_cli_rejects_symlinked_runtime_root_without_deleting_external_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    external = tmp_path / "application-outputs"
    task_dir = external / "checkpoints" / "app" / "task"
    manager = CheckpointManager("agent", checkpoint_dir=task_dir)
    manager.save_task_tree(
        "task",
        {
            "task_id": "task",
            "agent_name": "agent",
            "status": "failed",
            "created_at": "2020-01-01T00:00:00+00:00",
        },
    )
    runtime_link = tmp_path / ".agentloom"
    runtime_link.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(
        "src.__main__._configured_runtime_home",
        lambda: RuntimeHome(runtime_link),
    )

    listed = CliRunner().invoke(main, ["list-tasks"])
    cleaned = CliRunner().invoke(main, ["clean-tasks", "--all"])

    assert listed.exit_code != 0
    assert cleaned.exit_code != 0
    assert "symlink" in (listed.output + cleaned.output)
    assert task_dir.exists()
