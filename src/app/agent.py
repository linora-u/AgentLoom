"""Application-owned Supervisor and Worker orchestration."""

# Checkpoint / Resume support
import hashlib as _hashlib
import os
import uuid
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agentloom.app.lifecycle import ApplicationRunLifecycle

from agentloom.app.composition import build_builtin_runtime_registry
from agentloom.app.validation import (
    AgentConfigNormalizer,
)
from agentloom.config import (
    C,
    build_effective_agent_config_snapshot,
)
from agentloom.execution.agent_runtime import (
    AgentRuntime,
    AgentRuntimeRequest,
    AgentRuntimeResult,
    JSONValue,
    RuntimeCapabilities,
    RuntimeCheckpointEnvelope,
    RuntimeDefinition,
    RuntimeEvent,
    RuntimeModelSelection,
    require_runtime_state,
)
from agentloom.execution.hooks import (
    HookConfigLayer,
    HookEvent,
    HookPlan,
    HookPlanCompiler,
    builtin_hook_handlers,
)
from agentloom.execution.logging import (
    get_global_logger,
    get_logger,
)
from agentloom.execution.model_binding import ModelTurnBinding
from agentloom.execution.prompts.environment import get_agent_environment_prompt
from agentloom.execution.skills.catalog import SkillCatalog
from agentloom.execution.skills.parser import build_skills_prompt
from agentloom.execution.tool_gateway import (
    AgentLoomToolGateway,
    ToolBinding,
    bind_tool,
)
from agentloom.execution.trace import (
    bind_local_run,
    bind_root_run,
    capture_explicit_execution_context,
    generate_id,
    get_current_hook_run,
    require_root_run_state,
    sub_task_context,
)
from agentloom.execution.workspace import ensure_workspace_mounted_once


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

    _config: dict[str, Any]

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
        self,
        model_binding: ModelTurnBinding | None = None,
        logger: Any | None = None,
        model_cache: bool = True,
    ):
        """
        Initialize agent.

        Args:
            model_binding: Optional canonical model binding. If omitted, the
                configured LiteLLM profile is resolved.
            logger: Optional logger instance.
            model_cache: Whether to enable model caching.
        """
        runtime_id = getattr(self, "_config", {}).get("agent_runtime", "smolagents")
        # Transitional smol binding path. Native runtimes receive profile data
        # without constructing a Python provider adapter.
        self._model_binding = model_binding
        self._model_selection = None
        if runtime_id == "smolagents":
            if self._model_binding is None:
                self._model_binding = self._resolve_model_binding(model_cache=model_cache)
        else:
            from agentloom.config.http_headers import normalize_http_headers

            settings = C.llm.for_type(self.default_model_type)
            self._model_selection = RuntimeModelSelection(
                model_type=(self.default_model_type or C.llm.default_model_type).strip().lower(),
                model_id=settings.model,
                protocol=settings.adapter,
                settings=settings.model_dump(mode="json"),
                request_headers=normalize_http_headers(settings.extra_headers),
            )
        if self._model_binding is not None and not isinstance(self._model_binding, ModelTurnBinding):
            raise TypeError("model_binding must be a ModelTurnBinding")

        # Initialize logger
        self._logger = logger

        # Task ID
        self._task_id = None

        # A single Agent definition may only assemble and execute one runtime
        # at a time. Independent Agents and factory-created Workers still run
        # concurrently.
        self._cached_runtime_run_lock = RLock()

        # Generate unique agent ID
        self._agent_id = self._generate_agent_id()

        self._hook_plan = HookPlan(builtin_hook_handlers())

    def _resolve_model_binding(
        self,
        *,
        model_cache: bool,
    ) -> ModelTurnBinding:
        from agentloom.integrations.litellm.model_binding import (
            resolve_litellm_model_turn_binding,
        )

        return resolve_litellm_model_turn_binding(
            self.default_model_type,
            model_cache=model_cache,
        )

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

    def _emit_task_start(self, runtime_agent: Any, task: str, *args, **kwargs):
        """Broadcast a generic TaskCreated lifecycle event via the active Hook Run.

        Every explicitly configured ``TaskCreated`` Hook will be notified.

        Worker agents running inside a ``sub_task_context`` skip this event
        because their lifecycle is already represented by
        SubagentStart/SubagentStop.
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

    def _inject_memory_snapshot(
        self,
        tasks: list[str | None],
    ) -> list[str | None]:
        if not tasks:
            return tasks
        root_state = None
        try:
            from agentloom.self_learning.paths import self_learning_enabled
            from agentloom.self_learning.persistence.memory_store import MemoryStore

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
        first = tasks[0]
        return [
            snapshot if first is None else f"{snapshot}\n\n{first}",
            *tasks[1:],
        ]

    def get_all_tools(self, agent_type: str = "worker") -> list:
        """
        Get all available tools.

        Args:
            agent_type: Agent type (retained for compatibility).

        Returns:
            List: Merged tool list.
        """
        return self._get_tools()

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
    def _resolve_unique_tools(tools: list[Any]) -> list[Any]:
        uniq_tools: list[Any] = []
        seen: dict[Any, Any] = {}
        for tool_item in tools:
            key = tool_item.definition.name if isinstance(tool_item, ToolBinding) else getattr(
                tool_item,
                "name",
                getattr(tool_item, "__name__", tool_item),
            )
            if key in seen:
                if seen[key] is tool_item:
                    continue
                raise ValueError(f"Duplicate tool name: {key}")
            seen[key] = tool_item
            uniq_tools.append(tool_item)
        return uniq_tools

    @staticmethod
    def _emit_hook_user_message(runtime_logger: Any, message: str) -> None:
        rendered = f"[hook] {message}"
        runtime_logger.info(rendered)

    def _validate_model(self) -> bool:
        """
        Validate whether model is available.

        Returns:
            bool: Whether model is available.
        """

        return self._model_binding is not None or self._model_selection is not None

    def _get_agent_type(self) -> AgentType:
        """
        Get agent type; subclasses should override this method.

        Returns:
            AgentType: Agent type.
        """
        return AgentType.WORKER  # Default to worker agent

@dataclass(frozen=True)
class AgentRoleProfile:
    agent_type: AgentType
    cache_runtime_agent: bool = False
    enable_sub_task_tracking: bool = False
    inject_default_file_tools: bool = False


class RoleDrivenAgent(BaseAgent):
    """
    Role-driven agent base class.

    Unifies worker/supervisor behavior through role profile and hooks.
    """

    COMMON_REQUIRED_FIELDS: tuple[str, ...] = ("name", "description", "workflow")
    REQUIRED_CONFIG_FIELDS: tuple[str, ...] = ()
    def __init__(
        self,
        config: dict | None = None,
        project_path: str = "",
        model_binding: ModelTurnBinding | None = None,
        logger: Any | None = None,
        model_cache: bool = True,
        **kwargs,
    ):
        ensure_workspace_mounted_once()

        self._project_path = project_path
        if config is None:
            self._config = {}
        elif isinstance(config, dict):
            self._config = deepcopy(config)
        else:
            raise ValueError(f"Agent config must be a dictionary, got {type(config).__name__}")
        self._normalized = None
        effective_snapshot = build_effective_agent_config_snapshot(
            self._config,
            source_name=str(self._config.get("_yaml_file_path") or self._config.get("name") or self.__class__.__name__),
        )
        self._effective_agent_config_snapshot = effective_snapshot
        self._effective_agent_config = effective_snapshot.values
        self._config["_effective_agent_config_snapshot"] = deepcopy(effective_snapshot)

        self._before_config_validation(**kwargs)
        normalized: Any | None = self._validate_config()
        if normalized is not None:
            self._normalized = normalized

        resolved_logger = self.resolve_agent_logger_from_config(
            self._config,
            provided_logger=logger,
        )

        super().__init__(
            model_binding=model_binding,
            logger=resolved_logger,
            model_cache=model_cache,
        )

        runtime_logger = self._effective_logger()
        self._skill_catalog = self._config.get("_skill_catalog_snapshot")
        if self._skill_catalog is None:
            self._skill_catalog = self.initialize_skill_catalog(logger=runtime_logger)
            self._config["_skill_catalog_snapshot"] = self._skill_catalog
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
        self._hook_plan = self._config.get("_hook_plan_snapshot")
        if self._hook_plan is None:
            self._hook_plan = HookPlanCompiler().compile(
                hook_layers,
                internal_handlers=builtin_hook_handlers(),
            )
            self._config["_hook_plan_snapshot"] = self._hook_plan
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

    def _effective_logger(self) -> Any | None:
        return getattr(self, "logger", None) or getattr(self, "_logger", None)

    def _before_config_validation(self, **kwargs) -> None:
        """Hook: run before config validation."""

    def _after_role_init(self, **kwargs) -> None:
        """Hook: run after role-driven initialization."""

    def _required_config_fields(self) -> tuple[str, ...]:
        return tuple(self.REQUIRED_CONFIG_FIELDS)

    def _validate_role_specific_config(self, normalized: Any | None) -> None:
        """Role-specific validation hook after common validation and normalization."""

    def _validate_config(self) -> Any | None:
        """Validate config with common template and return normalized object when applicable."""
        normalized = AgentConfigNormalizer.validate_role_driven_config(
            self._config,
            required_fields=self._required_config_fields(),
            build_normalized=self._build_normalized_config,
            validate_role_specific=self._validate_role_specific_config,
        )
        return normalized

    def _build_normalized_config(self) -> Any | None:
        """Build normalized config object when needed."""
        return None

    def _ensure_normalized(self) -> Any | None:
        if self._normalized is None:
            self._normalized = self._build_normalized_config()
        return self._normalized

    @staticmethod
    def resolve_agent_logger_from_config(
        config: dict,
        *,
        provided_logger: Any | None = None,
    ) -> Any | None:
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

    def initialize_skill_catalog(self, logger: Any | None = None) -> SkillCatalog:
        """Resolve conventional and explicitly configured Skill sources once."""
        log = get_logger(logger, __name__)
        from agentloom.app.definition import skill_catalog

        catalog = skill_catalog(self._effective_agent_config_snapshot, logger=log)
        log.info("Agent '%s' resolved Skills: %s", self.name, [item.name for item in catalog.summaries()])
        return catalog

    @abstractmethod
    def _role_profile(self) -> AgentRoleProfile:
        """Return role profile."""
        raise NotImplementedError

    def _runtime_agent_name(self) -> str | None:
        """Optional runtime-level name passed to the selected runtime."""
        return None

    def _runtime_agent_description(self) -> str | None:
        """Optional runtime-level description passed to the selected runtime."""
        return None

    def _transform_task(self, task: str | None) -> str | None:
        """Task transformation hook."""
        return task

    def _transform_tasks(self, task: str | None) -> list[str | None]:
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
            from agentloom.execution.goal import normalize_goal_config

            goal = normalize_goal_config(
                self._config,
                source=self._config.get("name", "supervisor"),
            )
            if goal.enabled:
                from agentloom.execution.native_tools import ToolManifestEntry
                from agentloom.tools.goal import get_goal, update_goal

                tools = list(tools)
                for tool, capability in ((get_goal, "goal.read"), (update_goal, "goal.update")):
                    binding = bind_tool(tool)
                    tools.append(replace(binding, manifest_entry=ToolManifestEntry(
                        logical_name=binding.definition.name, visible_name=binding.definition.name,
                        owner="platform", provider="agentloom", capability=capability,
                        operation="platform", parameters=binding.definition.parameters,
                    )))
        return tools

    def _build_tool_gateway(self) -> AgentLoomToolGateway:
        profile = self._role_profile()
        tools = self._resolve_unique_tools(self._build_runtime_tools(profile))
        bindings: list[ToolBinding] = [bind_tool(tool) for tool in tools]
        mcp_manager = getattr(self, "_mcp_manager", None)
        resource_closers = (
            (mcp_manager.disconnect_all,)
            if mcp_manager is not None
            and callable(getattr(mcp_manager, "disconnect_all", None))
            else ()
        )
        return AgentLoomToolGateway(
            bindings,
            resource_closers=resource_closers,
        )

    def _build_runtime_instructions(self, gateway: AgentLoomToolGateway) -> str:
        sections = [str(self._config["workflow"]).strip(), get_agent_environment_prompt()]
        if any(item.name == "skill" for item in gateway.definitions):
            if self._skill_catalog is None:
                raise RuntimeError("Skill Tool requires a resolved Skill catalog")
            sections.append(build_skills_prompt(self._skill_catalog.summaries()))
        return "\n\n".join(section for section in sections if section.strip())

    def _build_runtime_definition(self) -> RuntimeDefinition:
        runtime_id = AgentConfigNormalizer.validate_agent_runtime_config(
            self._config
        )
        normalized = self._ensure_normalized()
        if runtime_id == "smolagents":
            from importlib.util import find_spec

            if find_spec("smolagents") is None:
                raise RuntimeError(
                    "The smol runtime is not installed. Install 'AgentLoom[smol]' "
                    "or run uv sync --locked --extra smol in the checkout."
                )
        from agentloom.app.runtime_options import normalize_runtime_options

        options, sources = normalize_runtime_options(
            self._config, snapshot=self._effective_agent_config_snapshot,
            agent_root=C.agent_root,
        )
        gateway = self._build_tool_gateway()
        return RuntimeDefinition(
            runtime_id=runtime_id,
            name=self._runtime_agent_name() or self.name,
            description=self._runtime_agent_description() or self.description,
            model=self._model_binding,
            model_selection=self._model_selection,
            instance_id=self._agent_id,
            tool_gateway=gateway,
            runtime_options=options,
            option_sources=sources,
            requirements=AgentConfigNormalizer.runtime_requirements(
                self._config, effective_config=self._effective_agent_config,
                hook_plan=self._hook_plan,
            ),
            instructions=self._build_runtime_instructions(gateway),
            output_contract=getattr(normalized, "output_contract", None),
            project_root=str(C.agent_root),
        )

    def build_runtime(self) -> AgentRuntime:
        """Build the configured complete-run Agent runtime adapter."""

        registry = build_builtin_runtime_registry()
        definition = self._build_runtime_definition()
        try:
            runtime = registry.create(definition)
        except BaseException:
            definition.tool_gateway.close()
            raise
        profile = self._role_profile()
        if profile.enable_sub_task_tracking:
            return SubTaskTrackedAgent(runtime, self.name, instance_id=self._agent_id)
        return runtime

    def _bind_hook_message_sink(self, runtime_agent: Any) -> None:
        """Bind Hook user-message delivery to the current runtime-neutral run."""

        hook_run = get_current_hook_run(required=True)
        runtime_logger = get_logger(self._effective_logger(), __name__)
        hook_run.set_user_message_sink(
            lambda message: self._emit_hook_user_message(
                runtime_logger,
                message,
            )
        )

    def run(
        self,
        task: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        checkpoint_manager: Any | None = None,
        application_lifecycle: "ApplicationRunLifecycle | None" = None,
        resume: bool = False,
        additional_args: dict[str, Any] | None = None,
    ) -> JSONValue:
        """Run inside one explicit root-run binding.

        The first agent in the call tree owns the binding and the session
        lifecycle. Delegated agents inherit the root through ``ContextVar``
        propagation and therefore cannot emit duplicate SessionStart/End.
        """

        def _run_once() -> JSONValue:
            from agentloom.app.invocation import AgentInvocation

            # Every invocation gets a fresh local id. The outermost invocation
            # also owns it as the root; delegated workers keep their own local id.
            local_run_id = run_id or str(uuid.uuid4())
            with bind_local_run(local_run_id):
                with bind_root_run(local_run_id) as owns_root_run:
                    return AgentInvocation(
                        self,
                        task=task,
                        task_id=task_id,
                        checkpoint_manager=checkpoint_manager,
                        application_lifecycle=application_lifecycle,
                        resume=resume,
                        additional_args=additional_args,
                        owns_root_run=owns_root_run,
                    ).run()

        with self._cached_runtime_run_lock:
            return _run_once()

class SubTaskTrackedAgent:
    """
    Sub-task tracing wrapper that provides an isolated tracing chain for worker agents.

    This class wraps a complete-run ``AgentRuntime`` and automatically creates
    sub-task context during execution.
    Telemetry collection has been removed; agent_id is injected for LiteLLM/Langfuse tracing.
    """

    def __init__(self, runtime: AgentRuntime, agent_name: str, *, instance_id: str | None = None):
        """
        Initialize sub-task tracing wrapper.

        Args:
            runtime: Runtime-neutral complete-run Agent adapter.
            agent_name: Agent name, used to generate sub-task IDs.
            instance_id: Stable invocation identity for owned tool resources.
        """
        self._runtime = runtime
        self._agent_name = agent_name
        self._instance_id = instance_id
        self._log = get_logger(getattr(runtime, "logger", None), __name__)

    @property
    def runtime_id(self) -> str:
        return self._runtime.runtime_id

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return self._runtime.capabilities

    @property
    def logger(self) -> Any:
        return getattr(self._runtime, "logger", None)

    @staticmethod
    def _compute_input_hash(task_text: str | None) -> str:
        """Short hash of the worker input for skip-on-resume matching."""
        return _hashlib.sha256(str(task_text).encode()).hexdigest()[:16]

    def _snapshot_runtime(self) -> RuntimeCheckpointEnvelope | None:
        """Return the wrapped runtime's opaque state envelope."""
        try:
            return self._runtime.snapshot()
        except Exception:
            return None

    def _subagent_event(
        self,
        request: AgentRuntimeRequest,
        *,
        phase: str,
        sub_task_id: str,
        error: str | None = None,
    ) -> RuntimeEvent:
        details = {
            "phase": phase,
            "agent_name": self._agent_name,
            "sub_task_id": sub_task_id,
        }
        if error is not None:
            details["error"] = error
        event = RuntimeEvent(
            kind="subagent",
            application_id=request.application_id,
            task_id=request.task_id,
            run_id=request.run_id,
            details=details,
        )
        if request.event_sink is not None:
            try:
                request.event_sink(event)
            except Exception:
                pass
        return event

    def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult:
        """Run callable within sub-task context, broadcasting lifecycle events.

        Emits ``SubagentStart`` before execution and ``SubagentStop`` after
        (with ``success`` / ``error`` fields). Explicitly configured Hooks may
        observe these parent-owned lifecycle events.

        Worker preparation is atomic: a resumed invocation claims unfinished
        work first, otherwise claims one completed result, or allocates a new
        call.  Fresh runs always allocate and execute.
        """
        from agentloom.execution.checkpoint.coordinator import CheckpointCoordinator

        with sub_task_context(self._agent_name, agent_id=self._instance_id) as sub_task_id:
            started_event = self._subagent_event(
                request,
                phase="started",
                sub_task_id=sub_task_id,
            )
            self._log.debug(
                "Starting sub-task %s (agent: %s)",
                sub_task_id,
                self._agent_name,
            )

            coord = CheckpointCoordinator.current()
            input_hash = self._compute_input_hash(request.task)
            task_text = request.task or ""

            # Claim/allocate exactly one logical call before side effects.  The
            # explicit outcome distinguishes a cached ``None``/empty result
            # from an invocation that still needs execution.
            if coord is not None:
                preparation = coord.prepare_worker_call(
                    self._agent_name,
                    input_hash,
                    task_text,
                )
                if not preparation.should_execute:
                    self._log.info(
                        "Skipping completed worker %s (input_hash=%s)",
                        self._agent_name,
                        input_hash[:8],
                    )
                    completed_event = self._subagent_event(
                        request,
                        phase="completed",
                        sub_task_id=sub_task_id,
                    )
                    return AgentRuntimeResult(
                        state="success",
                        output=preparation.cached_result,
                        events=(started_event, completed_event),
                    )
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

            checkpoint = (
                coord.load_worker_runtime_checkpoint(
                    self._agent_name,
                    call_index,
                )
                if coord is not None
                else None
            )
            checkpoint_sink = (
                coord.worker_checkpoint_sink(
                    self._agent_name,
                    call_index,
                    input_hash,
                    task_text,
                )
                if coord is not None
                else request.checkpoint_sink
            )
            runtime_request = replace(
                request,
                continue_session=request.continue_session
                or checkpoint is not None,
                checkpoint=checkpoint or request.checkpoint,
                checkpoint_sink=checkpoint_sink,
            )

            try:
                result = self._runtime.run(runtime_request)
                require_runtime_state(
                    result,
                    allowed_states={"success"},
                    error_prefix="Worker run did not complete successfully",
                )
            except KeyboardInterrupt:
                self._subagent_event(
                    request,
                    phase="interrupted",
                    sub_task_id=sub_task_id,
                )
                if coord is not None:
                    coord.record_worker_interrupted(
                        self._agent_name,
                        call_index,
                        input_hash,
                        task_text,
                        self._snapshot_runtime(),
                    )
                raise
            except Exception as exc:
                self._subagent_event(
                    request,
                    phase="failed",
                    sub_task_id=sub_task_id,
                    error=str(exc),
                )
                if coord is not None:
                    coord.record_worker_failure(
                        self._agent_name,
                        call_index,
                        input_hash,
                        task_text,
                        str(exc),
                        self._snapshot_runtime(),
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
                    task_text,
                    result.output,
                    result.checkpoint or self._snapshot_runtime(),
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

            self._log.debug("Finished sub-task %s", sub_task_id)
            completed_event = self._subagent_event(
                request,
                phase="completed",
                sub_task_id=sub_task_id,
            )
            return replace(
                result,
                events=(started_event, *result.events, completed_event),
            )

    def snapshot(self) -> RuntimeCheckpointEnvelope:
        return self._runtime.snapshot()

    def close(self) -> None:
        self._runtime.close()
