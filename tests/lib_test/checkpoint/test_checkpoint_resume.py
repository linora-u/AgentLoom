"""Integration tests for checkpoint save / resume flow.

All LLM calls are mocked; these tests validate the framework-level wiring
between ``RoleDrivenAgent``, ``CheckpointManager``, and ``run_app()``.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.lib.checkpoint import CheckpointManager, CheckpointSerializer
from src.lib.checkpoint.coordinator import CheckpointCoordinator

# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def cm(tmp_path: Path) -> CheckpointManager:
    return CheckpointManager(
        "test_supervisor",
        checkpoints_root=tmp_path,
        run_id="run_test",
    )


@pytest.fixture()
def task_id() -> str:
    return "task_resume_test"


class _StepCallbacks:
    def __init__(self) -> None:
        self.callbacks = []

    def register(self, step_type, callback) -> None:
        self.callbacks.append((step_type, callback))


# ── Supervisor checkpoint save ───────────────────────────────────────────


class TestSupervisorCheckpointSave:

    def test_completed_step_identity_not_step_number_controls_deduplication(self):
        """A resumed run may restart step numbering at one."""
        from smolagents.memory import ActionStep
        from smolagents.monitoring import Timing

        from src.lib.checkpoint.coordinator import _steps_including_completed

        previous = ActionStep(
            step_number=1,
            timing=Timing(start_time=time.time()),
            observations="previous attempt",
        )
        current = ActionStep(
            step_number=1,
            timing=Timing(start_time=time.time()),
            observations="resumed attempt",
        )

        assert _steps_including_completed([previous], previous) == [previous]
        assert _steps_including_completed([previous], current) == [previous, current]

    def test_save_on_completed(self, cm: CheckpointManager, task_id: str):
        """After a successful run, checkpoint is saved with status=completed."""
        from smolagents.memory import ActionStep, TaskStep
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
        from smolagents.memory import ActionStep, TaskStep
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

    def test_step_callback_persists_completed_step_before_memory_append(
        self,
        cm: CheckpointManager,
        task_id: str,
    ):
        """smolagents calls callbacks before appending the completed step."""
        from smolagents.memory import ActionStep, TaskStep
        from smolagents.monitoring import Timing

        task_step = TaskStep(task="do work")
        previous_step = ActionStep(
            step_number=1,
            timing=Timing(start_time=time.time()),
            observations="previous attempt",
        )
        action_step = ActionStep(
            # smolagents restarts numbering for a resumed run.
            step_number=1,
            timing=Timing(start_time=time.time()),
            observations="side effect complete",
        )
        callbacks = _StepCallbacks()
        inner = SimpleNamespace(
            memory=SimpleNamespace(steps=[task_step, previous_step]),
            step_callbacks=callbacks,
        )
        coord = CheckpointCoordinator(cm, task_id, "do work")
        heartbeat = MagicMock()
        file_history = MagicMock()
        coord.set_supervisor_heartbeat(heartbeat)
        coord._file_history = file_history
        coord.register_supervisor_step_callback(inner)

        callback = callbacks.callbacks[0][1]
        callback(action_step, agent=inner)

        loaded = cm.load_supervisor_checkpoint(task_id)
        restored = CheckpointSerializer.deserialize_memory_steps(loaded["memory_steps"])
        assert loaded["step_count"] == 3
        assert restored[-1].observations == "side effect complete"
        heartbeat.update_step.assert_called_with(3)
        file_history.make_post_step_snapshot.assert_called_with(3)

        # Also tolerate callback timing where the framework has already
        # appended the same step; the checkpoint must not duplicate it.
        inner.memory.steps.append(action_step)
        callback(action_step, agent=inner)
        assert cm.load_supervisor_checkpoint(task_id)["step_count"] == 3

    def test_inherited_supervisor_callback_does_not_store_worker_step(
        self,
        cm: CheckpointManager,
        task_id: str,
    ):
        from smolagents.memory import ActionStep, TaskStep
        from smolagents.monitoring import Timing

        supervisor_callbacks = _StepCallbacks()
        supervisor = SimpleNamespace(
            memory=SimpleNamespace(steps=[TaskStep(task="supervise")]),
            step_callbacks=supervisor_callbacks,
        )
        worker_callbacks = _StepCallbacks()
        worker = SimpleNamespace(
            memory=SimpleNamespace(steps=[TaskStep(task="work")]),
            step_callbacks=worker_callbacks,
        )
        worker_step = ActionStep(
            step_number=1,
            timing=Timing(start_time=time.time()),
            observations="worker-only result",
        )
        coord = CheckpointCoordinator(cm, task_id, "supervise")
        coord.register_supervisor_step_callback(supervisor)
        coord.register_worker_step_callback(worker)

        inherited_callback = worker_callbacks.callbacks[0][1]
        inherited_callback(worker_step, agent=worker)

        loaded = cm.load_supervisor_checkpoint(task_id)
        assert loaded["step_count"] == 1
        assert "worker-only result" not in str(loaded["memory_steps"])


# ── Supervisor checkpoint restore ────────────────────────────────────────


class TestSupervisorRestore:

    def test_restore_memory_steps(self, cm: CheckpointManager, task_id: str):
        """Deserialised memory steps should match the originals."""
        from smolagents.memory import ActionStep, TaskStep, ToolCall
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


class TestWorkerRestore:

    def test_restore_worker_memory_steps_for_incomplete_resumed_call(self, cm: CheckpointManager, task_id: str):
        from smolagents.memory import ActionStep, TaskStep
        from smolagents.monitoring import Timing

        steps = [
            TaskStep(task="worker task"),
            ActionStep(step_number=1, timing=Timing(start_time=time.time()), observations="partial"),
        ]
        cm.record_worker_started(task_id, "worker_a", input_hash="h", task_input="worker task")
        cm.record_worker_finished(
            task_id,
            "worker_a",
            call_index=0,
            status="interrupted",
            input_hash="h",
            task_input="worker task",
        )
        cm.save_worker_checkpoint(
            task_id,
            "worker_a",
            call_index=0,
            input_hash="h",
            memory_steps=steps,
            task_input="worker task",
            status="interrupted",
        )

        runtime_agent = SimpleNamespace(memory=SimpleNamespace(steps=[]))
        coord = CheckpointCoordinator(cm, task_id, "supervisor task", resume=True)

        assert coord.restore_worker(runtime_agent, "worker_a", 0) is True
        assert len(runtime_agent.memory.steps) == 2
        assert runtime_agent.memory.steps[0].task == "worker task"

    def test_drops_incomplete_last_action(self, cm: CheckpointManager, task_id: str):
        """An interrupted ActionStep (tool_calls but no observations) should be dropped."""
        from smolagents.memory import ActionStep, TaskStep, ToolCall
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

    def test_parallel_same_name_workers_bind_trackers_and_share_one_heartbeat(
        self,
        monkeypatch,
        tmp_path: Path,
    ):
        """Same-name batch calls keep per-invocation trackers and one writer."""

        start_barrier = threading.Barrier(2)

        class _CheckpointManager:
            run_id = "run_parallel"

            def __init__(self):
                self._index = 0
                self._index_lock = threading.Lock()

            def worker_dir(self, task_id, agent_name):
                return tmp_path / task_id / "workers" / agent_name

            def prepare_worker_call(self, *args, **kwargs):
                with self._index_lock:
                    call_index = self._index
                    self._index += 1
                if call_index < 2:
                    start_barrier.wait(timeout=2)
                return SimpleNamespace(
                    call_index=call_index,
                    should_execute=True,
                    cached_result=None,
                )

            def directory_storage(self, *args, **kwargs):
                return object()

        heartbeat_instances = []
        heartbeat_instances_lock = threading.Lock()

        class _Heartbeat:
            def __init__(self, **kwargs):
                # Let the old unlocked get/create sequence overlap so this
                # regression deterministically observes duplicate writers.
                time.sleep(0.05)
                self._calls = {}
                self._lock = threading.Lock()
                self.start_count = 0
                self.stop_count = 0
                with heartbeat_instances_lock:
                    heartbeat_instances.append(self)

            def register_call(self, call_index):
                with self._lock:
                    self._calls[call_index] = "running"

            def start(self):
                self.start_count += 1

            def update_call_status(self, call_index, status):
                with self._lock:
                    self._calls[call_index] = status

            def all_calls_terminal(self):
                with self._lock:
                    return bool(self._calls) and all(
                        status in {"completed", "failed"}
                        for status in self._calls.values()
                    )

            def stop(self):
                self.stop_count += 1

            def close(self):
                pass

        monkeypatch.setattr(
            "src.lib.checkpoint.coordinator.WorkerHeartbeat",
            _Heartbeat,
        )

        manager = _CheckpointManager()
        coord = CheckpointCoordinator(manager, "task_parallel", "supervise")
        coord._step_cb = lambda *args, **kwargs: None
        workers = [
            SimpleNamespace(
                memory=SimpleNamespace(steps=[]),
                step_callbacks=_StepCallbacks(),
            )
            for _ in range(2)
        ]

        def _start(runtime_agent, input_hash):
            coord.register_worker_step_callback(runtime_agent, "worker_a")
            return coord.prepare_worker_call(
                "worker_a",
                input_hash,
                input_hash,
                runtime_agent=runtime_agent,
            ).call_index

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(_start, worker, f"hash-{index}")
                for index, worker in enumerate(workers)
            ]
            call_indexes = {future.result(timeout=3) for future in futures}

        assert call_indexes == {0, 1}
        assert [len(worker.step_callbacks.callbacks) for worker in workers] == [2, 2]
        assert len(heartbeat_instances) == 1
        heartbeat = heartbeat_instances[0]
        assert set(heartbeat._calls) == {0, 1}

        coord._update_worker_heartbeat("worker_a", 0, "completed")
        assert heartbeat.stop_count == 0
        coord._update_worker_heartbeat("worker_a", 1, "completed")
        assert heartbeat.stop_count == 1

        # A later sequential call reuses and restarts the same writer.
        later_worker = SimpleNamespace(
            memory=SimpleNamespace(steps=[]),
            step_callbacks=_StepCallbacks(),
        )
        coord.register_worker_step_callback(later_worker, "worker_a")
        assert coord.prepare_worker_call(
            "worker_a",
            "hash-2",
            "third",
            runtime_agent=later_worker,
        ).call_index == 2
        assert len(heartbeat_instances) == 1
        assert heartbeat.start_count == 3
        assert len(later_worker.step_callbacks.callbacks) == 2

        coord.stop_all_worker_heartbeats()

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

    def test_worker_success_preserves_empty_string_result(
        self,
        cm: CheckpointManager,
        task_id: str,
    ):
        call_index = cm.record_worker_started(
            task_id,
            "empty_worker",
            input_hash="hash",
            task_input="task",
        )
        coord = CheckpointCoordinator(cm, task_id, "supervise")

        coord.record_worker_success(
            "empty_worker",
            call_index,
            "hash",
            "task",
            "",
            [],
        )

        checkpoint = cm.load_worker_checkpoint(
            task_id,
            "empty_worker",
            call_index=call_index,
        )
        call = cm.load_task_tree(task_id)["workers"]["empty_worker"][0]
        assert checkpoint["status"] == "completed"
        assert checkpoint["result"] == ""
        assert call["result"] == ""

    def test_step_tracker_persists_completed_step_before_memory_append(
        self,
        cm: CheckpointManager,
        task_id: str,
    ):
        from smolagents.memory import ActionStep, TaskStep
        from smolagents.monitoring import Timing

        task_step = TaskStep(task="worker task")
        previous_step = ActionStep(
            step_number=1,
            timing=Timing(start_time=time.time()),
            observations="previous worker attempt",
        )
        action_step = ActionStep(
            # A resumed worker also restarts local step numbering.
            step_number=1,
            timing=Timing(start_time=time.time()),
            observations="worker side effect complete",
        )
        callbacks = _StepCallbacks()
        worker = SimpleNamespace(
            memory=SimpleNamespace(steps=[task_step, previous_step]),
            step_callbacks=callbacks,
        )
        heartbeat = MagicMock()
        coord = CheckpointCoordinator(cm, task_id, "supervise")
        coord._worker_heartbeats["worker_a"] = heartbeat
        coord.register_worker_step_tracker(
            worker,
            "worker_a",
            0,
            input_hash="hash",
            task_input="worker task",
        )

        callback = callbacks.callbacks[0][1]
        callback(action_step, agent=worker)

        loaded = cm.load_worker_checkpoint(task_id, "worker_a", call_index=0)
        restored = CheckpointSerializer.deserialize_memory_steps(loaded["memory_steps"])
        assert loaded["step_count"] == 3
        assert restored[-1].observations == "worker side effect complete"
        heartbeat.update_call_step.assert_called_with(0, 3)

        worker.memory.steps.append(action_step)
        callback(action_step, agent=worker)
        assert cm.load_worker_checkpoint(task_id, "worker_a", call_index=0)["step_count"] == 3


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
