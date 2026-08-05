"""Single owner for one Application Run's terminal lifecycle.

The runner allocates resources and the Supervisor performs model work, but
neither owns terminal checkpoint state independently.  Both report into this
module so checkpoint state, Goal projection, and the final Run outcome are
settled by one lifecycle object.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from src.application_run import RunPhase
from src.lib.checkpoint import CheckpointManager
from src.lib.goal import GoalBudgetLimitedError

_RUN_ARTIFACT_COPY_CHUNK_BYTES = 1024 * 1024

ApplicationRunOutcome = Literal[
    "completed",
    "budget_limited",
    "interrupted",
    "failed",
]


@dataclass(slots=True)
class _AgentInvocation:
    coordinator: Any | None
    runtime_agent: Any | None
    result: str | None
    error: BaseException | None


@dataclass(slots=True)
class ApplicationRunFinalization:
    """Durable collaborators for one ordered terminal transaction."""

    runtime_context: Any
    checkpoint_manager: CheckpointManager | None
    task_id: str
    event_start_offset: int | None
    manifest_updates: dict[str, Any]
    cleanup_on_success: bool
    log: Any
    task_tree_cleanup_max_bytes: int = 1024 * 1024


@dataclass(slots=True)
class ApplicationRunResources:
    """Resources whose lifetimes end after terminal persistence."""

    supervisor: Any | None
    heartbeat: Any | None
    file_history: Any | None
    log: Any
    task_lease: Any | None
    checkpoint_manager: CheckpointManager | None


class ApplicationRunLifecycle:
    """Own terminal state for exactly one top-level Application Run.

    Agent code reports the runtime snapshot here but does not persist a
    terminal checkpoint itself.  The runner later commits the same outcome to
    checkpoint evidence, Run artifacts, and the Run manifest. Public API events
    remain an adapter concern after this object has settled the outcome.
    """

    def __init__(self) -> None:
        self._invocation: _AgentInvocation | None = None
        self._goal: dict[str, object] | None = None
        self._checkpoint_outcome: ApplicationRunOutcome | None = None
        self._phase: RunPhase = "initialization"
        self._outcome: ApplicationRunOutcome | None = None
        self._result = ""
        self._error: BaseException | None = None
        self._checkpoint_deletion_started = False
        self._resumable = False

    @property
    def phase(self) -> RunPhase:
        return self._phase

    @property
    def outcome(self) -> ApplicationRunOutcome:
        if self._outcome is None:
            raise RuntimeError("Application Run has no terminal outcome")
        return self._outcome

    @property
    def result(self) -> str:
        return self._result

    @property
    def error(self) -> BaseException | None:
        return self._error

    @property
    def error_message(self) -> str | None:
        if self._error is None:
            return None
        message = str(self._error)
        if message:
            return message
        if isinstance(self._error, KeyboardInterrupt):
            return "run interrupted"
        return type(self._error).__name__

    @property
    def resumable(self) -> bool:
        return self._resumable

    @property
    def goal(self) -> dict[str, object] | None:
        return None if self._goal is None else dict(self._goal)

    def report_agent_invocation(
        self,
        *,
        coordinator: Any | None,
        runtime_agent: Any | None,
        result: object | None,
        error: BaseException | None,
        goal: Mapping[str, object] | None,
    ) -> None:
        """Capture the root Agent's final in-memory state without committing it."""

        if self._invocation is not None:
            raise RuntimeError("Application Run Agent invocation was already reported")
        self._invocation = _AgentInvocation(
            coordinator=coordinator,
            runtime_agent=runtime_agent,
            result=None if result is None else str(result),
            error=error,
        )
        if goal is not None:
            self._goal = dict(goal)

    def settle_reported_agent_invocation(self) -> None:
        """Settle a standalone Agent invocation from its captured return state."""

        invocation = self._invocation
        if invocation is None:
            raise RuntimeError("Application Run Agent invocation was not reported")
        if invocation.error is None:
            self.complete_execution(invocation.result)
        else:
            self.fail_execution(invocation.error)

    def enter_execution(self) -> None:
        if self._phase != "initialization":
            raise RuntimeError(f"Application Run cannot enter execution from {self._phase}")
        self._phase = "execution"

    def complete_execution(self, result: object | None) -> None:
        if self._phase != "execution":
            raise RuntimeError(f"Application Run cannot complete execution from {self._phase}")
        self._result = "" if result is None else str(result)
        self._error = None
        self._outcome = "completed"
        self._phase = "finalization"

    def fail_execution(self, error: BaseException) -> None:
        if self._phase not in {"initialization", "execution"}:
            raise RuntimeError(f"Application Run cannot fail execution from {self._phase}")
        self._error = error
        self._outcome = self.outcome_for(error)

    def fail_finalization(self, error: BaseException) -> None:
        if self._phase not in {"execution", "finalization"}:
            raise RuntimeError(f"Application Run cannot fail finalization from {self._phase}")
        self._phase = "finalization"
        self._error = error
        self._outcome = self.outcome_for(error)

    def enter_cleanup(self) -> None:
        self._phase = "cleanup"

    def fail_cleanup(self, error: BaseException) -> None:
        self._phase = "cleanup"
        self._error = error
        self._outcome = self.outcome_for(error)

    @staticmethod
    def outcome_for(error: BaseException | None) -> ApplicationRunOutcome:
        if error is None:
            return "completed"
        if isinstance(error, GoalBudgetLimitedError):
            return "budget_limited"
        if isinstance(error, KeyboardInterrupt):
            return "interrupted"
        return "failed"

    def commit_checkpoint(
        self,
        *,
        checkpoint_manager: CheckpointManager | None,
        task_id: str,
    ) -> None:
        """Commit a terminal Agent checkpoint once for each settled outcome.

        A real RoleDrivenAgent reports its coordinator and runtime memory.  A
        different Supervisor implementation may only return or raise; in that
        case the Run still owns and records the task-tree terminal state.
        """

        outcome = self.outcome
        error_message = self.error_message
        result = self._result if outcome == "completed" else None
        if self._checkpoint_outcome == outcome or checkpoint_manager is None:
            return

        invocation = self._invocation
        if invocation is not None and invocation.coordinator is not None and invocation.runtime_agent is not None:
            invocation.coordinator.save_supervisor(
                invocation.runtime_agent,
                outcome,
                result=result if outcome == "completed" else None,
                error=error_message,
            )
        else:
            checkpoint_manager.record_task_status_changed(
                task_id,
                outcome,
                result=result if outcome == "completed" else None,
                error=error_message,
            )
        self._checkpoint_outcome = outcome

    def finalize_run(self, finalization: ApplicationRunFinalization) -> None:
        """Commit checkpoint, evidence, manifest, and optional success cleanup."""

        try:
            goal_snapshot = self.goal
            if goal_snapshot is not None:
                finalization.manifest_updates["goal"] = goal_snapshot
            self.commit_checkpoint(
                checkpoint_manager=finalization.checkpoint_manager,
                task_id=finalization.task_id,
            )
            if self.outcome == "interrupted":
                self._validate_resumable_checkpoint(finalization)

            if self.outcome == "completed":
                _persist_run_observability(
                    finalization.runtime_context,
                    finalization.checkpoint_manager,
                    finalization.task_id,
                    result=self.result,
                    event_start_offset=finalization.event_start_offset,
                    manifest_updates=finalization.manifest_updates,
                )
            else:
                try:
                    _persist_run_observability(
                        finalization.runtime_context,
                        finalization.checkpoint_manager,
                        finalization.task_id,
                        result=None,
                        event_start_offset=finalization.event_start_offset,
                        manifest_updates=finalization.manifest_updates,
                    )
                except Exception as exc:
                    finalization.log.warning(
                        "Failed to persist Run observability: %s",
                        exc,
                    )

            manifest_updates = {
                "status": self.outcome,
                "ended_at": datetime.now().astimezone().isoformat(),
                **finalization.manifest_updates,
            }
            if self.error_message:
                manifest_updates["error"] = self.error_message
            try:
                finalization.runtime_context.update_manifest(**manifest_updates)
            except Exception as exc:
                if self.outcome == "completed":
                    raise
                finalization.log.warning(
                    "Failed to persist terminal manifest: %s",
                    exc,
                )

            if (
                self.outcome == "completed"
                and finalization.cleanup_on_success
                and finalization.checkpoint_manager is not None
            ):
                try:
                    tree = finalization.checkpoint_manager.load_task_tree_projection(
                        finalization.task_id,
                        max_bytes=finalization.task_tree_cleanup_max_bytes,
                    )
                    if tree and tree.get("status") == "completed":
                        self._checkpoint_deletion_started = True
                        if finalization.checkpoint_manager.delete_task(
                            finalization.task_id
                        ):
                            finalization.log.info(
                                "Cleaned up checkpoint for completed task %s",
                                finalization.task_id,
                            )
                except Exception:
                    pass
        except BaseException as exc:
            self.fail_finalization(exc)
            if (
                finalization.checkpoint_manager is not None
                and not self._checkpoint_deletion_started
            ):
                if isinstance(exc, KeyboardInterrupt):
                    try:
                        finalization.runtime_context.validate_checkpoint_path(
                            require_exists=True,
                        )
                        self.commit_checkpoint(
                            checkpoint_manager=finalization.checkpoint_manager,
                            task_id=finalization.task_id,
                        )
                        self._resumable = True
                    except Exception as checkpoint_exc:
                        finalization.log.warning(
                            "Failed to reopen checkpoint after finalization interruption: %s",
                            checkpoint_exc,
                        )
                else:
                    self.commit_checkpoint(
                        checkpoint_manager=finalization.checkpoint_manager,
                        task_id=finalization.task_id,
                    )
            raise

    def persist_uncaught_failure(
        self,
        *,
        runtime_context: Any,
        manifest_initialized: bool,
        manifest_updates: Mapping[str, Any],
        error: BaseException,
    ) -> None:
        """Best-effort terminal manifest for failures outside finalization."""

        if self._phase == "cleanup":
            self.fail_cleanup(error)
        elif self._phase == "finalization":
            if self._error is not error:
                self.fail_finalization(error)
        else:
            self.fail_execution(error)
        if not manifest_initialized:
            return
        try:
            runtime_context.update_manifest(
                **manifest_updates,
                status=self.outcome,
                ended_at=datetime.now().astimezone().isoformat(),
                error=self.error_message,
            )
        except Exception:
            return

    def _validate_resumable_checkpoint(
        self,
        finalization: ApplicationRunFinalization,
    ) -> None:
        if finalization.checkpoint_manager is None:
            return
        try:
            finalization.runtime_context.validate_checkpoint_path(
                require_exists=True,
            )
            self._resumable = True
        except Exception as checkpoint_exc:
            finalization.log.warning(
                "Failed to persist resumable checkpoint: %s",
                checkpoint_exc,
            )

    def close_execution_resources(
        self,
        *,
        supervisor: Any | None,
        heartbeat: Any | None,
        file_history: Any | None,
        log: Any,
    ) -> None:
        """Close all resources scoped to active Agent execution."""

        try:
            from src.tools.shell.background_task import BackgroundTaskRegistry

            BackgroundTaskRegistry.get_instance().terminate_current_run()
        except Exception as exc:
            log.debug("Background task teardown skipped: %s", exc)
        try:
            from src.tools.shell.process import ShellProcessRegistry

            ShellProcessRegistry.get_instance().release_current_run()
        except Exception as exc:
            log.debug("Shell session teardown skipped: %s", exc)
        if heartbeat is not None:
            try:
                heartbeat.stop()
            except Exception:
                pass
            try:
                heartbeat.close()
            except Exception:
                pass
        try:
            mcp_manager = getattr(supervisor, "_mcp_manager", None)
            if mcp_manager is not None:
                mcp_manager.disconnect_all()
        except Exception:
            pass
        if file_history is not None:
            try:
                file_history.close()
            except Exception:
                pass
        invocation = self._invocation
        if invocation is not None and invocation.coordinator is not None:
            self.close_agent_coordinator(invocation.coordinator)

    def close_agent_coordinator(self, coordinator: Any | None = None) -> None:
        """Deactivate the reported coordinator through one teardown path."""

        target = coordinator
        if target is None and self._invocation is not None:
            target = self._invocation.coordinator
        if target is None:
            return
        from src.lib.checkpoint.coordinator import CheckpointCoordinator

        try:
            CheckpointCoordinator.deactivate(target)
        except BaseException as exc:
            self.fail_cleanup(exc)
            raise

    def close_resources(self, resources: ApplicationRunResources) -> None:
        """Close execution and checkpoint resources without leaking either side."""

        execution_cleanup_error: BaseException | None = None
        try:
            self.close_execution_resources(
                supervisor=resources.supervisor,
                heartbeat=resources.heartbeat,
                file_history=resources.file_history,
                log=resources.log,
            )
        except BaseException as exc:
            execution_cleanup_error = exc
        try:
            self.close_checkpoint_resources(
                task_lease=resources.task_lease,
                checkpoint_manager=resources.checkpoint_manager,
            )
        except BaseException as exc:
            if execution_cleanup_error is None:
                raise
            execution_cleanup_error.add_note(
                f"Checkpoint resource cleanup also failed: {exc}"
            )
        if execution_cleanup_error is not None:
            raise execution_cleanup_error

    def close_checkpoint_resources(
        self,
        *,
        task_lease: Any | None,
        checkpoint_manager: CheckpointManager | None,
    ) -> None:
        """Release checkpoint resources while preserving the primary error."""

        task_lease_error: BaseException | None = None
        if task_lease is not None:
            try:
                task_lease.release()
            except BaseException as exc:
                self.enter_cleanup()
                task_lease_error = exc
        if checkpoint_manager is not None:
            try:
                checkpoint_manager.close()
            except BaseException as exc:
                self.enter_cleanup()
                if task_lease_error is None:
                    raise
                task_lease_error.add_note(f"Checkpoint manager close also failed: {exc}")
        if task_lease_error is not None:
            raise task_lease_error

    def release_run_lease(self, run_lease: Any | None) -> None:
        if run_lease is None:
            return
        try:
            run_lease.release()
        except BaseException:
            self.enter_cleanup()
            raise


def _persist_run_observability(
    runtime_context: Any,
    checkpoint_manager: CheckpointManager | None,
    task_id: str,
    *,
    result: str | None,
    event_start_offset: int | None,
    manifest_updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Copy terminal task evidence into the immutable Run directory."""

    if manifest_updates is None:
        manifest_updates = {}
    if result is not None:
        result_artifact = runtime_context.artifacts_dir / "result.txt"
        runtime_context.atomic_write_run_file(result_artifact, result)
        manifest_updates.update(
            result_artifact="artifacts/result.txt",
            result_size=len(result.encode("utf-8")),
        )

    if checkpoint_manager is None:
        return manifest_updates

    goal = checkpoint_manager.load_goal(task_id)
    if goal is not None:
        runtime_context.atomic_write_run_file(
            runtime_context.audit_dir / "goal.json",
            json.dumps(
                goal,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        manifest_updates.update(
            goal=goal,
            goal_artifact="audit/goal.json",
        )

    try:
        runtime_context.atomic_write_run_file_chunks(
            runtime_context.audit_dir / "task_tree.json",
            _checkpoint_file_chunks(
                checkpoint_manager,
                task_id,
                relative_path="task_tree.json",
            ),
        )
    except FileNotFoundError:
        pass
    else:
        manifest_updates["task_tree_artifact"] = "audit/task_tree.json"

    if event_start_offset is not None:
        event_stats = {"count": 0, "complete": True}
        event_size = runtime_context.atomic_write_run_file_chunks(
            runtime_context.audit_dir / "task_events.jsonl",
            _run_event_chunks(
                checkpoint_manager,
                task_id,
                start_offset=event_start_offset,
                stats=event_stats,
            ),
        )
        manifest_updates.update(
            task_events_artifact="audit/task_events.jsonl",
            task_events_run_id=runtime_context.run_id,
            task_events_count=event_stats["count"],
            task_events_size=event_size,
            task_events_complete=event_stats["complete"],
        )
    return manifest_updates


def _task_events_size(
    checkpoint_manager: CheckpointManager,
    task_id: str,
) -> int:
    try:
        with checkpoint_manager.task_storage(task_id) as storage:
            return storage.stat_file("task_events.jsonl").st_size
    except (FileNotFoundError, OSError, RuntimeError):
        return 0


def _run_event_chunks(
    checkpoint_manager: CheckpointManager,
    task_id: str,
    *,
    start_offset: int,
    stats: dict[str, Any],
):
    with checkpoint_manager.task_storage(task_id) as storage:
        try:
            with storage.open_binary_reader("task_events.jsonl") as stream:
                size = os.fstat(stream.fileno()).st_size
                if start_offset > size:
                    stats["complete"] = False
                    return
                stream.seek(start_offset)
                line_has_content = False
                while True:
                    chunk = stream.read(_RUN_ARTIFACT_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    segments = chunk.split(b"\n")
                    for segment in segments[:-1]:
                        if line_has_content or segment.strip():
                            stats["count"] += 1
                        line_has_content = False
                    if segments[-1].strip():
                        line_has_content = True
                    yield chunk
                if line_has_content:
                    stats["count"] += 1
        except FileNotFoundError:
            stats["complete"] = False


def _checkpoint_file_chunks(
    checkpoint_manager: CheckpointManager,
    task_id: str,
    *,
    relative_path: str,
):
    """Stream one maintained checkpoint projection without replaying events."""

    with checkpoint_manager.task_storage(task_id) as storage:
        with storage.open_binary_reader(relative_path) as stream:
            while True:
                chunk = stream.read(_RUN_ARTIFACT_COPY_CHUNK_BYTES)
                if not chunk:
                    return
                yield chunk
