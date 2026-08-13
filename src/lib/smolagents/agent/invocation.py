"""One Agent invocation's ordering, bindings, and runtime-resource ownership."""

from __future__ import annotations

from contextlib import nullcontext
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from src.lib.config import C
from src.lib.smolagents.hooks import HookEvent, HookRun
from src.lib.utils.workspace import ensure_workspace_mounted_once
from src.trace import (
    bind_explicit_execution_context,
    capture_explicit_execution_context,
    generate_id,
    require_root_run_id,
)

if TYPE_CHECKING:
    from src.application_run_lifecycle import ApplicationRunLifecycle


current_worker_memory: ContextVar[list | None] = ContextVar("current_worker_memory", default=None)


def require_runtime_result(
    run_result: Any,
    *,
    allowed_states: set[str],
    error_prefix: str,
) -> None:
    run_state = str(getattr(run_result, "state", "") or "")
    if run_state not in allowed_states:
        raise RuntimeError(f"{error_prefix}: {run_state or 'missing_run_state'}")


def require_successful_runtime_result(run_result: Any) -> None:
    require_runtime_result(
        run_result,
        allowed_states={"success"},
        error_prefix="Agent run did not complete successfully",
    )


def require_goal_runtime_result(run_result: Any) -> None:
    require_runtime_result(
        run_result,
        allowed_states={"success", "max_steps_error"},
        error_prefix="Agent Goal segment failed",
    )


def goal_continuation_prompt(state: Any) -> str:
    budget = "unlimited"
    if state.token_budget is not None:
        remaining = max(state.token_budget - state.used_tokens, 0)
        budget = (
            f"{state.used_tokens}/{state.token_budget} tokens used; "
            f"{remaining} tokens remain before the next-request fence"
        )
    return (
        "Continue working toward the active Goal using the existing conversation "
        "and tool state. Do not restart or repeat completed work.\n\n"
        f"Goal ID: {state.goal_id}\n"
        "Objective: unchanged from the initial task context; call get_goal only "
        "if you need to inspect the canonical objective again.\n"
        f"Goal status: {state.status}\n"
        f"Token budget: {budget}\n\n"
        "A normal final answer does not complete the Goal. Only after the entire "
        "objective is delivered and verified, call update_goal with status="
        "'complete' and concise evidence."
    )


def goal_completion_output(segment_output: Any, evidence: str | None) -> Any:
    if (
        isinstance(segment_output, str)
        and segment_output.startswith("Error in generating final LLM output:")
    ):
        return evidence
    return segment_output if segment_output is not None else evidence


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
        from src.lib.checkpoint.coordinator import CheckpointCoordinator
        from src.lib.goal import (
            GoalStateProvider,
            build_goal_objective,
            goal_objective_fingerprint,
            normalize_goal_config,
        )

        owner = self.owner
        transformed_tasks = owner._transform_tasks(self.task)
        if not transformed_tasks:
            raise ValueError("Agent task transformation produced no tasks")
        transformed_tasks = owner._inject_memory_snapshot(transformed_tasks)
        transformed_task = "\n\n".join(transformed_tasks)

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
                description=str(owner._config.get("description", "")),
                workflow=owner._config["workflow"],
                task=self.task,
            )
            goal_fingerprint = goal_objective_fingerprint(
                description=str(owner._config.get("description", "")),
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
            from src.application_run_lifecycle import ApplicationRunLifecycle

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

        def execute() -> str:
            return self._execute_bound(
                transformed_tasks=transformed_tasks,
                transformed_task=transformed_task,
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
        transformed_tasks: list[str],
        transformed_task: str,
        final_task_id: str,
        goal_config: Any,
        goal_provider: Any,
        coordinator: Any,
        lifecycle: ApplicationRunLifecycle | None,
        owns_lifecycle: bool,
    ) -> str:
        from src.lib.goal import bind_goal_state_provider
        from src.lib.todo import ensure_todo_state_provider

        owner = self.owner
        session_started = False
        session_result = None
        session_error: BaseException | None = None
        runtime_agent = None
        agent_id = owner.get_agent_id()
        previous_model_agent_id = (
            getattr(owner._model, "agent_id", ...) if hasattr(owner._model, "agent_id") else ...
        )
        if previous_model_agent_id is not ...:
            owner._model.agent_id = agent_id

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
        todo_binding = ensure_todo_state_provider()
        todo_binding.__enter__()

        try:
            runtime_agent = owner.build_runtime_agent()
            owner._bind_hook_message_sink(runtime_agent)
            ensure_workspace_mounted_once()
            if self.owns_root_run:
                owner._emit_session_lifecycle_event(HookEvent.SESSION_START, transformed_task)
                session_started = True

            self._prepare_checkpoint(runtime_agent, coordinator)
            result = self._run_runtime(
                runtime_agent,
                transformed_tasks=transformed_tasks,
                goal_provider=goal_provider,
            )
            try:
                current_worker_memory.set(list(runtime_agent.memory.steps))
            except Exception:
                pass
            owner._emit_task_lifecycle_event(
                HookEvent.TASK_COMPLETED,
                transformed_task,
                result=result,
            )
            session_result = result
            return result
        except BaseException as exc:
            from src.lib.goal import GoalBudgetLimitedError

            session_error = exc
            if not isinstance(exc, GoalBudgetLimitedError) and isinstance(exc, Exception):
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
                    runtime_agent=runtime_agent,
                    coordinator=coordinator,
                    lifecycle=lifecycle,
                    owns_lifecycle=owns_lifecycle,
                    session_started=session_started,
                    session_result=session_result,
                    session_error=session_error,
                    goal_provider=goal_provider,
                    previous_model_agent_id=previous_model_agent_id,
                )
            except BaseException as exc:
                lifecycle_error = exc
            finally:
                try:
                    todo_binding.__exit__(None, None, None)
                finally:
                    try:
                        goal_binding.__exit__(None, None, None)
                    finally:
                        execution_binding.__exit__(None, None, None)
            if lifecycle_error is not None:
                raise lifecycle_error

    def _prepare_checkpoint(self, runtime_agent: Any, coordinator: Any) -> None:
        if coordinator is None:
            return
        if self.resume and self.checkpoint_manager is not None:
            coordinator.restore(runtime_agent)
            if coordinator._supervisor_heartbeat is not None:
                try:
                    coordinator._supervisor_heartbeat.update_step(len(runtime_agent.memory.steps))
                except Exception:
                    pass
        if self.checkpoint_manager is not None:
            coordinator.register_supervisor_step_callback(runtime_agent)
        else:
            coordinator.register_worker_step_callback(runtime_agent, agent_name=self.owner.name)

    def _run_runtime(
        self,
        runtime_agent: Any,
        *,
        transformed_tasks: list[str],
        goal_provider: Any,
    ) -> Any:
        if goal_provider is None:
            result = None
            for task_index, current_task in enumerate(transformed_tasks):
                run_kwargs: dict[str, Any] = {
                    "task": current_task,
                    "return_full_result": True,
                }
                if self.additional_args:
                    run_kwargs["additional_args"] = dict(self.additional_args)
                if self.resume or task_index > 0:
                    run_kwargs["reset"] = False
                if task_index > 0 and getattr(
                    runtime_agent,
                    "_agent_loom_supports_reset_false_task_step_control",
                    False,
                ):
                    run_kwargs["_skip_task_step_on_reset_false"] = False
                run_result = runtime_agent.run(**run_kwargs)
                require_successful_runtime_result(run_result)
                result = getattr(run_result, "output", None)
            return result

        initial_state = goal_provider.snapshot()
        segment_index = 0
        while True:
            state = goal_provider.snapshot()
            if state.status == "complete":
                return state.evidence
            goal_provider.assert_request_allowed()
            use_initial_context = segment_index == 0 and not initial_state.goal_started
            current_task = (
                transformed_tasks[0]
                if use_initial_context
                else goal_continuation_prompt(state)
            )
            run_kwargs = {"task": current_task, "return_full_result": True}
            if self.additional_args:
                run_kwargs["additional_args"] = dict(self.additional_args)
            if self.resume or segment_index > 0 or not use_initial_context:
                run_kwargs["reset"] = False
            if not use_initial_context and getattr(
                runtime_agent,
                "_agent_loom_supports_reset_false_task_step_control",
                False,
            ):
                run_kwargs["_skip_task_step_on_reset_false"] = False
            try:
                run_result = runtime_agent.run(**run_kwargs)
            except Exception as exc:
                from src.lib.goal import GoalBudgetLimitedError, GoalCompleteError

                terminal_state = goal_provider.snapshot()
                if isinstance(exc, GoalCompleteError) or terminal_state.status == "complete":
                    return terminal_state.evidence
                if terminal_state.status == "budget_limited":
                    raise GoalBudgetLimitedError(terminal_state) from exc
                raise
            require_goal_runtime_result(run_result)
            segment_output = getattr(run_result, "output", None)
            segment_index += 1
            state = goal_provider.snapshot()
            if state.status == "complete":
                return goal_completion_output(segment_output, state.evidence)
            goal_provider.assert_request_allowed()

    def _finalize(
        self,
        *,
        transformed_task: str,
        final_task_id: str,
        runtime_agent: Any,
        coordinator: Any,
        lifecycle: ApplicationRunLifecycle | None,
        owns_lifecycle: bool,
        session_started: bool,
        session_result: Any,
        session_error: BaseException | None,
        goal_provider: Any,
        previous_model_agent_id: Any,
    ) -> BaseException | None:
        owner = self.owner
        lifecycle_error: BaseException | None = None
        try:
            goal_snapshot = goal_provider.snapshot().to_dict() if goal_provider is not None else None
            if lifecycle is not None:
                try:
                    lifecycle.report_agent_invocation(
                        coordinator=coordinator,
                        runtime_agent=runtime_agent,
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
                    transformed_task,
                    result=session_result,
                    error=session_error,
                )
                if session_error is None:
                    self._review_finished_run()
            if previous_model_agent_id is not ...:
                owner._model.agent_id = previous_model_agent_id
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
            from src.extensions.self_learning.paths import review_config, self_learning_enabled

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
                from src.extensions.self_learning.reviewer import review_finished_run

                review_finished_run(
                    root_run_id=require_root_run_id(),
                    agent_config=effective_config,
                )
        except Exception:
            if owner._logger:
                owner._logger.warning("Completed-run memory review failed unexpectedly")
