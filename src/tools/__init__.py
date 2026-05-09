"""Tools package for AI agents."""
from src.tools.code_editor import (
    code_edit,
    code_replace,
    code_search,
    git_auto_commit,
    git_check_dirty,
    git_commit_files,
    search_and_replace,
    write_whole_file,
)
from src.tools.codex import codex
from src.tools.git import (
    get_git_diff_content,
    git_grep_files,
    is_path_in_repo,
)
from src.tools.shell import (
    check_background_task,
    kill_background_task,
    list_background_tasks,
    shell_tool,
)
from src.tools.skills import list_skills, load_skill

from .file_ops import (
    append_markdown_sections,
    browse_directory,
    copy_file,
    # file_manager
    delete_file,
    edit_file,
    # Kept tools
    get_file_outline,
    move_file,
    # Core file tools (aligned with upstream)
    read_file,
    rename_file,
    # file_searcher
    search_files,
    write_file,
    write_markdown_file,
    write_markdown_file_raw,
)
from .search import (
    ast_grep_search_file,
    glob_search,
    grep_search,
    lsp_find_definition,
    lsp_find_references,
    lsp_get_document_symbols,
    lsp_get_workspace_symbols,
    lsp_hover,
)
from .todo import todo_write

# Re-export tool metadata utilities for convenient access.
from .tool_meta import ToolMeta, get_tool_meta, resolve_tool_function  # noqa: F401, E402

__all__ = [
    # Search & navigation
    "grep_search",
    "glob_search",
    "ast_grep_search_file",
    "lsp_find_definition",
    "lsp_find_references",
    "lsp_get_document_symbols",
    "lsp_hover",
    "lsp_get_workspace_symbols",
    # Code editor
    "code_search",
    "code_replace",
    "code_edit",
    "search_and_replace",
    "write_whole_file",
    "git_commit_files",
    "git_auto_commit",
    "git_check_dirty",
    # Git
    "get_git_diff_content",
    "git_grep_files",
    "is_path_in_repo",
    # Skills
    "load_skill",
    "list_skills",
    # Shell
    "shell_tool",
    "check_background_task",
    "kill_background_task",
    "list_background_tasks",
    # Codex
    "codex",
    # File ops — core (aligned with upstream)
    "read_file",
    "edit_file",
    "write_file",
    # File ops — kept tools
    "get_file_outline",
    "browse_directory",
    "write_markdown_file",
    "write_markdown_file_raw",
    "append_markdown_sections",
    "delete_file",
    "move_file",
    "rename_file",
    "copy_file",
    "search_files",
    # Todo
    "todo_write",
    # Tool metadata
    "resolve_tool_function",
    "get_tool_meta",
    "ToolMeta",
]
