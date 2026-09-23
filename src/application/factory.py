import copy
import hashlib
import inspect
import json
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from agentloom.application.agent import AgentRoleProfile, AgentType, RoleDrivenAgent
from agentloom.application.definition import extract_markdown_definition, load_agent_definition
from agentloom.application.imports.dynamic_import import load_function
from agentloom.application.validation import (
    AgentConfigNormalizer,
    NormalizedAgentConfig,
    resolve_input_schema_object,
)
from agentloom.application.workflows import get_worker_agent_yaml_path, infer_category_from_yaml_path
from agentloom.configuration import C
from agentloom.configuration.config import EffectiveAgentConfigSnapshot
from agentloom.execution.logging import (
    get_logger,
)
from agentloom.execution.model_protocol import ToolDefinition
from agentloom.execution.tool_gateway import ToolBinding, bind_tool
from agentloom.tools.loader import resolve_tool_function
from agentloom.tools.selection import resolve_runtime_toolsets

_FIXED_ARGS_CONFIG_KEY = "fixed_args"


def _get_fixed_tool_args(tool_config: dict[str, Any]) -> dict[str, Any]:
    raw_fixed_args = tool_config.get(_FIXED_ARGS_CONFIG_KEY)
    if raw_fixed_args is None:
        return {}
    if not isinstance(raw_fixed_args, dict):
        raise ValueError(
            f"Tool '{tool_config.get('name')}' fixed_args must be a dictionary when provided"
        )
    return dict(raw_fixed_args)


def _bind_fixed_tool_args(tool_func: Callable, tool_name: str, fixed_args: dict[str, Any]) -> Callable:
    if not fixed_args:
        return tool_func

    AgentConfigNormalizer.validate_fixed_tool_args(tool_func, tool_name, fixed_args)
    signature = inspect.signature(tool_func)
    parameters = signature.parameters

    visible_parameters = [
        parameter for arg_name, parameter in parameters.items()
        if arg_name not in fixed_args
    ]
    visible_signature = signature.replace(parameters=visible_parameters)

    @wraps(tool_func)
    def fixed_args_tool(*args, **kwargs):
        visible_kwargs = dict(kwargs)
        for arg_name in fixed_args:
            visible_kwargs.pop(arg_name, None)
        bound = visible_signature.bind_partial(*args, **visible_kwargs)
        call_kwargs = dict(bound.arguments)
        call_kwargs.update(fixed_args)
        return tool_func(**call_kwargs)

    annotations = dict(getattr(tool_func, "__annotations__", {}))
    for arg_name in fixed_args:
        annotations.pop(arg_name, None)
    fixed_args_tool.__name__ = tool_name
    fixed_args_tool.__qualname__ = tool_name
    fixed_args_tool.__annotations__ = annotations
    fixed_args_tool.__signature__ = visible_signature  # type: ignore[attr-defined]
    fixed_args_tool._agentloom_fixed_args = tuple(sorted(fixed_args))  # type: ignore[attr-defined]
    fixed_args_tool._agentloom_fixed_values = copy.deepcopy(fixed_args)  # type: ignore[attr-defined]
    return fixed_args_tool


class YamlConfiguredAgent(RoleDrivenAgent):
    """
    Dynamic agent based on YAML configuration.

    Define agent attributes such as name, description, and tools in YAML
    to enable fast configuration and deployment.
    """

    REQUIRED_CONFIG_FIELDS = RoleDrivenAgent.COMMON_REQUIRED_FIELDS

    def _build_normalized_config(self) -> NormalizedAgentConfig:
        return AgentConfigNormalizer.build_worker_normalized_config(
            self._config,
            agent_root=C.agent_root,
            source_name="agent",
        )

    def _execution_validation_agent_root(self) -> str:
        return str(C.agent_root)

    def process_tool_query(self, query):
        return query

    def _role_profile(self) -> AgentRoleProfile:
        return AgentRoleProfile(
            agent_type=AgentType.WORKER,
            cache_runtime_agent=False,
            enable_sub_task_tracking=True,
            inject_default_file_tools=False
        )

    def _runtime_agent_name(self) -> str | None:
        return self.name

    def _runtime_agent_description(self) -> str | None:
        return self.description

    def _get_tools(self):
        """Get the tool list from configuration."""
        logger = getattr(self, "logger", None) or getattr(self, "_logger", None)
        effective = getattr(self, "_effective_agent_config", None)
        tools, mcp_manager = YamlAgentFactory.get_tools_from_config(self._config, logger=logger, effective_agent_config=effective)
        self._mcp_manager = mcp_manager
        return tools

    def agent_as_tool(self):
        """
        Dynamically generate an agent tool from YAML configuration.

        **Factory mode (thread-safe)**: Each call to the returned tool function
        creates a *new* Agent instance (sharing Model, Config, Logger, Tools)
        so that concurrent calls don't corrupt shared ``memory.steps`` / ``state``.

        The returned tool also has a ``.batch()`` method for parallel execution:
        ``tool.batch(tasks)`` automatically reads the YAML ``concurrency`` field
        and uses ``ParallelAgentExecutor`` under the hood.
        """
        normalized = self._ensure_normalized()
        function_name = self.name
        input_schema = normalized.input_schema
        if not isinstance(input_schema, dict):
            raise ValueError(f"Worker Agent '{function_name}' has no input schema")
        object_schema = resolve_input_schema_object(input_schema)
        properties = {
            **dict(object_schema.get("properties") or {}),
            **dict(input_schema.get("properties") or {}),
        }
        required_names = [
            name
            for name in dict.fromkeys(
                [
                    *object_schema.get("required", []),
                    *input_schema.get("required", []),
                ]
            )
            if isinstance(name, str)
        ]
        optional_names = [name for name in properties if name not in required_names]
        ordered_input_names = required_names + optional_names
        additional_properties = input_schema.get(
            "additionalProperties",
            object_schema.get("additionalProperties", True),
        )
        accepts_additional_properties = additional_properties is not False

        # ── Factory mode: capture shared immutable state ──
        # Agent instances are stateful (memory.steps, state, step_number),
        # so we create a NEW agent per call. These components are safe to share:
        _shared_model_binding = getattr(self, "_model_binding", None)
        _shared_logger = getattr(self, "logger", None) or getattr(self, "_logger", None)
        _frozen_config = self._config  # read-only dict
        _AgentClass = self.__class__
        _yaml_concurrency = self._config.get("concurrency")  # "auto" / int / None
        _model_type = self._config.get("model_type", "powerful")

        def _create_fresh_agent():
            """Every Worker call constructs a new owner, including native providers."""
            return _AgentClass(
                config=_frozen_config,
                model_binding=_shared_model_binding,
                logger=_shared_logger,
            )

        def _build_user_input(input_payload: dict[str, Any]) -> str:
            """Project validated Tool arguments into one Worker user message."""
            if len(properties) == 1 and len(input_payload) == 1:
                only_name = next(iter(properties))
                only_value = input_payload.get(only_name)
                if isinstance(only_value, str):
                    return only_value
            return json.dumps(
                input_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )

        # Dynamically create the tool function (factory mode)
        def dynamic_agent_tool(*args, **kwargs):
            if len(args) > len(ordered_input_names):
                raise TypeError(
                    f"{function_name}() takes {len(ordered_input_names)} positional arguments but {len(args)} were given"
                )

            input_payload: dict[str, Any] = {}
            for idx, value in enumerate(args):
                input_payload[ordered_input_names[idx]] = value

            for key, value in kwargs.items():
                if key not in properties and not accepts_additional_properties:
                    raise TypeError(f"{function_name}() got an unexpected keyword argument '{key}'")
                if key in input_payload:
                    raise TypeError(f"{function_name}() got multiple values for argument '{key}'")
                input_payload[key] = value

            missing_required = [name for name in required_names if name not in input_payload]
            if missing_required:
                raise TypeError(
                    f"{function_name}() missing {len(missing_required)} required positional argument(s): "
                    + ", ".join(missing_required)
                )

            assert normalized.input_validator is not None
            normalized.input_validator(input_payload)
            user_input = _build_user_input(input_payload)

            # Factory mode: create a NEW agent for each call (thread-safe)
            agent = _create_fresh_agent()
            return agent.run(user_input)

        # ── Attach .batch() method for parallel execution ──
        def batch(tasks, concurrency=None, on_progress=None):
            """
            Execute multiple tasks in parallel using this agent tool.

            Concurrency priority: ``concurrency`` param > YAML ``concurrency`` field > auto.

            Args:
                tasks: List of dicts, each passed as ``**kwargs`` to this tool.
                concurrency: Override concurrency (int or ``"auto"``). If None,
                             reads from YAML config; if YAML also unset, uses auto.
                on_progress: Optional callback ``(completed, total, TaskResult)``.

            Returns:
                List[TaskResult]: One result per task.
            """
            from agentloom.execution.concurrency import ParallelAgentExecutor

            # Priority chain: param > YAML > None (auto)
            effective = concurrency if concurrency is not None else _yaml_concurrency
            # Normalize "auto" string to None (executor treats None as auto)
            if effective == "auto":
                effective = None

            executor = ParallelAgentExecutor(
                max_workers=effective,
                model_type=_model_type,
            )
            return executor.execute_batch(tasks, dynamic_agent_tool, on_progress)

        dynamic_agent_tool.batch = batch
        # Store metadata for introspection
        dynamic_agent_tool._agent_loom_concurrency = _yaml_concurrency
        dynamic_agent_tool._agent_loom_model_type = _model_type

        description_lines = [self.description.strip(), "", "Args:"]
        for name in ordered_input_names:
            spec = properties[name]
            required_tag = "required" if name in required_names else "optional"
            description_lines.append(
                f"    {name} ({spec.get('type', 'any')}, {required_tag}): "
                f"{spec.get('description', '')}"
            )
        generated_docstring = "\n".join(description_lines)

        # Set dynamic function name and docstring
        dynamic_agent_tool.__name__ = function_name
        dynamic_agent_tool.__doc__ = generated_docstring

        annotations: dict[str, Any] = {"return": Any}
        signature_params = []
        for name in ordered_input_names:
            annotations[name] = Any
            default = (
                inspect.Parameter.empty
                if name in required_names
                else None
            )
            signature_params.append(
                inspect.Parameter(
                    name=name,
                    kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=default,
                    annotation=Any,
                )
            )
        if accepts_additional_properties:
            extra_name = "additional_properties"
            while extra_name in properties:
                extra_name = f"_{extra_name}"
            annotations[extra_name] = Any
            signature_params.append(
                inspect.Parameter(
                    name=extra_name,
                    kind=inspect.Parameter.VAR_KEYWORD,
                    annotation=Any,
                )
            )

        dynamic_agent_tool.__annotations__ = annotations
        dynamic_agent_tool.__signature__ = inspect.Signature(
            parameters=signature_params,
            return_annotation=Any,
        )
        dynamic_agent_tool._agentloom_tool_definition = ToolDefinition(  # type: ignore[attr-defined]
            name=function_name,
            description=self.description.strip(),
            parameters=input_schema,
            strict=True,
        )
        dynamic_agent_tool._agentloom_input_validator = (  # type: ignore[attr-defined]
            normalized.input_validator
        )
        dynamic_agent_tool._agentloom_input_object_schema = (  # type: ignore[attr-defined]
            object_schema
        )
        dynamic_agent_tool._agentloom_recovery_descriptor = lambda arguments: {  # type: ignore[attr-defined]
            "agent_name": function_name,
            "input_hash": hashlib.sha256(
                _build_user_input(dict(arguments)).encode()
            ).hexdigest()[:16],
            "task_input": _build_user_input(dict(arguments)),
        }

        # Fail fast through AgentLoom's runtime-neutral Tool schema seam.
        try:
            binding = bind_tool(dynamic_agent_tool)
            if binding.definition.name != function_name:
                raise ValueError("generated Tool name does not match its schema")
            from agentloom.execution.native_tools import ToolManifestEntry

            dynamic_agent_tool._agentloom_manifest_entry = ToolManifestEntry(  # type: ignore[attr-defined]
                logical_name=function_name, visible_name=function_name,
                owner="platform", provider="agentloom", capability="worker.invoke",
                operation="platform", parameters=binding.definition.parameters,
            )
        except Exception as e:
            raise ValueError(
                f"Failed to generate agent tool schema for '{function_name}': {e}"
            ) from e

        return dynamic_agent_tool


class YamlConfiguredSupervisorAgent(RoleDrivenAgent):
    """
    Supervisor agent based on YAML configuration.
    """
    REQUIRED_CONFIG_FIELDS = RoleDrivenAgent.COMMON_REQUIRED_FIELDS

    def _before_config_validation(self, **kwargs) -> None:
        # Extract yaml_file_path from config
        yaml_file_path = self._config.get('_yaml_file_path')
        self._yaml_file_path = Path(yaml_file_path) if yaml_file_path else None

        pinned_application_id = self._config.get("_application_id")
        if (
            isinstance(pinned_application_id, str)
            and pinned_application_id
            and isinstance(
                self._config.get("_effective_agent_config_snapshot"),
                EffectiveAgentConfigSnapshot,
            )
        ):
            self._inferred_category = pinned_application_id
            return

        # Infer category from file path for ordinary, non-pinned definitions.
        if self._yaml_file_path:
            self._inferred_category = infer_category_from_yaml_path(self._yaml_file_path)
            return
        raise ValueError("Configuration file path not found: cannot get _yaml_file_path from config")

    def _build_normalized_config(self) -> NormalizedAgentConfig:
        return AgentConfigNormalizer.build_supervisor_normalized_config(
            self._config,
            agent_root=C.agent_root,
            source_name="supervisor",
        )

    def _execution_validation_agent_root(self) -> str:
        return str(C.agent_root)

    def _validate_role_specific_config(self, normalized: Any | None) -> None:
        AgentConfigNormalizer.validate_worker_agents_config(self._config.get('worker_agents', []))

    @property
    def workflow_category(self) -> str:
        """Return the inferred category."""
        return self._inferred_category

    def _role_profile(self) -> AgentRoleProfile:
        return AgentRoleProfile(
            agent_type=AgentType.SUPERVISOR,
            cache_runtime_agent=True,
            enable_sub_task_tracking=False,
            inject_default_file_tools=False,
        )

    def _get_tools(self) -> list:
        """Get the tool list from configuration."""
        tools = []
        worker_logger = getattr(self, "logger", None) or getattr(self, "_logger", None)
        log = get_logger(worker_logger, __name__)

        # Load explicitly configured tools and toolsets. Skill activation never
        # changes this authority boundary.
        effective = getattr(self, "_effective_agent_config", None)
        standard_tools, mcp_manager = YamlAgentFactory.get_tools_from_config(self._config, logger=worker_logger, effective_agent_config=effective)
        self._mcp_manager = mcp_manager
        tools.extend(standard_tools)

        expected_agents = self._config.get('worker_agents', [])
        if not expected_agents:
            log.info("[YamlConfiguredSupervisorAgent] Successfully loaded all tools for supervisor agent.")
            return tools

        source_path = self._config.get("_yaml_file_path")
        worker_agents_folder = (Path(source_path).parent / "worker_agents"
                                if source_path else get_worker_agent_yaml_path(self.workflow_category))

        pinned_workers = self._config.get("_worker_definitions")
        pinned_worker_paths = self._config.get("_worker_definition_paths")
        if isinstance(pinned_workers, dict) and isinstance(
            pinned_worker_paths,
            dict,
        ):
            resolved_worker_agents = [
                (
                    item["path"],
                    Path(pinned_worker_paths[item["path"]]),
                )
                for item in expected_agents
            ]
        else:
            resolved_worker_agents = AgentConfigNormalizer.precheck_worker_agent_paths(
                expected_agents, worker_agents_folder, agent_root=C.agent_root,
            )

        for configured_path, found_file in resolved_worker_agents:
            try:
                # Load configuration
                pinned_workers = self._config.get("_worker_definitions", {})
                if str(found_file) in pinned_workers:
                    agent_config = copy.deepcopy(pinned_workers[str(found_file)])
                else:
                    agent_config = YamlAgentFactory._load_config_from_file(found_file)

                # Create agent tool
                agent_tool = YamlAgentFactory.create_agent_as_tool(
                    agent_config,
                    logger=worker_logger,
                    _source_path_is_pinned=str(found_file) in pinned_workers,
                )
                if agent_tool is not None:
                    tools.append(agent_tool)
            except Exception as e:
                msg = f"Failed to load worker agent from path '{configured_path}' ({found_file}): {e}"
                log.error(msg)
                raise ValueError(msg) from e

        log.info("[YamlConfiguredSupervisorAgent] Successfully loaded all tools for supervisor agent.")
        return tools


def _load_mcp_tools(
    *,
    config: dict,
    effective_agent_config: dict | None,
    agent_root: Path,
    append_tool: Callable,
    log: Any,
) -> Any | None:
    """Load MCP tools from ``mcp_servers`` config and append them via *append_tool*.

    Returns a :class:`McpManager` instance when at least one MCP server is
    configured, or ``None`` otherwise. Legacy connection failures are logged;
    strict discovery and tool selection errors fail before returning tools.
    """
    strict = effective_agent_config is not None and "_mcp_settings_snapshot" in effective_agent_config
    global_raw = (effective_agent_config or {}).get("mcp_servers")
    agent_raw = config.get("mcp_servers")

    if global_raw is None and agent_raw is None:
        return None

    manager = None
    try:
        from agentloom.integrations.mcp.config import merge_mcp_configs, parse_mcp_yaml_value
        from agentloom.integrations.mcp.manager import McpManager

        if effective_agent_config is not None and "_mcp_settings_snapshot" in effective_agent_config:
            merged = effective_agent_config["_mcp_settings_snapshot"]
        else:
            global_settings = parse_mcp_yaml_value(global_raw, agent_root) if global_raw is not None else None
            agent_settings = parse_mcp_yaml_value(agent_raw, agent_root) if agent_raw is not None else None
            merged = merge_mcp_configs(global_settings, agent_settings)

        if merged is None or not merged.configs:
            log.debug("[MCP] No MCP servers configured after parsing")
            return None

        manager = McpManager(merged)
        manager.connect_all()
        if strict:
            failed = sorted(name for name, status in manager.get_server_status().items()
                            if not status.get("connected"))
            if failed:
                raise RuntimeError("MCP connection failed for server(s): " + ", ".join(failed))

    except ImportError as exc:
        if manager is not None:
            manager.disconnect_all()
        if strict:
            raise
        log.warning("[MCP] MCP support not available: %s", exc)
        return None
    except Exception as exc:
        if manager is not None:
            manager.disconnect_all()
        if strict:
            raise
        log.warning("[MCP] Unexpected error loading MCP tools: %s", exc)
        return None

    try:
        for tool in manager.get_all_tools():
            append_tool(tool)
    except BaseException:
        manager.disconnect_all()
        raise
    return manager


class YamlAgentFactory:
    """
    YAML agent factory class.

    Provides capabilities for creating agent tools from YAML configuration.
    Supports loading from .yaml files and .md files that contain YAML code blocks.
    """

    @staticmethod
    def get_tools_from_config(config: dict, logger: Any = None, effective_agent_config: dict | None = None) -> tuple:
        """Load tool list from a configuration dictionary.

        Returns
        -------
        tuple[list, McpManager | None]
            A 2-tuple of (tools, mcp_manager).  ``mcp_manager`` is ``None``
            when no MCP servers are configured.
        """
        log = get_logger(logger, __name__)
        selected_toolsets = resolve_runtime_toolsets(config, effective_agent_config)
        config = dict(config)
        for key in ("tools", "toolsets"):
            if effective_agent_config is not None and key in effective_agent_config:
                config[key] = copy.deepcopy(effective_agent_config[key])
        tools = []
        seen = set()

        def _tool_name(tool_obj) -> str | None:
            if isinstance(tool_obj, ToolBinding):
                return tool_obj.definition.name
            return getattr(tool_obj, "name", None) or getattr(tool_obj, "__name__", None)

        def _append_tool(tool_obj, explicit_name: str | None = None):
            tool_name = explicit_name or _tool_name(tool_obj)
            if tool_name and tool_name in seen:
                raise ValueError(f"Duplicate tool name: {tool_name}")
            if tool_name:
                seen.add(tool_name)
            tools.append(tool_obj)

        raw_tools = config.get("tools") or []
        AgentConfigNormalizer.validate_tools_config_entries(raw_tools)
        explicit_tool_names = {
            tool_config["name"]
            for tool_config in raw_tools
        }

        for tool_name in selected_toolsets:
            if tool_name in explicit_tool_names:
                continue
            tool_function = resolve_tool_function(tool_name)
            _append_tool(tool_function, explicit_name=tool_name)
            log.info(f"[YamlAgentFactory] Loaded toolset tool: {tool_name}")

        if 'tools' not in config:
            # Still check for MCP tools even when no explicit tools are listed.
            mcp_manager = _load_mcp_tools(
                config=config,
                effective_agent_config=effective_agent_config,
                agent_root=C.agent_root,
                append_tool=_append_tool,
                log=log,
            )
            from agentloom.execution.permissions.policy_summary import patch_shell_tool_security
            patch_shell_tool_security(tools, log)
            return tools, mcp_manager

        for tool_config in raw_tools:
            tool_name = tool_config.get('name')
            fixed_args = _get_fixed_tool_args(tool_config)

            # Check whether this is a dynamically loaded tool
            if 'module' in tool_config and 'function' in tool_config:
                # Use dynamic loading
                module = tool_config['module']
                function = tool_config['function']
                try:
                    loaded_function = load_function(module, function)
                except (ImportError, AttributeError, TypeError) as e:
                    log.error(f"[YamlAgentFactory] Failed to load dynamic tool: {tool_name} from {module}.{function}. Error: {e}")
                    raise ValueError(f"Failed to dynamically load tool '{tool_name}': {e}") from e

                loaded_function = _bind_fixed_tool_args(loaded_function, tool_name, fixed_args)
                _append_tool(loaded_function, explicit_name=tool_name)
                log.info(f"[YamlAgentFactory] Successfully loaded dynamic tool: {tool_name} from {module}.{function}")
            else:
                # Registry-based built-in resolution.
                try:
                    tool_function = resolve_tool_function(tool_name)
                except ValueError as e:
                    log.error(f"[YamlAgentFactory] Failed to find predefined tool: {tool_name}")
                    raise ValueError(f"Tool '{tool_name}' not found, please verify the tool name") from e
                tool_function = _bind_fixed_tool_args(tool_function, tool_name, fixed_args)
                _append_tool(tool_function, explicit_name=tool_name)
                log.info(f"[YamlAgentFactory] Successfully loaded predefined tool: {tool_name}")

        # Phase 3: MCP tools (connect to external MCP servers)
        mcp_manager = _load_mcp_tools(
            config=config,
            effective_agent_config=effective_agent_config,
            agent_root=C.agent_root,
            append_tool=_append_tool,
            log=log,
        )

        # Phase 4: Patch shell_tool description with dynamic security policy
        from agentloom.execution.permissions.policy_summary import patch_shell_tool_security
        patch_shell_tool_security(tools, log)

        return tools, mcp_manager

    @staticmethod
    def _extract_yaml_from_markdown(content: str) -> tuple[dict, str]:
        """
        Extract YAML configuration and workflow content from Markdown.

        Args:
            content: Markdown file content.

        Returns:
            tuple: (yaml_config, workflow_content)
        """
        return extract_markdown_definition(content)

    @staticmethod
    def _prepare_agent_config(
        config: dict,
        *,
        source_path: str | Path | None = None,
        source_path_is_pinned: bool = False,
    ) -> dict:
        if not isinstance(config, dict):
            raise ValueError(f"Agent configuration must be a mapping, got {type(config).__name__}")

        prepared = copy.deepcopy(config)
        if source_path is not None:
            prepared["_yaml_file_path"] = str(Path(source_path).resolve())
            return prepared

        raw_path = prepared.get("_yaml_file_path")
        if isinstance(raw_path, str) and raw_path.strip():
            prepared["_yaml_file_path"] = (
                raw_path
                if source_path_is_pinned
                else str(Path(raw_path).expanduser().resolve())
            )
        return prepared

    @staticmethod
    def _load_config_from_file(config_path: str | Path) -> dict:
        """
        Load configuration from file, supporting .yaml and .md files.

        Args:
            config_path: Configuration file path.

        Returns:
            dict: Parsed configuration dictionary.
        """
        return load_agent_definition(config_path)

    @staticmethod
    def create_agent_tool(config_path: str | Path | dict,
                         agent_class=None,
                         model_binding=None) -> list:
        """
        Create an agent tool from YAML configuration.

        Args:
            config_path: YAML/Markdown config file path or config dictionary.
            agent_class: Optional custom agent class, defaults to YamlConfiguredAgent.
            model_binding: Optional resolved model binding.

        Returns:
            List: List of functions decorated by @tool.
        """
        # Use custom class or default YamlConfiguredAgent
        AgentClass = agent_class or YamlConfiguredAgent

        # Use dict directly when provided; otherwise load from file
        if isinstance(config_path, dict):
            config = YamlAgentFactory._prepare_agent_config(config_path)
        else:
            config = YamlAgentFactory._load_config_from_file(config_path)

        # Create configured agent
        agent = AgentClass(
            config=config,
            model_binding=model_binding,
        )

        # Return agent tool list directly; tools are already decorated with @tool
        return agent._get_tools()

    @staticmethod
    def create_agent_as_tool(config_path: str | Path | dict,
                            agent_class=None,
                            model_binding=None,
                            logger: Any = None,
                            _source_path_is_pinned: bool = False,
                            **kwargs
                            ) -> Callable | None:
        """
        Create an agent-as-tool from YAML configuration.

        Returns a single callable tool function with a ``.batch()`` method
        for parallel execution, or ``None`` if the YAML has no
        the Agent's input schema (or the default ``task`` schema).

        Each creation captures a fresh definition and configuration snapshot.
        The returned callable retains that snapshot for its lifetime.

        Args:
            config_path: YAML/Markdown config file path or config dictionary.
            agent_class: Optional custom agent class, defaults to YamlConfiguredAgent.
            model_binding: Optional resolved model binding.
            logger: Optional logger instance.

        Returns:
            Optional[Callable]: The agent tool function, or None.
        """
        # Each creation owns a new definition/configuration snapshot. Reusing
        # a path-only callable would retain Application overrides and resources
        # after edits, even when the Agent YAML itself did not change.
        # Use custom class or default YamlConfiguredAgent
        AgentClass = agent_class or YamlConfiguredAgent

        # Use dict directly when provided; otherwise load from file
        if isinstance(config_path, dict):
            config = YamlAgentFactory._prepare_agent_config(
                config_path,
                source_path_is_pinned=_source_path_is_pinned,
            )
        else:
            config = YamlAgentFactory._load_config_from_file(config_path)

        # Create configured agent
        agent = AgentClass(
            config=config,
            model_binding=model_binding,
            logger=logger,
            **kwargs
        )

        # Return single agent tool or None
        tool = agent.agent_as_tool()
        effective_logger = logger or getattr(agent, "logger", None) or getattr(agent, "_logger", None)
        log = get_logger(effective_logger, __name__)
        if tool is not None:
            log.info(f"[YamlAgentFactory] Successfully created agent tool: {tool.__name__} from {config_path if not isinstance(config_path, dict) else 'dict'}")
        else:
            log.error(f"[YamlAgentFactory] Failed to create agent tool from {config_path if not isinstance(config_path, dict) else 'dict'} (disabled or missing config)")
        return tool


    @staticmethod
    def run_agents_parallel(
        config_path: str | Path | dict,
        tasks: list,
        max_workers: int | None = None,
        logger: Any = None,
        on_progress=None,
    ) -> list:
        """
        Create an Agent-as-Tool and execute a batch of tasks in parallel.

        This is a convenience method that combines ``create_agent_as_tool()``
        with ``ParallelAgentExecutor.execute_batch()``, encapsulating the
        common pattern of creating a worker agent and calling it concurrently
        on multiple inputs (e.g. analysing many directories in parallel).

        Args:
            config_path: YAML/Markdown config file path or config dict.
            tasks: List of dicts, each passed as ``**kwargs`` to the agent tool.
            max_workers: Max parallel threads (default: auto from RPM).
            logger: Optional logger instance.
            on_progress: Optional callback ``(completed, total, TaskResult)``.

        Returns:
            List[TaskResult]: One result per task.
        """
        from agentloom.execution.concurrency import ParallelAgentExecutor

        agent_tool = YamlAgentFactory.create_agent_as_tool(config_path, logger=logger)
        if agent_tool is None:
            raise RuntimeError(
                f"Failed to create agent tool from "
                f"{config_path if not isinstance(config_path, dict) else 'dict'}"
            )

        # Determine model_type for the executor
        if isinstance(config_path, dict):
            model_type = config_path.get("model_type", "powerful")
        else:
            try:
                config = YamlAgentFactory._load_config_from_file(config_path)
                model_type = config.get("model_type", "powerful")
            except Exception:
                model_type = "powerful"

        executor = ParallelAgentExecutor(
            max_workers=max_workers,
            model_type=model_type,
        )
        return executor.execute_batch(tasks, agent_tool, on_progress)

    @staticmethod
    def create_agents_as_tools_from_folder(folder_path: str | Path,
                                          agent_class=None,
                                          model_binding=None,
                                          logger: Any = None,
                                          **kwargs
                                          ) -> list:
        """
        Load all YAML and Markdown files from a folder and create agent-as-tools.

        Args:
            folder_path: Folder path containing YAML/Markdown config files.
            agent_class: Optional custom agent class, defaults to YamlConfiguredAgent.
            model_binding: Optional resolved model binding.
            logger: Optional logger instance.

        Returns:
            List: List of all dynamically generated agent tool functions.
        """
        folder_path = Path(folder_path)
        all_tools: list[Callable] = []
        log = get_logger(logger, __name__)

        if not folder_path.exists() or not folder_path.is_dir():
            return all_tools

        # Iterate through all YAML and Markdown files in the folder
        for config_file in list(folder_path.glob("*.yaml")) + list(folder_path.glob("*.yml")) + list(folder_path.glob("*.md")):
            try:
                agent_tool = YamlAgentFactory.create_agent_as_tool(
                    config_file,
                    agent_class=agent_class,
                    model_binding=model_binding,
                    logger=logger,
                    **kwargs
                )
                if agent_tool is not None:
                    all_tools.append(agent_tool)
            except Exception as e:
                log.error(f"Failed to load agent from {config_file}: {e}")

        return all_tools

    @staticmethod
    def load_agents_from_directory(directory: str | Path, agent_class=None) -> dict[str, list]:
        """
        Load all YAML and Markdown config files from a directory and create agent tools.

        Args:
            directory: Directory containing YAML/Markdown configuration files.
            agent_class: Optional custom agent class, defaults to YamlConfiguredAgent.

        Returns:
            Dict[str, List]: Mapping from agent name to tool list.
        """
        directory = Path(directory)
        agents = {}
        log = get_logger(None, __name__)

        # Iterate through all supported configuration file formats
        for config_file in list(directory.glob("*.yaml")) + list(directory.glob("*.yml")) + list(directory.glob("*.md")):
            try:
                tools = YamlAgentFactory.create_agent_tool(config_file, agent_class=agent_class)
                # Read configuration to get the agent name
                config = YamlAgentFactory._load_config_from_file(config_file)
                agent_name = config['name']
                agents[agent_name] = tools
            except Exception as e:
                log.error(f"Failed to load config file {config_file}: {e}")

        return agents
