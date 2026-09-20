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

import pytest
from agentloom.runtime.agent_runtime import RuntimeCheckpointEnvelope
from agentloom.runtime.checkpoint import CheckpointManager
from agentloom.runtime.checkpoint.coordinator import CheckpointCoordinator

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
            "agentloom.runtime.checkpoint.coordinator.WorkerHeartbeat",
            _Heartbeat,
        )

        manager = _CheckpointManager()
        coord = CheckpointCoordinator(manager, "task_parallel", "supervise")
        def _start(input_hash):
            return coord.prepare_worker_call(
                "worker_a",
                input_hash,
                input_hash,
            ).call_index

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(_start, f"hash-{index}")
                for index in range(2)
            ]
            call_indexes = {future.result(timeout=3) for future in futures}

        assert call_indexes == {0, 1}
        assert len(heartbeat_instances) == 1
        heartbeat = heartbeat_instances[0]
        assert set(heartbeat._calls) == {0, 1}

        coord._update_worker_heartbeat("worker_a", 0, "completed")
        assert heartbeat.stop_count == 0
        coord._update_worker_heartbeat("worker_a", 1, "completed")
        assert heartbeat.stop_count == 1

        # A later sequential call reuses and restarts the same writer.
        assert coord.prepare_worker_call(
            "worker_a",
            "hash-2",
            "third",
        ).call_index == 2
        assert len(heartbeat_instances) == 1
        assert heartbeat.start_count == 3

        coord.stop_all_worker_heartbeats()

    def test_worker_completed_in_tree(self, cm: CheckpointManager, task_id: str):
        cm.save_task_tree(task_id, {"task_id": task_id, "status": "running", "agent_name": "sup", "workers": {}})
        cm.save_worker_runtime_checkpoint(task_id, "scan_worker", status="completed", result="42 files")
        tree = cm.load_task_tree(task_id)
        # Update tree using v2 list format
        tree.setdefault("workers", {})["scan_worker"] = [{"status": "completed", "result_summary": "42 files", "call_index": 0, "input_hash": ""}]
        cm.save_task_tree(task_id, tree)
        loaded = cm.load_task_tree(task_id)
        assert loaded["workers"]["scan_worker"][0]["status"] == "completed"

    def test_worker_failed_in_tree(self, cm: CheckpointManager, task_id: str):
        cm.save_task_tree(task_id, {"task_id": task_id, "status": "running", "agent_name": "sup", "workers": {}})
        cm.save_worker_runtime_checkpoint(task_id, "bad_worker", status="failed", error="timeout")
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
            None,
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

    def test_worker_success_checkpoint_write_failure_propagates_without_losing_completion(
        self,
        cm: CheckpointManager,
        task_id: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        call_index = cm.record_worker_started(
            task_id,
            "side_effect_worker",
            input_hash="hash",
            task_input="perform side effect",
        )
        coord = CheckpointCoordinator(cm, task_id, "supervise")

        def fail_checkpoint_write(*args, **kwargs):
            raise OSError("checkpoint write failed")

        monkeypatch.setattr(
            cm,
            "save_worker_runtime_checkpoint",
            fail_checkpoint_write,
        )

        with pytest.raises(OSError, match="checkpoint write failed"):
            coord.record_worker_success(
                "side_effect_worker",
                call_index,
                "hash",
                "perform side effect",
                "completed side effect",
                None,
            )

        call = cm.load_task_tree(task_id)["workers"]["side_effect_worker"][0]
        assert call["status"] == "completed"
        assert call["result"] == "completed side effect"

        resumed = CheckpointManager(
            "test_supervisor",
            checkpoints_root=tmp_path,
            run_id="run_resume",
        )
        preparation = CheckpointCoordinator(
            resumed,
            task_id,
            "supervise",
            resume=True,
        ).prepare_worker_call(
            "side_effect_worker",
            "hash",
            "perform side effect",
        )
        assert preparation.should_execute is False
        assert preparation.cached_result == "completed side effect"


class TestSupervisorCheckpoint:
    @pytest.mark.parametrize(
        "status",
        ["completed", "failed", "interrupted"],
    )
    def test_terminal_checkpoint_write_failure_propagates(
        self,
        cm: CheckpointManager,
        task_id: str,
        monkeypatch: pytest.MonkeyPatch,
        status: str,
    ) -> None:
        cm.save_task_tree(
            task_id,
            {
                "task_id": task_id,
                "status": "running",
                "agent_name": "sup",
                "workers": {},
            },
        )
        coord = CheckpointCoordinator(cm, task_id, "supervise")
        monkeypatch.setattr(
            cm,
            "save_supervisor_runtime_checkpoint",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("terminal checkpoint write failed")
            ),
        )

        with pytest.raises(
            OSError,
            match="terminal checkpoint write failed",
        ):
            coord.save_runtime_checkpoint(
                RuntimeCheckpointEnvelope(
                    runtime_id="smolagents",
                    runtime_version="test",
                    state_schema_version=2,
                    progress=1,
                    payload={
                        "memory_steps": [],
                        "canonical_model_items": [],
                    },
                ),
                status,
            )

        assert cm.load_task_tree(task_id)["checkpoint_degraded"] is True

    def test_running_checkpoint_write_failure_remains_best_effort(
        self,
        cm: CheckpointManager,
        task_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cm.save_task_tree(
            task_id,
            {
                "task_id": task_id,
                "status": "running",
                "agent_name": "sup",
                "workers": {},
            },
        )
        coord = CheckpointCoordinator(cm, task_id, "supervise")
        monkeypatch.setattr(
            cm,
            "save_supervisor_runtime_checkpoint",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("running checkpoint write failed")
            ),
        )

        coord.save_runtime_checkpoint(
            RuntimeCheckpointEnvelope(
                runtime_id="smolagents",
                runtime_version="test",
                state_schema_version=2,
                progress=1,
                payload={
                    "memory_steps": [],
                    "canonical_model_items": [],
                },
            ),
            "running",
        )

        assert cm.load_task_tree(task_id)["checkpoint_degraded"] is True

    @pytest.mark.parametrize("auxiliary", ["heartbeat", "file_history"])
    def test_terminal_checkpoint_ignores_auxiliary_refresh_failure(
        self,
        cm: CheckpointManager,
        task_id: str,
        auxiliary: str,
    ) -> None:
        cm.save_task_tree(
            task_id,
            {
                "task_id": task_id,
                "status": "running",
                "agent_name": "sup",
                "workers": {},
            },
        )
        coord = CheckpointCoordinator(cm, task_id, "supervise")
        failing = SimpleNamespace(
            update_step=lambda *_args: (_ for _ in ()).throw(
                OSError("heartbeat refresh failed")
            ),
            make_post_step_snapshot=lambda *_args: (_ for _ in ()).throw(
                OSError("file history refresh failed")
            ),
        )
        if auxiliary == "heartbeat":
            coord._supervisor_heartbeat = failing
        else:
            coord._file_history = failing

        coord.save_runtime_checkpoint(
            RuntimeCheckpointEnvelope(
                runtime_id="smolagents",
                runtime_version="test",
                state_schema_version=2,
                progress=1,
                payload={
                    "memory_steps": [],
                    "canonical_model_items": [],
                },
            ),
            "completed",
            result="done",
        )

        checkpoint = cm.load_supervisor_checkpoint(task_id)
        tree = cm.load_task_tree(task_id)
        assert checkpoint["status"] == "completed"
        assert tree["status"] == "completed"
        assert tree.get("checkpoint_degraded") is not True

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


@pytest.mark.parametrize(
    "output",
    ["exact Worker answer", "", None, "RunResult(output='legitimate text')"],
)
def test_completed_cache_uses_exact_stored_result(tmp_path, output):
    initial = CheckpointManager("supervisor", checkpoints_root=tmp_path, run_id="run_initial")
    task = "task_legacy_worker"
    call_index = initial.record_worker_started(task, "worker", input_hash="same-input", task_input="delegate")
    initial.save_worker_runtime_checkpoint(
        task,
        "worker",
        call_index=call_index,
        input_hash="same-input",
        task_input="delegate",
        status="completed",
        result=output,
    )
    initial.record_worker_finished(task, "worker", call_index=call_index, input_hash="same-input",
                                   task_input="delegate", status="completed", result=output)
    before = initial.load_worker_checkpoint(task, "worker", call_index=0)
    initial.close()

    resumed = CheckpointManager("supervisor", checkpoints_root=tmp_path, run_id="run_resume")
    coordinator = CheckpointCoordinator(resumed, task, "delegate", resume=True)
    result = coordinator.prepare_worker_call("worker", "same-input", "delegate")
    assert result.should_execute is False
    assert result.cached_result == output
    assert resumed.load_worker_checkpoint(task, "worker", call_index=0) == before
    assert len(resumed.load_task_tree(task)["workers"]["worker"]) == 1
    resumed.close()
