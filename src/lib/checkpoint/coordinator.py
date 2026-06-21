"""
CheckpointCoordinator — single owner of checkpoint logic for one task run.

Usage
-----
Supervisor (in ``run()`` before executing):

    coord = CheckpointCoordinator.activate(cm, task_id, task_text)
    coord.restore(runtime_agent)
    coord.register_supervisor_step_callback(runtime_agent)
    # ... run agent ...
    coord.save_supervisor(runtime_agent, "completed", result=result)

Worker (inside its own ``run()``):

    coord = CheckpointCoordinator.current()
    if coord:
        coord.register_worker_step_callback(runtime_agent)
    # ... run agent ...

SubTaskTrackedAgent lifecycle:

    coord = CheckpointCoordinator.current()
    cached = coord.check_worker_skip(agent_name, input_hash) if coord else None
    call_index = coord.record_worker_start(...) if coord else 0
    try:
        result = callable_fn(...)
        if coord: coord.record_worker_success(...)
    except Exception:
        if coord: coord.record_worker_failure(...)
        raise
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import replace
import threading
from typing import Any, Optional

from src.lib.context_engine import (
    ContextEngine,
    ContextEngineConfig,
    clear_current_context_engine,
    set_current_context_engine,
)
from src.lib.logging import get_logger
from src.lib.heartbeat.worker_heartbeat import WorkerHeartbeat


_logger = get_logger(__name__)

# Single ContextVar — replaces the previous two (_current_checkpoint_manager
# and _step_checkpoint_cb) in base_agent.py.
_current_coordinator: ContextVar[Optional["CheckpointCoordinator"]] = ContextVar(
    "_current_coordinator", default=None
)
# Stores a heartbeat writer set by runner.py before supervisor.run(); consumed by activate().
_pending_supervisor_heartbeat: ContextVar[Any] = ContextVar("_pending_supervisor_heartbeat", default=None)
# Stores a file history manager set by runner.py before supervisor.run(); consumed by activate().
_pending_file_history: ContextVar[Any] = ContextVar("_pending_file_history", default=None)
_active_coordinator_lock = threading.Lock()
_active_coordinator: Optional["CheckpointCoordinator"] = None


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
        # Set after register_supervisor_step_callback(); workers inherit it.
        self._step_cb: Any = None
        # Worker heartbeat writers — one per worker_name.
        self._worker_heartbeats: dict[str, WorkerHeartbeat] = {}
        # Supervisor heartbeat writer (set via set_supervisor_heartbeat).
        self._supervisor_heartbeat: Any = None
        # Pending worker runtime agents waiting for call_index after record_worker_start().
        # Maps agent_name -> runtime_agent (inner CodeAgentV2 / ToolCallingAgentV2).
        self._pending_worker_runtime: dict[str, Any] = {}
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

    # ── ContextVar lifecycle ─────────────────────────────────────────

    @classmethod
    def activate(
        cls,
        checkpoint_manager: Any,
        task_id: str,
        task_text: str,
        *,
        resume: bool = False,
    ) -> "CheckpointCoordinator":
        """Create and store a new coordinator for this task.  Called by supervisor."""
        global _active_coordinator
        coord = cls(checkpoint_manager, task_id, task_text, resume=resume)
        _current_coordinator.set(coord)
        with _active_coordinator_lock:
            _active_coordinator = coord
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
        coord._activate_context_engine()
        return coord

    @staticmethod
    def current() -> Optional["CheckpointCoordinator"]:
        """Return the coordinator inherited from the current context (may be None)."""
        coord = _current_coordinator.get()
        if coord is not None:
            return coord
        with _active_coordinator_lock:
            return _active_coordinator

    @staticmethod
    def deactivate(coord: Optional["CheckpointCoordinator"] = None) -> None:
        """Clear the active coordinator after a supervisor run finishes."""
        global _active_coordinator
        if coord is not None:
            clear_current_context_engine(coord._context_engine)
        elif _active_coordinator is not None:
            clear_current_context_engine(_active_coordinator._context_engine)
        else:
            clear_current_context_engine(None)
        if _current_coordinator.get() is coord or coord is None:
            _current_coordinator.set(None)
        with _active_coordinator_lock:
            if coord is None or _active_coordinator is coord:
                _active_coordinator = None

    def _activate_context_engine(self) -> None:
        config = ContextEngineConfig.from_runtime()
        if config.store.ttl_seconds is None:
            from src.lib.config import C

            ttl = C.get_nested("checkpoint", "max_resume_age", default=None)
            if ttl is not None:
                config = replace(config, store=replace(config.store, ttl_seconds=int(ttl)))
        self._context_engine = ContextEngine(
            self._cm.context_store_dir(self._task_id),
            config=config,
        )
        set_current_context_engine(self._context_engine)

    # ── Supervisor ops ───────────────────────────────────────────────

    def restore(self, runtime_agent: Any) -> None:
        """Inject saved memory steps into *runtime_agent* for resumption.

        Uses the full conversation recovery pipeline (ported from the
        reference implementation) to filter out unresolved tool uses,
        orphaned thinking steps, and empty steps before injecting them
        into the agent's memory.
        """
        try:
            from src.lib.checkpoint.serializer import CheckpointSerializer
            from src.lib.checkpoint.conversation_recovery import prepare_steps_for_resume

            sup_ckpt = self._cm.load_supervisor_checkpoint(self._task_id)
            if sup_ckpt is None:
                return

            steps = CheckpointSerializer.deserialize_memory_steps(
                sup_ckpt.get("memory_steps", [])
            )

            # Full pipeline: filter bad steps + detect interruption.
            steps, interruption = prepare_steps_for_resume(steps)
            self._interruption_state = interruption

            runtime_agent.memory.steps = steps
            _logger.info(
                "Restored %d memory steps from checkpoint %s (interruption=%s)",
                len(steps), self._task_id, interruption.kind,
            )
        except Exception as exc:
            _logger.warning("Failed to restore checkpoint: %s", exc)

    def register_supervisor_step_callback(self, runtime_agent: Any) -> None:
        """Register a step callback that saves a checkpoint after every ActionStep.

        Also stores the callback on this coordinator so worker agents can
        inherit it via ``register_worker_step_callback()``.
        """
        from smolagents.memory import ActionStep

        inner_agent = getattr(runtime_agent, "_agent", runtime_agent)
        cb_registry = getattr(inner_agent, "step_callbacks", None)
        if cb_registry is None:
            return

        def _on_step_complete(memory_step, **kwargs):
            try:
                self.save_supervisor(runtime_agent, "running")
                if self._supervisor_heartbeat is not None:
                    self._supervisor_heartbeat.update_step(len(runtime_agent.memory.steps))
                # Create post-step file history snapshot.
                if self._file_history is not None:
                    try:
                        step_num = len(runtime_agent.memory.steps)
                        self._file_history.make_post_step_snapshot(step_num)
                    except Exception as fh_exc:
                        _logger.debug("file_history snapshot failed: %s", fh_exc)
            except Exception as exc:
                _logger.warning("step_checkpoint_callback failed: %s", exc, exc_info=True)

        cb_registry.register(ActionStep, _on_step_complete)
        # Store for workers to inherit.
        self._step_cb = _on_step_complete

    def register_file_history_hook(self, hook_manager: Any) -> None:
        """Register file-history pre-edit backups on an agent's hook manager."""
        if self._file_history is None or hook_manager is None:
            return
        if getattr(hook_manager, "_agentloom_file_history_hook_registered", False):
            return
        try:
            from src.lib.checkpoint.file_history_hook import FileHistoryHook
            from src.lib.smolagents.hooks.types import HookEvent

            hook_manager.register_hook(
                HookEvent.PRE_TOOL_USE,
                "*",
                FileHistoryHook(self._file_history),
                source="file_history",
                allow_duplicates=False,
            )
            setattr(hook_manager, "_agentloom_file_history_hook_registered", True)
        except Exception as exc:
            _logger.warning("Failed to register file history hook: %s", exc)

    def save_supervisor(
        self,
        runtime_agent: Any,
        status: str,
        *,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Save supervisor checkpoint + update task_tree status."""
        try:
            self._cm.save_supervisor_checkpoint(
                self._task_id,
                memory_steps=list(runtime_agent.memory.steps),
                task_text=self._task_text,
                status=status,
                config_snapshot=getattr(runtime_agent, "_config", None),
                result=result,
                error=error,
                context_store=self._context_engine.stats_snapshot() if self._context_engine else None,
            )
            self._cm.record_task_status_changed(
                self._task_id,
                status,
                result=result,
                error=error,
            )

            _logger.info(
                "Checkpoint saved [%s] task_id=%s steps=%d",
                status,
                self._task_id,
                len(runtime_agent.memory.steps),
            )
        except Exception as exc:
            _logger.error("Failed to save checkpoint: %s", exc, exc_info=True)
            # Mark the task tree as degraded so resume can detect partial saves.
            try:
                self._cm.update_task_tree(
                    self._task_id,
                    lambda t: {**t, "checkpoint_degraded": True},
                )
            except Exception:
                pass  # Disk may be full; nothing more we can do.

    # ── Worker ops ───────────────────────────────────────────────────

    def register_worker_step_callback(self, runtime_agent: Any, agent_name: str = "") -> None:
        """Register the inherited supervisor step callback on a worker's runtime agent.

        This is the clean replacement for the previous ``elif checkpoint_manager is None``
        block in ``_execute_agent()``.

        Also stores *runtime_agent* keyed by *agent_name* so that
        ``record_worker_start()`` can later register the dedicated worker step
        tracker (which needs the real call_index, only available after start).
        """
        if self._step_cb is None:
            return
        try:
            from smolagents.memory import ActionStep

            inner = getattr(runtime_agent, '_agent', runtime_agent)
            cb_reg = getattr(inner, 'step_callbacks', None)
            if cb_reg is not None:
                cb_reg.register(ActionStep, self._step_cb)

            # Store for later use in record_worker_start().
            if agent_name:
                self._pending_worker_runtime[agent_name] = inner
        except Exception as exc:
            _logger.warning("Failed to register worker step callback: %s", exc)

    def register_worker_step_tracker(
        self,
        runtime_agent: Any,
        agent_name: str,
        call_index: int,
        *,
        input_hash: str = "",
        task_input: str = "",
    ) -> None:
        """Register a dedicated step-counter callback for a specific worker call.

        Called from ``SubTaskTrackedAgent._execute_with_lifecycle`` **after**
        ``record_worker_start()`` so that ``agent_name`` and ``call_index`` are
        already known.  This updates the worker heartbeat ``step`` field on
        every ActionStep so the dashboard reflects real-time progress.
        """
        try:
            from smolagents.memory import ActionStep

            inner = getattr(runtime_agent, '_agent', runtime_agent)
            cb_reg = getattr(inner, 'step_callbacks', None)
            if cb_reg is None:
                return

            def _on_worker_step(memory_step, **kwargs):
                try:
                    self._cm.save_worker_checkpoint(
                        self._task_id,
                        agent_name,
                        call_index=call_index,
                        input_hash=input_hash,
                        memory_steps=list(inner.memory.steps),
                        task_input=str(task_input),
                        status="running",
                    )
                    whb = self._worker_heartbeats.get(agent_name)
                    if whb is not None:
                        whb.update_call_step(
                            call_index,
                            len(inner.memory.steps),
                        )
                except Exception as exc:
                    _logger.debug("Worker step tracker failed: %s", exc)

            cb_reg.register(ActionStep, _on_worker_step)
        except Exception as exc:
            _logger.warning("Failed to register worker step tracker: %s", exc)

    def check_worker_skip(
        self, agent_name: str, input_hash: str
    ) -> Optional[str]:
        """Return cached result if this worker call already completed, else None."""
        try:
            tree = self._cm.load_task_tree(self._task_id)
            if tree:
                calls = tree.get("workers", {}).get(agent_name)
                if isinstance(calls, list):
                    for entry in calls:
                        if (
                            entry.get("input_hash") == input_hash
                            and entry.get("status") == "completed"
                            and entry.get("result") is not None
                        ):
                            return entry["result"]
        except Exception:
            pass
        return None

    def record_worker_start(
        self, agent_name: str, input_hash: str, task_input: str
    ) -> int:
        """Record a worker call start in the task_tree.  Returns call_index."""
        call_index = 0
        try:
            call_index = self._cm.record_worker_started(
                self._task_id,
                agent_name,
                input_hash=input_hash,
                task_input=str(task_input),
                reuse_incomplete=self._resume,
            )
        except Exception:
            pass

        # ── Worker heartbeat: register call ──
        try:
            whb = self._worker_heartbeats.get(agent_name)
            if whb is None:
                hb_path = (
                    self._cm._task_dir(self._task_id)
                    / "workers" / agent_name / "heartbeat.json"
                )
                whb = WorkerHeartbeat(
                    path=hb_path, agent_name=agent_name,
                )
                whb.start()
                self._worker_heartbeats[agent_name] = whb
            whb.register_call(call_index)
        except Exception as exc:
            _logger.debug("Worker heartbeat register failed: %s", exc)

        # ── Worker step tracker: register dedicated callback now that call_index is known ──
        try:
            inner = self._pending_worker_runtime.pop(agent_name, None)
            if inner is not None:
                self.register_worker_step_tracker(
                    inner,
                    agent_name,
                    call_index,
                    input_hash=input_hash,
                    task_input=str(task_input),
                )
        except Exception as exc:
            _logger.debug("Worker step tracker register failed: %s", exc)

        return call_index

    def restore_worker(self, runtime_agent: Any, agent_name: str, call_index: int) -> bool:
        """Inject saved worker memory for an incomplete resumed worker call."""
        try:
            if not self._resume:
                return False
            from src.lib.checkpoint.serializer import CheckpointSerializer
            from src.lib.checkpoint.conversation_recovery import prepare_steps_for_resume

            worker_ckpt = self._cm.load_worker_checkpoint(
                self._task_id,
                agent_name,
                call_index=call_index,
            )
            if not worker_ckpt or worker_ckpt.get("status") == "completed":
                return False
            raw_steps = worker_ckpt.get("memory_steps") or []
            if not raw_steps:
                return False
            steps = CheckpointSerializer.deserialize_memory_steps(raw_steps)
            steps, interruption = prepare_steps_for_resume(steps)
            inner = getattr(runtime_agent, "_agent", runtime_agent)
            inner.memory.steps = steps
            _logger.info(
                "Restored worker %s #%d with %d steps (interruption=%s)",
                agent_name,
                call_index,
                len(steps),
                interruption.kind,
            )
            return True
        except Exception as exc:
            _logger.warning("Failed to restore worker checkpoint: %s", exc)
            return False

    def record_worker_success(
        self,
        agent_name: str,
        call_index: int,
        input_hash: str,
        task_input: str,
        result: Optional[str],
        worker_mem: Any,
    ) -> None:
        """Record successful worker completion."""
        try:
            full_result = str(result) if result else None
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
            self._cm.save_worker_checkpoint(
                self._task_id,
                agent_name,
                call_index=call_index,
                input_hash=input_hash,
                memory_steps=worker_mem,
                task_input=str(task_input),
                status="completed",
                result=stored_result,
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
            # ── Worker heartbeat: mark completed ──
            self._update_worker_heartbeat(agent_name, call_index, "completed")
        except Exception:
            pass

    def record_worker_failure(
        self,
        agent_name: str,
        call_index: int,
        input_hash: str,
        task_input: str,
        error: str,
        worker_mem: Any,
    ) -> None:
        """Record worker failure."""
        try:
            self._cm.save_worker_checkpoint(
                self._task_id,
                agent_name,
                call_index=call_index,
                input_hash=input_hash,
                memory_steps=worker_mem,
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
        worker_mem: Any,
    ) -> None:
        """Record an interrupted worker so resume can continue the same call."""
        try:
            self._cm.save_worker_checkpoint(
                self._task_id,
                agent_name,
                call_index=call_index,
                input_hash=input_hash,
                memory_steps=worker_mem,
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

    def get_worker_heartbeat(self, agent_name: str) -> "WorkerHeartbeat | None":
        """Return the heartbeat writer for *agent_name* (if any)."""
        return self._worker_heartbeats.get(agent_name)

    def stop_all_worker_heartbeats(self) -> None:
        """Stop all worker heartbeat writers.  Called by runner on shutdown."""
        for whb in self._worker_heartbeats.values():
            try:
                whb.stop()
            except Exception:
                pass
        self._worker_heartbeats.clear()
