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
ALLOWED_TOOL_CALL_TYPES = {"code_act", "tool_call"}
ALLOWED_EXECUTION_ENV_TYPES = {"local", "docker", "e2b", "wasm"}
ALLOWED_WORKER_EXTENSIONS = {".yaml", ".yml", ".md"}
ALLOWED_SKILL_LOAD_MODES = {"on-demand", "eager"}


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
                                "message": "未找到 config/llm.yaml，无法定位项目根目录",
                                "suggestion": "请在项目根目录下执行脚本，确保 config/llm.yaml 存在",
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
            _validate_dynamic_tools(config, file_path, errors, project_root)
            _validate_skills_config(config, file_path, errors, project_root)
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
