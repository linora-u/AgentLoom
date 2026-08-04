"""Lightweight validation shared by the runner and read-only project tools.

This module deliberately imports no Agent implementation, model provider, or
tool registry.  Catalog/index callers can validate configuration without
paying the cost of constructing the execution runtime.
"""

from __future__ import annotations

from pathlib import Path

from src.lib.smolagents.agent.agent_validation import (
    AgentConfigNormalizer,
    build_normalized_execution_config,
    validate_execution_config_payload,
    validate_todo_config,
)

REQUIRED_YAML_FIELDS = ("name", "workflow", "description")


def validate_required_yaml_fields(config: dict, yaml_path: Path | str) -> None:
    missing: list[str] = []
    invalid: list[str] = []
    for field in ("name", "description"):
        value = config.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field)
        elif not isinstance(value, str):
            invalid.append(f"{field} must be a non-empty string")

    workflow = config.get("workflow")
    workflow_valid = False
    if isinstance(workflow, str):
        workflow_valid = bool(workflow.strip())
    elif isinstance(workflow, list):
        workflow_valid = bool(workflow) and all(isinstance(item, str) and item.strip() for item in workflow)
    if not workflow_valid:
        missing.append("workflow")

    if invalid:
        problems: list[str] = []
        if missing:
            problems.append(f"missing: {', '.join(missing)}")
        problems.extend(invalid)
        raise ValueError(
            f"YAML 配置文件 {yaml_path} 必填字段无效: {'; '.join(problems)}\n"
            f"请确保 YAML 中包含以下字段: {', '.join(REQUIRED_YAML_FIELDS)}"
        )
    if missing:
        raise ValueError(
            f"YAML 配置文件 {yaml_path} 缺少必填字段: {', '.join(missing)}\n"
            f"请确保 YAML 中包含以下字段: {', '.join(REQUIRED_YAML_FIELDS)}"
        )


def validate_runtime_agent_config(
    config: dict,
    yaml_path: Path | str,
    *,
    agent_root: Path | str,
) -> None:
    validate_required_yaml_fields(config, yaml_path)
    AgentConfigNormalizer.validate_runtime_tool_references(config)
    AgentConfigNormalizer.validate_workflow_config(config)
    AgentConfigNormalizer.validate_skills_config(config)
    AgentConfigNormalizer.validate_max_steps_config(config)
    AgentConfigNormalizer.validate_tool_call_type_config(
        config,
        default_tool_call_type="tool_call",
        allowed_tool_call_types=("tool_call", "code_act"),
    )
    AgentConfigNormalizer.validate_agent_function_schema(config)
    AgentConfigNormalizer.validate_worker_agents_config(config.get("worker_agents", []))
    validate_todo_config(config, source=str(yaml_path))
    normalized_execution = build_normalized_execution_config(
        config,
        source_name=str(yaml_path),
        agent_root=agent_root,
    )
    validate_execution_config_payload(normalized_execution)


def validate_runtime_worker_config(
    config: dict,
    yaml_path: Path | str,
    *,
    agent_root: Path | str,
) -> None:
    validate_runtime_agent_config(config, yaml_path, agent_root=agent_root)
    if AgentConfigNormalizer.validate_agent_function_schema(config) is None:
        raise ValueError(f"Worker Agent configuration {yaml_path} agent_function_schema is required")
