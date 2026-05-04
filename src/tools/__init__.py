"""Tools package for AI agents."""
from .search import (
    grep_search,
    glob_search,
    ast_grep_search_file,
    lsp_find_definition,
    lsp_find_references,
    lsp_get_document_symbols,
    lsp_hover,
    lsp_get_workspace_symbols,
)

from src.tools.code_editor import (
    code_search,
    code_replace,
    code_edit,
    search_and_replace,
    write_whole_file,
    git_commit_files,
    git_auto_commit,
    git_check_dirty,
)
from src.tools.git import (
    get_git_diff_content,
    git_grep_files,
    is_path_in_repo,
)
from src.tools.skills import load_skill, list_skills
from src.tools.shell import (
    shell_tool,
    check_background_task,
    kill_background_task,
    list_background_tasks,
)
from .todo import todo_write
from .file_ops import (
    # Core file tools (aligned with upstream)
    read_file,
    edit_file,
    write_file,
    # Kept tools
    get_file_outline,
    browse_directory,
    write_markdown_file,
    write_markdown_file_raw,
    append_markdown_sections,
    # file_manager
    delete_file,
    move_file,
    rename_file,
    copy_file,
    # file_searcher
    search_files,
)

# Re-export tool metadata utilities for convenient access.
from .tool_meta import resolve_tool_function, get_tool_meta, ToolMeta  # noqa: F401, E402

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
