"""Public tool exports.

Only functions backed by ``src.tools.registry.ToolSpec`` are built-in tools.
Additional helper modules may still be imported by their direct module paths,
but ``resolve_tool_function()`` resolves registry entries only.
"""

from src.tools.context import loom_retrieve_context
from src.tools.file_ops import (
    append_markdown_sections,
    edit_file,
    get_file_outline,
    list_directory,
    read_file,
    write_file,
    write_markdown_file,
    write_markdown_file_raw,
)
from src.tools.search import (
    ast_grep_search_file,
    glob_search,
    grep_search,
    lsp_find_definition,
    lsp_find_references,
    lsp_get_document_symbols,
    lsp_get_workspace_symbols,
    lsp_hover,
)
from src.tools.shell import (
    check_background_task,
    kill_background_task,
    list_background_tasks,
    shell_tool,
)
from src.tools.self_learning import memory, session_scroll, session_search, skill_manage
from src.tools.skills import list_skills, load_skill
from src.tools.todo import todo_write

from .tool_meta import (  # noqa: E402
    DEFAULT_TOOLSETS,
    ToolSpec,
    get_tool_meta,
    get_tool_spec,
    list_tool_specs,
    list_toolsets,
    resolve_tool_function,
    resolve_toolsets,
)

__all__ = [
    "shell_tool",
    "check_background_task",
    "kill_background_task",
    "list_background_tasks",
    "read_file",
    "edit_file",
    "write_file",
    "list_directory",
    "grep_search",
    "glob_search",
    "loom_retrieve_context",
    "load_skill",
    "list_skills",
    "todo_write",
    "write_markdown_file",
    "write_markdown_file_raw",
    "append_markdown_sections",
    "get_file_outline",
    "ast_grep_search_file",
    "lsp_find_definition",
    "lsp_find_references",
    "lsp_get_document_symbols",
    "lsp_hover",
    "lsp_get_workspace_symbols",
    "session_search",
    "session_scroll",
    "memory",
    "skill_manage",
    "DEFAULT_TOOLSETS",
    "ToolSpec",
    "get_tool_meta",
    "get_tool_spec",
    "list_tool_specs",
    "list_toolsets",
    "resolve_tool_function",
    "resolve_toolsets",
]
