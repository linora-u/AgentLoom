from __future__ import annotations

import inspect
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Callable, Optional

from src.lib.config.config_validation import TODO_MODES, normalize_todo_mode_value
from src.lib.goal import GoalConfig, normalize_goal_config
from src.lib.logging import get_logger, validate_logging_config


@dataclass
class NormalizedAgentConfig:
    agent_function_schema: Optional[dict] = None
    goal: GoalConfig = dataclass_field(default_factory=GoalConfig)


_ALLOWED_EXECUTION_ENV_TYPES = {"local", "e2b", "docker", "wasm"}
_WORKFLOW_VALIDATION_ERROR = (
    "workflow field must be a non-empty string or non-empty list of non-empty strings"
)


@dataclass(frozen=True)
class NormalizedExecutionConfig:
    executor_type: str
    executor_kwargs: dict[str, Any]
    prompt_template_path: Optional[str]
    planning_interval: Optional[int] = None


def _resolve_agent_root(agent_root: Path | str) -> Path:
    return Path(agent_root).expanduser().resolve()


def normalize_execution_env(config: dict, source: str) -> dict[str, Any]:
    raw_execution_env = config.get("execution_env")
    if raw_execution_env is None:
        return {"type": "local", "executor_kwargs": {}}
    if not isinstance(raw_execution_env, dict):
        raise ValueError(f"{source} must be a dictionary when provided")

    normalized: dict[str, Any] = {
        "type": "local",
        "executor_kwargs": {},
    }

    raw_type = raw_execution_env.get("type", "local")
    if not isinstance(raw_type, str) or not raw_type.strip():
        raise ValueError(f"{source}.type must be a non-empty string")
    normalized_type = raw_type.strip().lower()
    if normalized_type not in _ALLOWED_EXECUTION_ENV_TYPES:
        raise ValueError(
            f"{source}.type must be one of ['local', 'e2b', 'docker', 'wasm'], "
            f"current value: {raw_type}"
        )
    normalized["type"] = normalized_type

    raw_executor_kwargs = raw_execution_env.get("executor_kwargs", {})
    if raw_executor_kwargs is None:
        raw_executor_kwargs = {}
    if not isinstance(raw_executor_kwargs, dict):
        raise ValueError(f"{source}.executor_kwargs must be a dictionary when provided")
    normalized["executor_kwargs"] = dict(raw_executor_kwargs)

    # bash_path is silently ignored — shell is auto-detected from $SHELL.

    return normalized


def resolve_execution_prompt_template_path(
    raw_path: str,
    source: str,
    *,
    agent_root: Path | str,
) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"{source} must be a non-empty string path")
    path_obj = Path(raw_path.strip()).expanduser()
    if not path_obj.is_absolute():
        path_obj = (_resolve_agent_root(agent_root) / path_obj).resolve()
    else:
        path_obj = path_obj.resolve()
    return path_obj


def normalize_execution_prompt_template_path_value(
    raw_prompt: Any,
    source: str,
    *,
    agent_root: Path | str,
) -> Optional[str]:
    if raw_prompt is None:
        return None

    raw_path: Any
    if isinstance(raw_prompt, str):
        raw_path = raw_prompt
    elif isinstance(raw_prompt, dict):
        if "path" not in raw_prompt:
            raise ValueError(f"{source} must include 'path' when prompt is a mapping")
        raw_path = raw_prompt.get("path")
    else:
        raise ValueError(f"{source} must be a string or mapping with 'path'")

    resolved = resolve_execution_prompt_template_path(
        raw_path,
        f"{source} path",
        agent_root=agent_root,
    )
    return str(resolved)


def normalize_execution_prompt_template_path(
    config: dict,
    source: str,
    *,
    agent_root: Path | str,
) -> Optional[str]:
    return normalize_execution_prompt_template_path_value(
        config.get("prompt"),
        source,
        agent_root=agent_root,
    )


def normalize_execution_planning_interval_value(raw_value: Any) -> Optional[int]:
    return normalize_positive_int_value(raw_value)


def validate_todo_config(config: dict, *, source: str) -> str:
    """Validate and return the effective current-task Todo mode."""

    raw_todo = config.get("todo", {})
    if not isinstance(raw_todo, dict):
        raise ValueError(f"{source}.todo must be a mapping")
    unexpected = sorted(set(raw_todo) - {"mode"})
    if unexpected:
        raise ValueError(
            f"{source}.todo has unsupported field(s): {', '.join(unexpected)}"
        )
    raw_mode = normalize_todo_mode_value(raw_todo.get("mode", "auto"))
    if not isinstance(raw_mode, str) or raw_mode not in TODO_MODES:
        allowed = ", ".join(sorted(TODO_MODES))
        raise ValueError(f"{source}.todo.mode must be one of: {allowed}")
    return raw_mode


def normalize_positive_int_value(raw_value: Any) -> Optional[int]:
    if raw_value is None:
        return None
    if isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, int):
        return raw_value if raw_value > 0 else None
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return None
        try:
            parsed = int(text)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def build_normalized_execution_config(
    config: dict,
    *,
    source_name: str,
    agent_root: Path | str,
) -> NormalizedExecutionConfig:
    name = str(config.get("name", source_name))
    execution_env = normalize_execution_env(config, source=f"{name}.execution_env")
    prompt_template_path = normalize_execution_prompt_template_path(
        config,
        source=f"{name}.prompt",
        agent_root=agent_root,
    )
    planning_interval = normalize_execution_planning_interval_value(config.get("planning_interval"))

    return NormalizedExecutionConfig(
        executor_type=str(execution_env.get("type", "local")),
        executor_kwargs=dict(execution_env.get("executor_kwargs", {})),
        prompt_template_path=prompt_template_path,
        planning_interval=planning_interval,
    )


def validate_execution_config_payload(normalized: Any) -> NormalizedExecutionConfig:
    if not isinstance(normalized, NormalizedExecutionConfig):
        raise ValueError("execution normalized config must be NormalizedExecutionConfig")

    executor_type = normalized.executor_type
    if not isinstance(executor_type, str) or not executor_type.strip():
        raise ValueError("execution normalized executor_type must be a non-empty string")
    executor_type = executor_type.strip().lower()
    if executor_type not in _ALLOWED_EXECUTION_ENV_TYPES:
        raise ValueError(
            "execution normalized executor_type must be one of ['local', 'e2b', 'docker', 'wasm']"
        )

    executor_kwargs = normalized.executor_kwargs
    if not isinstance(executor_kwargs, dict):
        raise ValueError("execution normalized executor_kwargs must be a dictionary")

    prompt_template_path = normalized.prompt_template_path
    if prompt_template_path is not None:
        if not isinstance(prompt_template_path, str) or not prompt_template_path.strip():
            raise ValueError("execution normalized prompt_template_path must be a non-empty string when provided")
        prompt_template_path = prompt_template_path.strip()

    planning_interval = normalized.planning_interval
    if planning_interval is not None:
        if isinstance(planning_interval, bool) or not isinstance(planning_interval, int) or planning_interval <= 0:
            raise ValueError("execution normalized planning_interval must be a positive integer when provided")

    return NormalizedExecutionConfig(
        executor_type=executor_type,
        executor_kwargs=dict(executor_kwargs),
        prompt_template_path=prompt_template_path,
        planning_interval=planning_interval,
    )


class AgentConfigNormalizer:
    @staticmethod
    def validate_tools_config_entries(tool_configs: Any) -> None:
        """Validate the ``tools`` field from an Agent YAML.

        ``tools`` must be a list of tool declaration dicts, each with
        at least a ``name`` key.  Shell settings and tool mapping are
        configured via separate top-level keys (``shell_settings``,
        ``tools_mapping``).
        """
        if tool_configs is None:
            return
        if not isinstance(tool_configs, list):
            raise ValueError("tools configuration must be a list when provided")
        for tool_config in tool_configs:
            if not isinstance(tool_config, dict):
                raise ValueError("Tool configuration must be a dictionary")
            if "name" not in tool_config:
                raise ValueError("Tool configuration is missing required 'name' field")
            tool_name = tool_config["name"]
            if not isinstance(tool_name, str) or not tool_name.strip():
                raise ValueError("Tool configuration 'name' must be a non-empty string")
            if "module" in tool_config or "function" in tool_config:
                if "module" not in tool_config or "function" not in tool_config:
                    raise ValueError(
                        f"Dynamically loaded tool '{tool_name}' must include both 'module' and 'function' fields"
                    )
                for field_name in ("module", "function"):
                    value = tool_config[field_name]
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(
                            f"Dynamically loaded tool '{tool_name}' {field_name} "
                            "must be a non-empty string"
                        )
            if "fixed_args" in tool_config and tool_config["fixed_args"] is not None:
                if not isinstance(tool_config["fixed_args"], dict):
                    raise ValueError(
                        f"Tool '{tool_name}' fixed_args must be a dictionary when provided"
                    )

    @staticmethod
    def validate_required_fields(config: dict, required_fields: list[str] | tuple[str, ...]) -> None:
        for field in required_fields:
            if field not in config:
                raise ValueError(f"Configuration is missing required field: {field}")

    @staticmethod
    def validate_tools_config(config: dict) -> None:
        AgentConfigNormalizer.validate_tools_config_entries(config.get("tools"))

    @staticmethod
    def validate_runtime_tool_references(config: dict) -> None:
        """Resolve declarative built-ins without constructing or running tools.

        Dynamic tools are intentionally limited to structural validation here:
        importing their configured module can execute arbitrary application
        code, so that remains part of actual Agent construction.
        """

        from src.tools.catalog import resolve_toolsets
        from src.tools.loader import resolve_tool_function

        AgentConfigNormalizer.validate_tools_config(config)

        if "toolsets" in config:
            raw_toolsets = config["toolsets"]
            if not isinstance(raw_toolsets, list):
                raise ValueError("toolsets must be a list of toolset names when provided")
            resolve_toolsets(raw_toolsets)

        for tool_config in config.get("tools", []):
            if "module" in tool_config and "function" in tool_config:
                continue
            tool_function = resolve_tool_function(tool_config["name"])
            AgentConfigNormalizer.validate_fixed_tool_args(
                tool_function,
                tool_config["name"],
                dict(tool_config.get("fixed_args") or {}),
            )

    @staticmethod
    def validate_fixed_tool_args(
        tool_function: Callable[..., Any],
        tool_name: str,
        fixed_args: dict[str, Any],
    ) -> None:
        """Validate the keyword binding contract without invoking the tool."""

        parameters = inspect.signature(tool_function).parameters
        accepts_var_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        unknown_args = [
            argument
            for argument in fixed_args
            if argument not in parameters and not accepts_var_kwargs
        ]
        if unknown_args:
            joined_args = ", ".join(sorted(unknown_args))
            raise ValueError(f"Unknown fixed_args for tool '{tool_name}': {joined_args}")

    @staticmethod
    def validate_max_steps_config(config: dict) -> None:
        if "max_steps" not in config:
            return
        max_steps = config["max_steps"]
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps <= 0:
            raise ValueError("max_steps must be a positive integer when provided")

    @staticmethod
    def validate_workflow_config(config: dict) -> None:
        workflow = config.get("workflow")
        if workflow is None:
            return
        if isinstance(workflow, str):
            if workflow.strip():
                return
            raise ValueError(_WORKFLOW_VALIDATION_ERROR)
        if isinstance(workflow, list):
            if workflow and all(isinstance(item, str) and item.strip() for item in workflow):
                return
            raise ValueError(_WORKFLOW_VALIDATION_ERROR)
        raise ValueError(_WORKFLOW_VALIDATION_ERROR)

    @staticmethod
    def normalize_workflow_items(workflow: Any) -> list[str]:
        """Return workflow as validated sequential text items."""
        if isinstance(workflow, str) and workflow.strip():
            return [workflow]
        if isinstance(workflow, list) and workflow and all(isinstance(item, str) and item.strip() for item in workflow):
            return list(workflow)
        raise ValueError(_WORKFLOW_VALIDATION_ERROR)

    @staticmethod
    def validate_skills_config(config: dict) -> None:
        if "skills" in config:
            skills_conf = config["skills"]
            if not isinstance(skills_conf, (list, dict, str)):
                raise ValueError("Configuration error: skills must be a list, dict, or string path")

    @staticmethod
    def resolve_tool_call_type(
        config: dict,
        *,
        default_tool_call_type: str,
        allowed_tool_call_types: tuple[str, ...],
    ) -> str:
        tool_call_type = config.get("tool_call_type", default_tool_call_type)
        if tool_call_type not in allowed_tool_call_types:
            raise ValueError(
                f"tool_call_type must be 'tool_call' or 'code_act', current value: {tool_call_type}"
            )
        return tool_call_type

    @staticmethod
    def validate_tool_call_type_config(
        config: dict,
        *,
        default_tool_call_type: str,
        allowed_tool_call_types: tuple[str, ...],
    ) -> None:
        AgentConfigNormalizer.resolve_tool_call_type(
            config,
            default_tool_call_type=default_tool_call_type,
            allowed_tool_call_types=allowed_tool_call_types,
        )

    @staticmethod
    def validate_role_driven_config(
        config: dict,
        *,
        required_fields: tuple[str, ...],
        default_tool_call_type: str,
        allowed_tool_call_types: tuple[str, ...],
        build_normalized: Callable[[], Any | None],
        validate_role_specific: Callable[[Any | None], None],
    ) -> Any | None:
        if required_fields:
            AgentConfigNormalizer.validate_required_fields(config, list(required_fields))
            AgentConfigNormalizer.validate_tools_config(config)
            AgentConfigNormalizer.validate_workflow_config(config)
            AgentConfigNormalizer.validate_skills_config(config)

        AgentConfigNormalizer.validate_tool_call_type_config(
            config,
            default_tool_call_type=default_tool_call_type,
            allowed_tool_call_types=allowed_tool_call_types,
        )

        normalized = build_normalized()
        validate_role_specific(normalized)
        return normalized

    @staticmethod
    def validate_skill_dependencies(
        config: dict,
        skills_manager: Any,
        *,
        default_tools: list[str] | tuple[str, ...] | set[str],
        logger: Any = None,
    ) -> None:
        """Check whether all skill-declared dependency tools exist in agent configuration."""
        log = get_logger(logger, __name__)
        skills = getattr(skills_manager, "skills", None)
        if not skills:
            return

        agent_tool_names = set(default_tools)
        for tool_item in config.get("tools", []):
            if isinstance(tool_item, dict):
                agent_tool_names.add(tool_item.get("name"))

        missing_tools_map: dict[str, list[str]] = {}
        for skill_name, skill in skills.items():
            allowed_tools = getattr(getattr(skill, "metadata", None), "allowed_tools", None)
            if not allowed_tools:
                continue

            missing_tools = [
                tool_name for tool_name in allowed_tools
                if tool_name not in agent_tool_names
            ]
            if missing_tools:
                missing_tools_map[skill_name] = missing_tools

        if missing_tools_map:
            lines = []
            lines.append(f"\n{'='*60}")
            lines.append("SKILL CONFIGURATION INTEGRITY CHECK FAILED")
            lines.append(f"{'='*60}\n")

            lines.append(f"Agent: {config.get('name')}")
            lines.append("Issue: Skills require tools that are not configured in 'xxx_agent.yaml'.\n")

            lines.append("Missing Tools by Skill:")
            for skill_name, tools in missing_tools_map.items():
                tools_str = ", ".join([f"'{tool_name}'" for tool_name in tools])
                lines.append(f"  • [Skill: {skill_name}]")
                lines.append(f"     MISSING -> {tools_str}")

            lines.append(f"{'='*60}")
            warning_msg = "\n".join(lines)
            log.warning(warning_msg)

    @staticmethod
    def validate_agent_function_schema(config: dict) -> Optional[dict]:
        """
        Validate and normalize worker tool schema.

        Optional field: if absent, worker is not exported as a tool.
        """
        raw_schema = config.get("agent_function_schema")
        if raw_schema is None:
            return None
        if not isinstance(raw_schema, dict):
            raise ValueError("agent_function_schema must be a dictionary when provided")

        description = raw_schema.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("agent_function_schema.description must be a non-empty string")

        raw_inputs = raw_schema.get("inputs")
        if not isinstance(raw_inputs, dict) or not raw_inputs:
            raise ValueError("agent_function_schema.inputs must be a non-empty dictionary")

        normalized_inputs: dict[str, dict[str, Any]] = {}
        for param_name, param_spec in raw_inputs.items():
            if not isinstance(param_name, str) or not param_name.isidentifier():
                raise ValueError(f"agent_function_schema.inputs key '{param_name}' must be a valid identifier")
            if not isinstance(param_spec, dict):
                raise ValueError(f"agent_function_schema.inputs.{param_name} must be a dictionary")

            param_description = param_spec.get("description")
            if not isinstance(param_description, str) or not param_description.strip():
                raise ValueError(f"agent_function_schema.inputs.{param_name}.description must be a non-empty string")

            required_raw = param_spec.get("required", True)
            if not isinstance(required_raw, bool):
                raise ValueError(f"agent_function_schema.inputs.{param_name}.required must be a boolean when provided")

            normalized_inputs[param_name] = {
                # Input types are normalized to string for a stable tool contract.
                "type": "string",
                "description": param_description.strip(),
                "required": required_raw,
            }

        raw_output = raw_schema.get("output")
        if not isinstance(raw_output, dict):
            raise ValueError("agent_function_schema.output must be a dictionary")

        output_description = raw_output.get("description")
        if not isinstance(output_description, str) or not output_description.strip():
            raise ValueError("agent_function_schema.output.description must be a non-empty string")

        return {
            "description": description.strip(),
            "inputs": normalized_inputs,
            "output": {
                "description": output_description.strip(),
            },
        }

    @staticmethod
    def validate_worker_agents_config(worker_agents_config: list[dict]) -> None:
        """Validate the worker_agents path-only schema."""
        if not isinstance(worker_agents_config, list):
            raise ValueError("worker_agents must be a list")

        errors = []
        for idx, agent_conf in enumerate(worker_agents_config):
            if not isinstance(agent_conf, dict):
                errors.append(f"worker_agents[{idx}] must be a dictionary with required 'path' field")
                continue

            if "name" in agent_conf:
                errors.append(f"worker_agents[{idx}] uses unsupported field 'name'; use 'path' only")

            path_value = agent_conf.get("path")
            if not isinstance(path_value, str) or not path_value.strip():
                errors.append(f"worker_agents[{idx}] is missing required non-empty 'path' field")

        if errors:
            raise ValueError("worker_agents configuration error:\n- " + "\n- ".join(errors))

    @staticmethod
    def resolve_worker_agent_config_path(
        path_value: str,
        worker_agents_folder: Path,
        *,
        agent_root: Path | str,
    ) -> Path:
        path_value = path_value.strip()
        configured_path = Path(path_value)

        if configured_path.is_absolute():
            return configured_path.resolve()

        if "/" in path_value or "\\" in path_value:
            return (Path(agent_root).resolve() / configured_path).resolve()

        if configured_path.suffix:
            # Has a file extension — resolve in worker_agents folder.
            # Precheck will reject unsupported extensions (.txt etc.).
            return (worker_agents_folder / configured_path).resolve()
        raise ValueError(
            f"worker_agents path '{path_value}' is missing a file extension; "
            f"must end with .yaml, .yml, or .md (e.g. '{path_value}.yaml')"
        )

    @classmethod
    def precheck_worker_agent_paths(
        cls,
        expected_agents: list[dict],
        worker_agents_folder: Path,
        *,
        agent_root: Path | str,
    ) -> list[tuple[str, Path]]:
        if not expected_agents:
            return []
        cls.validate_worker_agents_config(expected_agents)

        errors: list[str] = []
        resolved_items: list[tuple[str, Path]] = []

        if not worker_agents_folder.exists():
            errors.append(f"worker_agents folder not found: {worker_agents_folder}")
        elif not worker_agents_folder.is_dir():
            errors.append(f"worker_agents path is not a directory: {worker_agents_folder}")

        for idx, agent_conf in enumerate(expected_agents):
            configured_path = agent_conf["path"].strip()
            resolved_path = cls.resolve_worker_agent_config_path(
                configured_path,
                worker_agents_folder,
                agent_root=agent_root,
            )

            if not resolved_path.exists():
                errors.append(
                    f"worker_agents[{idx}] path '{configured_path}' resolved to '{resolved_path}' does not exist"
                )
                continue

            if not resolved_path.is_file():
                errors.append(
                    f"worker_agents[{idx}] path '{configured_path}' resolved to '{resolved_path}' is not a file"
                )
                continue

            if resolved_path.suffix.lower() not in (".yaml", ".yml", ".md"):
                errors.append(
                    f"worker_agents[{idx}] path '{configured_path}' resolved to '{resolved_path}' has unsupported extension"
                )
                continue

            resolved_items.append((configured_path, resolved_path))

        if errors:
            raise ValueError("worker_agents precheck failed:\n- " + "\n- ".join(errors))

        return resolved_items

    @classmethod
    def build_worker_normalized_config(
        cls,
        config: dict,
        *,
        agent_root: Path,
        source_name: str,
    ) -> NormalizedAgentConfig:
        _ = agent_root
        if "goal" in config:
            raise ValueError(
                f"Worker Agent configuration {source_name} must not define goal; "
                "Goal mode is Supervisor-only"
            )
        return NormalizedAgentConfig(
            agent_function_schema=cls.validate_agent_function_schema(config),
            goal=GoalConfig(),
        )

    @classmethod
    def build_supervisor_normalized_config(
        cls,
        config: dict,
        *,
        agent_root: Path,
        source_name: str,
    ) -> NormalizedAgentConfig:
        _ = agent_root
        name = str(config.get("name", source_name))
        return NormalizedAgentConfig(
            agent_function_schema=None,
            goal=normalize_goal_config(config, source=name),
        )
