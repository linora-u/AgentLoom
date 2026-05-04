import os
from pathlib import Path

# ============================================================================
# Configuration
# ============================================================================
context_dir = "./temp"

# Max files to list in context (avoid token explosion on huge projects)
_MAX_FILES_IN_CONTEXT = 200


# ============================================================================
# Tool function
# ============================================================================
def get_module_context() -> str:
    """
    Get module context information, including project path, description,
    and file list.

    The project path is read from the ``AI_QA_PROJECT_PATH`` environment
    variable (set by ``ai_quality_analysis_demo.py``).

    Returns:
        str: Formatted string containing project path and file listing.
    """
    project_path = os.environ.get("AI_QA_PROJECT_PATH", "").strip()
    if not project_path:
        return (
            f"中间报告存放目录: {context_dir}\n"
            "警告: 未指定项目路径 (AI_QA_PROJECT_PATH)。"
            "请在启动时传入 project_path 参数。"
        )

    p = Path(project_path)
    if not p.is_dir():
        return f"错误: 项目路径不存在或不是目录: {project_path}"

    # Collect source files (common code extensions)
    code_exts = {
        ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp",
        ".h", ".hpp", ".go", ".rs", ".rb", ".cs", ".swift", ".kt",
        ".scala", ".lua", ".sh", ".bash", ".yaml", ".yml", ".toml",
        ".json", ".xml", ".sql", ".proto", ".cmake", ".makefile",
    }
    files: list[str] = []
    try:
        for fp in sorted(p.rglob("*")):
            if fp.is_file() and fp.suffix.lower() in code_exts:
                # Skip hidden dirs and common non-source dirs
                parts = fp.relative_to(p).parts
                if any(part.startswith(".") or part in (
                    "node_modules", "__pycache__", ".git", "venv", ".venv",
                    "build", "dist", "target",
                ) for part in parts):
                    continue
                files.append(str(fp.relative_to(p)))
                if len(files) >= _MAX_FILES_IN_CONTEXT:
                    break
    except Exception as exc:
        return f"扫描项目目录时出错: {exc}"

    truncated = f" (仅显示前 {_MAX_FILES_IN_CONTEXT} 个)" if len(files) >= _MAX_FILES_IN_CONTEXT else ""
    file_list = "\n".join(f"  - {f}" for f in files) if files else "  (未发现代码文件)"

    context = (
        f"## 项目分析上下文\n\n"
        f"**项目路径**: {project_path}\n"
        f"**代码文件数量**: {len(files)}{truncated}\n"
        f"**中间报告目录**: {context_dir}\n\n"
        f"### 代码文件列表\n{file_list}\n\n"
        f"### 说明\n"
        f"- 各阶段子 agent 生成的中间分析报告存放在 {context_dir} 目录中。\n"
        f"- 请使用 shell_tool 或 read_file_content 读取上述文件进行分析。\n"
        f"- 所有分析都应基于 **{project_path}** 目录下的代码。\n"
    )
    return context
