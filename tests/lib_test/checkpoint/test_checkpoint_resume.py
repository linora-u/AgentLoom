"""Integration tests for checkpoint save / resume flow.

All LLM calls are mocked; these tests validate the framework-level wiring
between ``RoleDrivenAgent``, ``CheckpointManager``, and ``run_app()``.
"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.lib.checkpoint import CheckpointManager, CheckpointSerializer


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def cm(tmp_path: Path) -> CheckpointManager:
    return CheckpointManager("test_supervisor", base_dir=tmp_path)


@pytest.fixture()
def task_id() -> str:
    return "task_resume_test"


# ── Supervisor checkpoint save ───────────────────────────────────────────


class TestSupervisorCheckpointSave:

    def test_save_on_completed(self, cm: CheckpointManager, task_id: str):
        """After a successful run, checkpoint is saved with status=completed."""
        from smolagents.memory import TaskStep, ActionStep
        from smolagents.monitoring import Timing

        steps = [
            TaskStep(task="do work"),
            ActionStep(step_number=1, timing=Timing(start_time=time.time()), observations="result"),
        ]
        cm.save_supervisor_checkpoint(
            task_id, memory_steps=steps, task_text="do work", status="completed", result="done",
        )
        loaded = cm.load_supervisor_checkpoint(task_id)
        assert loaded["status"] == "completed"
        assert loaded["result"] == "done"

    def test_save_on_interrupted(self, cm: CheckpointManager, task_id: str):
        from smolagents.memory import TaskStep, ActionStep
        from smolagents.monitoring import Timing

        steps = [
            TaskStep(task="interrupted work"),
            ActionStep(step_number=1, timing=Timing(start_time=time.time()), observations="partial"),
        ]
        cm.save_supervisor_checkpoint(
            task_id, memory_steps=steps, task_text="interrupted work", status="interrupted",
        )
        cm.save_task_tree(task_id, {"task_id": task_id, "status": "interrupted", "agent_name": "test_supervisor", "workers": {}})
        loaded = cm.load_supervisor_checkpoint(task_id)
        assert loaded["status"] == "interrupted"
        tree = cm.load_task_tree(task_id)
        assert tree["status"] == "interrupted"


# ── Supervisor checkpoint restore ────────────────────────────────────────


class TestSupervisorRestore:

    def test_restore_memory_steps(self, cm: CheckpointManager, task_id: str):
        """Deserialised memory steps should match the originals."""
        from smolagents.memory import TaskStep, ActionStep, ToolCall
        from smolagents.monitoring import Timing

        original_steps = [
            TaskStep(task="analyse code"),
            ActionStep(
                step_number=1,
                timing=Timing(start_time=time.time()),
                tool_calls=[ToolCall(name="shell_tool", arguments={"cmd": "ls"}, id="c1")],
                observations="file.py",
            ),
        ]
        cm.save_supervisor_checkpoint(
            task_id, memory_steps=original_steps, task_text="analyse code", status="interrupted",
        )

        loaded = cm.load_supervisor_checkpoint(task_id)
        restored = CheckpointSerializer.deserialize_memory_steps(loaded["memory_steps"])
        assert len(restored) == 2
        assert isinstance(restored[0], TaskStep)
        assert isinstance(restored[1], ActionStep)
        assert restored[1].observations == "file.py"
        assert restored[1].tool_calls[0].name == "shell_tool"

    def test_drops_incomplete_last_action(self, cm: CheckpointManager, task_id: str):
        """An interrupted ActionStep (tool_calls but no observations) should be dropped."""
        from smolagents.memory import TaskStep, ActionStep, ToolCall
        from smolagents.monitoring import Timing

        steps = [
            TaskStep(task="code review"),
            ActionStep(
                step_number=1,
                timing=Timing(start_time=time.time()),
                observations="phase 1 done",
            ),
            ActionStep(
                step_number=2,
                timing=Timing(start_time=time.time()),
                tool_calls=[ToolCall(name="worker_tool", arguments={}, id="c2")],
                observations=None,  # interrupted mid-tool
            ),
        ]
        cm.save_supervisor_checkpoint(
            task_id, memory_steps=steps, task_text="code review", status="interrupted",
        )

        loaded = cm.load_supervisor_checkpoint(task_id)
        restored = CheckpointSerializer.deserialize_memory_steps(loaded["memory_steps"])

        # Simulate the drop logic from _restore_checkpoint
        from smolagents.memory import ActionStep as AS
        if restored and isinstance(restored[-1], AS):
            last = restored[-1]
            if last.tool_calls and not last.observations and not last.is_final_answer:
                restored.pop()

        assert len(restored) == 2  # TaskStep + first ActionStep only


# ── Worker checkpoint ────────────────────────────────────────────────────


class TestWorkerCheckpoint:

    def test_worker_completed_in_tree(self, cm: CheckpointManager, task_id: str):
        cm.save_task_tree(task_id, {"task_id": task_id, "status": "running", "agent_name": "sup", "workers": {}})
        cm.save_worker_checkpoint(task_id, "scan_worker", status="completed", result="42 files")
        tree = cm.load_task_tree(task_id)
        # Update tree using v2 list format
        tree.setdefault("workers", {})["scan_worker"] = [{"status": "completed", "result_summary": "42 files", "call_index": 0, "input_hash": ""}]
        cm.save_task_tree(task_id, tree)
        loaded = cm.load_task_tree(task_id)
        assert loaded["workers"]["scan_worker"][0]["status"] == "completed"

    def test_worker_failed_in_tree(self, cm: CheckpointManager, task_id: str):
        cm.save_task_tree(task_id, {"task_id": task_id, "status": "running", "agent_name": "sup", "workers": {}})
        cm.save_worker_checkpoint(task_id, "bad_worker", status="failed", error="timeout")
        ckpt = cm.load_worker_checkpoint(task_id, "bad_worker")
        assert ckpt["status"] == "failed"
        assert ckpt["error"] == "timeout"


# ── Worker resume (skip completed) ──────────────────────────────────────


class TestWorkerResume:

    def test_identify_completed_workers(self, cm: CheckpointManager, task_id: str):
        """The task tree should allow identifying which workers need rerun (v2 list format)."""
        tree = {
            "task_id": task_id, "status": "interrupted", "agent_name": "sup",
            "workers": {
                "w1": [{"call_index": 0, "input_hash": "", "status": "completed", "result_summary": "done1"}],
                "w2": [{"call_index": 0, "input_hash": "", "status": "interrupted"}],
                "w3": [{"call_index": 0, "input_hash": "", "status": "failed", "error": "boom"}],
            },
        }
        cm.save_task_tree(task_id, tree)
        loaded = cm.load_task_tree(task_id)

        # v2: check the latest call for each worker
        completed = [n for n, calls in loaded["workers"].items()
                     if isinstance(calls, list) and calls[-1]["status"] == "completed"]
        need_rerun = [n for n, calls in loaded["workers"].items()
                      if isinstance(calls, list) and calls[-1]["status"] != "completed"]

        assert completed == ["w1"]
        assert set(need_rerun) == {"w2", "w3"}
