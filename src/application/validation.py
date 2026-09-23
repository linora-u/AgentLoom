from __future__ import annotations

import inspect
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from agentloom.application.composition import build_builtin_runtime_registry
from agentloom.execution.agent_runtime import (
    OutputContract,
    RuntimeRequirements,
)
from agentloom.execution.goal import GoalConfig, normalize_goal_config
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry


@dataclass
class NormalizedAgentConfig:
    input_schema: dict[str, Any] | None = None
    input_validator: Callable[[object], None] | None = None
    output_contract: OutputContract | None = None
    goal: GoalConfig = dataclass_field(default_factory=GoalConfig)


_WORKFLOW_VALIDATION_ERROR = "workflow field must be a non-empty string"
_DEFAULT_WORKER_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "Task for this Agent.",
        },
    },
    "required": ["task"],
    "additionalProperties": False,
}


def _reject_remote_schema_refs(value: object, *, field_name: str, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in {"$ref", "$dynamicRef"} and (
                not isinstance(child, str) or not child.startswith("#")
            ):
                raise ValueError(
                    f"{field_name} contains a remote reference at {child_path}"
                )
            _reject_remote_schema_refs(
                child,
                field_name=field_name,
                path=child_path,
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_remote_schema_refs(
                child,
                field_name=field_name,
                path=f"{path}[{index}]",
            )


def _compile_input_schema(
    raw_schema: object,
) -> tuple[dict[str, Any], Callable[[object], None]]:
    if not isinstance(raw_schema, dict):
        raise ValueError("input_schema must be a JSON Schema mapping")
    schema = deepcopy(raw_schema)
    _reject_remote_schema_refs(schema, field_name="input_schema")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError(
            f"input_schema must be valid Draft 2020-12: {exc.message}"
        ) from exc
    if schema.get("type") != "object":
        raise ValueError("input_schema root type must be object")
    validator = Draft202012Validator(schema, registry=Registry())
    return schema, validator.validate


class AgentConfigNormalizer:
    @staticmethod
    def validate_agent_runtime_config(
        config: dict,
        *,
        effective_config: dict[str, Any] | None = None,
        hook_plan=None,
    ) -> str:
        """Return the explicitly selected, currently registered Agent runtime."""

        runtime_id = config.get("agent_runtime")
        if runtime_id is None:
            available = ", ".join(
                build_builtin_runtime_registry().runtime_ids
            )
            raise ValueError(
                "Configuration is missing required 'agent_runtime' field; "
                f"available runtimes: {available}"
            )
        requirements = AgentConfigNormalizer.runtime_requirements(
            config, effective_config=effective_config, hook_plan=hook_plan,
        )
        build_builtin_runtime_registry().validate(
            runtime_id,
            requirements=requirements,
        )
        return runtime_id

    @staticmethod
    def runtime_requirements(config: dict, *, effective_config: dict | None = None, hook_plan=None) -> RuntimeRequirements:
        """Derive requirements from selected functions, not from an engine assumption."""
        from agentloom.tools.selection import resolve_runtime_toolsets

        effective = effective_config if effective_config is not None else config
        AgentConfigNormalizer.validate_tools_config_entries(effective.get("tools"))
        selected_tools = resolve_runtime_toolsets(config, effective_config)
        tools_selected = bool(effective.get("tools") or selected_tools)
        checkpoint = effective.get("checkpoint", {})
        concurrency = effective.get("concurrency", config.get("concurrency"))
        goal = normalize_goal_config(config, source=str(config.get("name", "agent"))).enabled
        subagents = bool(effective.get("worker_agents", config.get("worker_agents")))
        mcp = effective.get("mcp_servers")
        # smol's own terminal tool is installed by its adapter.
        structured_tools = bool(tools_selected or mcp or subagents or goal or config.get("agent_runtime") == "smolagents")
        return RuntimeRequirements(
            structured_tools=structured_tools,
            parallel_tools=structured_tools and (concurrency == "auto" or (
                isinstance(concurrency, int) and not isinstance(concurrency, bool) and concurrency > 1
            )),
            checkpoint_resume=isinstance(checkpoint, dict) and checkpoint.get("enabled") is True,
            subagents=subagents,
            goal=goal,
            stop_hooks=bool(hook_plan is not None and any(
                handler.event.value == "Stop" and handler.source != "internal"
                for handler in hook_plan.handlers
            )),
            structured_output="output_schema" in config,
        )

    @staticmethod
    def validate_tools_config_entries(tool_configs: Any) -> None:
        """Validate the ``tools`` field from an Agent YAML.

        ``tools`` must be a list of tool declaration dicts, each with
        at least a ``name`` key. Shell settings are configured through the
        separate top-level ``shell_settings`` key.
        """
        if tool_configs is None:
            return
        if not isinstance(tool_configs, list):
            raise ValueError("tools configuration must be a list when provided")
        seen_names: set[str] = set()
        for tool_config in tool_configs:
            if not isinstance(tool_config, dict):
                raise ValueError("Tool configuration must be a dictionary")
            if "name" not in tool_config:
                raise ValueError("Tool configuration is missing required 'name' field")
            tool_name = tool_config["name"]
            if not isinstance(tool_name, str) or not tool_name.strip():
                raise ValueError("Tool configuration 'name' must be a non-empty string")
            if tool_name in seen_names:
                raise ValueError(f"Duplicate tool name: {tool_name}")
            seen_names.add(tool_name)
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
        """Validate declarative built-in references without importing tools.

        Dynamic tools are intentionally limited to structural validation here:
        importing their configured module can execute arbitrary application
        code, so that remains part of actual Agent construction. Built-in
        ``fixed_args`` are checked against the signature contract stored in the
        metadata catalog, then checked against the callable again at construction.
        """

        from agentloom.tools.catalog import get_tool_spec, resolve_toolsets

        AgentConfigNormalizer.validate_tools_config(config)

        if "toolsets" in config:
            raw_toolsets = config["toolsets"]
            if not isinstance(raw_toolsets, list):
                raise ValueError("toolsets must be a list of toolset names when provided")
            resolve_toolsets(raw_toolsets)

        for tool_config in config.get("tools") or []:
            if "module" in tool_config and "function" in tool_config:
                continue
            spec = get_tool_spec(tool_config["name"])
            fixed_args = dict(tool_config.get("fixed_args") or {})
            if spec.accepts_extra_fixed_args:
                continue
            unknown_args = sorted(set(fixed_args) - set(spec.fixed_arg_names))
            if unknown_args:
                joined_args = ", ".join(unknown_args)
                raise ValueError(
                    f"Unknown fixed_args for tool '{spec.name}': {joined_args}"
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
    def validate_workflow_config(config: dict) -> None:
        workflow = config.get("workflow")
        if workflow is None:
            return
        if isinstance(workflow, str) and workflow.strip():
            return
        raise ValueError(_WORKFLOW_VALIDATION_ERROR)

    @staticmethod
    def validate_skills_config(config: dict) -> None:
        if "skills" not in config:
            return
        skills_conf = config["skills"]
        if not isinstance(skills_conf, dict) or set(skills_conf) != {"paths"}:
            raise ValueError("Configuration error: skills must contain only a 'paths' list")
        paths = skills_conf["paths"]
        if not isinstance(paths, list) or any(not isinstance(item, str) or not item.strip() for item in paths):
            raise ValueError("Configuration error: skills.paths must be a list of non-empty path strings")

    @staticmethod
    def validate_removed_fields(config: dict) -> None:
        """Keep removed-field rejection identical in preflight and construction."""
        if "tools_mapping" in config:
            raise ValueError(
                "Configuration error: tools_mapping was removed; Skills do not grant tools"
            )
        if "agent_function_schema" in config:
            raise ValueError(
                "Configuration error: agent_function_schema was removed; "
                "use input_schema and output_schema"
            )

    @staticmethod
    def validate_role_driven_config(
        config: dict,
        *,
        required_fields: tuple[str, ...],
        build_normalized: Callable[[], Any | None],
        validate_role_specific: Callable[[Any | None], None],
    ) -> Any | None:
        AgentConfigNormalizer.validate_removed_fields(config)
        AgentConfigNormalizer.validate_agent_runtime_config(config)
        if required_fields:
            AgentConfigNormalizer.validate_required_fields(config, list(required_fields))
            AgentConfigNormalizer.validate_tools_config(config)
            AgentConfigNormalizer.validate_workflow_config(config)
            AgentConfigNormalizer.validate_skills_config(config)

        normalized = build_normalized()
        validate_role_specific(normalized)
        return normalized

    @staticmethod
    def validate_agent_schemas(
        config: dict,
        *,
        include_input: bool,
    ) -> tuple[
        dict[str, Any] | None,
        Callable[[object], None] | None,
        OutputContract | None,
    ]:
        input_schema = None
        input_validator = None
        if include_input:
            input_schema, input_validator = _compile_input_schema(
                config.get("input_schema", _DEFAULT_WORKER_INPUT_SCHEMA)
            )
        elif "input_schema" in config:
            _compile_input_schema(config["input_schema"])

        output_contract = None
        if "output_schema" in config:
            name = str(config.get("name") or "agent")
            output_contract = OutputContract(
                name=f"{name}_output",
                schema=config["output_schema"],
            )
        return input_schema, input_validator, output_contract

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
        from agentloom.application.paths import resolve_worker_reference
        return resolve_worker_reference(path_value, worker_agents_folder, project_root=agent_root)

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
        cls.validate_removed_fields(config)
        if "goal" in config:
            raise ValueError(
                f"Worker Agent configuration {source_name} must not define goal; "
                "Goal mode is Supervisor-only"
            )
        input_schema, input_validator, output_contract = cls.validate_agent_schemas(
            config,
            include_input=True,
        )
        return NormalizedAgentConfig(
            input_schema=input_schema,
            input_validator=input_validator,
            output_contract=output_contract,
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
        cls.validate_removed_fields(config)
        name = str(config.get("name", source_name))
        _, _, output_contract = cls.validate_agent_schemas(
            config,
            include_input=False,
        )
        return NormalizedAgentConfig(
            output_contract=output_contract,
            goal=normalize_goal_config(config, source=name),
        )
