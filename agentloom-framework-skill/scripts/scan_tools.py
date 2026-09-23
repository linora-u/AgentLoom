"""
AgentLoom Framework Skill — 辅助扫描工具

提供轻量扫描能力，用于审核前快速获取 Application 的结构化信息。
这些工具只做确定性的目录扫描和字段提取，不做分析判断。
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any

import yaml
from agentloom.application.definition import (
    definition_error,
    discover_application_definition_files,
    load_agent_definition,
)


def scan_app_structure(app_path: str) -> str:
    """扫描 Application 目录结构，提取 YAML/Markdown 与工具相关关键字段。

    返回结构化的文本摘要，包含：
    - Supervisor 配置摘要（name、description 前 80 字、tools 列表、worker_agents 列表、agent_runtime、model_type、max_steps）
    - 每个 Worker 的配置摘要（name、input_schema、output_schema、tools 列表、agent_runtime、model_type、max_steps）
    - agent_tools/ 下的 Python 文件名、公开函数名与 docstring 摘要
    - 入口脚本路径（如有）
    - 动态能力发现（有效 tools 与 toolsets）

    不做任何分析判断，只提取事实。

    Args:
        app_path: Application 路径，如 "applications/<app_name>"。
                  支持绝对路径或相对于当前工作目录的相对路径。

    Returns:
        结构化的文本摘要（Markdown 格式）。
    """
    normalized = app_path.strip() if isinstance(app_path, str) else ""
    if not normalized:
        return (
            "❌ Application 路径为空\n\n"
            "💡 请提供应用路径，例如：applications/<app_name> 或绝对路径"
        )

    root_error = _validate_root_precondition_for_relative_path(normalized)
    if root_error:
        return root_error

    app_dir = _resolve_app_path(normalized)
    if not app_dir.is_dir():
        return (
            f"❌ 路径不存在: {app_dir}\n\n"
            f"💡 请确保：\n"
            f"   1. 已切换到能解析该相对路径的目录\n"
            f"   2. 或传入绝对路径（如 /path/to/project/applications/my_app）\n"
            f"   当前工作目录: {Path.cwd()}"
        )

    sections: list[str] = [f"# Application 结构扫描: {app_dir.name}\n"]

    workflows_dir = app_dir / "workflows"
    if workflows_dir.is_dir():
        definitions = discover_application_definition_files(workflows_dir)
        supervisor_configs = [item.path for item in definitions if item.role == "supervisor"]
        worker_configs = [item.path for item in definitions if item.role == "worker"]
        for sup_cfg in supervisor_configs:
            sections.append(_extract_agent_summary(sup_cfg, role="Supervisor"))

        if worker_configs:
            sections.append(f"\n## Worker Agents ({len(worker_configs)} 个)\n")
            for worker_cfg in worker_configs:
                sections.append(_extract_agent_summary(worker_cfg, role="Worker"))
    else:
        sections.append("⚠️ 未找到 workflows/ 目录\n")

    tools_dir = app_dir / "agent_tools"
    if tools_dir.is_dir():
        py_files = sorted(f for f in tools_dir.iterdir() if f.suffix == ".py")
        sections.append(f"\n## 自定义 Tools ({len(py_files)} 个文件)\n")
        for py_file in py_files:
            capabilities = _extract_python_tool_capabilities(py_file)
            if capabilities:
                formatted = []
                for func_name, doc in capabilities:
                    line = func_name
                    if doc:
                        line += f" — {doc}"
                    formatted.append(line)
                cap_text = "; ".join(formatted)
            else:
                cap_text = "(无顶层函数)"
            sections.append(f"- **{py_file.name}**: {cap_text}")
    else:
        sections.append("\n## 自定义 Tools\n\n无 agent_tools/ 目录\n")

    entry_scripts = sorted(
        f
        for f in app_dir.iterdir()
        if f.suffix == ".py" and f.is_file() and not f.name.startswith("__")
    )
    if entry_scripts:
        sections.append("\n## 入口脚本\n")
        for entry in entry_scripts:
            sections.append(f"- {entry.name}")

    sections.append(_discover_tool_capabilities(app_dir))

    return "\n".join(sections)


def extract_workflow_text(yaml_path: str) -> str:
    """从单个 YAML/Markdown 文件中提取 workflow 字段文本。

    Args:
        yaml_path: Agent 配置文件路径。支持 .yaml/.yml/.md。

    Returns:
        workflow 字段文本。如果不存在或解析失败则返回提示信息。
    """
    normalized = yaml_path.strip() if isinstance(yaml_path, str) else ""
    if not normalized:
        return "❌ 文件路径为空"

    root_error = _validate_root_precondition_for_relative_path(normalized)
    if root_error:
        return root_error

    fpath = _resolve_app_path(normalized)
    if not fpath.is_file():
        return (
            f"❌ 文件不存在: {fpath}\n\n"
            f"💡 请确保：\n"
            f"   1. 已切换到能解析该相对路径的目录\n"
            f"   2. 或传入绝对路径\n"
            f"   当前工作目录: {Path.cwd()}"
        )

    data, err = _load_agent_config_from_file(fpath)
    if err:
        return f"❌ Agent 配置解析失败: {err}"
    if not isinstance(data, dict):
        return "❌ YAML 内容不是字典格式"

    name = data.get("name", "(未命名)")
    workflow = data.get("workflow")
    if workflow is None:
        return f"⚠️ {name}: 未找到 workflow 字段"

    return f"# {name} — workflow 全文\n\n{_render_workflow_text(workflow)}"


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------


def _validate_root_precondition_for_relative_path(path_str: str) -> str | None:
    """对相对路径执行 AgentLoom 根目录前置校验。"""
    configured = Path(path_str)
    if configured.is_absolute():
        return None

    llm_yaml = Path.cwd() / "config" / "llm.yaml"
    if llm_yaml.is_file():
        return None

    return (
        "❌ 根目录前置条件不满足\n\n"
        "要求：先进入 AgentLoom 根目录再执行检测/更新。\n"
        "根目录判定条件：存在 `config/llm.yaml`。\n"
        f"当前工作目录: {Path.cwd()}\n"
        f"检查结果: 未找到 {llm_yaml}\n\n"
        "若当前目录是新建 worktree 或干净 checkout，`config/llm.yaml` 可能因 `.gitignore` "
        "不会自动带过来。请从同机可信 AgentLoom 工作区复制，或让用户提供本地配置；"
        "不要提交该文件，也不要凭空生成模型配置。\n\n"
        "💡 请先执行：\n"
        "   cd /path/to/AgentLoom\n"
        "   pwd\n"
        "   ls config/llm.yaml\n"
        "   git check-ignore -v config/llm.yaml || true"
    )


def _resolve_app_path(path_str: str) -> Path:
    """将路径解析为绝对路径。"""
    path_obj = Path(path_str)
    if path_obj.is_absolute():
        return path_obj
    return Path.cwd() / path_obj


def _render_workflow_text(workflow: Any) -> str:
    """Render the stored value without inventing list execution semantics."""
    return str(workflow)


def _discover_tool_capabilities(app_dir: Path) -> str:
    """动态发现工具能力线索，避免依赖硬编码工具清单。"""
    lines = ["\n## 工具能力发现（动态）\n"]

    yaml_candidates = _find_system_yaml_candidates(app_dir)
    existing_configs = [cfg for cfg in yaml_candidates if cfg.is_file()]

    if not existing_configs:
        lines.append("- 未发现 `config/system.yaml`，默认工具能力无法直接确认（按推断处理）")
        return "\n".join(lines)

    overlay_chain = list(reversed(existing_configs))
    lines.append("- 配置覆盖链（低优先级 -> 高优先级）：")
    for idx, cfg in enumerate(overlay_chain, start=1):
        lines.append(f"  {idx}. `{cfg}`")

    effective: dict[str, Any] = {}
    default_source: Path | None = None
    for cfg in overlay_chain:
        snapshot = _load_yaml_dict(cfg)
        if not snapshot:
            continue
        effective = _deep_merge_dict(effective, snapshot)
        if _has_tools_default(snapshot):
            default_source = cfg

    default_toolsets = effective.get("default_toolsets", []) if isinstance(effective, dict) else []

    if isinstance(default_toolsets, list):
        if default_toolsets:
            lines.append(f"- 有效 `default_toolsets` ({len(default_toolsets)} 项): {', '.join(str(t) for t in default_toolsets)}")
        else:
            lines.append("- 有效 `default_toolsets`: []")
        if default_source is not None:
            lines.append(f"- `default_toolsets` 来源层级: `{default_source}`（列表替换语义）")
    else:
        lines.append("- 有效 `default_toolsets`: (配置存在但类型不是 list，按不可用处理)")

    lines.append("- Agent 只通过模型原生结构化 tool call 调用已注册工具。")

    return "\n".join(lines)


def _find_system_yaml_candidates(app_dir: Path) -> list[Path]:
    """按就近原则返回可能的 system.yaml 路径。"""
    candidates = [app_dir / "config" / "system.yaml"]
    for parent in app_dir.parents:
        candidates.append(parent / "config" / "system.yaml")

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate not in seen:
            unique.append(candidate)
            seen.add(candidate)
    return unique


def _has_tools_default(snapshot: dict[str, Any]) -> bool:
    return "default_toolsets" in snapshot


def _deep_merge_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """实现与配置文档一致的合并语义：dict 深度合并，列表/标量整体替换。"""
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge_dict(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_yaml_dict(path: Path) -> dict[str, Any]:
    """安全读取 YAML 字典，失败时返回空字典。"""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _extract_agent_summary(agent_file: Path, role: str = "Agent") -> str:
    """从 Agent 配置文件中提取关键字段生成摘要。"""
    data, err = _load_agent_config_from_file(agent_file)
    if err:
        return f"\n### {agent_file.name} ({role})\n\n❌ 解析失败: {err}\n"
    if not isinstance(data, dict):
        return f"\n### {agent_file.name} ({role})\n\n❌ 内容不是字典格式\n"

    name = data.get("name", "(未命名)")
    desc = data.get("description") or ""
    desc_preview = desc[:80].replace("\n", " ").strip() + ("..." if len(desc) > 80 else "")

    model_type = data.get("model_type", "(未指定)")
    agent_runtime = data.get("agent_runtime", "(未指定)")
    max_steps = data.get("max_steps", "(默认)")
    planning_interval = data.get("planning_interval")
    prompt_cfg = data.get("prompt")
    skills_cfg = data.get("skills")
    toolsets_cfg = data.get("toolsets", None)


    tools = data.get("tools", [])
    if isinstance(tools, list):
        tool_names = [
            entry.get("name", "(未命名)") if isinstance(entry, dict) else str(entry)
            for entry in tools
        ]
    else:
        tool_names = []

    worker_agents = data.get("worker_agents", [])
    if isinstance(worker_agents, list):
        worker_paths = [
            worker.get("path", str(worker)) if isinstance(worker, dict) else str(worker)
            for worker in worker_agents
        ]
    else:
        worker_paths = []

    input_schema_text = _format_json_schema_summary(data.get("input_schema"))
    output_schema_text = _format_json_schema_summary(data.get("output_schema"))

    lines = [f"\n### {agent_file.name} ({role})\n"]
    lines.append(f"- **name**: {name}")
    lines.append(f"- **description**: {desc_preview}")
    lines.append(f"- **model_type**: {model_type}")
    lines.append(f"- **agent_runtime**: {agent_runtime}")

    lines.append(f"- **max_steps**: {max_steps}")

    if toolsets_cfg is not None:
        if isinstance(toolsets_cfg, list):
            value = ", ".join(str(item) for item in toolsets_cfg) if toolsets_cfg else "[]"
        else:
            value = str(toolsets_cfg)
        lines.append(f"- **toolsets 覆盖**: {value}")

    if planning_interval is not None:
        lines.append(f"- **planning_interval**: {planning_interval}")

    if prompt_cfg is not None:
        if isinstance(prompt_cfg, dict):
            prompt_path = prompt_cfg.get("path", str(prompt_cfg))
        else:
            prompt_path = str(prompt_cfg)
        lines.append(f"- **prompt**: {prompt_path}")

    if tool_names:
        lines.append(f"- **tools**: {', '.join(tool_names)}")
    else:
        lines.append("- **tools**: (无)")

    if worker_paths:
        lines.append(f"- **worker_agents**: {', '.join(worker_paths)}")

    if input_schema_text:
        lines.append(f"- **input_schema**:\n{input_schema_text}")

    if output_schema_text:
        lines.append(f"- **output_schema**:\n{output_schema_text}")

    if skills_cfg is not None:
        lines.append(f"- **skills**: {_format_skills_summary(skills_cfg)}")

    return "\n".join(lines)


def _format_json_schema_summary(schema: Any) -> str:
    """Summarize a JSON Schema without interpreting or validating it."""
    if schema is None:
        return ""
    if not isinstance(schema, dict):
        return f"  - **value**: {schema}"

    lines = [f"  - **type**: {schema.get('type', '(未指定)')}"]
    description = schema.get("description")
    if description:
        lines.append(f"  - **description**: {description}")

    properties = schema.get("properties")
    if isinstance(properties, dict):
        property_names = list(properties)
        lines.append(
            "  - **properties**: "
            + (", ".join(property_names) if property_names else "(无)")
        )
        property_descriptions = []
        for name, property_schema in properties.items():
            if not isinstance(property_schema, dict):
                continue
            property_description = property_schema.get("description")
            if property_description:
                property_descriptions.append(f"{name}={property_description}")
        if property_descriptions:
            lines.append(
                "  - **property descriptions**: "
                + "; ".join(property_descriptions)
            )

    required = schema.get("required")
    if isinstance(required, list):
        lines.append(
            "  - **required**: "
            + (", ".join(str(name) for name in required) if required else "(无)")
        )
    return "\n".join(lines)


def _format_skills_summary(skills_cfg: Any) -> str:
    if not isinstance(skills_cfg, dict) or not isinstance(skills_cfg.get("paths"), list):
        return "(无效配置)"
    paths = [str(path) for path in skills_cfg["paths"]]
    return ", ".join(paths) if paths else "(仅约定目录)"


def _load_agent_config_from_file(agent_file: Path) -> tuple[dict[str, Any] | None, str | None]:
    """复用共享定义解析器；结构扫描不构造模型或执行工具。"""
    try:
        return load_agent_definition(agent_file), None
    except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as exc:
        return None, definition_error(exc)


def _extract_python_tool_capabilities(py_file: Path) -> list[tuple[str, str]]:
    """提取 Python 工具公开函数及 docstring 摘要。"""
    try:
        source = py_file.read_text(encoding="utf-8")
    except Exception:
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # 语法错误时做退化提取，尽量保留函数名信息。
        return [(name, "") for name in _extract_python_functions(py_file)]

    results: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        func_name = node.name
        if func_name.startswith("_"):
            continue

        doc = ast.get_docstring(node) or ""
        doc_summary = _summarize_docstring(doc)
        results.append((f"{func_name}()", doc_summary))

    return results


def _summarize_docstring(doc: str, max_len: int = 60) -> str:
    """取 docstring 首行并截断。"""
    if not doc:
        return ""

    first_line = doc.strip().splitlines()[0].strip()
    if len(first_line) <= max_len:
        return first_line
    return first_line[:max_len].rstrip() + "..."


def _extract_python_functions(py_file: Path) -> list[str]:
    """从 Python 文件中退化提取顶层公开函数名（仅函数名）。"""
    functions: list[str] = []
    try:
        with py_file.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.startswith("def "):
                    continue
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "(" in stripped:
                    func_name = stripped[4:stripped.index("(")].strip()
                    if not func_name.startswith("_"):
                        functions.append(func_name + "()")
    except Exception:
        pass
    return functions
