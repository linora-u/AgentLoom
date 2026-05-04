"""
AI Agent code editor tools.

Provides a complete set of code editing capabilities for AI Agents:

1. **code_search**  — Intelligent code search/location (read-only).
2. **code_replace** — Single-replacement with 12-level matching engine.
3. **code_edit**    — Batch editing via SEARCH/REPLACE blocks.

4. **write_whole_file** — Create or overwrite files with complete content.

5. **git_commit_files** — Stage and commit specific files.
6. **git_auto_commit**  — Auto-commit with generated commit messages.
7. **git_check_dirty**  — Check for uncommitted changes.

Note:
    ``delete_file`` has been moved to ``src.tools.file_ops.file_manager``.

Backward-compatible aliases:
- ``search_and_replace`` → delegates to ``code_edit``

All tools are designed for fully automated AI Agent use — no human
interaction or confirmation prompts required.
"""

# New tools
from .code_search_tool import code_search
from .code_replace_tool import code_replace
from .code_edit_tool import code_edit

# Existing tools
from .write_file_tool import write_whole_file
from .git_tool import git_commit_files, git_auto_commit, git_check_dirty

# Internal re-exports for advanced use
from ._match_engine import SearchReplaceError

# Backward-compatible alias — search_and_replace delegates to code_edit
search_and_replace = code_edit

__all__ = [
    # New tools
    "code_search",
    "code_replace",
    "code_edit",
    # Existing tools
    "write_whole_file",
    "git_commit_files",
    "git_auto_commit",
    "git_check_dirty",
    # Backward-compatible
    "search_and_replace",
    # Exceptions
    "SearchReplaceError",
]
