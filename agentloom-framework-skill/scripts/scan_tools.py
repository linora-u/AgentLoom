"""
AgentLoom Framework Skill — 辅助扫描工具

提供轻量扫描能力，用于审核前快速获取 Application 的结构化信息。
这些工具只做确定性的目录扫描和字段提取，不做分析判断。
"""

from __future__ import annotations

import ast
import copy
import re
from pathlib import Path
from typing import Any

import yaml


_AGENT_CONFIG_EXTS = {".yaml", ".yml", ".md"}


def scan_app_structure(app_path: str) -> str:
    """扫描 Application 目录结构，提取 YAML/Markdown 与工具相关关键字段。

    返回结构化的文本摘要，包含：
    - Supervisor 配置摘要（name、description 前 80 字、tools 列表、worker_agents 列表、model_type、execution_env、max_steps）
    - 每个 Worker 的配置摘要（name、agent_function_schema 的 inputs/output、tools 列表、execution_env、max_steps）
    - agent_tools/ 下的 Python 文件名、公开函数名与 docstring 摘要
    - 入口脚本路径（如有）
    - 动态能力发现（有效 tools.default、tools_mapping/legacy 映射线索）

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
        supervisor_configs = sorted(
            f
            for f in workflows_dir.iterdir()
            if f.suffix.lower() in _AGENT_CONFIG_EXTS and f.is_file()
        )
        for sup_cfg in supervisor_configs:
            sections.append(_extract_agent_summary(sup_cfg, role="Supervisor"))

        worker_dir = workflows_dir / "worker_agents"
        if worker_dir.is_dir():
            worker_configs = sorted(
                f
                for f in worker_dir.iterdir()
                if f.suffix.lower() in _AGENT_CONFIG_EXTS and f.is_file()
            )
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
    """Render workflow strings or list[str] as readable sequential text."""
    if isinstance(workflow, list):
        parts: list[str] = []
        for idx, item in enumerate(workflow, start=1):
            parts.append(f"## Workflow {idx}\n\n{item}")
        return "\n\n".join(parts)
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

    default_tools = effective.get("default_loaded_tools", []) if isinstance(effective, dict) else []

    if isinstance(default_tools, list):
        if default_tools:
            lines.append(f"- 有效 `default_loaded_tools` ({len(default_tools)} 项): {', '.join(str(t) for t in default_tools)}")
        else:
            lines.append("- 有效 `default_loaded_tools`: []")
        if default_source is not None:
            lines.append(f"- `default_loaded_tools` 来源层级: `{default_source}`（列表替换语义）")
    else:
        lines.append("- 有效 `default_loaded_tools`: (配置存在但类型不是 list，按不可用处理)")

    mapping_info = _summarize_tools_mapping(effective)
    lines.extend(mapping_info)

    lines.append("- 默认工具加载规则: 当 Agent `execution_env.type` 为 `docker`/`e2b` 时，默认工具列表会整体跳过。")

    return "\n".join(lines)


def _summarize_tools_mapping(effective: Any) -> list[str]:
    """输出 tools_mapping 与 legacy mapping 的摘要。"""
    if not isinstance(effective, dict):
        return ["- 映射信息: 未发现有效配置"]

    lines: list[str] = []
    tools_mapping = effective.get("tools_mapping", {})
    legacy_mapping = tools_mapping.get("mapping", {}) if isinstance(tools_mapping, dict) else {}

    claude_mapping = {}
    if isinstance(tools_mapping, dict):
        raw = tools_mapping.get("Claude", {})
        if isinstance(raw, dict):
            claude_mapping = raw

    if claude_mapping:
        pairs = ", ".join(f"{k}->{v}" for k, v in sorted(claude_mapping.items()))
        lines.append(f"- 有效 `tools_mapping.Claude` ({len(claude_mapping)} 项): {pairs}")
        if isinstance(legacy_mapping, dict) and legacy_mapping:
            lines.append("- 检测到 legacy `tools_mapping.mapping`，但 `tools_mapping.Claude` 已存在，legacy 将被忽略。")
        return lines

    if isinstance(legacy_mapping, dict) and legacy_mapping:
        pairs = ", ".join(f"{k}->{v}" for k, v in sorted(legacy_mapping.items()))
        lines.append("- `tools_mapping.Claude` 未配置或为空。")
        lines.append(f"- 检测到 legacy `tools_mapping.mapping` ({len(legacy_mapping)} 项): {pairs}")
        lines.append("- 运行时兼容行为: legacy `tools_mapping.mapping` 会作为 `Claude` 映射回退来源。")
        return lines

    lines.append("- 映射信息: 未发现 `tools_mapping.Claude` 或 legacy `tools_mapping.mapping`")
    return lines


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
    return "default_loaded_tools" in snapshot


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
    tool_call_type = data.get("tool_call_type", "(未指定)")
    max_steps = data.get("max_steps", "(默认)")
    planning_interval = data.get("planning_interval")
    prompt_cfg = data.get("prompt")
    skills_cfg = data.get("skills")
    default_loaded_tools_cfg = data.get("default_loaded_tools", None)

    execution_env_type = "(未指定)"
    execution_env = data.get("execution_env")
    if isinstance(execution_env, dict):
        execution_env_type = execution_env.get("type", "(未指定)")

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

    schema = data.get("agent_function_schema", {})
    schema_text = ""
    if schema and isinstance(schema, dict):
        inputs = schema.get("inputs", {})
        output = schema.get("output", {})
        input_names = list(inputs.keys()) if isinstance(inputs, dict) else []
        output_desc = output.get("description", "(未定义)") if isinstance(output, dict) else str(output)
        schema_text = (
            f"  - **inputs**: {', '.join(input_names) if input_names else '(无)'}\n"
            f"  - **output**: {output_desc}\n"
        )

    lines = [f"\n### {agent_file.name} ({role})\n"]
    lines.append(f"- **name**: {name}")
    lines.append(f"- **description**: {desc_preview}")
    lines.append(f"- **model_type**: {model_type}")
    lines.append(f"- **tool_call_type**: {tool_call_type}")
    lines.append(f"- **execution_env.type**: {execution_env_type}")
    if isinstance(execution_env_type, str) and execution_env_type.strip().lower() in {"docker", "e2b"}:
        lines.append("- **默认工具加载**: 跳过 `default_loaded_tools`（execution_env.type=remote）")

    lines.append(f"- **max_steps**: {max_steps}")

    if default_loaded_tools_cfg is not None:
        if isinstance(default_loaded_tools_cfg, list):
            value = ", ".join(str(item) for item in default_loaded_tools_cfg) if default_loaded_tools_cfg else "[]"
        else:
            value = str(default_loaded_tools_cfg)
        lines.append(f"- **default_loaded_tools 覆盖**: {value}")

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

    if schema_text:
        lines.append(f"- **agent_function_schema**:\n{schema_text}")

    if skills_cfg is not None:
        lines.append(f"- **skills**: {_format_skills_summary(skills_cfg)}")

    return "\n".join(lines)


def _format_skills_summary(skills_cfg: Any) -> str:
    def _format_item(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("path", item))
        return str(item)

    if isinstance(skills_cfg, dict) and "items" in skills_cfg:
        items = skills_cfg.get("items")
        if isinstance(items, list):
            skill_items = [_format_item(item) for item in items]
        elif items is None:
            skill_items = []
        else:
            skill_items = [_format_item(items)]

        suffix = []
        load_mode = skills_cfg.get("load-mode")
        if load_mode:
            suffix.append(f"load-mode={load_mode}")
        if skills_cfg.get("allow-scripts") is False:
            suffix.append("allow-scripts=false")
        if skills_cfg.get("allow-network") is False:
            suffix.append("allow-network=false")

        summary = ", ".join(skill_items) if skill_items else "(无)"
        if suffix:
            summary += f" ({', '.join(suffix)})"
        return summary

    if isinstance(skills_cfg, list):
        return ", ".join(_format_item(item) for item in skills_cfg)
    if isinstance(skills_cfg, dict):
        return _format_item(skills_cfg)
    return str(skills_cfg)


def _load_agent_config_from_file(agent_file: Path) -> tuple[dict[str, Any] | None, str | None]:
    """加载 Agent 配置文件，支持 .yaml/.yml/.md。"""
    try:
        with agent_file.open("r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return None, f"读取失败: {e}"

    suffix = agent_file.suffix.lower()
    try:
        if suffix in {".yaml", ".yml"}:
            data = yaml.safe_load(content)
        elif suffix == ".md":
            data, _ = _extract_yaml_from_markdown(content)
        else:
            return None, f"不支持的文件后缀: {suffix}"
    except Exception as e:
        return None, str(e)

    if not isinstance(data, dict):
        return None, "内容不是字典格式"
    return data, None


def _extract_yaml_from_markdown(content: str) -> tuple[dict[str, Any], str]:
    """从 Markdown 中提取 YAML 代码块，并将剩余文本作为 workflow。"""
    yaml_pattern = r"```yaml\s*\n(.*?)\n```"
    match = re.search(yaml_pattern, content, re.DOTALL)
    if not match:
        raise ValueError("Markdown 中未找到 YAML 代码块")

    yaml_content = match.group(1)
    parsed = yaml.safe_load(yaml_content)
    if not isinstance(parsed, dict):
        raise ValueError("Markdown YAML 代码块内容不是字典")

    workflow_content = re.sub(yaml_pattern, "", content, flags=re.DOTALL).strip()
    if workflow_content:
        parsed["workflow"] = workflow_content

    return parsed, workflow_content


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
