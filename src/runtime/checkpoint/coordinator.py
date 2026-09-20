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

from agentloom.runtime.agent_runtime import RuntimeCheckpointEnvelope
from agentloom.runtime.context_engine import (
    ContextEngine,
    ContextEngineConfig,
    clear_current_context_engine,
    set_current_context_engine,
)
from agentloom.runtime.heartbeat.worker_heartbeat import WorkerHeartbeat
from agentloom.runtime.logging import get_logger

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

    def load_todos(self, agent_path: str) -> dict[str, Any]:
        """Load the active task's Todo snapshot for one Agent scope."""

        return self._cm.load_todos(self._task_id, agent_path)

    def replace_todos(self, agent_path: str, items: Any) -> dict[str, Any]:
        """Atomically replace the active task's Todo snapshot for one Agent."""

        return self._cm.replace_todos(self._task_id, agent_path, items)

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
            from agentloom.configuration import C

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

    def load_runtime_checkpoint(self) -> RuntimeCheckpointEnvelope | None:
        """Load the selected runtime's opaque supervisor checkpoint."""

        checkpoint = self._cm.load_supervisor_checkpoint(self._task_id)
        if checkpoint is None:
            return None
        raw = checkpoint.get("runtime_checkpoint")
        if not isinstance(raw, dict):
            return None
        return RuntimeCheckpointEnvelope.from_dict(raw)

    def save_runtime_checkpoint(
        self,
        checkpoint: RuntimeCheckpointEnvelope,
        status: str,
        *,
        result: str | None = None,
        error: str | None = None,
        require_durable: bool = False,
    ) -> None:
        """Persist a runtime-owned state envelope without inspecting its payload."""

        try:
            checkpoint = self._with_storage_identity(checkpoint)
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
            )
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
            return

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
    ) -> tuple[bool, str]:
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
            and (
                call.get("result") is None
                or isinstance(call.get("result"), str)
            )
        ]
        if len(matches) > 1:
            raise ValueError("Worker recovery evidence is ambiguous")
        if not matches:
            return False, ""
        return True, str(matches[0].get("result") or "")

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
        result: Any,
        runtime_checkpoint: RuntimeCheckpointEnvelope | None,
    ) -> None:
        """Record successful worker completion."""
        full_result = None if result is None else str(result)
        stored_result = full_result
        if full_result and self._context_engine is not None:
            stored_result = (
                self._context_engine.compress_tool_result(
                    full_result,
                    tool_name=agent_name,
                    source=f"worker_result:{agent_name}",
                )
                or full_result
            )
        self._cm.record_worker_finished(
            self._task_id,
            agent_name,
            call_index=call_index,
            input_hash=input_hash,
            task_input=str(task_input),
            status="completed",
            result=stored_result,
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
            result=stored_result,
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
