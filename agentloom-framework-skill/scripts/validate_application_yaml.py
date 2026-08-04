#!/usr/bin/env python3
"""Validate generated AgentLoom application YAML configuration.

Public CLI:
    .venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py \
      --app-root applications/<app_name>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

REQUIRED_FIELDS = ("name", "description", "workflow")
FORBIDDEN_TOP_LEVEL = {"model", "llm", "langfuse"}
GLOBAL_ONLY_TOP_LEVEL = {"runtime", "logging"}
ALLOWED_TOOL_CALL_TYPES = {"code_act", "tool_call"}
ALLOWED_EXECUTION_ENV_TYPES = {"local", "docker", "e2b", "wasm"}
ALLOWED_WORKER_EXTENSIONS = {".yaml", ".yml", ".md"}
ALLOWED_SKILL_LOAD_MODES = {"on-demand", "eager"}
ALLOWED_CONCURRENCY_VALUES = {"auto"}
LLM_ONLY_TOP_LEVEL = {"model", "llm", "langfuse"}


def _is_positive_int_value(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value > 0
    return False


def _is_positive_int_or_numeric_string(value: Any) -> bool:
    if _is_positive_int_value(value):
        return True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        try:
            parsed = int(text)
        except ValueError:
            return False
        return parsed > 0
    return False


def _load_llm_model_index(project_root: Path) -> tuple[set[str], str, Path | None]:
    llm_path = project_root / "config" / "llm.yaml"
    if not llm_path.is_file():
        return set(), "", None
    raw = yaml.safe_load(llm_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return set(), "", llm_path
    model_raw = raw.get("model", {})
    if not isinstance(model_raw, dict):
        return set(), "", llm_path
    model_types = {
        str(key).strip().lower()
        for key, value in model_raw.items()
        if key not in {"default_model_type", "common"} and isinstance(value, dict)
    }
    default_model_type = str(model_raw.get("default_model_type") or "").strip().lower()
    return model_types, default_model_type, llm_path


def _validate_model_type_reference(
    config: dict[str, Any],
    file_path: Path,
    errors: list[dict[str, str]],
    project_root: Path,
    *,
    model_types: set[str],
    default_model_type: str,
    llm_path: Path | None,
) -> None:
    if llm_path is None:
        return

    model_type = config.get("model_type")
    if model_type is None:
        if not default_model_type:
            _add_error(
                errors,
                file_path=file_path,
                field="model_type",
                rule="model_type_or_default_required",
                message="Agent 未配置 model_type，且 config/llm.yaml 未配置 model.default_model_type",
                suggestion="在 Agent YAML 显式设置 model_type，或在 config/llm.yaml 设置 model.default_model_type",
                project_root=project_root,
            )
        elif default_model_type not in model_types:
            _add_error(
                errors,
                file_path=file_path,
                field="model_type",
                rule="default_model_type_exists",
                message=f"config/llm.yaml 的默认模型类型不存在: {default_model_type}",
                suggestion=f"把 model.default_model_type 改成以下之一: {', '.join(sorted(model_types))}",
                project_root=project_root,
            )
        return

    if not isinstance(model_type, str) or not model_type.strip():
        _add_error(
            errors,
            file_path=file_path,
            field="model_type",
            rule="type_non_empty_string",
            message="model_type 必须是非空字符串",
            suggestion="设置为 config/llm.yaml 中已有的模型类型",
            project_root=project_root,
        )
        return

    normalized = model_type.strip().lower()
    if normalized not in model_types:
        _add_error(
            errors,
            file_path=file_path,
            field="model_type",
            rule="model_type_exists",
            message=f"model_type={model_type!r} 未在 config/llm.yaml 中定义",
            suggestion=f"改为以下之一，或先在 config/llm.yaml 添加该模型类型: {', '.join(sorted(model_types))}",
            project_root=project_root,
        )


def _add_error(
    errors: list[dict[str, str]],
    *,
    file_path: Path,
    field: str,
    rule: str,
    message: str,
    suggestion: str,
    project_root: Path,
) -> None:
    try:
        display_path = str(file_path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        display_path = str(file_path.resolve())

    errors.append(
        {
            "file": display_path,
            "field": field,
            "rule": rule,
            "message": message,
            "suggestion": suggestion,
        }
    )


def _discover_project_root(start: Path) -> Path | None:
    current = start.resolve()
    # Prefer config/llm.yaml — it only exists at the project root,
    # unlike config/system.yaml which may also appear in application subdirectories.
    # In new worktrees this ignored local file may be missing; callers should
    # surface that as an environment precondition, not as an Application error.
    for candidate in [current, *current.parents]:
        if (candidate / "config" / "llm.yaml").is_file():
            return candidate
    # Fallback to config/system.yaml for environments without llm.yaml (e.g. tests).
    for candidate in [current, *current.parents]:
        if (candidate / "config" / "system.yaml").is_file():
            return candidate
    return None


def _extract_yaml_from_markdown(content: str) -> dict[str, Any]:
    yaml_pattern = r"```yaml\s*\n(.*?)\n```"
    match = re.search(yaml_pattern, content, re.DOTALL)
    if not match:
        raise ValueError("Markdown 文件中未找到 ```yaml ... ``` 代码块")
    loaded = yaml.safe_load(match.group(1))
    if not isinstance(loaded, dict):
        raise ValueError("Markdown 中的 YAML 代码块必须是字典对象")

    workflow_content = re.sub(yaml_pattern, "", content, flags=re.DOTALL).strip()
    if workflow_content:
        loaded["workflow"] = workflow_content

    return loaded


def _load_agent_config(file_path: Path) -> dict[str, Any]:
    text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        loaded = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ValueError("YAML 顶层必须是字典对象")
        return loaded
    if suffix == ".md":
        return _extract_yaml_from_markdown(text)
    raise ValueError(f"不支持的文件类型: {suffix}")


def _resolve_worker_path(configured_path: str, supervisor_file: Path, project_root: Path) -> Path:
    path_value = configured_path.strip()
    configured = Path(path_value)
    worker_agents_folder = supervisor_file.parent / "worker_agents"

    if configured.is_absolute():
        return configured.resolve()
    if "/" in path_value or "\\" in path_value:
        return (project_root / configured).resolve()
    if configured.suffix.lower() in ALLOWED_WORKER_EXTENSIONS:
        return (worker_agents_folder / configured).resolve()
    return (worker_agents_folder / f"{path_value}.yaml").resolve()


def _validate_common_rules(
    config: dict[str, Any],
    file_path: Path,
    errors: list[dict[str, str]],
    project_root: Path,
) -> None:
    for field in ("name", "description"):
        value = config.get(field)
        if not isinstance(value, str) or not value.strip():
            _add_error(
                errors,
                file_path=file_path,
                field=field,
                rule="required_non_empty_string",
                message=f"字段 '{field}' 必须是非空字符串",
                suggestion=f"为 '{field}' 提供有效文本内容",
                project_root=project_root,
            )

    workflow = config.get("workflow")
    if isinstance(workflow, str):
        if not workflow.strip():
            _add_error(
                errors,
                file_path=file_path,
                field="workflow",
                rule="required_non_empty_string",
                message="字段 'workflow' 必须是非空字符串或非空字符串列表",
                suggestion="为 'workflow' 提供有效文本内容，或改为非空 list[str]",
                project_root=project_root,
            )
    elif isinstance(workflow, list):
        if not workflow:
            _add_error(
                errors,
                file_path=file_path,
                field="workflow",
                rule="required_non_empty_list",
                message="字段 'workflow' 列表不能为空",
                suggestion="至少提供一个 workflow 字符串条目",
                project_root=project_root,
            )
        for idx, item in enumerate(workflow):
            if not isinstance(item, str) or not item.strip():
                _add_error(
                    errors,
                    file_path=file_path,
                    field=f"workflow[{idx}]",
                    rule="required_non_empty_string",
                    message="workflow 列表项必须是非空字符串",
                    suggestion="删除空条目，或为该 workflow 条目补充有效文本",
                    project_root=project_root,
                )
    else:
        _add_error(
            errors,
            file_path=file_path,
            field="workflow",
            rule="type_string_or_list",
            message="字段 'workflow' 必须是非空字符串或非空字符串列表",
            suggestion="使用 workflow: | 多行文本，或 workflow: [list[str]] 顺序工作流",
            project_root=project_root,
        )

    for field in FORBIDDEN_TOP_LEVEL:
        if field in config:
            _add_error(
                errors,
                file_path=file_path,
                field=field,
                rule="forbidden_top_level_key",
                message=f"禁止在 Agent YAML 顶层使用 '{field}'",
                suggestion="删除该字段，并改为在 config/llm.yaml 中配置模型参数",
                project_root=project_root,
            )

    for field in GLOBAL_ONLY_TOP_LEVEL:
        if field in config:
            _add_error(
                errors,
                file_path=file_path,
                field=field,
                rule="global_only_top_level_key",
                message=f"禁止在 Agent YAML 顶层使用全局配置 '{field}'",
                suggestion=f"删除该字段；'{field}' 只能配置在项目根 config/system.yaml",
                project_root=project_root,
            )

    tool_call_type = config.get("tool_call_type")
    if tool_call_type is not None and tool_call_type not in ALLOWED_TOOL_CALL_TYPES:
        _add_error(
            errors,
            file_path=file_path,
            field="tool_call_type",
            rule="allowed_values",
            message=f"tool_call_type={tool_call_type!r} 非法",
            suggestion="仅使用 'code_act' 或 'tool_call'",
            project_root=project_root,
        )

    execution_env = config.get("execution_env")
    if execution_env is not None:
        if not isinstance(execution_env, dict):
            _add_error(
                errors,
                file_path=file_path,
                field="execution_env",
                rule="type_dict",
                message="execution_env 必须是字典",
                suggestion="使用 execution_env: {type: local|docker|e2b|wasm}",
                project_root=project_root,
            )
        else:
            env_type = execution_env.get("type")
            if not isinstance(env_type, str) or env_type.strip().lower() not in ALLOWED_EXECUTION_ENV_TYPES:
                _add_error(
                    errors,
                    file_path=file_path,
                    field="execution_env.type",
                    rule="allowed_values",
                    message=f"execution_env.type={env_type!r} 非法",
                    suggestion="仅使用 local/docker/e2b/wasm",
            project_root=project_root,
        )


def _validate_runtime_options(
    config: dict[str, Any],
    file_path: Path,
    errors: list[dict[str, str]],
    project_root: Path,
) -> None:
    if "max_steps" in config and not _is_positive_int_value(config.get("max_steps")):
        _add_error(
            errors,
            file_path=file_path,
            field="max_steps",
            rule="positive_integer",
            message="max_steps 必须是正整数",
            suggestion="删除该字段使用默认值 80，或设置为正整数",
            project_root=project_root,
        )

    if "planning_interval" in config and not _is_positive_int_or_numeric_string(config.get("planning_interval")):
        _add_error(
            errors,
            file_path=file_path,
            field="planning_interval",
            rule="positive_integer_or_numeric_string",
            message="planning_interval 必须是正整数或数字字符串",
            suggestion="删除该字段禁用周期 planning，或设置为如 3 的正整数",
            project_root=project_root,
        )

    if "todo" in config:
        todo = config.get("todo")
        if not isinstance(todo, dict):
            _add_error(
                errors,
                file_path=file_path,
                field="todo",
                rule="type_dict",
                message="todo 必须是字典",
                suggestion="使用 todo: {mode: auto|on|off}",
                project_root=project_root,
            )
        else:
            todo_mode = todo.get("mode", "auto")
            valid_todo_mode = isinstance(todo_mode, bool) or (
                isinstance(todo_mode, str)
                and todo_mode in {"auto", "on", "off"}
            )
        if isinstance(todo, dict) and (
            set(todo) - {"mode"} or not valid_todo_mode
        ):
            _add_error(
                errors,
                file_path=file_path,
                field="todo.mode",
                rule="allowed_values",
                message="todo 只能包含 mode，且 mode 必须是 auto、on 或 off",
                suggestion="使用 todo: {mode: auto|on|off}",
                project_root=project_root,
            )

    if "concurrency" in config:
        concurrency = config.get("concurrency")
        valid = (
            _is_positive_int_value(concurrency)
            or (
                isinstance(concurrency, str)
                and concurrency.strip().lower() in ALLOWED_CONCURRENCY_VALUES
            )
        )
        if not valid:
            _add_error(
                errors,
                file_path=file_path,
                field="concurrency",
                rule="positive_integer_or_auto",
                message="concurrency 仅支持正整数或字符串 'auto'",
                suggestion="删除该字段使用 auto，或设置为正整数，例如 4",
                project_root=project_root,
            )


def _validate_prompt_config(
    config: dict[str, Any],
    file_path: Path,
    errors: list[dict[str, str]],
    project_root: Path,
) -> None:
    if "prompt" not in config:
        return
    prompt = config.get("prompt")
    if isinstance(prompt, str):
        if prompt.strip():
            return
    elif isinstance(prompt, dict):
        path_value = prompt.get("path")
        if isinstance(path_value, str) and path_value.strip():
            return

    _add_error(
        errors,
        file_path=file_path,
        field="prompt",
        rule="type_string_or_mapping_with_path",
        message="prompt 必须是非空字符串路径，或包含非空 path 的字典",
        suggestion="使用 prompt: applications/<app>/sysprompt/code_agent.yaml 或 prompt: {path: ...}",
        project_root=project_root,
    )


def _validate_overlay_config_types(
    config: dict[str, Any],
    file_path: Path,
    errors: list[dict[str, str]],
    project_root: Path,
) -> None:
    dict_fields = (
        "system",
        "tool_access_control",
        "execution_env",
        "code_agent",
        "shell_settings",
        "tools_mapping",
    )
    for field in dict_fields:
        if field in config and not isinstance(config.get(field), dict):
            _add_error(
                errors,
                file_path=file_path,
                field=field,
                rule="type_dict",
                message=f"{field} 必须是字典",
                suggestion=f"删除 {field} 或改为 YAML mapping",
                project_root=project_root,
            )

    for field_name in ("default_toolsets", "toolsets"):
        if field_name not in config:
            continue
        tools = config.get(field_name)
        if (
            not isinstance(tools, list)
            or any(not isinstance(item, str) or not item.strip() for item in tools)
        ):
            _add_error(
                errors,
                file_path=file_path,
                field=field_name,
                rule="type_list_of_strings",
                message=f"{field_name} 必须是字符串列表",
                suggestion="使用如 toolsets: ['core_file', 'core_search']；空列表表示显式关闭内置工具",
                project_root=project_root,
            )

    if "default_loaded_tools" in config:
        _add_error(
            errors,
            file_path=file_path,
            field="default_loaded_tools",
            rule="removed_field",
            message="default_loaded_tools 已删除",
            suggestion="全局配置使用 default_toolsets；Agent YAML 使用 toolsets，空列表表示无内置工具",
            project_root=project_root,
        )

    tools_mapping = config.get("tools_mapping")
    if isinstance(tools_mapping, dict) and "mapping" in tools_mapping:
        _add_error(
            errors,
            file_path=file_path,
            field="tools_mapping.mapping",
            rule="removed_field",
            message="tools_mapping.mapping legacy fallback 已删除",
            suggestion="改用 tools_mapping.Claude 等平台键",
            project_root=project_root,
        )


def _validate_mcp_servers_config(
    config: dict[str, Any],
    file_path: Path,
    errors: list[dict[str, str]],
    project_root: Path,
) -> None:
    if "mcp_servers" not in config:
        return
    value = config.get("mcp_servers")
    if isinstance(value, str):
        if value.strip():
            return
    elif isinstance(value, list):
        if all(isinstance(item, str) and item.strip() for item in value):
            return
    elif isinstance(value, dict):
        has_path = isinstance(value.get("path"), str) and value.get("path", "").strip()
        paths = value.get("paths")
        has_paths = isinstance(paths, list) and all(isinstance(item, str) and item.strip() for item in paths)
        if has_path or has_paths:
            for key in ("timeout", "tool_timeout"):
                if key in value and not _is_positive_int_value(value.get(key)):
                    _add_error(
                        errors,
                        file_path=file_path,
                        field=f"mcp_servers.{key}",
                        rule="positive_integer",
                        message=f"mcp_servers.{key} 必须是正整数秒数",
                        suggestion=f"删除 {key} 使用默认值，或设置为正整数",
                        project_root=project_root,
                    )
            if "tool_name_prefix" in value and not isinstance(value.get("tool_name_prefix"), bool):
                _add_error(
                    errors,
                    file_path=file_path,
                    field="mcp_servers.tool_name_prefix",
                    rule="type_bool",
                    message="mcp_servers.tool_name_prefix 必须是布尔值",
                    suggestion="设置为 true/false，或删除使用默认值 true",
                    project_root=project_root,
                )
            return

    _add_error(
        errors,
        file_path=file_path,
        field="mcp_servers",
        rule="type_string_list_or_mapping_with_path",
        message="mcp_servers 必须是路径字符串、路径字符串列表，或包含 path/paths 的字典",
        suggestion="使用 mcp_servers: 'config/.mcp.json' 或 mcp_servers: {path: 'config/.mcp.json'}",
        project_root=project_root,
    )


def _validate_dynamic_tools(
    config: dict[str, Any],
    file_path: Path,
    errors: list[dict[str, str]],
    project_root: Path,
) -> None:
    tools = config.get("tools")
    if tools is None:
        return
    if not isinstance(tools, list):
        _add_error(
            errors,
            file_path=file_path,
            field="tools",
            rule="type_list",
            message="tools 必须是列表",
            suggestion="将 tools 改为列表，元素为包含 name 的字典",
            project_root=project_root,
        )
        return

    for idx, tool in enumerate(tools):
        if not isinstance(tool, dict):
            _add_error(
                errors,
                file_path=file_path,
                field=f"tools[{idx}]",
                rule="type_dict",
                message="tools 列表项必须是字典",
                suggestion="将该项改为 {'name': '<tool_name>'}",
                project_root=project_root,
            )
            continue

        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():
            _add_error(
                errors,
                file_path=file_path,
                field=f"tools[{idx}].name",
                rule="required_non_empty_string",
                message="工具配置必须包含非空 name",
                suggestion="预定义工具写 {'name': 'read_file'}；动态工具还要补 module/function",
                project_root=project_root,
            )
            continue

        has_module = "module" in tool
        has_function = "function" in tool
        if has_module != has_function:
            _add_error(
                errors,
                file_path=file_path,
                field=f"tools[{idx}]",
                rule="dynamic_tool_pair_required",
                message="动态工具必须同时包含 module 和 function",
                suggestion="为该工具同时补齐 module 与 function，或删除两者回退为预定义工具",
                project_root=project_root,
            )

        if "fixed_args" in tool and tool.get("fixed_args") is not None and not isinstance(tool.get("fixed_args"), dict):
            _add_error(
                errors,
                file_path=file_path,
                field=f"tools[{idx}].fixed_args",
                rule="type_dict",
                message="fixed_args 必须是字典",
                suggestion="删除 fixed_args，或使用 fixed_args: {参数名: 固定值}",
                project_root=project_root,
            )


def _validate_single_skill_entry(
    item: Any,
    *,
    file_path: Path,
    field_prefix: str,
    errors: list[dict[str, str]],
    project_root: Path,
) -> None:
    if isinstance(item, str):
        if not item.strip():
            _add_error(
                errors,
                file_path=file_path,
                field=field_prefix,
                rule="required_non_empty_string",
                message="skills 路径字符串不能为空",
                suggestion="填写有效的 skills 路径，例如 skills/agent-recall-with-files",
                project_root=project_root,
            )
        return

    if not isinstance(item, dict):
        _add_error(
            errors,
            file_path=file_path,
            field=field_prefix,
            rule="type_dict_or_string",
            message="skills 列表项必须是字符串或字典",
            suggestion="使用字符串路径，或使用 {path/platform/load-mode/allow-scripts/allow-network} 字典",
            project_root=project_root,
        )
        return

    path_value = item.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        _add_error(
            errors,
            file_path=file_path,
            field=f"{field_prefix}.path",
            rule="required_non_empty_string",
            message="skills.path 必须是非空字符串",
            suggestion="填写有效路径，例如 skills/agent-recall-with-files",
            project_root=project_root,
        )

    platform = item.get("platform")
    if platform is not None and (not isinstance(platform, str) or not platform.strip()):
        _add_error(
            errors,
            file_path=file_path,
            field=f"{field_prefix}.platform",
            rule="type_non_empty_string",
            message="skills.platform 必须是非空字符串",
            suggestion="删除该字段或设置为有效平台名（如 Claude）",
            project_root=project_root,
        )

    _validate_skill_runtime_options(
        item,
        file_path=file_path,
        field_prefix=field_prefix,
        errors=errors,
        project_root=project_root,
    )


def _validate_skill_runtime_options(
    options: dict[str, Any],
    *,
    file_path: Path,
    field_prefix: str,
    errors: list[dict[str, str]],
    project_root: Path,
) -> None:
    load_mode = options.get("load-mode")
    if load_mode is not None and (
        not isinstance(load_mode, str)
        or load_mode.strip().lower() not in ALLOWED_SKILL_LOAD_MODES
    ):
        _add_error(
            errors,
            file_path=file_path,
            field=f"{field_prefix}.load-mode",
            rule="allowed_values",
            message="skills.load-mode 仅支持 on-demand 或 eager",
            suggestion="删除该字段或设置为 on-demand/eager",
            project_root=project_root,
        )

    for key in ("allow-scripts", "allow-network"):
        value = options.get(key)
        if value is not None and not isinstance(value, bool):
            _add_error(
                errors,
                file_path=file_path,
                field=f"{field_prefix}.{key}",
                rule="type_bool",
                message=f"skills.{key} 必须是布尔值",
                suggestion=f"将 {key} 设置为 true 或 false，或删除该字段使用默认允许",
                project_root=project_root,
            )


def _validate_skill_items(
    items: Any,
    *,
    file_path: Path,
    field_prefix: str,
    errors: list[dict[str, str]],
    project_root: Path,
) -> None:
    if isinstance(items, (str, dict)):
        _validate_single_skill_entry(
            items,
            file_path=file_path,
            field_prefix=field_prefix,
            errors=errors,
            project_root=project_root,
        )
        return

    if not isinstance(items, list):
        _add_error(
            errors,
            file_path=file_path,
            field=field_prefix,
            rule="type_list_dict_or_string",
            message="skills.items 必须是 list / dict / string",
            suggestion="使用字符串路径，或使用路径字典，或使用列表组合多个 skill",
            project_root=project_root,
        )
        return

    for idx, item in enumerate(items):
        _validate_single_skill_entry(
            item,
            file_path=file_path,
            field_prefix=f"{field_prefix}[{idx}]",
            errors=errors,
            project_root=project_root,
        )


def _validate_skills_config(
    config: dict[str, Any],
    file_path: Path,
    errors: list[dict[str, str]],
    project_root: Path,
) -> None:
    skills = config.get("skills")
    if skills is None:
        return

    if isinstance(skills, str):
        _validate_single_skill_entry(
            skills,
            file_path=file_path,
            field_prefix="skills",
            errors=errors,
            project_root=project_root,
        )
        return

    if isinstance(skills, dict):
        if "items" in skills:
            _validate_skill_runtime_options(
                skills,
                file_path=file_path,
                field_prefix="skills",
                errors=errors,
                project_root=project_root,
            )
            _validate_skill_items(
                skills.get("items"),
                file_path=file_path,
                field_prefix="skills.items",
                errors=errors,
                project_root=project_root,
            )
        else:
            _validate_single_skill_entry(
                skills,
                file_path=file_path,
                field_prefix="skills",
                errors=errors,
                project_root=project_root,
            )
        return

    if not isinstance(skills, list):
        _add_error(
            errors,
            file_path=file_path,
            field="skills",
            rule="type_list_dict_or_string",
            message="skills 必须是 list / dict / string",
            suggestion="使用三种受支持格式之一：string / dict(path...) / list",
            project_root=project_root,
        )
        return

    _validate_skill_items(
        skills,
        file_path=file_path,
        field_prefix="skills",
        errors=errors,
        project_root=project_root,
    )


def _validate_system_config_map(
    config: dict[str, Any],
    file_path: Path,
    errors: list[dict[str, str]],
    project_root: Path,
) -> None:
    for field in LLM_ONLY_TOP_LEVEL:
        if field in config:
            _add_error(
                errors,
                file_path=file_path,
                field=field,
                rule="forbidden_top_level_key",
                message=f"禁止在 system.yaml 中使用 '{field}'",
                suggestion="删除该字段，并改为在全局本地 config/llm.yaml 中配置",
                project_root=project_root,
            )

    for field in GLOBAL_ONLY_TOP_LEVEL:
        if field in config:
            _add_error(
                errors,
                file_path=file_path,
                field=field,
                rule="global_only_top_level_key",
                message=f"禁止在 Application system.yaml 中使用全局配置 '{field}'",
                suggestion=f"删除该字段；'{field}' 只能配置在项目根 config/system.yaml",
                project_root=project_root,
            )

    _validate_skills_config(config, file_path, errors, project_root)
    _validate_overlay_config_types(config, file_path, errors, project_root)
    _validate_mcp_servers_config(config, file_path, errors, project_root)

    checkpoint = config.get("checkpoint")
    if checkpoint is not None:
        if not isinstance(checkpoint, dict):
            _add_error(
                errors,
                file_path=file_path,
                field="checkpoint",
                rule="type_dict",
                message="checkpoint 必须是字典",
                suggestion="使用 checkpoint: {enabled: true, cleanup_on_success: true, max_resume_age: 604800, heartbeat_interval: 5}",
                project_root=project_root,
            )
        else:
            for key in ("enabled", "cleanup_on_success"):
                if key in checkpoint and not isinstance(checkpoint.get(key), bool):
                    _add_error(
                        errors,
                        file_path=file_path,
                        field=f"checkpoint.{key}",
                        rule="type_bool",
                        message=f"checkpoint.{key} 必须是布尔值",
                        suggestion=f"将 {key} 设置为 true 或 false",
                        project_root=project_root,
                    )
            for key in ("max_resume_age", "heartbeat_interval"):
                if key in checkpoint and not _is_positive_int_value(checkpoint.get(key)):
                    _add_error(
                        errors,
                        file_path=file_path,
                        field=f"checkpoint.{key}",
                        rule="positive_integer",
                        message=f"checkpoint.{key} 必须是正整数秒数",
                        suggestion=f"删除 {key} 使用默认值，或设置为正整数",
                        project_root=project_root,
                    )

    lsp_servers = config.get("lsp_servers")
    if lsp_servers is not None and not isinstance(lsp_servers, dict):
        _add_error(
            errors,
            file_path=file_path,
            field="lsp_servers",
            rule="type_dict",
            message="lsp_servers 必须是字典",
            suggestion="删除该字段，或使用 lsp_servers: {enabled: true, servers: ['python']}",
            project_root=project_root,
        )

    for field in ("tool_metadata", "tool_output_limits"):
        if field in config and not isinstance(config.get(field), dict):
            _add_error(
                errors,
                file_path=file_path,
                field=field,
                rule="type_dict",
                message=f"{field} 必须是字典",
                suggestion=f"删除 {field} 或改为 YAML mapping",
                project_root=project_root,
            )


def _validate_supervisor_worker_agents(
    config: dict[str, Any],
    supervisor_file: Path,
    errors: list[dict[str, str]],
    project_root: Path,
) -> set[Path]:
    referenced_workers: set[Path] = set()
    worker_agents = config.get("worker_agents")
    if worker_agents is None:
        return referenced_workers

    if not isinstance(worker_agents, list):
        _add_error(
            errors,
            file_path=supervisor_file,
            field="worker_agents",
            rule="type_list",
            message="worker_agents 必须是列表",
            suggestion="使用 worker_agents: [{path: '...'}]",
            project_root=project_root,
        )
        return referenced_workers

    for idx, item in enumerate(worker_agents):
        field = f"worker_agents[{idx}]"
        if not isinstance(item, dict):
            _add_error(
                errors,
                file_path=supervisor_file,
                field=field,
                rule="type_dict",
                message="worker_agents 列表项必须是字典",
                suggestion="改为 {'path': 'applications/<app>/workflows/worker_agents/<step>.yaml'}",
                project_root=project_root,
            )
            continue

        if "name" in item:
            _add_error(
                errors,
                file_path=supervisor_file,
                field=f"{field}.name",
                rule="unsupported_field",
                message="worker_agents 不支持 name 字段",
                suggestion="删除 name，仅保留 path 字段",
                project_root=project_root,
            )

        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            _add_error(
                errors,
                file_path=supervisor_file,
                field=f"{field}.path",
                rule="required_non_empty_string",
                message="worker_agents.path 必须是非空字符串",
                suggestion="填写有效的 Worker YAML 路径",
                project_root=project_root,
            )
            continue

        resolved = _resolve_worker_path(raw_path, supervisor_file, project_root)
        referenced_workers.add(resolved)

        if resolved.suffix.lower() not in ALLOWED_WORKER_EXTENSIONS:
            _add_error(
                errors,
                file_path=supervisor_file,
                field=f"{field}.path",
                rule="allowed_extension",
                message=f"Worker 文件后缀不支持: {resolved.suffix}",
                suggestion="使用 .yaml / .yml / .md 文件",
                project_root=project_root,
            )
            continue

        if not resolved.exists():
            _add_error(
                errors,
                file_path=supervisor_file,
                field=f"{field}.path",
                rule="path_exists",
                message=f"Worker 路径不存在: {resolved}",
                suggestion="检查 path 拼写或先创建对应 Worker 文件",
                project_root=project_root,
            )
            continue

        if not resolved.is_file():
            _add_error(
                errors,
                file_path=supervisor_file,
                field=f"{field}.path",
                rule="path_is_file",
                message=f"Worker 路径不是文件: {resolved}",
                suggestion="将 path 指向具体的 Worker YAML/Markdown 文件，而不是目录",
                project_root=project_root,
            )

    return referenced_workers


def _validate_worker_schema(
    config: dict[str, Any],
    worker_file: Path,
    errors: list[dict[str, str]],
    project_root: Path,
) -> None:
    schema = config.get("agent_function_schema")
    if not isinstance(schema, dict):
        _add_error(
            errors,
            file_path=worker_file,
            field="agent_function_schema",
            rule="required_dict_for_worker",
            message="Worker 缺少 agent_function_schema 或类型不是字典",
            suggestion="为 Worker 增加完整的 agent_function_schema（description/inputs/output）",
            project_root=project_root,
        )
        return

    description = schema.get("description")
    if not isinstance(description, str) or not description.strip():
        _add_error(
            errors,
            file_path=worker_file,
            field="agent_function_schema.description",
            rule="required_non_empty_string",
            message="agent_function_schema.description 必须是非空字符串",
            suggestion="补充 Worker 工具用途描述",
            project_root=project_root,
        )

    inputs = schema.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        _add_error(
            errors,
            file_path=worker_file,
            field="agent_function_schema.inputs",
            rule="required_non_empty_dict",
            message="agent_function_schema.inputs 必须是非空字典",
            suggestion="至少定义一个输入参数（如 query）",
            project_root=project_root,
        )
    else:
        for key, spec in inputs.items():
            if not isinstance(key, str) or not key.isidentifier():
                _add_error(
                    errors,
                    file_path=worker_file,
                    field=f"agent_function_schema.inputs.{key}",
                    rule="identifier_key",
                    message=f"输入参数名 {key!r} 不是合法 Python 标识符",
                    suggestion="将参数名改为合法标识符（如 query/file_path/module_name）",
                    project_root=project_root,
                )
                continue

            if not isinstance(spec, dict):
                _add_error(
                    errors,
                    file_path=worker_file,
                    field=f"agent_function_schema.inputs.{key}",
                    rule="type_dict",
                    message="输入参数配置必须是字典",
                    suggestion="设置 description 与 required 字段",
                    project_root=project_root,
                )
                continue

            param_desc = spec.get("description")
            if not isinstance(param_desc, str) or not param_desc.strip():
                _add_error(
                    errors,
                    file_path=worker_file,
                    field=f"agent_function_schema.inputs.{key}.description",
                    rule="required_non_empty_string",
                    message="输入参数 description 必须是非空字符串",
                    suggestion=f"为参数 {key} 补充 description",
                    project_root=project_root,
                )

            if "required" in spec and not isinstance(spec.get("required"), bool):
                _add_error(
                    errors,
                    file_path=worker_file,
                    field=f"agent_function_schema.inputs.{key}.required",
                    rule="type_bool",
                    message="required 字段必须是布尔值",
                    suggestion="将 required 设置为 true 或 false",
                    project_root=project_root,
                )

    output = schema.get("output")
    if not isinstance(output, dict):
        _add_error(
            errors,
            file_path=worker_file,
            field="agent_function_schema.output",
            rule="required_dict",
            message="agent_function_schema.output 必须是字典",
            suggestion="补充 output.description 字段",
            project_root=project_root,
        )
    else:
        out_desc = output.get("description")
        if not isinstance(out_desc, str) or not out_desc.strip():
            _add_error(
                errors,
                file_path=worker_file,
                field="agent_function_schema.output.description",
                rule="required_non_empty_string",
                message="agent_function_schema.output.description 必须是非空字符串",
                suggestion="补充输出描述",
                project_root=project_root,
            )


def _collect_agent_files(workflows_dir: Path) -> list[Path]:
    files: list[Path] = []
    if not workflows_dir.is_dir():
        return files
    files.extend(sorted(workflows_dir.glob("*.yaml")))
    files.extend(sorted(workflows_dir.glob("*.yml")))
    files.extend(sorted(workflows_dir.glob("*.md")))

    worker_dir = workflows_dir / "worker_agents"
    if worker_dir.is_dir():
        files.extend(sorted(worker_dir.glob("*.yaml")))
        files.extend(sorted(worker_dir.glob("*.yml")))
        files.extend(sorted(worker_dir.glob("*.md")))
    return files


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate generated AgentLoom application YAML files.")
    parser.add_argument(
        "--app-root",
        required=True,
        help="Application root path, e.g. applications/code_review",
    )
    return parser.parse_args()


def main() -> int:
    try:
        args = _parse_args()
        project_root = _discover_project_root(Path.cwd())
        if project_root is None:
            print(
                json.dumps(
                    {
                        "summary": {
                            "valid": False,
                            "error_count": 1,
                            "files_checked": 0,
                            "app_root": args.app_root,
                        },
                        "errors": [
                            {
                                "file": str(Path.cwd()),
                                "field": "project_root",
                                "rule": "project_root_discovery",
                                "message": "未找到 config/llm.yaml 或 config/system.yaml，无法定位项目根目录",
                                "suggestion": (
                                    "请在项目根目录下执行脚本；若这是新建 worktree 或干净 checkout，"
                                    "config/llm.yaml 可能因 .gitignore 未自动带过来，应从同机可信工作区复制"
                                    "或让用户提供本地配置，不要提交该文件"
                                ),
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            )
            return 2

        app_root = Path(args.app_root).expanduser()
        if not app_root.is_absolute():
            app_root = (project_root / app_root).resolve()
        else:
            app_root = app_root.resolve()

        errors: list[dict[str, str]] = []
        model_types, default_model_type, llm_path = _load_llm_model_index(project_root)

        if not app_root.exists() or not app_root.is_dir():
            _add_error(
                errors,
                file_path=app_root,
                field="app_root",
                rule="path_exists",
                message=f"Application 根目录不存在: {app_root}",
                suggestion="确认 --app-root 参数，如 applications/<app_name>",
                project_root=project_root,
            )
            payload = {
                "summary": {
                    "valid": False,
                    "error_count": len(errors),
                    "files_checked": 0,
                    "app_root": str(app_root),
                },
                "errors": errors,
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 1

        config_files_checked = 0
        app_system_config = app_root / "config" / "system.yaml"
        if app_system_config.is_file():
            config_files_checked += 1
            try:
                loaded_system = yaml.safe_load(app_system_config.read_text(encoding="utf-8")) or {}
                if not isinstance(loaded_system, dict):
                    raise ValueError("system.yaml 顶层必须是字典对象")
                _validate_system_config_map(loaded_system, app_system_config, errors, project_root)
            except Exception as exc:  # noqa: BLE001
                _add_error(
                    errors,
                    file_path=app_system_config,
                    field="yaml_parse",
                    rule="parse_success",
                    message=f"应用级 system.yaml 解析失败: {exc}",
                    suggestion="修复 YAML 语法或顶层结构后重试",
                    project_root=project_root,
                )

        workflows_dir = app_root / "workflows"
        if not workflows_dir.is_dir():
            _add_error(
                errors,
                file_path=workflows_dir,
                field="workflows",
                rule="path_exists",
                message=f"缺少 workflows 目录: {workflows_dir}",
                suggestion="先创建 workflows 目录并放置 Agent YAML",
                project_root=project_root,
            )
            payload = {
                "summary": {
                    "valid": False,
                    "error_count": len(errors),
                    "files_checked": 0,
                    "app_root": str(app_root),
                },
                "errors": errors,
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 1

        agent_files = _collect_agent_files(workflows_dir)
        parsed: dict[Path, dict[str, Any]] = {}
        referenced_workers: set[Path] = set()

        for file_path in agent_files:
            try:
                config = _load_agent_config(file_path)
            except Exception as exc:  # noqa: BLE001
                _add_error(
                    errors,
                    file_path=file_path,
                    field="yaml_parse",
                    rule="parse_success",
                    message=f"文件解析失败: {exc}",
                    suggestion="修复 YAML/Markdown 语法后重试",
                    project_root=project_root,
                )
                continue

            parsed[file_path.resolve()] = config
            _validate_common_rules(config, file_path, errors, project_root)
            _validate_runtime_options(config, file_path, errors, project_root)
            _validate_prompt_config(config, file_path, errors, project_root)
            _validate_overlay_config_types(config, file_path, errors, project_root)
            _validate_mcp_servers_config(config, file_path, errors, project_root)
            _validate_dynamic_tools(config, file_path, errors, project_root)
            _validate_skills_config(config, file_path, errors, project_root)
            _validate_model_type_reference(
                config,
                file_path,
                errors,
                project_root,
                model_types=model_types,
                default_model_type=default_model_type,
                llm_path=llm_path,
            )
            referenced_workers.update(
                _validate_supervisor_worker_agents(config, file_path, errors, project_root)
            )

        for file_path, config in parsed.items():
            path_obj = Path(file_path)
            if "worker_agents" in path_obj.parts or path_obj in referenced_workers:
                _validate_worker_schema(config, path_obj, errors, project_root)

        payload = {
            "summary": {
                "valid": len(errors) == 0,
                "error_count": len(errors),
                "files_checked": len(parsed),
                "config_files_checked": config_files_checked,
                "app_root": str(app_root),
            },
            "errors": errors,
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if not errors else 1
    except SystemExit as exc:
        # argparse exits with code 2 on invalid arguments.
        return int(exc.code) if isinstance(exc.code, int) else 2
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "summary": {"valid": False, "error_count": 1, "files_checked": 0, "app_root": ""},
                    "errors": [
                        {
                            "file": "",
                            "field": "runtime",
                            "rule": "unexpected_exception",
                            "message": f"脚本运行异常: {exc}",
                            "suggestion": "检查脚本和输入参数",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
