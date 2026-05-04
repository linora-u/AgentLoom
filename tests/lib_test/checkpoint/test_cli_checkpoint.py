"""Tests for checkpoint-related CLI commands (list-tasks, clean-tasks)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from src.__main__ import main
from src.lib.checkpoint.checkpoint_manager import CheckpointManager


@pytest.fixture()
def populated_runtime(tmp_path: Path, monkeypatch):
    """Create a fake checkpoint dir with two checkpoints, then point the manager there."""
    ckpt_root = tmp_path / "ckpt"
    ckpt_root.mkdir()
    monkeypatch.setenv("AGENT_LOOM_RUNTIME_ROOT", str(ckpt_root))

    cm1 = CheckpointManager("agent_a", base_dir=ckpt_root)
    cm1.save_task_tree("t1", {"task_id": "t1", "agent_name": "agent_a", "status": "interrupted", "created_at": "2026-03-31T10:00:00Z"})

    cm2 = CheckpointManager("agent_b", base_dir=ckpt_root)
    cm2.save_task_tree("t2", {"task_id": "t2", "agent_name": "agent_b", "status": "failed", "created_at": "2026-03-31T11:00:00Z"})

    return tmp_path


class TestListTasks:

    def test_empty(self, tmp_path: Path, monkeypatch):
        empty_dir = tmp_path / "empty_ckpt"
        empty_dir.mkdir()
        monkeypatch.setenv("AGENT_LOOM_RUNTIME_ROOT", str(empty_dir))
        runner = CliRunner()
        result = runner.invoke(main, ["list-tasks"])
        assert result.exit_code == 0
        assert "No resumable tasks" in result.output

    def test_shows_entries(self, populated_runtime):
        runner = CliRunner()
        result = runner.invoke(main, ["list-tasks"])
        assert result.exit_code == 0
        assert "t1" in result.output
        assert "agent_a" in result.output
        assert "t2" in result.output
        assert "agent_b" in result.output


class TestCleanTasks:

    def test_clean_all(self, populated_runtime):
        runner = CliRunner()
        result = runner.invoke(main, ["clean-tasks", "--all"])
        assert result.exit_code == 0
        assert "Cleaned" in result.output

        # Verify empty
        result2 = runner.invoke(main, ["list-tasks"])
        assert "No resumable tasks" in result2.output
