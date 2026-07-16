"""
Abstract base class for agents.

Defines common interfaces and behavior patterns for all agents.
Provides shared capabilities such as model management and execution environment integration.
"""

import os
import uuid
from abc import ABC, abstractmethod
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, List, Optional

from smolagents import (
    AgentLogger,
    AgentGenerationError,
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
from smolagents.models import ChatMessage, MessageRole

from src.lib.smolagents.models.model_manager import (
    ModelConfigBuilder,
    get_model,
)
from src.lib.smolagents.memory.context_compression import ConversationHistoryManager
from src.lib.smolagents.monkey_patch import install_agentloom_runtime_adapters
from src.lib.smolagents.agent.tool_argument_coercion import coerce_tool_arguments
from src.lib.smolagents.models.tool_call_parser import (
    ToolCallParseError,
    parse_json_with_repair,
)
from src.lib.smolagents.skills.skills import SkillsManager
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
    resolve_execution_prompt_template_path,
    validate_execution_config_payload,
)
from src.lib.config.defaults import DEFAULT_MAX_TOKENS
from src.lib.config import C, build_effective_agent_config, get_code_agent_config, get_default_toolsets
from src.lib.config.config_validation import BoolParser  # noqa: F401 — used elsewhere
from src.lib.utils.workspace import ensure_workspace_mounted_once
from src.tools import resolve_toolsets
from src.trace import (
    bind_explicit_execution_context,
    capture_explicit_execution_context,
    generate_id,
    get_current_hook_manager,
    get_current_task_id,
    get_current_sub_task_id,
    sub_task_context,
    bind_local_run,
    bind_root_run,
    require_root_run_id,
)
from src.lib.smolagents.hooks import HookEvent, HookManager, register_builtin_hooks
from src.lib.smolagents.hooks.hook_manager import wrap_in_system_reminder
from src.lib.smolagents.hooks.tool_shim import clone_tool_for_runtime, inject_hooks
from src.lib.smolagents.tools.tools import ensure_tool_wrapped
from src.lib.smolagents.prompts.prompt_builder import build_prompt_templates

# Checkpoint / Resume support
import hashlib as _hashlib
from contextvars import ContextVar
from typing import Any as _Any

_current_worker_memory: ContextVar[list | None] = ContextVar("_current_worker_memory", default=None)

install_agentloom_runtime_adapters()


from src.lib.smolagents.agent.loom_mixin import LoomAgentMixin


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


def _require_successful_runtime_result(run_result: Any) -> None:
    """Reject structured runtime results that did not finish successfully."""
    run_state = str(getattr(run_result, "state", "") or "")
    if run_state != "success":
        state_label = run_state or "missing_run_state"
        raise RuntimeError(
            "Agent run did not complete successfully: "
            f"{state_label}"
        )


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
            bool(getattr(self, "return_full_result", False))
            if return_full_result is None
            else return_full_result
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
        before_run_callbacks: Optional[list] = None,
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
        before_run_callbacks: Optional[list] = None,
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
        required_call_context = (
            require_tool_calls() if callable(require_tool_calls) else nullcontext()
        )
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

    def execute_tool_call(self, tool_name: str, arguments: dict[str, str] | str) -> Any:
        """Execute a tool with AgentLoom-local schema-bound argument coercion."""

        available_tools = {**self.tools, **self.managed_agents}
        if tool_name not in available_tools:
            raise AgentToolExecutionError(
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
    def default_model_type(self) -> Optional[str]:
        """
        Default model type used for model selection.

        Returns:
            Optional[str]: Model type string. If None, the default model type
            from configuration is used.
        """
        pass

    @abstractmethod
    def _get_tools(self) -> List:
        """Get the list of tools used by the agent."""
        pass

    def __init__(self,
                 model=None,
                 execution_env: Optional[Any] = None,
                 logger: Optional[AgentLogger] = None,
                 model_cache: bool = True):
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
                logger=logger
            )
        else:
            self._model = model

        # Initialize execution environment
        self._execution_env: Optional[Any] = execution_env

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
        self._hook_manager = HookManager()
        register_builtin_hooks(self._hook_manager)

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
        """Broadcast a generic TaskCreated lifecycle event via HookManager.

        Any skill that has registered a ``TaskCreated`` hook will be notified.
        The framework does not know or care which skills are listening.

        Worker agents running inside a ``sub_task_context`` skip this event
        because they should only emit SubagentStart/SubagentStop — not
        TaskCreated, which would create a separate visualization.json file.
        """
        _ = runtime_agent
        _ = args
        _ = kwargs
        task_id = get_current_task_id()
        if task_id is None:
            return task

        # Workers run inside sub_task_context; only supervisors fire TaskCreated.
        if get_current_sub_task_id() is not None:
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
            self._hook_manager.trigger_hooks(
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
            self._hook_manager.flush_user_messages()
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
        error: Optional[BaseException] = None,
    ) -> None:
        task_id = get_current_task_id() or self._task_id
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
            self._hook_manager.trigger_hooks(
                event,
                "task",
                payload,
                tool_response=tool_response,
            )
            self._hook_manager.flush_user_messages()
        except Exception as exc:
            if self._logger:
                self._logger.warning("%s hook error: %s", event.value, exc)

    def _emit_session_lifecycle_event(
        self,
        event: HookEvent,
        task: str,
        *,
        result: Any = None,
        error: Optional[BaseException] = None,
    ) -> None:
        task_id = get_current_task_id() or self._task_id
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
            self._hook_manager.trigger_hooks(
                event,
                "session",
                payload,
                tool_response=tool_response,
            )
            self._hook_manager.flush_user_messages()
        except Exception as exc:
            if self._logger:
                self._logger.warning("%s hook error: %s", event.value, exc)

    def _inject_memory_snapshot(self, tasks: list[str]) -> list[str]:
        if not tasks:
            return tasks
        try:
            from src.extensions.self_learning.memory_store import MemoryStore
            from src.extensions.self_learning.paths import self_learning_enabled

            effective_config = getattr(self, "_effective_agent_config", None) or self._config
            if not self_learning_enabled(effective_config):
                return tasks
            snapshot = MemoryStore().snapshot_for_prompt(
                agent_config=effective_config,
            )
        except Exception as exc:
            if self._logger:
                self._logger.warning("Memory snapshot injection skipped: %s", exc)
            return tasks
        if not snapshot:
            return tasks
        return [f"{snapshot}\n\n{tasks[0]}", *tasks[1:]]

    def get_execution_tools(self) -> List:
        """
        Get tool list from execution environment.

        Returns:
            List: Execution environment tools.
        """
        if self._execution_env:
            return self._execution_env.tools()
        return []

    def get_all_tools(self, agent_type: str = "worker") -> List:
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
        global_backend = get_global_logger(create_if_missing=False)
        if global_backend is None:
            raise RuntimeError(
                "No logger available for runtime agent construction. "
                "Call initialize_global_logger_once(app_name) before creating agents."
            )
        return global_backend

    @staticmethod
    def _deduplicate_tools(tools: List[Any]) -> List[Any]:
        uniq_tools: List[Any] = []
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
        if self._hook_manager is not None:
            checks.insert(0, self._hook_manager.build_stop_check())
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

    def _build_model_config_builder(self) -> Optional[ModelConfigBuilder]:
        """Model-config overlay hook. Subclasses can return a typed builder."""
        return None


@dataclass(frozen=True)
class AgentRoleProfile:
    agent_type: AgentType
    tool_call_type: str
    cache_runtime_agent: bool = False
    enable_sub_task_tracking: bool = False
    additional_authorized_imports: Optional[list[str]] = None
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

    def __init__(self,
                 config: Optional[dict] = None,
                 project_path: str = "",
                 model=None,
                 execution_env: Optional[Any] = None,
                 logger: Optional[AgentLogger] = None,
                 model_cache: bool = True,
                 **kwargs):
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
        self._execution_normalized: Optional[NormalizedExecutionConfig] = None
        self._effective_agent_config = build_effective_agent_config(
            self._config,
            source_name=str(self._config.get("_yaml_file_path") or self._config.get("name") or self.__class__.__name__),
        )

        self._before_config_validation(**kwargs)
        normalized: Any | None = self._validate_config()
        if normalized is not None:
            self._normalized = normalized
        
        resolved_logger = self.resolve_agent_logger_from_config(
            self._config,
            provided_logger=logger,
        )

        super().__init__(
            model=model,
            execution_env=execution_env,
            logger=resolved_logger,
            model_cache=model_cache
        )
        self.tool_call_type = self._role_profile().tool_call_type

        runtime_logger = self._effective_logger()
        self._hook_manager = HookManager()
        register_builtin_hooks(self._hook_manager)
        self._skills_manager = SkillsManager(
            logger=runtime_logger,
            hook_manager=self._hook_manager,
        )
        self.initialize_skills_manager(self._config, logger=runtime_logger)
        try:
            default_toolsets = (
                self._config.get("toolsets")
                if "toolsets" in self._config
                else get_default_toolsets(self._effective_agent_config)
            )
            AgentConfigNormalizer.validate_skill_dependencies(
                self._config,
                self._skills_manager,
                default_tools=resolve_toolsets(default_toolsets),
                logger=runtime_logger,
            )
        except Exception:
            pass

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
    def default_model_type(self) -> Optional[str]:
        model_type = self._config.get("model_type")
        if model_type is None:
            return None
        return str(model_type)

    def _effective_logger(self) -> Optional[AgentLogger]:
        return getattr(self, "logger", None) or getattr(self, "_logger", None)

    def _before_config_validation(self, **kwargs) -> None:
        """Hook: run before config validation."""

    def _after_role_init(self, **kwargs) -> None:
        """Hook: run after role-driven initialization."""
        if 'max_steps' in self._config:
            self.max_steps = self._config['max_steps']

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
        provided_logger: Optional[AgentLogger] = None,
    ) -> Optional[AgentLogger]:
        _ = config
        if provided_logger is not None:
            return provided_logger

        global_backend = get_global_logger(create_if_missing=False)
        if global_backend is None:
            raise RuntimeError(
                "No logger available for role-driven agent initialization. "
                "Call initialize_global_logger_once(app_name) before creating agents."
            )
        return global_backend

    @staticmethod
    def _normalize_skills_config_items(skills_conf: Any) -> list[Any]:
        if skills_conf is None:
            return []
        if isinstance(skills_conf, dict) and "items" in skills_conf:
            items = skills_conf.get("items")
            if items is None:
                return []
            if isinstance(items, (str, dict)):
                return [items]
            if isinstance(items, list):
                return items
            raise ValueError("Configuration error: skills.items must be a list, dict, or string path")
        if isinstance(skills_conf, (str, dict)):
            return [skills_conf]
        if isinstance(skills_conf, list):
            return skills_conf
        raise ValueError("Configuration error: skills must be a list, dict, or string path")

    @staticmethod
    def _skills_config_defaults(skills_conf: Any, *, logger: Any) -> dict[str, Any]:
        log = get_logger(logger, __name__)
        defaults = {
            "load_mode": "on-demand",
            "allow_scripts": True,
            "allow_network": True,
        }
        if not isinstance(skills_conf, dict) or "items" not in skills_conf:
            return defaults
        defaults["load_mode"] = str(skills_conf.get("load-mode", "on-demand")).strip().lower()
        defaults["allow_scripts"] = BoolParser.parse(
            skills_conf.get("allow-scripts", True),
            default=True,
            field_name="skills.allow-scripts",
            logger=log,
        )
        defaults["allow_network"] = BoolParser.parse(
            skills_conf.get("allow-network", True),
            default=True,
            field_name="skills.allow-network",
            logger=log,
        )
        return defaults

    def _load_skills_from_config_entries(
        self,
        skills_manager: SkillsManager,
        skills_conf: Any,
        *,
        logger: Any,
    ) -> None:
        log = get_logger(logger, __name__)
        defaults = self._skills_config_defaults(skills_conf, logger=log)
        for sk in self._normalize_skills_config_items(skills_conf):
            sk_path = None
            sk_platform = None
            sk_load_mode = defaults["load_mode"]
            sk_allow_scripts = defaults["allow_scripts"]
            sk_allow_network = defaults["allow_network"]

            if isinstance(sk, dict):
                sk_path = sk.get('path')
                sk_platform = sk.get('platform')
                if "load-mode" in sk:
                    sk_load_mode = str(sk.get("load-mode", sk_load_mode)).strip().lower()
                if "allow-scripts" in sk:
                    sk_allow_scripts = BoolParser.parse(
                        sk.get("allow-scripts"),
                        default=sk_allow_scripts,
                        field_name="skills.items.allow-scripts",
                        logger=log,
                    )
                if "allow-network" in sk:
                    sk_allow_network = BoolParser.parse(
                        sk.get("allow-network"),
                        default=sk_allow_network,
                        field_name="skills.items.allow-network",
                        logger=log,
                    )
                if not sk_path:
                    msg = f"Skill configuration error: dictionary item is missing required 'path' field: {sk}"
                    log.warning(msg)
                    continue
            else:
                sk_path = sk

            if not sk_path:
                continue

            path_obj = Path(sk_path)
            if not path_obj.is_absolute():
                path_obj = Path(C.agent_root) / path_obj

            # Load skills from the directory and keep track of loaded skill names
            skills_manager.load_skills_from_directory(
                str(path_obj), platform=sk_platform,
                load_mode=sk_load_mode,
                allow_scripts=sk_allow_scripts,
                allow_network=sk_allow_network,
            )

    def initialize_skills_manager(self, config: dict, logger: Optional[AgentLogger] = None):
        """
        Initialize the current agent's SkillsManager in a unified way.

        Responsibilities:
        1. Set tool mappings (aliases)
        2. Load global skills from system config
        3. Load global default directory skills
        4. Load current agent's configured skills
        """
        log = get_logger(logger, __name__)
        skills_manager = self._skills_manager
        if skills_manager is None:
            skills_manager = SkillsManager.get_instance(logger=log)

        skills_manager.set_tools_mapping(C.get('tools_mapping', {}))

        # Load skills from effective agent config (app-level system.yaml overlay).
        # skills: []                         → explicit opt-out, skip default directory.
        # skills: null                       → not configured, only load default directory.
        # skills: [p1]                       → load entries + default directory.
        # skills: {load-mode, items: [...]}  → load entries with a shared policy.
        global_skills_conf = self._effective_agent_config.get("skills")

        if global_skills_conf:
            self._load_skills_from_config_entries(
                skills_manager,
                global_skills_conf,
                logger=log,
            )

        if global_skills_conf != []:
            skills_manager.load_skills_from_directory(str(Path(C.agent_root) / "skills"))

        if 'skills' in config:
            self._load_skills_from_config_entries(
                skills_manager,
                config['skills'],
                logger=log,
            )

        # Log skills loading summary
        loaded_skills = list(skills_manager.skills.keys()) if hasattr(skills_manager, 'skills') else []
        agent_name = config.get('name', 'unknown')
        if loaded_skills:
            log.info(f"Agent '{agent_name}' loaded skills: {loaded_skills}")
        else:
            log.warning(f"Agent '{agent_name}' has no skills loaded.")

    @abstractmethod
    def _role_profile(self) -> AgentRoleProfile:
        """Return role profile."""
        raise NotImplementedError

    def _runtime_agent_name(self) -> Optional[str]:
        """Optional runtime-level name passed to smolagents."""
        return None

    def _runtime_agent_description(self) -> Optional[str]:
        """Optional runtime-level description passed to smolagents."""
        return None

    def _build_model_config_builder(self) -> Optional[ModelConfigBuilder]:
        return None

    def _resolve_max_tokens_from_config(self) -> int:
        try:
            return C.llm.for_type(self.default_model_type).max_tokens
        except Exception:
            return DEFAULT_MAX_TOKENS

    def _resolve_smart_summary_from_config(self) -> bool:
        effective_cfg = self._effective_agent_config
        return effective_cfg.get("smart_summary", True) if isinstance(effective_cfg, dict) else True

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

    def _build_runtime_tools(self, profile: AgentRoleProfile) -> List:
        tools = self.get_all_tools(agent_type=profile.agent_type.value.lower())

        # Auto-inject todo_write when planning_interval is configured.
        planning_interval = normalize_positive_int_value(
            self._config.get("planning_interval")
        )
        if planning_interval is not None and planning_interval > 0:
            try:
                from src.tools.todo import todo_write as todo_write_tool
                tool_names = {
                    getattr(t, 'name', getattr(t, '__name__', None))
                    for t in tools
                }
                if 'todo_write' not in tool_names:
                    tools = tools + [todo_write_tool]
            except ImportError:
                pass

        return tools

    def build_runtime_agent(self) -> CodeAgent:
        profile = self._role_profile()

        if profile.cache_runtime_agent and self._runtime_agent is not None:
            return self._runtime_agent

        runtime_agent = self._create_agent(
            tools=self._build_runtime_tools(profile),
            **self._build_execution_agent_kwargs(profile),
        )

        if profile.cache_runtime_agent:
            self._runtime_agent = runtime_agent

        return runtime_agent

    def _resolve_effective_prompt_template_path(self) -> Optional[str]:
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
        prompt_template_path: Optional[str],
    ) -> Any:
        if not use_customized_prompt:
            return None

        return build_prompt_templates(
            prompt_template_path=prompt_template_path,
            effective_prompt_path=self._resolve_effective_prompt_template_path(),
            model_id=getattr(self._model, "model_id", None) if self._model else None,
            agent_root=C.agent_root,
            skills_manager=self._skills_manager,
            logger=runtime_logger,
            tool_call_type=self.tool_call_type,
            use_structured_output=getattr(self._model, 'supports_structured_output', 'false') == 'true',
        )

    def _create_agent(
        self,
        tools: List | None = None,
        *,
        additional_authorized_imports: Optional[List[str]] = None,
        additional_functions: Optional[dict[str, Any]] = None,
        enable_sub_task_tracking: bool = False,
        agent_name: Optional[str] = None,
        use_customized_prompt: bool = True,
        prompt_template_path: Optional[str] = None,
        executor_type: Optional[str] = None,
        executor_kwargs: Optional[dict[str, Any]] = None,
        planning_interval: Optional[int] = None,
        max_tokens: Optional[int] = None,
        smart_summary: Optional[bool] = None,
        runtime_name: Optional[str] = None,
        runtime_description: Optional[str] = None,
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

        wrapped_tools = ensure_tool_wrapped(self._deduplicate_tools(tools))
        hooked_tools = [
            inject_hooks(clone_tool_for_runtime(tool)) for tool in wrapped_tools
        ]

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
        )

        if self.tool_call_type == "tool_call":
            agent = ToolCallingAgentV2(
                tools=hooked_tools,
                stream_outputs=False,
                prompt_templates=prompt_templates,
                **agent_kwargs,
            )
        else:
            use_structured = getattr(self._model, 'supports_structured_output', 'false') == 'true'
            agent = CodeAgentV2(
                tools=hooked_tools,
                stream_outputs=False,
                prompt_templates=prompt_templates,
                additional_authorized_imports=resolved_additional_authorized_imports,
                use_structured_outputs_internally=use_structured,
                **agent_kwargs,
            )

        # Apply circuit-breaker threshold from YAML config (default: 5 consecutive parse errors)
        max_parse_errors = self._config.get("max_consecutive_parse_errors", 5)
        agent._max_consecutive_parse_errors = max_parse_errors  # type: ignore[attr-defined]

        if enable_sub_task_tracking:
            resolved_agent_name = agent_name or self.name
            agent = SubTaskTrackedAgent(agent, resolved_agent_name)

        if self._hook_manager is not None:
            setattr(agent, "_hook_manager", self._hook_manager)
            self._hook_manager.set_user_message_sink(
                lambda message, runtime_agent=agent, current_logger=runtime_logger: self._emit_hook_user_message(
                    runtime_agent,
                    current_logger,
                    message,
                )
            )
        return agent

    def run(
        self,
        task: str,
        task_id: Optional[str] = None,
        checkpoint_manager: Optional[Any] = None,
        resume: bool = False,
        additional_args: Optional[dict[str, Any]] = None,
    ) -> str:
        """Run inside one explicit root-run binding.

        The first agent in the call tree owns the binding and the session
        lifecycle. Delegated agents inherit the root through ``ContextVar``
        propagation and therefore cannot emit duplicate SessionStart/End.
        """
        def _run_once() -> str:
            # A HookManager belongs to an agent instance and can outlive many
            # runs; its construction id is therefore not a run identity. Every
            # invocation gets a fresh local id. The outermost invocation also
            # owns that id as the root, while delegated workers retain the
            # parent's root and use their fresh id only for leaf attribution.
            local_run_id = str(uuid.uuid4())
            with bind_local_run(local_run_id):
                with bind_root_run(local_run_id) as owns_root_run:
                    return self._run_with_root_context(
                        task,
                        task_id=task_id,
                        checkpoint_manager=checkpoint_manager,
                        resume=resume,
                        additional_args=additional_args,
                        owns_root_run=owns_root_run,
                    )

        role_profile_resolver = getattr(self, "_role_profile", None)
        cache_runtime_agent = bool(
            role_profile_resolver().cache_runtime_agent
            if callable(role_profile_resolver)
            else False
        )
        if cache_runtime_agent:
            with self._cached_runtime_run_lock:
                return _run_once()
        return _run_once()

    def _run_with_root_context(
        self,
        task: str,
        task_id: Optional[str] = None,
        checkpoint_manager: Optional[Any] = None,
        resume: bool = False,
        additional_args: Optional[dict[str, Any]] = None,
        *,
        owns_root_run: bool,
    ) -> str:
        from src.lib.checkpoint.coordinator import CheckpointCoordinator

        # Transform task before passing it to the runtime agent.
        transformed_tasks = self._transform_tasks(task)
        if not transformed_tasks:
            raise ValueError("Agent task transformation produced no tasks")
        transformed_tasks = self._inject_memory_snapshot(transformed_tasks)
        transformed_task = "\n\n".join(transformed_tasks)
        # Determine ID
        parent_execution_context = capture_explicit_execution_context()
        current_task_id = parent_execution_context.task_id
        final_task_id = current_task_id or task_id or generate_id(f"{self._get_agent_type().value.lower()}_{self.name}", prefix="task")
        self._task_id = final_task_id

        # Supervisor activates a new coordinator; workers inherit via ContextVar.
        if checkpoint_manager is not None:
            coord = CheckpointCoordinator.activate(
                checkpoint_manager,
                final_task_id,
                transformed_task,
                resume=resume,
            )
        else:
            coord = CheckpointCoordinator.current()

        def _execute_agent():
            session_started = False
            session_result = None
            session_error: Optional[BaseException] = None
            runtime_agent = None
            # Inject agent_id into model (for LiteLLM/Langfuse tracing)
            agent_id = self.get_agent_id()
            previous_model_agent_id = getattr(self._model, 'agent_id', ...) if hasattr(self._model, 'agent_id') else ...
            if previous_model_agent_id is not ...:
                self._model.agent_id = agent_id

            active_context = capture_explicit_execution_context()
            previous_runtime_path = active_context.runtime_agent_path
            if previous_runtime_path and previous_runtime_path != self.name:
                runtime_path = f"{previous_runtime_path}/{self.name}"
            else:
                runtime_path = self.name
            execution_binding = bind_explicit_execution_context(
                replace(
                    active_context,
                    agent_id=agent_id,
                    agent_name=self.name,
                    agent_config=self._effective_agent_config or self._config,
                    skills_manager=self._skills_manager,
                    hook_manager=self._hook_manager,
                    runtime_agent_path=runtime_path,
                )
            )
            execution_binding.__enter__()

            try:
                if coord is not None:
                    coord.register_file_history_hook(self._hook_manager)
                # Build tools only after the complete explicit context has
                # been bound.  LocalPythonExecutor/tool wrappers capture this
                # context before crossing their timeout thread boundary.
                runtime_agent = self.build_runtime_agent()
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
                                coord._supervisor_heartbeat.update_step(
                                    len(runtime_agent.memory.steps)
                                )
                            except Exception:
                                pass

                    # ── Incremental checkpoint: register step callback ──
                    if checkpoint_manager is not None:
                        # Supervisor: register and store callback for workers.
                        coord.register_supervisor_step_callback(runtime_agent)
                    else:
                        # Worker: inherit the supervisor's callback and store
                        # runtime_agent for later step-tracker registration in
                        # record_worker_start() (which knows call_index).
                        coord.register_worker_step_callback(
                            runtime_agent, agent_name=self.name
                        )

                # Pass reset=False when resuming (preserves injected memory) and
                # for later workflow items (preserves memory from previous runs).
                # Always request the structured result.  smolagents otherwise
                # returns only its fallback output for a max-steps termination,
                # which is indistinguishable from a successful final answer and
                # would incorrectly emit TaskCompleted and run memory review.
                result = None
                for task_index, current_task in enumerate(transformed_tasks):
                    run_kwargs: dict = {
                        "task": current_task,
                        "return_full_result": True,
                    }
                    if additional_args:
                        run_kwargs["additional_args"] = dict(additional_args)
                    if resume or task_index > 0:
                        run_kwargs["reset"] = False
                    if (
                        task_index > 0
                        and getattr(runtime_agent, "_agent_loom_supports_reset_false_task_step_control", False)
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

                if coord is not None and checkpoint_manager is not None:
                    coord.save_supervisor(runtime_agent, "completed", result=str(result) if result else None)

                return result
            except KeyboardInterrupt:
                session_error = KeyboardInterrupt()
                if coord is not None and checkpoint_manager is not None:
                    coord.save_supervisor(runtime_agent, "interrupted")
                raise
            except Exception as exc:
                session_error = exc
                self._emit_task_lifecycle_event(
                    HookEvent.STOP_FAILURE,
                    transformed_task,
                    error=exc,
                )
                if coord is not None and checkpoint_manager is not None:
                    coord.save_supervisor(runtime_agent, "failed", error=str(exc))
                raise
            finally:
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
                                memory_review_model,
                                self_learning_enabled,
                            )

                            effective_config = (
                                self._effective_agent_config or self._config
                            )
                            if (
                                self_learning_enabled(effective_config)
                                and memory_review_model(effective_config)
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
                                self._logger.warning(
                                    "Completed-run memory review failed unexpectedly"
                                )
                if previous_model_agent_id is not ...:
                    self._model.agent_id = previous_model_agent_id

                try:
                    if checkpoint_manager is not None and coord is not None:
                        CheckpointCoordinator.deactivate(coord)
                finally:
                    execution_binding.__exit__(None, None, None)

        if current_task_id:
            return _execute_agent()

        # Bind a fresh task id without mutating the legacy process-global
        # fallback, which can belong to another concurrent top-level run.
        with bind_explicit_execution_context(
            replace(parent_execution_context, task_id=final_task_id)
        ):
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
        excluded_attrs = {'run', '__call__'}
        for attr in dir(self._agent):
            if (not attr.startswith('_') and
                attr not in excluded_attrs and
                hasattr(self._agent, attr) and
                not callable(getattr(self._agent, attr, None))):
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
        (with ``success`` / ``error`` fields).  Any skill can subscribe to
        these events via its frontmatter ``hooks:`` — the framework does not
        know which skills are listening.

        **P1 skip-on-resume**: if a previous checkpoint shows this worker
        completed with the same ``input_hash``, return the cached result
        immediately without re-executing.
        """
        from src.lib.checkpoint.coordinator import CheckpointCoordinator

        with sub_task_context(self._agent_name) as sub_task_id:
            self._log.debug(f"Starting sub-task {sub_task_id} (agent: {self._agent_name}) via {call_label}")

            coord = CheckpointCoordinator.current()
            input_hash = self._compute_input_hash(task)

            # ── P1: Skip completed worker on resume ──
            if coord is not None:
                cached = coord.check_worker_skip(self._agent_name, input_hash)
                if cached is not None:
                    self._log.info(
                        "Skipping completed worker %s (input_hash=%s)",
                        self._agent_name, input_hash[:8],
                    )
                    return cached

            hook_manager = get_current_hook_manager()
            event_payload = {
                "agent_name": self._agent_name,
                "sub_task_id": sub_task_id,
            }
            if hook_manager is not None:
                try:
                    hook_manager.trigger_hooks(
                        HookEvent.SUBAGENT_START,
                        self._agent_name,
                        event_payload,
                    )
                    hook_manager.flush_user_messages()
                except Exception as hook_err:
                    self._log.warning("SubagentStart hook error: %s", hook_err)

            # ── Worker checkpoint: record start ──
            # Note: record_worker_start() also triggers register_worker_step_tracker()
            # using the runtime_agent stored by register_worker_step_callback().
            call_index = coord.record_worker_start(
                self._agent_name, input_hash, str(task)
            ) if coord is not None else 0
            worker_restored = (
                coord.restore_worker(self._agent, self._agent_name, call_index)
                if coord is not None
                else False
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
                        self._agent_name, call_index, input_hash, str(task),
                        self._snapshot_worker_memory(),
                    )
                raise
            except Exception as exc:
                if coord is not None:
                    coord.record_worker_failure(
                        self._agent_name, call_index, input_hash, str(task),
                        str(exc), self._snapshot_worker_memory(),
                    )
                if hook_manager is not None:
                    try:
                        hook_manager.trigger_hooks(
                            HookEvent.SUBAGENT_STOP,
                            self._agent_name,
                            {**event_payload, "success": False, "error": str(exc)},
                        )
                        hook_manager.flush_user_messages()
                    except Exception as hook_err:
                        self._log.warning("SubagentStop hook error: %s", hook_err)
                raise

            # ── Worker checkpoint: record success ──
            if coord is not None:
                coord.record_worker_success(
                    self._agent_name, call_index, input_hash, str(task),
                    result, self._snapshot_worker_memory(),
                )

            if hook_manager is not None:
                try:
                    hook_manager.trigger_hooks(
                        HookEvent.SUBAGENT_STOP,
                        self._agent_name,
                        {**event_payload, "success": True},
                    )
                    hook_manager.flush_user_messages()
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
        if name in ('run', '__call__'):
            raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")
        return getattr(self._agent, name)
