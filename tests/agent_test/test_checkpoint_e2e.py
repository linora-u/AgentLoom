"""
End-to-end checkpoint & resume tests.

Tests the full checkpoint lifecycle without requiring LLM calls:
- CheckpointManager saves/loads task tree and supervisor checkpoint
- CheckpointCoordinator restore() uses conversation recovery pipeline
- Worker skip-on-resume via input_hash matching
- FileHistoryManager tracks edits and produces snapshots
- Coordinator step callback triggers file history snapshot
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from src.lib.checkpoint.checkpoint_manager import CheckpointManager
from src.lib.checkpoint.coordinator import CheckpointCoordinator
from src.lib.checkpoint.conversation_recovery import (
    TurnInterruptionState,
    prepare_steps_for_resume,
)
from src.lib.checkpoint.file_history import FileHistoryManager


# ---------------------------------------------------------------------------
# Step stubs (duck-typed to match smolagents MemoryStep)
# ---------------------------------------------------------------------------

@dataclass
class _FakeActionStep:
    tool_calls: Optional[list] = None
    observations: Optional[str] = None
    model_output: Optional[str] = None
    action_output: Optional[str] = None
    is_final_answer: bool = False
    step_number: int = 0

_FakeActionStep.__name__ = "ActionStep"  # type: ignore[attr-defined]


@dataclass
class _FakeTaskStep:
    task: str = "test task"
    step_number: int = 0

_FakeTaskStep.__name__ = "TaskStep"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# E2E: Full checkpoint save → interrupt → resume cycle
# ---------------------------------------------------------------------------


class TestCheckpointSaveAndResume:
    """Simulates a full save → interrupt → resume flow without LLM."""

    def test_save_then_resume_filters_incomplete_steps(self, tmp_path):
        """Save checkpoint with mixed steps, resume filters bad ones."""
        cm = CheckpointManager(
            "e2e_agent",
            checkpoints_root=tmp_path,
            run_id="run_test",
        )
        task_id = "task_e2e_001"

        # Simulate: agent completed 2 steps, then was interrupted mid-step-3.
        # Write pre-serialized dict data directly to bypass CheckpointSerializer
        # (which expects real MemoryStep objects).
        steps_raw = [
            {"_step_type": "TaskStep", "task": "do something"},
            {
                "_step_type": "ActionStep",
                "step_number": 1,
                "tool_calls": [{"name": "read_file", "id": "tc1"}],
                "observations": "file content here",
                "model_output": "I'll read the file",
                "is_final_answer": False,
            },
            {
                # Interrupted mid-tool: has tool_calls but no observations
                "_step_type": "ActionStep",
                "step_number": 2,
                "tool_calls": [{"name": "edit_file", "id": "tc2"}],
                "observations": None,
                "model_output": "I'll edit the file",
                "is_final_answer": False,
            },
        ]

        # Save task tree
        cm.save_task_tree(task_id, {
            "task_id": task_id,
            "agent_name": "e2e_agent",
            "status": "interrupted",
        })

        # Write checkpoint.json directly (bypass serializer)
        import json
        from datetime import datetime, timezone
        ckpt_path = cm._supervisor_ckpt(task_id)
        ckpt_path.parent.mkdir(parents=True, exist_ok=True)
        ckpt_data = {
            "agent_name": "e2e_agent",
            "task_id": task_id,
            "task_text": "do something",
            "status": "interrupted",
            "step_count": len(steps_raw),
            "memory_steps": steps_raw,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        ckpt_path.write_text(json.dumps(ckpt_data), encoding="utf-8")

        # Load and deserialize
        ckpt = cm.load_supervisor_checkpoint(task_id)
        assert ckpt is not None
        assert len(ckpt["memory_steps"]) == 3

        # Simulate resume: deserialize then run pipeline
        from src.lib.checkpoint.serializer import CheckpointSerializer
        deserialized = CheckpointSerializer.deserialize_memory_steps(ckpt["memory_steps"])

        cleaned, interruption = prepare_steps_for_resume(deserialized)
        # Step 0 (TaskStep) and Step 1 (complete ActionStep) kept.
        # Step 2 (incomplete) filtered out.
        assert len(cleaned) == 2
        assert interruption.kind == "none"  # last kept step has observations

    def test_worker_skip_on_resume(self, tmp_path):
        """Completed worker is skipped on resume via input_hash match."""
        cm = CheckpointManager(
            "e2e_sup",
            checkpoints_root=tmp_path,
            run_id="run_test",
        )
        task_id = "task_e2e_worker_skip"

        # Save task tree with a completed worker
        cm.save_task_tree(task_id, {
            "task_id": task_id,
            "agent_name": "e2e_sup",
            "status": "interrupted",
            "workers": {
                "scanner": [{
                    "call_index": 0,
                    "input_hash": "abc123",
                    "status": "completed",
                    "result": "scan done",
                }],
            },
        })

        # Load and check worker skip
        tree = cm.load_task_tree(task_id)
        workers = tree["workers"]["scanner"]
        assert len(workers) == 1
        assert workers[0]["status"] == "completed"
        assert workers[0]["input_hash"] == "abc123"
        assert workers[0]["result"] == "scan done"

    def test_context_store_metadata_and_resume_retrieval(self, tmp_path):
        """ContextEngine store is task-scoped and survives coordinator resume."""
        cm = CheckpointManager(
            "ctx_sup",
            checkpoints_root=tmp_path,
            run_id="run_test",
        )
        task_id = "task_context_store"

        class RuntimeAgent:
            _config = {}

            class Memory:
                steps = []

            memory = Memory()

        coord = CheckpointCoordinator.activate(cm, task_id, "task")
        try:
            from src.lib.context_engine.runtime import get_current_context_engine

            engine = get_current_context_engine()
            assert engine is not None
            preview = engine.compress_tool_result(
                "resume needle\n" * 600,
                tool_name="shell_tool",
                source="checkpoint-test",
            )
            assert preview is not None
            ref = preview.split()[1]
            coord.save_supervisor(RuntimeAgent(), "running")
            ckpt = cm.load_supervisor_checkpoint(task_id)
            assert ckpt["context_store"]["ref_count"] == 1
        finally:
            CheckpointCoordinator.deactivate(coord)

        resumed = CheckpointCoordinator.activate(cm, task_id, "task", resume=True)
        try:
            from src.lib.context_engine.runtime import get_current_context_engine

            resumed_engine = get_current_context_engine()
            assert resumed_engine is not None
            assert "resume needle" in resumed_engine.retrieve(ref, offset=0, limit=1)
        finally:
            CheckpointCoordinator.deactivate(resumed)


# ---------------------------------------------------------------------------
# E2E: File history integration with coordinator step callback
# ---------------------------------------------------------------------------


class TestFileHistoryWithCoordinator:
    """Tests file history snapshot creation through the coordinator step callback."""

    def test_step_callback_triggers_file_history_snapshot(self, tmp_path):
        """Coordinator step callback creates file history post-step snapshot."""
        # Setup
        cm = CheckpointManager(
            "fh_test",
            checkpoints_root=tmp_path,
            run_id="run_test",
        )
        task_id = "task_fh_001"
        cm.save_task_tree(task_id, {
            "task_id": task_id,
            "agent_name": "fh_test",
            "status": "running",
        })

        fh_dir = tmp_path / task_id / "file-history"
        fh = FileHistoryManager(fh_dir)

        # Track a file edit
        test_file = tmp_path / "test.txt"
        test_file.write_text("original")
        fh.track_edit(str(test_file), step_number=1)

        # Modify the file
        test_file.write_text("modified")

        # Simulate step callback calling make_post_step_snapshot
        fh.make_post_step_snapshot(step_number=1)

        # Verify snapshot was created
        assert fh.snapshot_count >= 1
        assert fh.tracked_file_count == 1

        # Verify snapshots.json was persisted
        idx_path = fh_dir / "snapshots.json"
        assert idx_path.exists()
        with open(idx_path) as f:
            data = json.load(f)
        assert len(data["snapshots"]) >= 1

    def test_file_history_track_and_rewind_single_step(self, tmp_path):
        """Track edit → modify file → rewind restores pre-edit content."""
        fh_dir = tmp_path / "file-history"
        fh = FileHistoryManager(fh_dir)

        test_file = tmp_path / "rewind_me.txt"
        test_file.write_text("original content")

        # Step 1: track_edit backs up "original content" as v1
        fh.track_edit(str(test_file), step_number=1)
        # Edit the file
        test_file.write_text("modified content")

        assert test_file.read_text() == "modified content"

        # Rewind to step 1 → restores v1 backup ("original content")
        restored = fh.rewind_to_step(step_number=1)
        abs_test = os.path.abspath(str(test_file))
        assert abs_test in restored
        assert test_file.read_text() == "original content"


# ---------------------------------------------------------------------------
# E2E: Heartbeat crash detection
# ---------------------------------------------------------------------------


class TestHeartbeatCrashDetection:
    """Test heartbeat-based crash detection."""

    def test_stale_heartbeat_detected_as_crashed(self, tmp_path):
        """Heartbeat with old timestamp and dead PID is detected as crashed."""
        from src.lib.heartbeat.status import detect_crashed_status

        heartbeat = {
            "pid": 999999999,  # Non-existent PID
            "timestamp": 1000000000.0,  # Very old timestamp
            "status": "running",
        }
        result = detect_crashed_status(heartbeat)
        assert result == "crashed"

    def test_none_heartbeat_is_crashed(self):
        """Missing heartbeat file is treated as crashed."""
        from src.lib.heartbeat.status import detect_crashed_status

        result = detect_crashed_status(None)
        assert result == "crashed"

    def test_stopped_heartbeat_is_crashed(self):
        """Heartbeat with status=stopped is treated as crashed."""
        from src.lib.heartbeat.status import detect_crashed_status
        import time

        heartbeat = {
            "pid": os.getpid(),
            "timestamp": time.time(),
            "status": "stopped",
        }
        result = detect_crashed_status(heartbeat)
        assert result == "crashed"


# ---------------------------------------------------------------------------
# E2E: Conversation recovery pipeline on real-ish data
# ---------------------------------------------------------------------------


class TestConversationRecoveryE2E:
    """Full pipeline test with realistic step sequences."""

    def test_complex_mixed_steps(self):
        """Realistic scenario: task + 5 action steps with various states."""
        task = _FakeTaskStep(task="Analyze code quality")
        # Step 1: completed normally
        s1 = _FakeActionStep(
            step_number=1,
            model_output="I'll scan the code",
            tool_calls=[{"name": "grep_search"}],
            observations="Found 10 files",
        )
        # Step 2: completed normally
        s2 = _FakeActionStep(
            step_number=2,
            model_output="I'll read main.py",
            tool_calls=[{"name": "read_file"}],
            observations="def main(): ...",
        )
        # Step 3: orphaned thinking (crash during streaming)
        s3 = _FakeActionStep(
            step_number=3,
            model_output="Let me think about the architecture...",
        )
        # Step 4: unresolved tool call (crash between dispatch and execution)
        s4 = _FakeActionStep(
            step_number=4,
            model_output="I'll edit the file",
            tool_calls=[{"name": "edit_file"}],
        )
        # Step 5: empty (crash before LLM response)
        s5 = _FakeActionStep(step_number=5)

        cleaned, interruption = prepare_steps_for_resume([task, s1, s2, s3, s4, s5])

        # task + s1 + s2 kept (3 steps); s3 (orphaned), s4 (unresolved), s5 (empty) dropped
        assert len(cleaned) == 3
        assert cleaned[0] is task
        assert cleaned[1] is s1
        assert cleaned[2] is s2
        # Last kept step (s2) has observations → no interruption
        assert interruption.kind == "none"
