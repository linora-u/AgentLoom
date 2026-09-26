"""
CheckpointCoordinator — single owner of checkpoint logic for one task run.

Usage
-----
Supervisor (in ``run()`` before executing):

    coord = CheckpointCoordinator.activate(cm, task_id, task_text)
    checkpoint = coord.load_runtime_checkpoint()
    # ... run AgentRuntime with checkpoint and checkpoint sink ...
    coord.save_runtime_checkpoint(result.checkpoint, "completed")

Worker (inside ``SubTaskTrackedAgent.run()``):

    coord = CheckpointCoordinator.current()
    preparation = coord.prepare_worker_call(...) if coord else None
    if preparation is not None and not preparation.should_execute:
        return preparation.cached_result
    checkpoint = coord.load_worker_runtime_checkpoint(...)
    sink = coord.worker_checkpoint_sink(...)
    result = runtime.run(request_with_checkpoint_and_sink)
    coord.record_worker_success(..., result.checkpoint)
"""

from __future__ import annotations

import threading
from contextvars import ContextVar
from dataclasses import replace
from typing import Any
from uuid import uuid4

from agentloom.execution.agent_runtime import (
    JSONValue,
    RuntimeCheckpointEnvelope,
    copy_json_value,
)
from agentloom.execution.context_engine import (
    ContextEngine,
    ContextEngineConfig,
    clear_current_context_engine,
    set_current_context_engine,
)
from agentloom.execution.heartbeat.worker_heartbeat import WorkerHeartbeat
from agentloom.execution.logging import get_logger

_logger = get_logger(__name__)
_TERMINAL_CHECKPOINT_STATUSES = frozenset(
    {"completed", "failed", "interrupted"}
)

# Single ContextVar — replaces the previous two (_current_checkpoint_manager
# and _step_checkpoint_cb) in base_agent.py.
_current_coordinator: ContextVar[CheckpointCoordinator | None] = ContextVar(
    "_current_coordinator", default=None
)
# Stores a heartbeat writer set by runner.py before supervisor.run(); consumed by activate().
_pending_supervisor_heartbeat: ContextVar[Any] = ContextVar("_pending_supervisor_heartbeat", default=None)
# Stores a file history manager set by runner.py before supervisor.run(); consumed by activate().
_pending_file_history: ContextVar[Any] = ContextVar("_pending_file_history", default=None)


class CheckpointCoordinator:
    """Owns all checkpoint operations for a single supervisor task run.

    A coordinator is created by the supervisor via ``activate()`` and stored
    in a ``ContextVar`` so worker agents can inherit it via ``current()``
    without needing an explicit parameter.

    Workers do NOT own the coordinator — they borrow it to register callbacks
    and record their progress into the supervisor's task_tree.
    """

    def __init__(
        self,
        checkpoint_manager: Any,
        task_id: str,
        task_text: str,
        *,
        resume: bool = False,
    ) -> None:
        self._cm = checkpoint_manager
        self._task_id = task_id
        self._task_text = task_text
        self._resume = resume
        self._task_item_next_index = 0
        self._task_item_commit_id: str | None = None
        self._goal_phase_commit: dict[str, Any] | None = None
        self._goal_active_phase: dict[str, Any] | None = None
        # Worker heartbeat writers — one per worker_name.
        self._worker_heartbeats: dict[str, WorkerHeartbeat] = {}
        self._worker_heartbeats_lock = threading.Lock()
        # Supervisor heartbeat writer (set via set_supervisor_heartbeat).
        self._supervisor_heartbeat: Any = None
        # File history manager (set by runner.py after creation).
        self._file_history: Any = None
        self._context_engine: ContextEngine | None = None

    # ── Properties ──────────────────────────────────────────────────

    @property
    def checkpoint_manager(self) -> Any:
        return self._cm

    @property
    def task_id(self) -> str:
        return self._task_id

    @property
    def task_text(self) -> str:
        return self._task_text

    def _with_storage_identity(
        self,
        checkpoint: RuntimeCheckpointEnvelope,
    ) -> RuntimeCheckpointEnvelope:
        run_id = self._cm.run_id
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("checkpoint manager has no current run_id")
        if checkpoint.task_id not in {None, self._task_id}:
            raise ValueError("runtime checkpoint task_id does not match coordinator")
        if checkpoint.run_id not in {None, run_id}:
            raise ValueError("runtime checkpoint run_id does not match coordinator")
        return replace(
            checkpoint,
            task_id=self._task_id,
            run_id=run_id,
        )

    def load_goal(self) -> dict[str, Any] | None:
        """Load the root task's durable Goal state."""

        return self._cm.load_goal(self._task_id)

    def save_goal(self, state: Any) -> dict[str, Any]:
        """Atomically replace the root task's durable Goal state."""

        return self._cm.save_goal(self._task_id, state)

    # ── ContextVar lifecycle ─────────────────────────────────────────

    @classmethod
    def activate(
        cls,
        checkpoint_manager: Any,
        task_id: str,
        task_text: str,
        *,
        resume: bool = False,
        effective_config: dict[str, Any] | None = None,
    ) -> CheckpointCoordinator:
        """Create and store a new coordinator for this task.  Called by supervisor."""
        coord = cls(checkpoint_manager, task_id, task_text, resume=resume)
        _current_coordinator.set(coord)
        # Auto-inject any heartbeat writer set by runner.py before supervisor.run().
        pending_hb = _pending_supervisor_heartbeat.get()
        if pending_hb is not None:
            coord._supervisor_heartbeat = pending_hb
            _pending_supervisor_heartbeat.set(None)
        # Auto-inject any file history manager set by runner.py.
        pending_fh = _pending_file_history.get()
        if pending_fh is not None:
            coord._file_history = pending_fh
            _pending_file_history.set(None)
        coord._activate_context_engine(effective_config)
        return coord

    @staticmethod
    def current() -> CheckpointCoordinator | None:
        """Return the coordinator inherited from the current context (may be None)."""
        return _current_coordinator.get()

    @staticmethod
    def deactivate(coord: CheckpointCoordinator | None = None) -> None:
        """Clear the active coordinator after a supervisor run finishes."""
        current = _current_coordinator.get()
        target = coord or current
        if target is not None:
            target.stop_all_worker_heartbeats()
            if target._context_engine is not None:
                target._context_engine.close()
        clear_current_context_engine(
            target._context_engine if target is not None else None
        )
        if current is coord or coord is None:
            _current_coordinator.set(None)

    def _activate_context_engine(
        self,
        effective_config: dict[str, Any] | None,
    ) -> None:
        if effective_config is None:
            config = ContextEngineConfig.from_runtime()
            from agentloom.config import C

            checkpoint_config = C.get("checkpoint", {})
        else:
            config = ContextEngineConfig.from_mapping(
                effective_config.get("context_engine", {})
            )
            checkpoint_config = effective_config.get("checkpoint", {})
        if config.store.ttl_seconds is None:
            ttl = (
                checkpoint_config.get("max_resume_age")
                if isinstance(checkpoint_config, dict)
                else None
            )
            if ttl is not None:
                config = replace(config, store=replace(config.store, ttl_seconds=int(ttl)))
        self._context_engine = ContextEngine(
            self._cm.context_store_dir(self._task_id),
            config=config,
            storage=self._cm.directory_storage(
                self._task_id,
                self._cm.context_store_dir(self._task_id),
            ),
        )
        set_current_context_engine(self._context_engine)

    # ── Supervisor ops ───────────────────────────────────────────────

    @staticmethod
    def _sequence_fields(
        checkpoint: dict[str, Any] | None,
    ) -> tuple[int, str | None, dict[str, Any] | None, dict[str, Any] | None]:
        next_index = checkpoint.get("task_item_next_index", 0) if checkpoint else 0
        if type(next_index) is not int or next_index < 0:
            raise ValueError("Corrupt task sequence position in checkpoint")
        commit_id = checkpoint.get("task_item_commit_id") if checkpoint else None
        if commit_id is not None and (not isinstance(commit_id, str) or not commit_id):
            raise ValueError("Corrupt task item commit identity in checkpoint")
        commit = checkpoint.get("goal_phase_commit") if checkpoint else None
        active = checkpoint.get("goal_active_phase") if checkpoint else None
        if commit is not None and not isinstance(commit, dict):
            raise ValueError("Corrupt Goal phase commit in checkpoint")
        if active is not None and not isinstance(active, dict):
            raise ValueError("Corrupt active Goal phase in checkpoint")
        return next_index, commit_id, commit, active

    @classmethod
    def validate_goal_resume_boundary(
        cls, checkpoint_manager: Any, task_id: str | None, goal: dict[str, Any],
    ) -> None:
        """Reject Goal state that cannot be paired with a committed Runtime boundary."""

        checkpoint = checkpoint_manager.load_supervisor_checkpoint(task_id)
        next_index, _, commit, active = cls._sequence_fields(checkpoint)
        phase = goal["phase_index"]
        expected_index = phase + (1 if goal["status"] == "complete" else 0)
        if next_index != expected_index:
            raise ValueError("Cannot safely resume Goal: phase and Runtime checkpoint disagree")
        if expected_index > 0:
            expected_phase = phase if goal["status"] == "complete" else phase - 1
            if (
                commit is None
                or commit.get("status") != "complete"
                or commit.get("phase_index") != expected_phase
                or (goal["status"] == "complete" and commit.get("goal_id") != goal["goal_id"])
            ):
                raise ValueError("Cannot safely resume Goal: completion has no committed Runtime checkpoint")
        if goal["status"] == "active" and goal["goal_started"]:
            if (
                active is None
                or active.get("goal_id") != goal["goal_id"]
                or active.get("phase_index") != phase
            ):
                raise ValueError("Cannot safely resume Goal: active phase has no Runtime checkpoint")

    def load_runtime_checkpoint(self) -> RuntimeCheckpointEnvelope | None:
        """Load the selected runtime's opaque supervisor checkpoint."""

        checkpoint = self._cm.load_supervisor_checkpoint(self._task_id)
        if checkpoint is None:
            return None
        (
            self._task_item_next_index,
            self._task_item_commit_id,
            self._goal_phase_commit,
            self._goal_active_phase,
        ) = self._sequence_fields(checkpoint)
        raw = checkpoint.get("runtime_checkpoint")
        if not isinstance(raw, dict):
            if (
                self._task_item_next_index > 0
                or self._task_item_commit_id is not None
                or self._goal_phase_commit is not None
                or (
                    self._goal_active_phase is not None
                    and self._goal_active_phase.get("goal_started") is True
                )
            ):
                raise ValueError(
                    "Cannot safely resume: committed task position has no Runtime checkpoint"
                )
            return None
        return RuntimeCheckpointEnvelope.from_dict(raw)

    def load_task_item_next_index(self) -> int:
        """Return the next committed YAML user turn for this root Task."""

        checkpoint = self._cm.load_supervisor_checkpoint(self._task_id)
        (
            self._task_item_next_index,
            self._task_item_commit_id,
            self._goal_phase_commit,
            self._goal_active_phase,
        ) = self._sequence_fields(checkpoint)
        return self._task_item_next_index

    def committed_task_item(self) -> tuple[int, str, JSONValue] | None:
        """Return the latest committed root item for trace reconciliation."""

        if self._task_item_next_index == 0 or self._task_item_commit_id is None:
            return None
        return (
            self._task_item_next_index - 1,
            self._task_item_commit_id,
            self.load_task_item_output(),
        )

    def load_task_item_output(self) -> JSONValue:
        checkpoint = self._cm.load_supervisor_checkpoint(self._task_id)
        return copy_json_value(
            checkpoint.get("result") if checkpoint else None,
            field_name="committed task item output",
        )

    def save_runtime_checkpoint(
        self,
        checkpoint: RuntimeCheckpointEnvelope,
        status: str,
        *,
        result: JSONValue = None,
        error: str | None = None,
        require_durable: bool = False,
        task_item_next_index: int | None = None,
        goal_phase_commit: dict[str, Any] | None = None,
        goal_active_phase: dict[str, Any] | None = None,
    ) -> str | None:
        """Persist a runtime-owned state envelope without inspecting its payload."""

        try:
            checkpoint = self._with_storage_identity(checkpoint)
            next_index = (
                self._task_item_next_index
                if task_item_next_index is None
                else task_item_next_index
            )
            if isinstance(next_index, bool) or not isinstance(next_index, int) or next_index < 0:
                raise ValueError("task_item_next_index must be a non-negative integer")
            if next_index < self._task_item_next_index:
                raise ValueError("task_item_next_index cannot move backward")
            item_commit_id = (
                uuid4().hex
                if task_item_next_index is not None and next_index > self._task_item_next_index
                else self._task_item_commit_id
            )
            phase_commit = (
                self._goal_phase_commit if goal_phase_commit is None else goal_phase_commit
            )
            active_phase = (
                self._goal_active_phase if goal_active_phase is None else goal_active_phase
            )
            self._cm.save_supervisor_runtime_checkpoint(
                self._task_id,
                runtime_checkpoint=checkpoint.to_dict(),
                task_text=self._task_text,
                status=status,
                result=result,
                error=error,
                context_store=(
                    self._context_engine.stats_snapshot()
                    if self._context_engine
                    else None
                ),
                task_item_next_index=next_index,
                task_item_commit_id=item_commit_id,
                goal_phase_commit=phase_commit,
                goal_active_phase=active_phase,
            )
            self._task_item_next_index = next_index
            self._task_item_commit_id = item_commit_id
            self._goal_phase_commit = phase_commit
            self._goal_active_phase = active_phase
            self._cm.record_task_status_changed(
                self._task_id,
                status,
                result=result,
                error=error,
            )
        except Exception as exc:
            _logger.error("Failed to save runtime checkpoint: %s", exc, exc_info=True)
            try:
                self._cm.update_task_tree(
                    self._task_id,
                    lambda tree: {**tree, "checkpoint_degraded": True},
                )
            except Exception as degraded_error:
                exc.add_note(
                    "Marking checkpoint_degraded also failed: "
                    f"{type(degraded_error).__name__}: {degraded_error}"
                )
            if require_durable or status in _TERMINAL_CHECKPOINT_STATUSES:
                raise
            return None

        step_count = checkpoint.progress
        if self._supervisor_heartbeat is not None:
            try:
                self._supervisor_heartbeat.update_step(step_count)
            except Exception as exc:
                _logger.warning(
                    "Failed to update Supervisor heartbeat after checkpoint: %s",
                    exc,
                )
        if self._file_history is not None:
            try:
                self._file_history.make_post_step_snapshot(step_count)
            except Exception as exc:
                _logger.warning(
                    "Failed to snapshot file history after checkpoint: %s",
                    exc,
                )
        _logger.info(
            "Runtime checkpoint saved [%s] task_id=%s runtime=%s",
            status,
            self._task_id,
            checkpoint.runtime_id,
        )
        return item_commit_id if task_item_next_index is not None else None

    # ── Worker ops ───────────────────────────────────────────────────

    def prepare_worker_call(
        self,
        agent_name: str,
        input_hash: str,
        task_input: str,
    ) -> Any:
        """Atomically claim prior work/cache or allocate one new worker call."""
        worker_dir = self._cm.worker_dir(self._task_id, agent_name)
        preparation = self._cm.prepare_worker_call(
            self._task_id,
            agent_name,
            input_hash=input_hash,
            task_input=str(task_input),
            resume=self._resume,
        )
        if not preparation.should_execute:
            return preparation
        call_index = preparation.call_index

        # ── Worker heartbeat: register call ──
        try:
            with self._worker_heartbeats_lock:
                whb = self._worker_heartbeats.get(agent_name)
                if whb is None:
                    if not self._cm.run_id:
                        raise RuntimeError("checkpoint manager has no current run_id")
                    hb_path = worker_dir / "heartbeat.json"
                    whb = WorkerHeartbeat(
                        path=hb_path,
                        agent_name=agent_name,
                        run_id=self._cm.run_id,
                        storage=self._cm.directory_storage(
                            self._task_id,
                            hb_path.parent,
                        ),
                    )
                    self._worker_heartbeats[agent_name] = whb
                # Keep registration and terminal-state checks under the same
                # coordinator lock.  Otherwise one fast call can stop the
                # shared writer before a concurrent call has registered.
                whb.register_call(call_index)
                # ``start`` is idempotent and restarts a writer that a prior
                # sequential group of calls stopped after becoming terminal.
                whb.start()
        except Exception as exc:
            _logger.debug("Worker heartbeat register failed: %s", exc)

        return preparation

    def completed_worker_result(
        self,
        *,
        agent_name: str,
        input_hash: str,
        task_input: str,
        run_id: str,
    ) -> tuple[bool, JSONValue]:
        """Return one result proven complete in the specified original Run."""
        tree = self._cm.load_task_tree(self._task_id) or {}
        calls = (tree.get("workers") or {}).get(agent_name, [])
        if not isinstance(calls, list):
            calls = [calls]
        matches = [
            call for call in calls
            if isinstance(call, dict)
            and call.get("status") == "completed"
            and call.get("attempt_run_id") == run_id
            and call.get("input_hash") == input_hash
            and call.get("task_input") == task_input
        ]
        if len(matches) > 1:
            raise ValueError("Worker recovery evidence is ambiguous")
        if not matches:
            return False, ""
        return True, copy_json_value(
            matches[0].get("result"),
            field_name="recovered worker result",
        )

    def load_worker_runtime_checkpoint(
        self,
        agent_name: str,
        call_index: int,
    ) -> RuntimeCheckpointEnvelope | None:
        """Load one incomplete worker call's opaque runtime envelope."""

        if not self._resume:
            return None
        checkpoint = self._cm.load_worker_checkpoint(
            self._task_id,
            agent_name,
            call_index=call_index,
        )
        if not checkpoint or checkpoint.get("status") == "completed":
            return None
        raw = checkpoint.get("runtime_checkpoint")
        if not isinstance(raw, dict):
            return None
        return RuntimeCheckpointEnvelope.from_dict(raw)

    def load_completed_worker_checkpoint(
        self,
        agent_name: str,
        call_index: int,
    ) -> RuntimeCheckpointEnvelope | None:
        """Restore the conversation after a cached YAML task item."""

        checkpoint = self._cm.load_worker_checkpoint(
            self._task_id,
            agent_name,
            call_index=call_index,
        )
        if not checkpoint or checkpoint.get("status") != "completed":
            return None
        raw = checkpoint.get("runtime_checkpoint")
        if not isinstance(raw, dict):
            raise ValueError("Completed Worker item has no restorable Runtime checkpoint")
        return RuntimeCheckpointEnvelope.from_dict(raw)

    def worker_checkpoint_sink(
        self,
        agent_name: str,
        call_index: int,
        input_hash: str,
        task_input: str,
    ) -> Any:
        """Return a sink that persists runtime-owned Worker envelopes."""

        def save(checkpoint: RuntimeCheckpointEnvelope) -> None:
            checkpoint = self._with_storage_identity(checkpoint)
            self._cm.save_worker_runtime_checkpoint(
                self._task_id,
                agent_name,
                call_index=call_index,
                input_hash=input_hash,
                runtime_checkpoint=checkpoint.to_dict(),
                task_input=str(task_input),
                status="running",
            )
            heartbeat = self.get_worker_heartbeat(agent_name)
            if heartbeat is not None:
                heartbeat.update_call_step(call_index, checkpoint.progress)

        return save

    def record_worker_success(
        self,
        agent_name: str,
        call_index: int,
        input_hash: str,
        task_input: str,
        result: JSONValue,
        runtime_checkpoint: RuntimeCheckpointEnvelope | None,
    ) -> None:
        """Record successful worker completion."""
        self._cm.record_worker_finished(
            self._task_id,
            agent_name,
            call_index=call_index,
            input_hash=input_hash,
            task_input=str(task_input),
            status="completed",
            result=result,
        )
        self._cm.save_worker_runtime_checkpoint(
            self._task_id,
            agent_name,
            call_index=call_index,
            input_hash=input_hash,
            runtime_checkpoint=(
                runtime_checkpoint.to_dict()
                if runtime_checkpoint is not None
                else None
            ),
            task_input=str(task_input),
            status="completed",
            result=result,
        )
        # ── Worker heartbeat: mark completed ──
        self._update_worker_heartbeat(agent_name, call_index, "completed")

    def record_worker_failure(
        self,
        agent_name: str,
        call_index: int,
        input_hash: str,
        task_input: str,
        error: str,
        runtime_checkpoint: RuntimeCheckpointEnvelope | None,
    ) -> None:
        """Record worker failure."""
        try:
            self._cm.save_worker_runtime_checkpoint(
                self._task_id,
                agent_name,
                call_index=call_index,
                input_hash=input_hash,
                runtime_checkpoint=(
                    runtime_checkpoint.to_dict()
                    if runtime_checkpoint is not None
                    else None
                ),
                task_input=task_input[:500],
                status="failed",
                error=error,
            )
            self._cm.record_worker_finished(
                self._task_id,
                agent_name,
                call_index=call_index,
                input_hash=input_hash,
                task_input=str(task_input),
                status="failed",
                error=error[:300],
            )
            # ── Worker heartbeat: mark failed ──
            self._update_worker_heartbeat(agent_name, call_index, "failed")
        except Exception:
            pass

    def record_worker_interrupted(
        self,
        agent_name: str,
        call_index: int,
        input_hash: str,
        task_input: str,
        runtime_checkpoint: RuntimeCheckpointEnvelope | None,
    ) -> None:
        """Record an interrupted worker so resume can continue the same call."""
        try:
            self._cm.save_worker_runtime_checkpoint(
                self._task_id,
                agent_name,
                call_index=call_index,
                input_hash=input_hash,
                runtime_checkpoint=(
                    runtime_checkpoint.to_dict()
                    if runtime_checkpoint is not None
                    else None
                ),
                task_input=str(task_input),
                status="interrupted",
            )
            self._cm.record_worker_finished(
                self._task_id,
                agent_name,
                call_index=call_index,
                input_hash=input_hash,
                task_input=str(task_input),
                status="interrupted",
            )
            self._update_worker_heartbeat(agent_name, call_index, "interrupted")
        except Exception:
            pass


    # ── Worker heartbeat helpers ─────────────────────────────────────

    def _update_worker_heartbeat(
        self, agent_name: str, call_index: int, status: str
    ) -> None:
        """Update a worker call's heartbeat status; stop writer if all done."""
        try:
            with self._worker_heartbeats_lock:
                whb = self._worker_heartbeats.get(agent_name)
                if whb is None:
                    return
                whb.update_call_status(call_index, status)
                if whb.all_calls_terminal():
                    whb.stop()
        except Exception as exc:
            _logger.debug("Worker heartbeat update failed: %s", exc)

    def set_supervisor_heartbeat(self, heartbeat: Any) -> None:
        """Inject the supervisor heartbeat writer so step callbacks can update it."""
        self._supervisor_heartbeat = heartbeat

    @classmethod
    def set_pending_heartbeat(cls, heartbeat: Any) -> None:
        """Store heartbeat before supervisor.run(); activate() will pick it up."""
        _pending_supervisor_heartbeat.set(heartbeat)

    @classmethod
    def set_pending_file_history(cls, file_history: Any) -> None:
        """Store file history manager before supervisor.run(); activate() will pick it up."""
        _pending_file_history.set(file_history)

    def get_worker_heartbeat(self, agent_name: str) -> WorkerHeartbeat | None:
        """Return the heartbeat writer for *agent_name* (if any)."""
        with self._worker_heartbeats_lock:
            return self._worker_heartbeats.get(agent_name)

    def stop_all_worker_heartbeats(self) -> None:
        """Stop all worker heartbeat writers.  Called by runner on shutdown."""
        with self._worker_heartbeats_lock:
            heartbeats = list(self._worker_heartbeats.values())
            self._worker_heartbeats.clear()
        for whb in heartbeats:
            try:
                whb.stop()
            except Exception:
                pass
            try:
                whb.close()
            except Exception:
                pass
