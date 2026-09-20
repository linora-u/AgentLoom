"""One Agent invocation's ordering, bindings, and runtime-resource ownership."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from agentloom.configuration import C
from agentloom.runtime import get_current_run_context
from agentloom.runtime.agent_runtime import (
    AgentRuntimeRequest,
    AgentRuntimeResult,
    RuntimeEvent,
    RuntimeRequirements,
    require_runtime_state,
)
from agentloom.runtime.hooks import HookEvent, HookRun
from agentloom.runtime.trace import (
    bind_explicit_execution_context,
    capture_explicit_execution_context,
    generate_id,
    require_root_run_id,
)
from agentloom.runtime.workspace import ensure_workspace_mounted_once

if TYPE_CHECKING:
    from agentloom.application.lifecycle import ApplicationRunLifecycle


def _runtime_requirements(
    config: dict[str, Any],
    *,
    checkpoint_active: bool,
) -> RuntimeRequirements:
    """Compile only semantic features exercised by this invocation."""

    concurrency = config.get("concurrency")
    parallel_tools = (
        concurrency == "auto"
        or (
            isinstance(concurrency, int)
            and not isinstance(concurrency, bool)
            and concurrency > 1
        )
    )
    checkpoint = config.get("checkpoint")
    return RuntimeRequirements(
        structured_tools=True,
        parallel_tools=parallel_tools,
        checkpoint_resume=(
            checkpoint_active
            or (
                isinstance(checkpoint, dict)
                and checkpoint.get("enabled") is True
            )
        ),
        subagents=bool(config.get("worker_agents")),
    )


def _merge_runtime_events(
    observed: list[RuntimeEvent],
    result: AgentRuntimeResult,
    *,
    segment_start: int,
    lifecycle: ApplicationRunLifecycle | None,
) -> AgentRuntimeResult:
    """Retain one ordered invocation event history across runtime segments."""

    segment_events = observed[segment_start:]
    for event in result.events:
        if event not in segment_events:
            observed.append(event)
            segment_events.append(event)
            if lifecycle is not None:
                lifecycle.observe_runtime_event(event)
    return replace(result, events=tuple(observed))


@dataclass(slots=True)
class AgentInvocation:
    """Execute exactly one Agent invocation and release everything it owns."""

    owner: Any
    task: str
    task_id: str | None = None
    checkpoint_manager: Any | None = None
    application_lifecycle: ApplicationRunLifecycle | None = None
    resume: bool = False
    additional_args: dict[str, Any] | None = None
    owns_root_run: bool = False

    def run(self) -> str:
        from agentloom.runtime.checkpoint.coordinator import CheckpointCoordinator

        owner = self.owner
        transformed_tasks = owner._transform_tasks(self.task)
        if not transformed_tasks:
            raise ValueError("Agent task transformation produced no tasks")
        transformed_tasks = owner._inject_memory_snapshot(transformed_tasks)
        transformed_task = "\n\n".join(transformed_tasks)

        parent_context = capture_explicit_execution_context()
        current_task_id = parent_context.task_id
        final_task_id = (
            current_task_id
            or self.task_id
            or generate_id(f"{owner._get_agent_type().value.lower()}_{owner.name}", prefix="task")
        )
        owner._task_id = final_task_id

        if self.checkpoint_manager is not None:
            coordinator = CheckpointCoordinator.activate(
                self.checkpoint_manager,
                final_task_id,
                transformed_task,
                resume=self.resume,
                effective_config=owner._effective_agent_config or owner._config,
            )
        else:
            coordinator = CheckpointCoordinator.current()

        lifecycle = self.application_lifecycle
        owns_lifecycle = False
        if lifecycle is None and self.checkpoint_manager is not None:
            from agentloom.application.lifecycle import ApplicationRunLifecycle

            lifecycle = ApplicationRunLifecycle()
            lifecycle.enter_execution()
            owns_lifecycle = True

        def execute() -> str:
            return self._execute_bound(
                transformed_tasks=transformed_tasks,
                transformed_task=transformed_task,
                final_task_id=final_task_id,
                coordinator=coordinator,
                lifecycle=lifecycle,
                owns_lifecycle=owns_lifecycle,
            )

        if current_task_id:
            return execute()
        with bind_explicit_execution_context(replace(parent_context, task_id=final_task_id)):
            return execute()

    def _execute_bound(
        self,
        *,
        transformed_tasks: list[str],
        transformed_task: str,
        final_task_id: str,
        coordinator: Any,
        lifecycle: ApplicationRunLifecycle | None,
        owns_lifecycle: bool,
    ) -> str:
        from agentloom.runtime.todo import ensure_todo_state_provider

        owner = self.owner
        session_started = False
        session_result = None
        runtime_result = None
        session_error: BaseException | None = None
        runtime_agent = None
        agent_id = owner.get_agent_id()

        active_context = capture_explicit_execution_context()
        hook_agent_config = owner._effective_agent_config or owner._config
        hook_run = HookRun(
            owner._hook_plan,
            local_run_id=active_context.local_run_id or "",
            root_run_id=active_context.root_run_id or "",
            parent=active_context.hook_run,
            agent_config=hook_agent_config,
            project_root=str(C.agent_root),
        )
        runtime_path = (
            f"{active_context.runtime_agent_path}/{owner.name}"
            if active_context.runtime_agent_path
            else owner.name
        )
        execution_binding = bind_explicit_execution_context(
            replace(
                active_context,
                agent_id=agent_id,
                agent_name=owner.name,
                agent_config=owner._effective_agent_config or owner._config,
                skill_catalog=owner._skill_catalog,
                hook_run=hook_run,
                runtime_agent_path=runtime_path,
            )
        )
        execution_binding.__enter__()
        todo_binding = ensure_todo_state_provider()
        todo_binding.__enter__()

        try:
            runtime_agent = owner.build_runtime()
            owner._bind_hook_message_sink(runtime_agent)
            ensure_workspace_mounted_once()
            if self.owns_root_run:
                owner._emit_session_lifecycle_event(HookEvent.SESSION_START, transformed_task)
                session_started = True

            runtime_checkpoint, checkpoint_sink = self._prepare_checkpoint(
                coordinator
            )
            result, runtime_result = self._run_runtime(
                runtime_agent,
                transformed_tasks=transformed_tasks,
                lifecycle=lifecycle,
                runtime_checkpoint=runtime_checkpoint,
                checkpoint_sink=checkpoint_sink,
            )
            owner._emit_task_lifecycle_event(
                HookEvent.TASK_COMPLETED,
                transformed_task,
                result=result,
            )
            session_result = result
            return result
        except BaseException as exc:
            session_error = exc
            if isinstance(exc, Exception):
                owner._emit_task_lifecycle_event(
                    HookEvent.STOP_FAILURE,
                    transformed_task,
                    error=exc,
                )
            raise
        finally:
            lifecycle_error: BaseException | None = None
            try:
                lifecycle_error = self._finalize(
                    transformed_task=transformed_task,
                    final_task_id=final_task_id,
                    runtime_result=runtime_result,
                    coordinator=coordinator,
                    lifecycle=lifecycle,
                    owns_lifecycle=owns_lifecycle,
                    session_started=session_started,
                    session_result=session_result,
                    session_error=session_error,
                )
            except BaseException as exc:
                lifecycle_error = exc
            finally:
                try:
                    if runtime_agent is not None:
                        runtime_agent.close()
                except BaseException as exc:
                    if lifecycle_error is None:
                        lifecycle_error = exc
                    elif lifecycle_error is not exc:
                        lifecycle_error.add_note(
                            "Runtime close also failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
                finally:
                    try:
                        todo_binding.__exit__(None, None, None)
                    finally:
                        execution_binding.__exit__(None, None, None)
            if lifecycle_error is not None:
                if session_error is None:
                    raise lifecycle_error
                if lifecycle_error is not session_error:
                    session_error.add_note(
                        "Run cleanup also failed: "
                        f"{type(lifecycle_error).__name__}: {lifecycle_error}"
                    )

    def _prepare_checkpoint(self, coordinator: Any) -> tuple[Any, Any]:
        if coordinator is None:
            return None, None
        runtime_checkpoint = None
        if self.resume and self.checkpoint_manager is not None:
            runtime_checkpoint = coordinator.load_runtime_checkpoint()

        checkpoint_sink = None
        if self.checkpoint_manager is not None:
            def save_running_checkpoint(checkpoint: Any) -> None:
                coordinator.save_runtime_checkpoint(checkpoint, "running")

            checkpoint_sink = save_running_checkpoint
        return runtime_checkpoint, checkpoint_sink

    def _run_runtime(
        self,
        runtime_agent: Any,
        *,
        transformed_tasks: list[str],
        lifecycle: ApplicationRunLifecycle | None,
        runtime_checkpoint: Any = None,
        checkpoint_sink: Any = None,
    ) -> tuple[Any, Any]:
        runtime_context = get_current_run_context()
        execution_context = capture_explicit_execution_context()
        effective_config = (
            self.owner._effective_agent_config or self.owner._config
        )
        requirements = _runtime_requirements(
            effective_config,
            checkpoint_active=(
                runtime_checkpoint is not None
                or checkpoint_sink is not None
                or self.resume
            ),
        )
        request_identity = {
            "application_id": (
                runtime_context.application_id
                if runtime_context is not None
                else None
            ),
            "task_id": (
                runtime_context.task_id
                if runtime_context is not None
                else execution_context.task_id
            ),
            "run_id": (
                runtime_context.run_id
                if runtime_context is not None
                else (
                    execution_context.root_run_id
                    or execution_context.local_run_id
                )
            ),
            "requirements": requirements,
        }
        runtime_events: list[RuntimeEvent] = []

        def observe_runtime_event(event: RuntimeEvent) -> None:
            runtime_events.append(event)
            if lifecycle is not None:
                lifecycle.observe_runtime_event(event)

        result = None
        for task_index, current_task in enumerate(transformed_tasks):
            self.owner._emit_task_start(
                runtime_agent,
                current_task,
                additional_args=self.additional_args or {},
            )
            segment_start = len(runtime_events)
            run_result = runtime_agent.run(
                AgentRuntimeRequest(
                    task=current_task,
                    **request_identity,
                    event_sink=observe_runtime_event,
                    continue_session=self.resume or task_index > 0,
                    record_task=task_index > 0,
                    additional_args=self.additional_args or {},
                    checkpoint=(
                        runtime_checkpoint if task_index == 0 else None
                    ),
                    checkpoint_sink=checkpoint_sink,
                )
            )
            run_result = _merge_runtime_events(
                runtime_events,
                run_result,
                segment_start=segment_start,
                lifecycle=lifecycle,
            )
            require_runtime_state(
                run_result,
                allowed_states={"success"},
                error_prefix="Agent run did not complete successfully",
            )
            result = run_result.output
        return result, run_result

    def _finalize(
        self,
        *,
        transformed_task: str,
        final_task_id: str,
        runtime_result: Any,
        coordinator: Any,
        lifecycle: ApplicationRunLifecycle | None,
        owns_lifecycle: bool,
        session_started: bool,
        session_result: Any,
        session_error: BaseException | None,
    ) -> BaseException | None:
        owner = self.owner
        lifecycle_error: BaseException | None = None
        try:
            if lifecycle is not None:
                try:
                    lifecycle.report_agent_invocation(
                        coordinator=coordinator,
                        runtime_result=runtime_result,
                        result=session_result,
                        error=session_error,
                    )
                    if owns_lifecycle:
                        lifecycle.settle_reported_agent_invocation()
                        lifecycle.commit_checkpoint(
                            checkpoint_manager=self.checkpoint_manager,
                            task_id=final_task_id,
                        )
                except BaseException as exc:
                    lifecycle_error = exc
            if session_started:
                owner._emit_session_lifecycle_event(
                    HookEvent.SESSION_END,
                    transformed_task,
                    result=session_result,
                    error=session_error,
                )
                if session_error is None:
                    self._review_finished_run()
        finally:
            if (
                owns_lifecycle
                and self.checkpoint_manager is not None
                and coordinator is not None
            ):
                assert lifecycle is not None
                lifecycle.close_agent_coordinator(coordinator)
        return lifecycle_error

    def _review_finished_run(self) -> None:
        owner = self.owner
        try:
            from agentloom.self_learning.paths import review_config, self_learning_enabled

            effective_config = owner._effective_agent_config or owner._config
            review_policies = (
                review_config(effective_config, scope="application"),
                review_config(effective_config, scope="project"),
            )
            if self_learning_enabled(effective_config) and any(
                policy.get("enabled")
                and str((policy.get("trigger") or {}).get("mode") or "manual") != "manual"
                for policy in review_policies
            ):
                from agentloom.self_learning.reviewer import review_finished_run

                review_finished_run(
                    root_run_id=require_root_run_id(),
                    agent_config=effective_config,
                )
        except Exception:
            if owner._logger:
                owner._logger.warning("Completed-run memory review failed unexpectedly")
