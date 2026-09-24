"""One Application Agent invocation and its owned runtime resources."""

from __future__ import annotations

from contextlib import nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from agentloom.config import C
from agentloom.execution import get_current_run_context
from agentloom.execution.agent_runtime import (
    AgentRuntimeRequest,
    AgentRuntimeResult,
    JSONValue,
    RuntimeEvent,
    RuntimeEventSink,
    require_runtime_state,
)
from agentloom.execution.goal import goal_completion_output as _goal_completion_output
from agentloom.execution.goal import goal_continuation_prompt as _goal_continuation_prompt
from agentloom.execution.hooks import HookEvent, HookRun
from agentloom.execution.trace import (
    bind_explicit_execution_context,
    capture_explicit_execution_context,
    generate_id,
    require_root_run_id,
)
from agentloom.execution.workspace import ensure_workspace_mounted_once

if TYPE_CHECKING:
    from agentloom.app.lifecycle import ApplicationRunLifecycle


_RUNTIME_EVENT_SINK: ContextVar[RuntimeEventSink | None] = ContextVar(
    "agentloom_runtime_event_sink",
    default=None,
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
    task: str | None
    task_id: str | None = None
    checkpoint_manager: Any | None = None
    application_lifecycle: ApplicationRunLifecycle | None = None
    resume: bool = False
    additional_args: dict[str, Any] | None = None
    owns_root_run: bool = False

    def run(self) -> JSONValue:
        from agentloom.execution.checkpoint.coordinator import CheckpointCoordinator
        from agentloom.execution.goal import (
            GoalStateProvider,
            build_goal_objective,
            goal_objective_fingerprint,
            normalize_goal_config,
        )

        owner = self.owner
        lifecycle_tasks = owner._transform_tasks(self.task)
        if not lifecycle_tasks:
            raise ValueError("Agent task transformation produced no tasks")
        # Memory is model context, not newly supplied task/evidence. Recording
        # its trusted wrapper in lifecycle events would make the untrusted
        # history sanitizer correctly treat the wrapper as a forged fence.
        transformed_tasks = owner._inject_memory_snapshot(lifecycle_tasks)
        transformed_task = "\n\n".join(
            task for task in transformed_tasks if task is not None
        )

        goal_config = normalize_goal_config(
            owner._config,
            source=owner._config.get("name", "agent"),
        )
        goal_objective = None
        goal_fingerprint = None
        if goal_config.enabled:
            if not self.owns_root_run:
                raise ValueError("Goal mode can only be configured by the root Supervisor Agent")
            goal_objective = build_goal_objective(
                workflow=owner._config["workflow"],
                task=self.task,
            )
            goal_fingerprint = goal_objective_fingerprint(
                workflow=owner._config["workflow"],
                task=self.task,
            )

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
            from agentloom.app.lifecycle import ApplicationRunLifecycle

            lifecycle = ApplicationRunLifecycle()
            lifecycle.enter_execution()
            owns_lifecycle = True

        goal_provider = None
        if goal_config.enabled:
            assert goal_objective is not None and goal_fingerprint is not None
            try:
                goal_provider = GoalStateProvider.initialize(
                    config=goal_config,
                    objective=goal_objective,
                    objective_fingerprint=goal_fingerprint,
                    resume=self.resume,
                )
            except Exception:
                if self.checkpoint_manager is not None and coordinator is not None:
                    CheckpointCoordinator.deactivate(coordinator)
                raise

        def execute() -> JSONValue:
            from agentloom.execution.context_engine.runtime import ensure_task_context_engine
            with ensure_task_context_engine(owner._effective_agent_config or owner._config):
                return self._execute_bound(
                    transformed_tasks=transformed_tasks,
                    lifecycle_tasks=lifecycle_tasks,
                    final_task_id=final_task_id,
                    goal_config=goal_config,
                    goal_provider=goal_provider,
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
        transformed_tasks: list[str | None],
        lifecycle_tasks: list[str | None],
        final_task_id: str,
        goal_config: Any,
        goal_provider: Any,
        coordinator: Any,
        lifecycle: ApplicationRunLifecycle | None,
        owns_lifecycle: bool,
    ) -> JSONValue:
        from agentloom.execution.goal import bind_goal_state_provider

        owner = self.owner
        lifecycle_task = "\n\n".join(
            task for task in lifecycle_tasks if task is not None
        )
        session_started = False
        session_result = None
        runtime_result = None
        session_error: BaseException | None = None
        runtime_agent = None
        agent_id = owner.get_agent_id()

        active_context = capture_explicit_execution_context()
        hook_agent_config = owner._effective_agent_config or owner._config
        if goal_config.enabled:
            hook_agent_config = {
                **hook_agent_config,
                "goal": deepcopy(owner._config["goal"]),
            }
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
        goal_binding = (
            bind_goal_state_provider(goal_provider)
            if goal_provider is not None
            else nullcontext(None)
        )
        goal_binding.__enter__()

        try:
            runtime_agent = owner.build_runtime()
            owner._bind_hook_message_sink(runtime_agent)
            ensure_workspace_mounted_once()
            if self.owns_root_run:
                owner._emit_session_lifecycle_event(HookEvent.SESSION_START, lifecycle_task)
                session_started = True

            runtime_checkpoint, checkpoint_sink = self._prepare_checkpoint(
                coordinator
            )
            result, runtime_result = self._run_runtime(
                runtime_agent,
                transformed_tasks=transformed_tasks,
                lifecycle_tasks=lifecycle_tasks,
                goal_provider=goal_provider,
                lifecycle=lifecycle,
                runtime_checkpoint=runtime_checkpoint,
                checkpoint_sink=checkpoint_sink,
            )
            owner._emit_task_lifecycle_event(
                HookEvent.TASK_COMPLETED,
                lifecycle_task,
                result=result,
            )
            session_result = result
            return result
        except BaseException as exc:
            session_error = exc
            if isinstance(exc, Exception):
                owner._emit_task_lifecycle_event(
                    HookEvent.STOP_FAILURE,
                    lifecycle_task,
                    error=exc,
                )
            raise
        finally:
            lifecycle_error: BaseException | None = None
            try:
                lifecycle_error = self._finalize(
                    lifecycle_task=lifecycle_task,
                    final_task_id=final_task_id,
                    runtime_result=runtime_result,
                    coordinator=coordinator,
                    lifecycle=lifecycle,
                    owns_lifecycle=owns_lifecycle,
                    session_started=session_started,
                    session_result=session_result,
                    session_error=session_error,
                    goal_provider=goal_provider,
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
                        from agentloom.execution.resources import close_instance_resources

                        close_instance_resources(agent_id)
                    except BaseException as exc:
                        if lifecycle_error is None:
                            lifecycle_error = exc
                        else:
                            lifecycle_error.add_note(f"Resource close also failed: {exc}")
                    try:
                        goal_binding.__exit__(None, None, None)
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
                coordinator.save_runtime_checkpoint(checkpoint, "running", require_durable=True)

            checkpoint_sink = save_running_checkpoint
        return runtime_checkpoint, checkpoint_sink

    def _run_runtime(
        self,
        runtime_agent: Any,
        *,
        transformed_tasks: list[str | None],
        lifecycle_tasks: list[str | None],
        goal_provider: Any,
        lifecycle: ApplicationRunLifecycle | None,
        runtime_checkpoint: Any = None,
        checkpoint_sink: Any = None,
    ) -> tuple[Any, Any]:
        runtime_context = get_current_run_context()
        execution_context = capture_explicit_execution_context()
        effective_config = (
            self.owner._effective_agent_config or self.owner._config
        )
        from agentloom.app.validation import AgentConfigNormalizer

        requirements = AgentConfigNormalizer.runtime_requirements(
            self.owner._config, effective_config=effective_config,
            hook_plan=self.owner._hook_plan,
        )
        requirements = replace(requirements, checkpoint_resume=(
            requirements.checkpoint_resume or runtime_checkpoint is not None
            or checkpoint_sink is not None or self.resume
        ))
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
        parent_event_sink = _RUNTIME_EVENT_SINK.get()

        def observe_runtime_event(event: RuntimeEvent) -> None:
            runtime_events.append(event)
            if lifecycle is not None:
                lifecycle.observe_runtime_event(event)
            elif parent_event_sink is not None:
                parent_event_sink(event)

        def invoke_runtime(request: AgentRuntimeRequest) -> AgentRuntimeResult:
            start = len(runtime_events)
            token = _RUNTIME_EVENT_SINK.set(observe_runtime_event)
            try:
                from agentloom.execution.observability import get_current_trace_recorder

                recorder = get_current_trace_recorder()
                if recorder is not None:
                    binding = getattr(self.owner, "_model_binding", None)
                    selection = getattr(self.owner, "_model_selection", None)
                    if binding is not None:
                        configured_limit = binding.input_token_limit
                    elif selection is not None:
                        configured_limit = selection.settings.get("input_token_limit")
                    else:
                        configured_limit = None
                    input_limit = configured_limit if type(configured_limit) is int else None
                    recorder.record_agent_start(
                        task=request.task,
                        agent_name=self.owner.name,
                        runtime=runtime_agent.runtime_id,
                        input_token_limit=input_limit,
                    )
                try:
                    result = runtime_agent.run(request)
                except BaseException as exc:
                    if recorder is not None:
                        recorder.record_agent_end(state="failed", error=exc)
                    raise
                if recorder is not None:
                    recorder.record_agent_end(state=result.state)
                for event in result.events:
                    if event not in runtime_events[start:]:
                        observe_runtime_event(event)
                return result
            finally:
                _RUNTIME_EVENT_SINK.reset(token)

        if goal_provider is None:
            result = None
            for task_index, current_task in enumerate(transformed_tasks):
                self.owner._emit_task_start(
                    runtime_agent,
                    lifecycle_tasks[task_index] or "",
                    additional_args=self.additional_args or {},
                )
                segment_start = len(runtime_events)
                run_result = invoke_runtime(
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

        initial_state = goal_provider.snapshot()
        segment_index = 0
        while True:
            state = goal_provider.snapshot()
            if state.status == "complete":
                return state.evidence, None
            goal_provider.assert_request_allowed()
            use_initial_context = segment_index == 0 and not initial_state.goal_started
            current_task = (
                transformed_tasks[0]
                if use_initial_context
                else _goal_continuation_prompt(state)
            )
            try:
                self.owner._emit_task_start(
                    runtime_agent,
                    (lifecycle_tasks[0] if use_initial_context else current_task)
                    or "",
                    additional_args=self.additional_args or {},
                )
                segment_start = len(runtime_events)
                run_result = invoke_runtime(
                    AgentRuntimeRequest(
                        task=current_task,
                        **request_identity,
                        event_sink=observe_runtime_event,
                        continue_session=(
                            self.resume
                            or segment_index > 0
                            or not use_initial_context
                        ),
                        record_task=not use_initial_context,
                        additional_args=self.additional_args or {},
                        checkpoint=(
                            runtime_checkpoint if segment_index == 0 else None
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
            except Exception as exc:
                from agentloom.execution.goal import GoalCompleteError

                terminal_state = goal_provider.snapshot()
                # A completion commit does not authorize hiding a rejected Stop
                # gate, failed tool settlement, or another runtime failure.
                if isinstance(exc, GoalCompleteError):
                    return terminal_state.evidence, None
                raise
            require_runtime_state(
                run_result,
                allowed_states={"success", "max_steps_error"},
                error_prefix="Agent Goal segment failed",
            )
            segment_output = run_result.output
            segment_index += 1
            state = goal_provider.snapshot()
            if state.status == "complete":
                return _goal_completion_output(segment_output, state.evidence), run_result
            goal_provider.assert_request_allowed()

    def _finalize(
        self,
        *,
        lifecycle_task: str,
        final_task_id: str,
        runtime_result: Any,
        coordinator: Any,
        lifecycle: ApplicationRunLifecycle | None,
        owns_lifecycle: bool,
        session_started: bool,
        session_result: Any,
        session_error: BaseException | None,
        goal_provider: Any,
    ) -> BaseException | None:
        owner = self.owner
        lifecycle_error: BaseException | None = None
        try:
            goal_snapshot = goal_provider.snapshot().to_dict() if goal_provider is not None else None
            if lifecycle is not None:
                try:
                    lifecycle.report_agent_invocation(
                        coordinator=coordinator,
                        runtime_result=runtime_result,
                        result=session_result,
                        error=session_error,
                        goal=goal_snapshot,
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
                    lifecycle_task,
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
