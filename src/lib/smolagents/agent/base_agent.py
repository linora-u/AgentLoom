"""
Abstract base class for agents.

Defines common interfaces and behavior patterns for all agents.
Provides shared capabilities such as model management and execution environment integration.
"""

# Checkpoint / Resume support
import hashlib as _hashlib
import os
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from contextvars import ContextVar, copy_context
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.application_run_lifecycle import ApplicationRunLifecycle

from smolagents import (
    AgentAudio,
    AgentGenerationError,
    AgentImage,
    AgentLogger,
    AgentParsingError,
    AgentToolCallError,
    AgentToolExecutionError,
    CodeAgent,
    LogLevel,
    RunResult,
    Tool,
    ToolCallingAgent,
    validate_tool_arguments,
)
from smolagents.agents import ToolOutput
from smolagents.memory import ToolCall
from src.lib.config import (
    C,
    build_effective_agent_config_snapshot,
    get_code_agent_config,
)
from src.lib.config.defaults import DEFAULT_MAX_TOKENS
from src.lib.logging import (
    get_global_logger,
    get_logger,
)
from src.lib.smolagents.agent.agent_validation import (
    AgentConfigNormalizer,
    NormalizedExecutionConfig,
    build_normalized_execution_config,
    normalize_execution_prompt_template_path_value,
    normalize_positive_int_value,
    validate_execution_config_payload,
    validate_todo_config,
)
from src.lib.smolagents.agent.tool_argument_coercion import coerce_tool_arguments
from src.lib.smolagents.hooks import (
    HookConfigLayer,
    HookEvent,
    HookPlan,
    HookPlanCompiler,
    HookRun,
    builtin_hook_handlers,
)
from src.lib.smolagents.hooks.tool_shim import (
    ToolBlockedError,
    ToolBlockedResult,
    bind_tool_call_id,
    clone_tool_for_runtime,
    inject_hooks,
)
from src.lib.smolagents.models.model_manager import (
    ModelConfigBuilder,
    get_model,
)
from src.lib.smolagents.models.tool_call_parser import (
    ToolCallParseError,
    parse_json_with_repair,
)
from src.lib.smolagents.monkey_patch import install_agentloom_runtime_adapters
from src.lib.smolagents.prompts.prompt_builder import build_prompt_templates
from src.lib.smolagents.skills.catalog import SkillCatalog, SkillSource
from src.lib.smolagents.tool_protocol import ToolCallRecord, ToolErrorRecord
from src.lib.smolagents.tools.tools import ensure_tool_wrapped
from src.lib.utils.workspace import ensure_workspace_mounted_once
from src.tools.loader import resolve_tool_function
from src.trace import (
    bind_explicit_execution_context,
    bind_local_run,
    bind_root_run,
    capture_explicit_execution_context,
    generate_id,
    get_current_hook_run,
    require_root_run_id,
    require_root_run_state,
    sub_task_context,
)

_current_worker_memory: ContextVar[list | None] = ContextVar("_current_worker_memory", default=None)

install_agentloom_runtime_adapters()


from src.lib.smolagents.agent.loom_mixin import LoomAgentMixin  # noqa: E402


def _normalize_tool_arguments_object(arguments: dict[str, Any] | str) -> dict[str, Any] | str:
    if not isinstance(arguments, str):
        return arguments

    parsed: Any = arguments
    for _ in range(2):
        if not isinstance(parsed, str):
            break
        try:
            parsed = parse_json_with_repair(parsed)
        except Exception:
            return arguments

    return parsed if isinstance(parsed, dict) else arguments


def _require_runtime_result(
    run_result: Any,
    *,
    allowed_states: set[str],
    error_prefix: str,
) -> None:
    run_state = str(getattr(run_result, "state", "") or "")
    if run_state not in allowed_states:
        state_label = run_state or "missing_run_state"
        raise RuntimeError(f"{error_prefix}: {state_label}")


def _require_successful_runtime_result(run_result: Any) -> None:
    """Reject structured runtime results that did not finish successfully."""

    _require_runtime_result(
        run_result,
        allowed_states={"success"},
        error_prefix="Agent run did not complete successfully",
    )


def _require_goal_runtime_result(run_result: Any) -> None:
    """Accept both ordinary finals and max-step segment boundaries in Goal mode."""

    _require_runtime_result(
        run_result,
        allowed_states={"success", "max_steps_error"},
        error_prefix="Agent Goal segment failed",
    )


def _goal_continuation_prompt(state: Any) -> str:
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


def _goal_completion_output(segment_output: Any, evidence: str | None) -> Any:
    if (
        isinstance(segment_output, str)
        and segment_output.startswith("Error in generating final LLM output:")
    ):
        return evidence
    return segment_output if segment_output is not None else evidence


class _SuccessfulRunStateMixin:
    """Preserve the runtime return shape while rejecting failed run states."""

    def run(
        self,
        task: str,
        stream: bool = False,
        reset: bool = True,
        images: Any = None,
        additional_args: dict | None = None,
        max_steps: int | None = None,
        return_full_result: bool | None = None,
        **kwargs,
    ) -> Any:
        if stream:
            return super().run(
                task,
                stream=stream,
                reset=reset,
                images=images,
                additional_args=additional_args,
                max_steps=max_steps,
                return_full_result=return_full_result,
                **kwargs,
            )

        wants_full_result = (
            bool(getattr(self, "return_full_result", False)) if return_full_result is None else return_full_result
        )
        run_result = super().run(
            task,
            stream=False,
            reset=reset,
            images=images,
            additional_args=additional_args,
            max_steps=max_steps,
            return_full_result=True,
            **kwargs,
        )
        _require_successful_runtime_result(run_result)
        return run_result if wants_full_result else run_result.output


class CodeAgentV2(_SuccessfulRunStateMixin, LoomAgentMixin, CodeAgent):
    def __init__(
        self,
        *args,
        before_run_callbacks: list | None = None,
        **kwargs,
    ):
        max_tokens = kwargs.pop("max_tokens", None)
        smart_summary = kwargs.pop("smart_summary", True)
        self._init_loom_agent(
            before_run_callbacks,
            max_tokens=max_tokens,
            smart_summary=smart_summary,
        )
        super().__init__(*args, **kwargs)


class ToolCallingAgentV2(_SuccessfulRunStateMixin, LoomAgentMixin, ToolCallingAgent):
    def __init__(
        self,
        *args,
        before_run_callbacks: list | None = None,
        **kwargs,
    ):
        # Remove all code_act specific kwargs before calling ToolCallingAgent.__init__
        # ToolCallingAgent does not support these parameters
        kwargs.pop("executor_type", None)
        kwargs.pop("executor_kwargs", None)

        max_tokens = kwargs.pop("max_tokens", None)
        smart_summary = kwargs.pop("smart_summary", True)
        self._init_loom_agent(
            before_run_callbacks,
            max_tokens=max_tokens,
            smart_summary=smart_summary,
        )
        super().__init__(*args, **kwargs)

    def _step_stream(self, memory_step):
        """Keep each tool-calling action step inside its required-call protocol."""
        require_tool_calls = getattr(self.model, "require_tool_calls", None)
        required_call_context = require_tool_calls() if callable(require_tool_calls) else nullcontext()
        try:
            with required_call_context:
                yield from super()._step_stream(memory_step)
        except AgentGenerationError as exc:
            # LiteLLMModelV2 validates native tool calls at the provider
            # boundary. A bad tool name is model output, not an infrastructure
            # or implementation failure, so feed it back as a recoverable
            # parsing error and let the next ReAct step correct itself.
            cause = exc.__cause__
            if isinstance(cause, ToolCallParseError):
                raise AgentParsingError(
                    f"Error while parsing tool call from model output: {cause}",
                    self.logger,
                ) from cause
            raise

    @staticmethod
    def _tool_error_record(exc: Exception) -> ToolErrorRecord:
        """Classify an execution exception without exposing wrapper arguments."""

        cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
        if isinstance(exc, AgentToolCallError):
            kind = "invalid_arguments"
            stage = "input_validation"
            retryable = True
        else:
            kind = str(getattr(cause, "kind", "execution_error"))
            stage = str(getattr(cause, "stage", "tool_execution"))
            retryable = bool(getattr(cause, "retryable", True))
        return ToolErrorRecord(
            kind=kind,
            message=str(cause) or type(cause).__name__,
            retryable=retryable,
            stage=stage,
        )

    def process_tool_calls(self, chat_message, memory_step):
        """Settle every Tool call independently and persist its stable provider ID."""

        assert chat_message.tool_calls is not None
        calls = [
            ToolCall(
                name=call.function.name,
                arguments=call.function.arguments,
                id=call.id,
            )
            for call in chat_message.tool_calls
        ]
        memory_step.tool_calls = calls
        records = [
            ToolCallRecord(
                call_id=call.id,
                tool_name=call.name,
                input=call.arguments or {},
            )
            for call in calls
        ]
        memory_step.tool_results = records

        for call in calls:
            yield call

        def execute_one(index: int) -> tuple[int, ToolOutput]:
            call = calls[index]
            record = records[index]
            record.status = "running"
            record.started_at = time.time()
            try:
                result = self.execute_tool_call(
                    call.name,
                    call.arguments or {},
                    call_id=call.id,
                )
                if isinstance(result, ToolBlockedResult):
                    record.status = "blocked"
                    record.error = ToolErrorRecord(
                        kind=result.kind,
                        message=str(result),
                        retryable=result.retryable,
                        stage=result.stage,
                    )
                    observation = record.model_content()
                    is_final_answer = False
                    output = observation
                elif type(result) in {AgentImage, AgentAudio}:
                    observation_name = "image.png" if type(result) is AgentImage else "audio.mp3"
                    self.state[observation_name] = result
                    observation = f"Stored '{observation_name}' in memory."
                    record.status = "completed"
                    record.output = observation
                    is_final_answer = call.name == "final_answer"
                    output = result
                else:
                    observation = str(result).strip()
                    record.status = "completed"
                    record.output = result
                    is_final_answer = call.name == "final_answer"
                    output = result
            except Exception as exc:
                cause = exc.__cause__ if isinstance(exc.__cause__, Exception) else exc
                record.status = "blocked" if isinstance(cause, ToolBlockedError) else "error"
                record.error = self._tool_error_record(exc)
                observation = record.model_content()
                is_final_answer = False
                output = observation
            finally:
                record.ended_at = time.time()

            self.logger.log(f"Tool {call.name} [{call.id}] -> {record.status}", level=LogLevel.INFO)
            return index, ToolOutput(
                id=call.id,
                output=output,
                is_final_answer=is_final_answer,
                observation=observation,
                tool_call=call,
            )

        outputs: dict[int, ToolOutput] = {}
        if len(calls) == 1:
            index, output = execute_one(0)
            outputs[index] = output
            yield output
        else:
            with ThreadPoolExecutor(self.max_tool_threads) as executor:
                futures = []
                for index in range(len(calls)):
                    context = copy_context()
                    futures.append(executor.submit(context.run, execute_one, index))
                for future in as_completed(futures):
                    index, output = future.result()
                    outputs[index] = output
                    yield output

        memory_step.observations = "\n".join(outputs[index].observation for index in range(len(calls)))

    def execute_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, str] | str,
        *,
        call_id: str | None = None,
    ) -> Any:
        """Execute a tool with AgentLoom-local schema-bound argument coercion."""

        available_tools = {**self.tools, **self.managed_agents}
        if tool_name not in available_tools:
            raise AgentToolCallError(
                f"Unknown tool {tool_name}, should be one of: {', '.join(available_tools)}.",
                self.logger,
            )

        tool = available_tools[tool_name]
        arguments = _normalize_tool_arguments_object(arguments)
        arguments = self._substitute_state_variables(arguments)
        arguments = coerce_tool_arguments(tool, arguments)
        is_managed_agent = tool_name in self.managed_agents

        try:
            validate_tool_arguments(tool, arguments)
        except (ValueError, TypeError) as e:
            raise AgentToolCallError(str(e), self.logger) from e
        except Exception as e:
            error_msg = f"Error executing tool '{tool_name}' with arguments {str(arguments)}: {type(e).__name__}: {e}"
            raise AgentToolExecutionError(error_msg, self.logger) from e

        try:
            with bind_tool_call_id(call_id or uuid.uuid4().hex):
                if isinstance(arguments, dict):
                    return tool(**arguments) if is_managed_agent else tool(**arguments, sanitize_inputs_outputs=True)
                return tool(arguments) if is_managed_agent else tool(arguments, sanitize_inputs_outputs=True)
        except Exception as e:
            if is_managed_agent:
                error_msg = (
                    f"Error executing request to team member '{tool_name}' with arguments {str(arguments)}: {e}\n"
                    "Please try again or request to another team member"
                )
            else:
                error_msg = (
                    f"Error executing tool '{tool_name}' with arguments {str(arguments)}: {type(e).__name__}: {e}\n"
                    "Please try again or use another tool"
                )
            raise AgentToolExecutionError(error_msg, self.logger) from e


class AgentType(Enum):
    """Type of agent in the system."""

    SUPERVISOR = "supervisor"
    WORKER = "worker"
    TOOL_CALLING = "tool_calling"


class BaseAgent(ABC):
    """
    Abstract base class for agents.

    All agents should inherit from this class and implement required abstract methods.
    The base class provides shared initialization logic, model management,
    and execution environment integration.
    """

    # Default configuration, subclasses can override
    tool_call_type = "tool_call"  # tool_call, code_act
    max_steps = 80

    @property
    @abstractmethod
    def name(self) -> str:
        """Agent name."""
        pass

    @property
    @abstractmethod
    def default_model_type(self) -> str | None:
        """
        Default model type used for model selection.

        Returns:
            Optional[str]: Model type string. If None, the default model type
            from configuration is used.
        """
        pass

    @abstractmethod
    def _get_tools(self) -> list:
        """Get the list of tools used by the agent."""
        pass

    def __init__(
        self, model=None, execution_env: Any | None = None, logger: AgentLogger | None = None, model_cache: bool = True
    ):
        """
        Initialize agent.

        Args:
            model: Optional model instance. If omitted, the model manager selects one.
            execution_env: Optional execution environment instance.
            logger: Optional logger instance.
            model_cache: Whether to enable model caching.
        """
        # Initialize model
        model_builder = self._build_model_config_builder()
        if model is None:
            self._model = get_model(
                self.default_model_type,
                "smolagents",
                model_builder=model_builder,
                model_cache=model_cache,
                logger=logger,
            )
        else:
            self._model = model

        # Initialize execution environment
        self._execution_env: Any | None = execution_env

        # Initialize logger
        self._logger = logger

        # Callback list
        self._before_run_callbacks = [self._emit_task_start]

        # Task ID
        self._task_id = None

        # Supervisor runtimes are cached and smolagents agents keep mutable
        # memory/state/python-executor objects. A single BaseAgent instance may
        # therefore only drive its cached runtime once at a time. The lock is
        # per BaseAgent, so independent agents and factory-created workers still
        # run concurrently.
        self._cached_runtime_run_lock = RLock()

        # Generate unique agent ID
        self._agent_id = self._generate_agent_id()

        self._final_answer_checks = []
        self._hook_plan = HookPlan(builtin_hook_handlers())

    def _generate_agent_id(self) -> str:
        """
        Generate a unique agent ID using Snowflake algorithm.

        The prefix reflects the agent's role, e.g. ``supervisor_<id>`` or
        ``worker_<id>``, so IDs are self-describing in logs and traces.

        Returns:
            str: Agent ID in format "{agent_type}_{snowflake_id}".
        """
        prefix = self._get_agent_type().value.lower()
        return generate_id(self.name, prefix=prefix)

    def get_agent_id(self) -> str:
        """
        Get agent ID.

        Returns:
            str: Unique identifier of the agent.
        """
        return self._agent_id

    def set_execution_env(self, execution_env: Any):
        """Set execution environment."""
        self._execution_env = execution_env

    def set_final_answer_checks(self, check_func_list):
        """Set final-answer validation callbacks."""
        self._final_answer_checks = check_func_list

    def _emit_task_start(self, runtime_agent: Any, task: str, *args, **kwargs):
        """Broadcast a generic TaskCreated lifecycle event via the active Hook Run.

        Every explicitly configured ``TaskCreated`` Hook will be notified.

        Worker agents running inside a ``sub_task_context`` skip this event
        because they should only emit SubagentStart/SubagentStop — not
        TaskCreated, which would create a separate visualization.json file.
        """
        _ = runtime_agent
        _ = args
        _ = kwargs
        execution = capture_explicit_execution_context()
        task_id = execution.task_id
        if task_id is None:
            return task

        # Workers run inside sub_task_context; only supervisors fire TaskCreated.
        if execution.sub_task_id is not None:
            return task

        # Collect worker agent names from config (if this is a supervisor)
        worker_names = []
        _config = getattr(self, "_config", None)
        if isinstance(_config, dict):
            for w in _config.get("worker_agents", []):
                if isinstance(w, dict) and "path" in w:
                    worker_names.append(Path(w["path"]).stem)
                elif isinstance(w, str):
                    worker_names.append(w)

        try:
            hook_run = get_current_hook_run(required=True)
            hook_run.dispatch(
                HookEvent.TASK_CREATED,
                "task",
                {
                    "task_id": task_id,
                    "cwd": os.getcwd(),
                    "task_text": task,
                    "agent_name": self.name,
                    "worker_agents": worker_names,
                },
            )
            hook_run.flush_user_messages()
        except Exception as exc:
            if self._logger:
                self._logger.warning("TaskCreated hook error: %s", exc)
        return task

    def _emit_task_lifecycle_event(
        self,
        event: HookEvent,
        task: str,
        *,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        # A delegated Worker is represented by the parent's Subagent lifecycle.
        # TaskCreated/TaskCompleted/StopFailure belong only to the root run.
        execution = capture_explicit_execution_context()
        if execution.sub_task_id is not None:
            return
        task_id = execution.task_id or self._task_id
        if task_id is None:
            return

        payload = {
            "task_id": task_id,
            "cwd": os.getcwd(),
            "task_text": task,
            "agent_name": self.name,
        }
        tool_response = None

        if result is not None:
            tool_response = {"result": result}

        if error is not None:
            payload["error"] = str(error)
            payload["error_type"] = type(error).__name__
            tool_response = {
                "error": str(error),
                "error_type": type(error).__name__,
            }

        try:
            hook_run = get_current_hook_run(required=True)
            hook_run.dispatch(
                event,
                "task",
                payload,
                tool_response=tool_response,
            )
            hook_run.flush_user_messages()
        except Exception as exc:
            if self._logger:
                self._logger.warning("%s hook error: %s", event.value, exc)

    def _emit_session_lifecycle_event(
        self,
        event: HookEvent,
        task: str,
        *,
        result: Any = None,
        error: BaseException | None = None,
    ) -> None:
        task_id = capture_explicit_execution_context().task_id or self._task_id
        payload = {
            "task_id": task_id,
            "cwd": os.getcwd(),
            "task_text": task,
            "agent_name": self.name,
        }
        tool_response = None
        if result is not None:
            tool_response = {"result": result}
        if error is not None:
            payload["error"] = str(error)
            payload["error_type"] = type(error).__name__
            tool_response = {"error": str(error), "error_type": type(error).__name__}

        try:
            hook_run = get_current_hook_run(required=True)
            hook_run.dispatch(
                event,
                "session",
                payload,
                tool_response=tool_response,
            )
            hook_run.flush_user_messages()
        except Exception as exc:
            if self._logger:
                self._logger.warning("%s hook error: %s", event.value, exc)

    def _inject_memory_snapshot(self, tasks: list[str]) -> list[str]:
        if not tasks:
            return tasks
        root_state = None
        try:
            from src.extensions.self_learning.paths import self_learning_enabled
            from src.extensions.self_learning.persistence.memory_store import MemoryStore

            root_state = require_root_run_state()
            effective_config = getattr(self, "_effective_agent_config", None) or self._config
            if not self_learning_enabled(effective_config):
                root_state.get_or_create_memory_snapshot(lambda: "")
                return tasks
            snapshot = MemoryStore().snapshot_for_prompt(
                agent_config=effective_config,
                root_state=root_state,
            )
        except Exception as exc:
            if root_state is not None:
                root_state.get_or_create_memory_snapshot(lambda: "")
            if self._logger:
                self._logger.warning("Memory snapshot injection skipped: %s", exc)
            return tasks
        if not snapshot:
            return tasks
        return [f"{snapshot}\n\n{tasks[0]}", *tasks[1:]]

    def get_execution_tools(self) -> list:
        """
        Get tool list from execution environment.

        Returns:
            List: Execution environment tools.
        """
        if self._execution_env:
            return self._execution_env.tools()
        return []

    def get_all_tools(self, agent_type: str = "worker") -> list:
        """
        Get all available tools.

        Args:
            agent_type: Agent type (retained for compatibility).

        Returns:
            List: Merged tool list.
        """
        execution_tools = self.get_execution_tools()
        agent_tools = self._get_tools()

        return agent_tools + execution_tools

    @staticmethod
    def _resolve_runtime_logger_backend(provided_logger: Any) -> Any:
        if provided_logger is not None:
            return provided_logger
        current_backend = get_global_logger(create_if_missing=False)
        if current_backend is None:
            raise RuntimeError(
                "No logger available for runtime agent construction. "
                "Pass a logger or bind a run-scoped logger backend first."
            )
        return current_backend

    @staticmethod
    def _deduplicate_tools(tools: list[Any]) -> list[Any]:
        uniq_tools: list[Any] = []
        seen = set()
        for tool_item in tools:
            key = tool_item.name if isinstance(tool_item, Tool) else tool_item
            if key in seen:
                continue
            seen.add(key)
            uniq_tools.append(tool_item)
        return uniq_tools

    @staticmethod
    def _emit_hook_user_message(runtime_agent: Any, runtime_logger: Any, message: str) -> None:
        rendered = f"[hook] {message}"
        agent_logger = getattr(runtime_agent, "logger", None)
        if agent_logger is not None and hasattr(agent_logger, "log"):
            agent_logger.log(rendered, level=LogLevel.INFO)
            return
        runtime_logger.info(rendered)

    def _build_runtime_final_answer_checks(self) -> list[Callable]:
        checks = list(self._final_answer_checks)

        def _run_scoped_stop_check(final_answer: Any, memory: Any, **kwargs: Any) -> bool:
            hook_run = get_current_hook_run(required=True)
            return hook_run.build_stop_check()(final_answer, memory, **kwargs)

        checks.insert(0, _run_scoped_stop_check)
        return checks

    def _validate_model(self) -> bool:
        """
        Validate whether model is available.

        Returns:
            bool: Whether model is available.
        """

        return self._model is not None

    def _get_agent_type(self) -> AgentType:
        """
        Get agent type; subclasses should override this method.

        Returns:
            AgentType: Agent type.
        """
        return AgentType.WORKER  # Default to worker agent

    def _build_model_config_builder(self) -> ModelConfigBuilder | None:
        """Model-config overlay hook. Subclasses can return a typed builder."""
        return None


@dataclass(frozen=True)
class AgentRoleProfile:
    agent_type: AgentType
    tool_call_type: str
    cache_runtime_agent: bool = False
    enable_sub_task_tracking: bool = False
    additional_authorized_imports: list[str] | None = None
    inject_default_file_tools: bool = False


class RoleDrivenAgent(BaseAgent):
    """
    Role-driven agent base class.

    Unifies worker/supervisor behavior through role profile and hooks.
    """

    COMMON_REQUIRED_FIELDS: tuple[str, ...] = ("name", "description", "workflow")
    REQUIRED_CONFIG_FIELDS: tuple[str, ...] = ()
    ALLOWED_TOOL_CALL_TYPES: tuple[str, ...] = ("tool_call", "code_act")
    DEFAULT_TOOL_CALL_TYPE: str = "tool_call"

    def __init__(
        self,
        config: dict | None = None,
        project_path: str = "",
        model=None,
        execution_env: Any | None = None,
        logger: AgentLogger | None = None,
        model_cache: bool = True,
        **kwargs,
    ):
        ensure_workspace_mounted_once()

        self._project_path = project_path
        self._runtime_agent = None
        if config is None:
            self._config = {}
        elif isinstance(config, dict):
            self._config = deepcopy(config)
        else:
            raise ValueError(f"Agent config must be a dictionary, got {type(config).__name__}")
        self._normalized = None
        self._execution_normalized: NormalizedExecutionConfig | None = None
        effective_snapshot = build_effective_agent_config_snapshot(
            self._config,
            source_name=str(self._config.get("_yaml_file_path") or self._config.get("name") or self.__class__.__name__),
        )
        self._effective_agent_config_snapshot = effective_snapshot
        self._effective_agent_config = effective_snapshot.values

        self._before_config_validation(**kwargs)
        normalized: Any | None = self._validate_config()
        if normalized is not None:
            self._normalized = normalized

        resolved_logger = self.resolve_agent_logger_from_config(
            self._config,
            provided_logger=logger,
        )

        super().__init__(model=model, execution_env=execution_env, logger=resolved_logger, model_cache=model_cache)
        self.tool_call_type = self._role_profile().tool_call_type

        runtime_logger = self._effective_logger()
        self._skill_catalog = self.initialize_skill_catalog(logger=runtime_logger)
        hook_layers = tuple(
            HookConfigLayer(
                name=layer.name,
                config=layer.data,
                agent_root=layer.root,
                source_path=layer.source_path,
                priority=priority,
            )
            for priority, layer in enumerate(effective_snapshot.layers)
        )
        self._hook_plan = HookPlanCompiler().compile(
            hook_layers,
            internal_handlers=builtin_hook_handlers(),
        )
        self._after_role_init(**kwargs)

    def project_path(self):
        return self._project_path

    @property
    def name(self) -> str:
        return str(self._config.get("name", self.__class__.__name__))

    @property
    def description(self) -> str:
        return str(self._config.get("description", ""))

    @property
    def default_model_type(self) -> str | None:
        model_type = self._config.get("model_type")
        if model_type is None:
            return None
        return str(model_type)

    def _effective_logger(self) -> AgentLogger | None:
        return getattr(self, "logger", None) or getattr(self, "_logger", None)

    def _before_config_validation(self, **kwargs) -> None:
        """Hook: run before config validation."""

    def _after_role_init(self, **kwargs) -> None:
        """Hook: run after role-driven initialization."""
        if "max_steps" in self._config:
            self.max_steps = self._config["max_steps"]

    def _required_config_fields(self) -> tuple[str, ...]:
        return tuple(self.REQUIRED_CONFIG_FIELDS)

    def _resolve_tool_call_type(self) -> str:
        return AgentConfigNormalizer.resolve_tool_call_type(
            self._config,
            default_tool_call_type=self.DEFAULT_TOOL_CALL_TYPE,
            allowed_tool_call_types=self.ALLOWED_TOOL_CALL_TYPES,
        )

    def _validate_role_specific_config(self, normalized: Any | None) -> None:
        """Role-specific validation hook after common validation and normalization."""

    def _validate_config(self) -> Any | None:
        """Validate config with common template and return normalized object when applicable."""
        normalized = AgentConfigNormalizer.validate_role_driven_config(
            self._config,
            required_fields=self._required_config_fields(),
            default_tool_call_type=self.DEFAULT_TOOL_CALL_TYPE,
            allowed_tool_call_types=self.ALLOWED_TOOL_CALL_TYPES,
            build_normalized=self._build_normalized_config,
            validate_role_specific=self._validate_role_specific_config,
        )
        self._execution_normalized = self._build_execution_normalized_config()
        return normalized

    def _build_normalized_config(self) -> Any | None:
        """Build normalized config object when needed."""
        return None

    def _ensure_normalized(self) -> Any | None:
        if self._normalized is None:
            self._normalized = self._build_normalized_config()
        return self._normalized

    def _execution_validation_agent_root(self) -> str:
        return str(C.agent_root)

    def _build_execution_normalized_config(self) -> NormalizedExecutionConfig:
        cfg = dict(self._config)
        effective = getattr(self, "_effective_agent_config", None)
        if effective and "execution_env" in effective:
            cfg["execution_env"] = effective["execution_env"]

        return build_normalized_execution_config(
            cfg,
            source_name=self.__class__.__name__,
            agent_root=self._execution_validation_agent_root(),
        )

    def _ensure_execution_normalized(self) -> NormalizedExecutionConfig:
        if self._execution_normalized is None:
            self._execution_normalized = self._build_execution_normalized_config()
        return self._execution_normalized

    @staticmethod
    def resolve_agent_logger_from_config(
        config: dict,
        *,
        provided_logger: AgentLogger | None = None,
    ) -> AgentLogger | None:
        _ = config
        if provided_logger is not None:
            return provided_logger

        current_backend = get_global_logger(create_if_missing=False)
        if current_backend is None:
            raise RuntimeError(
                "No logger available for role-driven agent initialization. "
                "Pass a logger or bind a run-scoped logger backend first."
            )
        return current_backend

    def initialize_skill_catalog(self, logger: AgentLogger | None = None) -> SkillCatalog:
        """Resolve conventional and explicitly configured Skill sources once."""
        log = get_logger(logger, __name__)
        sources: list[SkillSource] = []
        for layer in self._effective_agent_config_snapshot.layers:
            scope = {
                "global_system": "project",
                "application_system": "application",
                "agent": "agent",
            }.get(layer.name)
            if scope is None:
                continue

            if scope in {"project", "application"}:
                sources.append(SkillSource(path=Path(layer.root) / "skills", scope=scope))

            configured = layer.data.get("skills") if isinstance(layer.data, dict) else None
            if configured is None:
                continue
            if not isinstance(configured, dict) or set(configured) != {"paths"}:
                raise ValueError("skills must be a mapping containing only a 'paths' list")
            paths = configured["paths"]
            if not isinstance(paths, list) or any(not isinstance(item, str) or not item.strip() for item in paths):
                raise ValueError("skills.paths must be a list of non-empty path strings")
            for raw_path in paths:
                path = Path(raw_path).expanduser()
                if not path.is_absolute():
                    path = Path(layer.root) / path
                sources.append(SkillSource(path=path, scope=scope))

        catalog = SkillCatalog.discover(sources, logger=log)
        log.info("Agent '%s' resolved Skills: %s", self.name, [item.name for item in catalog.summaries()])
        return catalog

    @abstractmethod
    def _role_profile(self) -> AgentRoleProfile:
        """Return role profile."""
        raise NotImplementedError

    def _runtime_agent_name(self) -> str | None:
        """Optional runtime-level name passed to smolagents."""
        return None

    def _runtime_agent_description(self) -> str | None:
        """Optional runtime-level description passed to smolagents."""
        return None

    def _build_model_config_builder(self) -> ModelConfigBuilder | None:
        return None

    def _resolve_max_tokens_from_config(self) -> int:
        try:
            return C.llm.for_type(self.default_model_type).max_tokens
        except Exception:
            return DEFAULT_MAX_TOKENS

    def _resolve_smart_summary_from_config(self) -> bool:
        effective_cfg = self._effective_agent_config
        return effective_cfg.get("smart_summary", True) if isinstance(effective_cfg, dict) else True

    def _resolve_todo_mode(self) -> str:
        effective_cfg = self._effective_agent_config
        config = effective_cfg if isinstance(effective_cfg, dict) else self._config
        return validate_todo_config(config, source=self.name)

    def _build_execution_agent_kwargs(self, profile: AgentRoleProfile) -> dict[str, Any]:
        """Build validated runtime kwargs for `_create_agent`."""
        self._ensure_normalized()
        execution_normalized = validate_execution_config_payload(self._ensure_execution_normalized())
        log = get_logger(self._effective_logger(), __name__)
        raw_planning_interval = self._config.get("planning_interval")
        if raw_planning_interval is not None and execution_normalized.planning_interval is None:
            log.warning(
                "Ignored invalid '%s.planning_interval'=%r; expected a positive integer or numeric string.",
                self.name,
                raw_planning_interval,
            )

        code_agent_cfg = get_code_agent_config(self._effective_agent_config)
        return {
            "additional_authorized_imports": profile.additional_authorized_imports,
            "additional_functions": code_agent_cfg.get("additional_functions", {}),
            "enable_sub_task_tracking": profile.enable_sub_task_tracking,
            "agent_name": self.name if profile.enable_sub_task_tracking else None,
            "executor_type": execution_normalized.executor_type,
            "executor_kwargs": dict(execution_normalized.executor_kwargs),
            "prompt_template_path": execution_normalized.prompt_template_path,
            "planning_interval": execution_normalized.planning_interval,
            "max_tokens": self._resolve_max_tokens_from_config(),
            "smart_summary": self._resolve_smart_summary_from_config(),
            "runtime_name": self._runtime_agent_name(),
            "runtime_description": self._runtime_agent_description(),
            "todo_mode": self._resolve_todo_mode(),
        }

    def _transform_task(self, task: str) -> str:
        """Task transformation hook."""
        return task

    def _transform_tasks(self, task: str) -> list[str]:
        """Transform a caller task into one or more runtime tasks."""
        transformed_task = self._transform_task(task)
        return [transformed_task]

    def _extra_telemetry_kwargs(self) -> dict:
        """Extra telemetry parameters (kept for subclass compatibility)."""
        return {}

    def _get_agent_type(self) -> AgentType:
        return self._role_profile().agent_type

    def _build_runtime_tools(self, profile: AgentRoleProfile) -> list:
        tools = self.get_all_tools(agent_type=profile.agent_type.value.lower())
        if profile.agent_type is AgentType.SUPERVISOR:
            from src.lib.goal import normalize_goal_config

            goal = normalize_goal_config(
                self._config,
                source=self._config.get("name", "supervisor"),
            )
            if goal.enabled:
                from src.tools.goal import get_goal, update_goal

                tools = [*tools, get_goal, update_goal]
        todo_mode = self._resolve_todo_mode()
        if todo_mode == "off":
            return [
                runtime_tool
                for runtime_tool in tools
                if getattr(
                    runtime_tool,
                    "name",
                    getattr(runtime_tool, "__name__", None),
                )
                != "todo_write"
            ]

        tool_names = {
            getattr(runtime_tool, "name", getattr(runtime_tool, "__name__", None))
            for runtime_tool in tools
        }
        if "todo_write" not in tool_names:
            tools = [*tools, resolve_tool_function("todo_write")]
        return tools

    def build_runtime_agent(self) -> CodeAgent:
        profile = self._role_profile()

        if profile.cache_runtime_agent and self._runtime_agent is not None:
            # The smolagents runtime may be cached, but Tool instances are
            # run-owned. Refresh them from shared definitions so mutable Tool
            # state cannot survive into the next invocation.
            fresh_tools = self._prepare_runtime_tools(self._build_runtime_tools(profile))
            self._runtime_agent.tools = {tool.name: tool for tool in fresh_tools}
            return self._runtime_agent

        runtime_agent = self._create_agent(
            tools=self._build_runtime_tools(profile),
            **self._build_execution_agent_kwargs(profile),
        )

        if profile.cache_runtime_agent:
            self._runtime_agent = runtime_agent

        return runtime_agent

    def _prepare_runtime_tools(self, tools: list[Any]) -> list[Tool]:
        wrapped = ensure_tool_wrapped(self._deduplicate_tools(tools))
        return [inject_hooks(clone_tool_for_runtime(tool)) for tool in wrapped]

    def _resolve_effective_prompt_template_path(self) -> str | None:
        # Priority: current agent effective config -> global system baseline.
        effective_cfg = self._effective_agent_config
        if isinstance(effective_cfg, dict):
            prompt_cfg = effective_cfg.get("prompt")
            if prompt_cfg is not None:
                return normalize_execution_prompt_template_path_value(
                    prompt_cfg,
                    f"{self.name}.effective.prompt",
                    agent_root=C.agent_root,
                )

        prompt_cfg = C.get("prompt")
        if prompt_cfg is not None:
            return normalize_execution_prompt_template_path_value(
                prompt_cfg,
                "config.prompt",
                agent_root=C.agent_root,
            )
        return None

    def _build_prompt_templates(
        self,
        *,
        runtime_logger: Any,
        use_customized_prompt: bool,
        prompt_template_path: str | None,
        skill_tool_enabled: bool,
    ) -> Any:
        if not use_customized_prompt:
            return None

        return build_prompt_templates(
            prompt_template_path=prompt_template_path,
            effective_prompt_path=self._resolve_effective_prompt_template_path(),
            model_id=getattr(self._model, "model_id", None) if self._model else None,
            agent_root=C.agent_root,
            skill_catalog=self._skill_catalog,
            skill_tool_enabled=skill_tool_enabled,
            logger=runtime_logger,
            tool_call_type=self.tool_call_type,
            use_structured_output=getattr(self._model, "supports_structured_output", "false") == "true",
            todo_mode=self._resolve_todo_mode(),
        )

    def _create_agent(
        self,
        tools: list | None = None,
        *,
        additional_authorized_imports: list[str] | None = None,
        additional_functions: dict[str, Any] | None = None,
        enable_sub_task_tracking: bool = False,
        agent_name: str | None = None,
        use_customized_prompt: bool = True,
        prompt_template_path: str | None = None,
        executor_type: str | None = None,
        executor_kwargs: dict[str, Any] | None = None,
        planning_interval: int | None = None,
        max_tokens: int | None = None,
        smart_summary: bool | None = None,
        runtime_name: str | None = None,
        runtime_description: str | None = None,
        todo_mode: str = "auto",
    ) -> CodeAgent:
        """
        Create configured agent instance.

        Args:
            tools: Tool list. If omitted, use get_all_tools().

        Returns:
            CodeAgent: Configured agent instance.
        """
        if tools is None:
            tools = self.get_all_tools()

        resolved_logger_backend = self._resolve_runtime_logger_backend(self._logger)
        runtime_logger = get_logger(resolved_logger_backend, __name__)

        hooked_tools = self._prepare_runtime_tools(tools)

        normalized_planning_interval = normalize_positive_int_value(planning_interval)
        if planning_interval is not None and normalized_planning_interval is None:
            runtime_logger.warning(
                "Ignored invalid planning_interval=%r; expected a positive integer or numeric string.",
                planning_interval,
            )

        agent_kwargs: dict[str, Any] = {
            "model": self._model,
            "verbosity_level": LogLevel.INFO,
            "max_steps": self.max_steps,
            "logger": resolved_logger_backend,
            "before_run_callbacks": list(self._before_run_callbacks),
            "final_answer_checks": self._build_runtime_final_answer_checks(),
        }
        if executor_type is not None:
            agent_kwargs["executor_type"] = executor_type
        if executor_kwargs is not None:
            agent_kwargs["executor_kwargs"] = dict(executor_kwargs)
        if normalized_planning_interval is not None:
            agent_kwargs["planning_interval"] = normalized_planning_interval
        if max_tokens is not None:
            agent_kwargs["max_tokens"] = max_tokens
        if smart_summary is not None:
            agent_kwargs["smart_summary"] = smart_summary
        if runtime_name is not None:
            agent_kwargs["name"] = runtime_name
        if runtime_description is not None:
            agent_kwargs["description"] = runtime_description

        if additional_functions is not None:
            if executor_type in {"docker", "e2b"}:
                runtime_logger.info(
                    "Skipped additional_functions injection for executor_type='%s' because the executor "
                    "constructor does not support this key.",
                    executor_type,
                )
            else:
                agent_kwargs.setdefault("executor_kwargs", {})
                agent_kwargs["executor_kwargs"]["additional_functions"] = dict(additional_functions)
                runtime_logger.info(
                    "Added additional_functions to executor_kwargs: %s",
                    list(additional_functions.keys()),
                )

        resolved_additional_authorized_imports = additional_authorized_imports
        if resolved_additional_authorized_imports is not None:
            resolved_additional_authorized_imports = list(resolved_additional_authorized_imports)
            if executor_type in {"docker", "e2b", "wasm"} and "*" in resolved_additional_authorized_imports:
                resolved_additional_authorized_imports = [
                    item for item in resolved_additional_authorized_imports if item != "*"
                ]
                runtime_logger.info(
                    "Removed wildcard '*' from additional_authorized_imports for executor_type='%s' "
                    "to avoid remote package-install side effects; keeping explicit imports: %s",
                    executor_type,
                    resolved_additional_authorized_imports,
                )

        prompt_templates = self._build_prompt_templates(
            runtime_logger=runtime_logger,
            use_customized_prompt=use_customized_prompt,
            prompt_template_path=prompt_template_path,
            skill_tool_enabled=any(getattr(tool, "name", None) == "skill" for tool in hooked_tools),
        )

        if self.tool_call_type == "tool_call":
            agent = ToolCallingAgentV2(
                tools=hooked_tools,
                stream_outputs=False,
                prompt_templates=prompt_templates,
                **agent_kwargs,
            )
        else:
            use_structured = getattr(self._model, "supports_structured_output", "false") == "true"
            agent = CodeAgentV2(
                tools=hooked_tools,
                stream_outputs=False,
                prompt_templates=prompt_templates,
                additional_authorized_imports=resolved_additional_authorized_imports,
                use_structured_outputs_internally=use_structured,
                **agent_kwargs,
            )

        agent._agent_loom_todo_mode = todo_mode

        # Apply circuit-breaker threshold from YAML config (default: 5 consecutive parse errors)
        max_parse_errors = self._config.get("max_consecutive_parse_errors", 5)
        agent._max_consecutive_parse_errors = max_parse_errors  # type: ignore[attr-defined]

        if enable_sub_task_tracking:
            resolved_agent_name = agent_name or self.name
            agent = SubTaskTrackedAgent(agent, resolved_agent_name)

        return agent

    def _bind_hook_message_sink(self, runtime_agent: Any) -> None:
        """Bind delivery to the current run, including cached runtimes."""

        hook_run = get_current_hook_run(required=True)
        runtime_logger = get_logger(
            getattr(runtime_agent, "logger", None) or self._effective_logger(),
            __name__,
        )
        hook_run.set_user_message_sink(
            lambda message: self._emit_hook_user_message(
                runtime_agent,
                runtime_logger,
                message,
            )
        )

    def run(
        self,
        task: str,
        task_id: str | None = None,
        run_id: str | None = None,
        checkpoint_manager: Any | None = None,
        application_lifecycle: "ApplicationRunLifecycle | None" = None,
        resume: bool = False,
        additional_args: dict[str, Any] | None = None,
    ) -> str:
        """Run inside one explicit root-run binding.

        The first agent in the call tree owns the binding and the session
        lifecycle. Delegated agents inherit the root through ``ContextVar``
        propagation and therefore cannot emit duplicate SessionStart/End.
        """

        def _run_once() -> str:
            # Every invocation gets a fresh local id. The outermost invocation
            # also owns it as the root; delegated workers keep their own local id.
            local_run_id = run_id or str(uuid.uuid4())
            with bind_local_run(local_run_id):
                with bind_root_run(local_run_id) as owns_root_run:
                    return self._run_with_root_context(
                        task,
                        task_id=task_id,
                        checkpoint_manager=checkpoint_manager,
                        application_lifecycle=application_lifecycle,
                        resume=resume,
                        additional_args=additional_args,
                        owns_root_run=owns_root_run,
                    )

        role_profile_resolver = getattr(self, "_role_profile", None)
        cache_runtime_agent = bool(
            role_profile_resolver().cache_runtime_agent if callable(role_profile_resolver) else False
        )
        if cache_runtime_agent:
            with self._cached_runtime_run_lock:
                return _run_once()
        return _run_once()

    def _run_with_root_context(
        self,
        task: str,
        task_id: str | None = None,
        checkpoint_manager: Any | None = None,
        application_lifecycle: "ApplicationRunLifecycle | None" = None,
        resume: bool = False,
        additional_args: dict[str, Any] | None = None,
        *,
        owns_root_run: bool,
    ) -> str:
        from src.lib.checkpoint.coordinator import CheckpointCoordinator
        from src.lib.goal import (
            GoalStateProvider,
            build_goal_objective,
            goal_objective_fingerprint,
            normalize_goal_config,
        )

        # Transform task before passing it to the runtime agent.
        transformed_tasks = self._transform_tasks(task)
        if not transformed_tasks:
            raise ValueError("Agent task transformation produced no tasks")
        transformed_tasks = self._inject_memory_snapshot(transformed_tasks)
        transformed_task = "\n\n".join(transformed_tasks)
        goal_config = normalize_goal_config(
            self._config,
            source=self._config.get("name", "agent"),
        )
        goal_objective = None
        goal_fingerprint = None
        if goal_config.enabled:
            if not owns_root_run:
                raise ValueError("Goal mode can only be configured by the root Supervisor Agent")
            goal_objective = build_goal_objective(
                description=str(self._config.get("description", "")),
                workflow=self._config["workflow"],
                task=task,
            )
            goal_fingerprint = goal_objective_fingerprint(
                description=str(self._config.get("description", "")),
                workflow=self._config["workflow"],
                task=task,
            )
        # Determine ID
        parent_execution_context = capture_explicit_execution_context()
        current_task_id = parent_execution_context.task_id
        final_task_id = (
            current_task_id
            or task_id
            or generate_id(f"{self._get_agent_type().value.lower()}_{self.name}", prefix="task")
        )
        self._task_id = final_task_id

        # Supervisor activates a new coordinator; workers inherit via ContextVar.
        if checkpoint_manager is not None:
            coord = CheckpointCoordinator.activate(
                checkpoint_manager,
                final_task_id,
                transformed_task,
                resume=resume,
                effective_config=self._effective_agent_config or self._config,
            )
        else:
            coord = CheckpointCoordinator.current()

        owns_application_lifecycle = False
        if application_lifecycle is None and checkpoint_manager is not None:
            from src.application_run_lifecycle import ApplicationRunLifecycle

            application_lifecycle = ApplicationRunLifecycle()
            application_lifecycle.enter_execution()
            owns_application_lifecycle = True

        goal_provider = None
        if goal_config.enabled:
            assert goal_objective is not None and goal_fingerprint is not None
            try:
                goal_provider = GoalStateProvider.initialize(
                    config=goal_config,
                    objective=goal_objective,
                    objective_fingerprint=goal_fingerprint,
                    resume=resume,
                )
            except Exception:
                if checkpoint_manager is not None and coord is not None:
                    CheckpointCoordinator.deactivate(coord)
                raise

        def _execute_agent():
            session_started = False
            session_result = None
            session_error: BaseException | None = None
            runtime_agent = None
            # Inject agent_id into model (for LiteLLM/Langfuse tracing)
            agent_id = self.get_agent_id()
            previous_model_agent_id = getattr(self._model, "agent_id", ...) if hasattr(self._model, "agent_id") else ...
            if previous_model_agent_id is not ...:
                self._model.agent_id = agent_id

            active_context = capture_explicit_execution_context()
            hook_agent_config = self._effective_agent_config or self._config
            if goal_config.enabled:
                # Goal is an Agent-owned lifecycle field, not a system overlay.
                # Preserve the raw validated value in the execution-time
                # identity snapshot used by root-only Goal tool guards.
                hook_agent_config = {
                    **hook_agent_config,
                    "goal": deepcopy(self._config["goal"]),
                }
            hook_run = HookRun(
                self._hook_plan,
                local_run_id=active_context.local_run_id or "",
                root_run_id=active_context.root_run_id or "",
                parent=active_context.hook_run,
                agent_config=hook_agent_config,
                project_root=str(C.agent_root),
            )
            previous_runtime_path = active_context.runtime_agent_path
            if previous_runtime_path:
                runtime_path = f"{previous_runtime_path}/{self.name}"
            else:
                runtime_path = self.name
            execution_binding = bind_explicit_execution_context(
                replace(
                    active_context,
                    agent_id=agent_id,
                    agent_name=self.name,
                    agent_config=self._effective_agent_config or self._config,
                    skill_catalog=self._skill_catalog,
                    hook_run=hook_run,
                    runtime_agent_path=runtime_path,
                )
            )
            execution_binding.__enter__()
            from src.lib.goal import bind_goal_state_provider
            from src.lib.todo import ensure_todo_state_provider

            goal_provider_binding = (
                bind_goal_state_provider(goal_provider)
                if goal_provider is not None
                else nullcontext(None)
            )
            goal_provider_binding.__enter__()
            todo_provider_binding = ensure_todo_state_provider()
            todo_provider_binding.__enter__()

            try:
                # Build tools only after the complete explicit context has
                # been bound.  LocalPythonExecutor/tool wrappers capture this
                # context before crossing their timeout thread boundary.
                runtime_agent = self.build_runtime_agent()
                self._bind_hook_message_sink(runtime_agent)
                ensure_workspace_mounted_once()
                if owns_root_run:
                    self._emit_session_lifecycle_event(
                        HookEvent.SESSION_START,
                        transformed_task,
                    )
                    session_started = True

                if coord is not None:
                    # ── Resume: restore memory from checkpoint ──
                    if resume and checkpoint_manager is not None:
                        coord.restore(runtime_agent)
                        # Sync heartbeat step to restored count so dashboard
                        # shows the correct value immediately (not 0).
                        if coord._supervisor_heartbeat is not None:
                            try:
                                coord._supervisor_heartbeat.update_step(len(runtime_agent.memory.steps))
                            except Exception:
                                pass

                    # ── Incremental checkpoint: register step callback ──
                    if checkpoint_manager is not None:
                        # Supervisor: register and store callback for workers.
                        coord.register_supervisor_step_callback(runtime_agent)
                    else:
                        # Worker: inherit the supervisor's callback.  The
                        # invocation later passes that runtime explicitly when
                        # atomic preparation allocates its call_index.
                        coord.register_worker_step_callback(runtime_agent, agent_name=self.name)

                # Pass reset=False when resuming (preserves injected memory) and
                # for later workflow items (preserves memory from previous runs).
                # Always request the structured result.  smolagents otherwise
                # returns only its fallback output for a max-steps termination,
                # which is indistinguishable from a successful final answer and
                # would incorrectly emit TaskCompleted and run memory review.
                result = None
                if goal_provider is not None:
                    initial_state = goal_provider.snapshot()
                    segment_index = 0
                    while True:
                        state = goal_provider.snapshot()
                        if state.status == "complete":
                            result = state.evidence
                            break
                        goal_provider.assert_request_allowed()
                        use_initial_context = segment_index == 0 and not initial_state.goal_started
                        current_task = (
                            transformed_tasks[0]
                            if use_initial_context
                            else _goal_continuation_prompt(state)
                        )
                        run_kwargs = {
                            "task": current_task,
                            "return_full_result": True,
                        }
                        if additional_args:
                            run_kwargs["additional_args"] = dict(additional_args)
                        if resume or segment_index > 0 or not use_initial_context:
                            run_kwargs["reset"] = False
                        if (
                            not use_initial_context
                            and getattr(
                                runtime_agent,
                                "_agent_loom_supports_reset_false_task_step_control",
                                False,
                            )
                        ):
                            run_kwargs["_skip_task_step_on_reset_false"] = False
                        try:
                            run_result = runtime_agent.run(**run_kwargs)
                        except Exception as exc:
                            from src.lib.goal import (
                                GoalBudgetLimitedError,
                                GoalCompleteError,
                            )

                            terminal_state = goal_provider.snapshot()
                            if (
                                isinstance(exc, GoalCompleteError)
                                or terminal_state.status == "complete"
                            ):
                                result = terminal_state.evidence
                                break
                            if terminal_state.status == "budget_limited":
                                raise GoalBudgetLimitedError(terminal_state) from exc
                            raise
                        _require_goal_runtime_result(run_result)
                        segment_output = getattr(run_result, "output", None)
                        segment_index += 1
                        state = goal_provider.snapshot()
                        if state.status == "complete":
                            # A CodeAgent can commit completion and call
                            # final_answer in the same model response. Preserve
                            # that user-facing delivery; evidence is only the
                            # durable fallback when no final output settled.
                            result = _goal_completion_output(
                                segment_output,
                                state.evidence,
                            )
                            break
                        goal_provider.assert_request_allowed()
                else:
                    for task_index, current_task in enumerate(transformed_tasks):
                        run_kwargs: dict = {
                            "task": current_task,
                            "return_full_result": True,
                        }
                        if additional_args:
                            run_kwargs["additional_args"] = dict(additional_args)
                        if resume or task_index > 0:
                            run_kwargs["reset"] = False
                        if task_index > 0 and getattr(
                            runtime_agent, "_agent_loom_supports_reset_false_task_step_control", False
                        ):
                            run_kwargs["_skip_task_step_on_reset_false"] = False
                        run_result = runtime_agent.run(**run_kwargs)
                        _require_successful_runtime_result(run_result)
                        result = getattr(run_result, "output", None)

                # Compatibility fallback for wrapper paths that cannot read
                # memory directly from the runtime agent.
                try:
                    _current_worker_memory.set(list(runtime_agent.memory.steps))
                except Exception:
                    pass

                self._emit_task_lifecycle_event(
                    HookEvent.TASK_COMPLETED,
                    transformed_task,
                    result=result,
                )
                session_result = result

                return result
            except BaseException as exc:
                from src.lib.goal import GoalBudgetLimitedError

                session_error = exc
                if isinstance(exc, GoalBudgetLimitedError):
                    raise
                if isinstance(exc, Exception):
                    self._emit_task_lifecycle_event(
                        HookEvent.STOP_FAILURE,
                        transformed_task,
                        error=exc,
                    )
                raise
            finally:
                application_lifecycle_error: BaseException | None = None
                try:
                    goal_snapshot = (
                        goal_provider.snapshot().to_dict()
                        if goal_provider is not None
                        else None
                    )
                    if application_lifecycle is not None:
                        try:
                            application_lifecycle.report_agent_invocation(
                                coordinator=coord,
                                runtime_agent=runtime_agent,
                                result=session_result,
                                error=session_error,
                                goal=goal_snapshot,
                            )
                            if owns_application_lifecycle:
                                application_lifecycle.settle_reported_agent_invocation()
                                application_lifecycle.commit_checkpoint(
                                    checkpoint_manager=checkpoint_manager,
                                    task_id=final_task_id,
                                )
                        except BaseException as exc:
                            application_lifecycle_error = exc
                    if session_started:
                        self._emit_session_lifecycle_event(
                            HookEvent.SESSION_END,
                            transformed_task,
                            result=session_result,
                            error=session_error,
                        )
                        if session_error is None:
                            # SessionEnd owns only the short, deterministic ledger
                            # write. Once a successful run is recorded, the root
                            # owner may perform the optional synchronous review
                            # while its trusted binding is still alive. Workers do
                            # not enter this block, and review failure cannot change
                            # the successful task result.
                            try:
                                from src.extensions.self_learning.paths import (
                                    review_config,
                                    self_learning_enabled,
                                )

                                effective_config = (
                                    self._effective_agent_config or self._config
                                )
                                review_policies = (
                                    review_config(effective_config, scope="application"),
                                    review_config(effective_config, scope="project"),
                                )
                                if (
                                    self_learning_enabled(effective_config)
                                    and any(
                                        policy.get("enabled")
                                        and str((policy.get("trigger") or {}).get("mode") or "manual")
                                        != "manual"
                                        for policy in review_policies
                                    )
                                ):
                                    from src.extensions.self_learning.reviewer import (
                                        review_finished_run,
                                    )

                                    review_finished_run(
                                        root_run_id=require_root_run_id(),
                                        agent_config=effective_config,
                                    )
                            except Exception:
                                if self._logger:
                                    self._logger.warning("Completed-run memory review failed unexpectedly")
                    if previous_model_agent_id is not ...:
                        self._model.agent_id = previous_model_agent_id
                finally:
                    try:
                        if (
                            owns_application_lifecycle
                            and checkpoint_manager is not None
                            and coord is not None
                        ):
                            assert application_lifecycle is not None
                            application_lifecycle.close_agent_coordinator(coord)
                    finally:
                        try:
                            todo_provider_binding.__exit__(None, None, None)
                        finally:
                            try:
                                goal_provider_binding.__exit__(None, None, None)
                            finally:
                                execution_binding.__exit__(None, None, None)
                if application_lifecycle_error is not None:
                    raise application_lifecycle_error

        if current_task_id:
            return _execute_agent()

        # Bind a fresh task id without mutating the legacy process-global
        # fallback, which can belong to another concurrent top-level run.
        with bind_explicit_execution_context(replace(parent_execution_context, task_id=final_task_id)):
            return _execute_agent()


class SubTaskTrackedAgent:
    """
    Sub-task tracing wrapper that provides an isolated tracing chain for worker agents.

    This class wraps the original CodeAgent/ToolCallingAgent and automatically
    creates sub-task context during execution.
    Telemetry collection has been removed; agent_id is injected for LiteLLM/Langfuse tracing.
    """

    def __init__(self, agent, agent_name: str):
        """
        Initialize sub-task tracing wrapper.

        Args:
            agent: Original CodeAgent or ToolCallingAgent instance.
            agent_name: Agent name, used to generate sub-task IDs.
        """
        self._agent = agent
        self._agent_name = agent_name
        self._log = get_logger(getattr(agent, "logger", None), __name__)

        # Proxy all attributes to original agent (except overridden methods)
        excluded_attrs = {"run", "__call__"}
        for attr in dir(self._agent):
            if (
                not attr.startswith("_")
                and attr not in excluded_attrs
                and hasattr(self._agent, attr)
                and not callable(getattr(self._agent, attr, None))
            ):
                setattr(self, attr, getattr(self._agent, attr))

    @staticmethod
    def _compute_input_hash(task_text: str) -> str:
        """Short hash of the worker input for skip-on-resume matching."""
        return _hashlib.sha256(str(task_text).encode()).hexdigest()[:16]

    def _snapshot_worker_memory(self) -> list | None:
        """Return the wrapped runtime agent memory at the lifecycle boundary."""
        try:
            memory = getattr(self._agent, "memory", None)
            steps = getattr(memory, "steps", None)
            if steps is not None:
                return list(steps)
        except Exception:
            pass
        return _current_worker_memory.get(None)

    def _execute_with_lifecycle(self, callable_fn, task, call_label, *args, **kwargs):
        """Run callable within sub-task context, broadcasting lifecycle events.

        Emits ``SubagentStart`` before execution and ``SubagentStop`` after
        (with ``success`` / ``error`` fields). Explicitly configured Hooks may
        observe these parent-owned lifecycle events.

        Worker preparation is atomic: a resumed invocation claims unfinished
        work first, otherwise claims one completed result, or allocates a new
        call.  Fresh runs always allocate and execute.
        """
        from src.lib.checkpoint.coordinator import CheckpointCoordinator

        with sub_task_context(self._agent_name) as sub_task_id:
            self._log.debug(f"Starting sub-task {sub_task_id} (agent: {self._agent_name}) via {call_label}")

            coord = CheckpointCoordinator.current()
            input_hash = self._compute_input_hash(task)

            # Claim/allocate exactly one logical call before side effects.  The
            # explicit outcome distinguishes a cached ``None``/empty result
            # from an invocation that still needs execution.
            if coord is not None:
                preparation = coord.prepare_worker_call(
                    self._agent_name,
                    input_hash,
                    str(task),
                    runtime_agent=self._agent,
                )
                if not preparation.should_execute:
                    self._log.info(
                        "Skipping completed worker %s (input_hash=%s)",
                        self._agent_name,
                        input_hash[:8],
                    )
                    return preparation.cached_result
                call_index = preparation.call_index
            else:
                call_index = 0

            lifecycle_run = get_current_hook_run(required=True)
            lifecycle_context = capture_explicit_execution_context()
            # Some managed-agent adapters enter the callee's HookRun before
            # invoking this wrapper. Subagent lifecycle belongs to the caller;
            # tool events inside the worker remain on the child run.
            if (
                lifecycle_context.agent_name == self._agent_name
                and (lifecycle_context.runtime_agent_path or "").split("/")[-1] == self._agent_name
                and lifecycle_run.parent is not None
            ):
                lifecycle_run = lifecycle_run.parent
            event_payload = {
                "agent_name": self._agent_name,
                "sub_task_id": sub_task_id,
            }
            try:
                lifecycle_run.dispatch(
                    HookEvent.SUBAGENT_START,
                    self._agent_name,
                    event_payload,
                )
                lifecycle_run.flush_user_messages()
            except Exception as hook_err:
                self._log.warning("SubagentStart hook error: %s", hook_err)

            worker_restored = (
                coord.restore_worker(self._agent, self._agent_name, call_index) if coord is not None else False
            )
            if worker_restored:
                kwargs.setdefault("reset", False)

            try:
                result = callable_fn(task, *args, **kwargs)
                # RoleDrivenAgent requests a structured RunResult from its
                # smolagents runtime.  Validate that result before crossing
                # the checkpoint success boundary; the outer RoleDrivenAgent
                # check is intentionally too late to prevent a completed
                # worker checkpoint from being reused on resume.
                if isinstance(result, RunResult):
                    _require_successful_runtime_result(result)
            except KeyboardInterrupt:
                if coord is not None:
                    coord.record_worker_interrupted(
                        self._agent_name,
                        call_index,
                        input_hash,
                        str(task),
                        self._snapshot_worker_memory(),
                    )
                raise
            except Exception as exc:
                if coord is not None:
                    coord.record_worker_failure(
                        self._agent_name,
                        call_index,
                        input_hash,
                        str(task),
                        str(exc),
                        self._snapshot_worker_memory(),
                    )
                try:
                    lifecycle_run.dispatch(
                        HookEvent.SUBAGENT_STOP,
                        self._agent_name,
                        {**event_payload, "success": False, "error": str(exc)},
                    )
                    lifecycle_run.flush_user_messages()
                except Exception as hook_err:
                    self._log.warning("SubagentStop hook error: %s", hook_err)
                raise

            # ── Worker checkpoint: record success ──
            if coord is not None:
                coord.record_worker_success(
                    self._agent_name,
                    call_index,
                    input_hash,
                    str(task),
                    result,
                    self._snapshot_worker_memory(),
                )

            try:
                lifecycle_run.dispatch(
                    HookEvent.SUBAGENT_STOP,
                    self._agent_name,
                    {**event_payload, "success": True},
                )
                lifecycle_run.flush_user_messages()
            except Exception as hook_err:
                self._log.warning("SubagentStop hook error: %s", hook_err)

            self._log.debug(f"Finished sub-task {sub_task_id}")
            return result

    def run(self, task: str, *args, **kwargs):
        """
        Run task within sub-task context.

        Args:
            task: Task description.
            *args, **kwargs: Arguments forwarded to original agent.

        Returns:
            Task execution result.
        """
        return self._execute_with_lifecycle(self._agent.run, task, "run", *args, **kwargs)

    def __call__(self, task: str, **kwargs):
        """
        Call agent within sub-task context (method used by smolagents framework).

        Args:
            task: Task description.
            **kwargs: Arguments forwarded to original agent.

        Returns:
            Task execution result.
        """
        return self._execute_with_lifecycle(self._agent, task, "__call__", **kwargs)

    def __getattr__(self, name):
        """Proxy undefined attributes to the original agent."""
        # Do not proxy methods already overridden here
        if name in ("run", "__call__"):
            raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")
        return getattr(self._agent, name)
