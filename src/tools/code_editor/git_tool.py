"""
Git editing tools for AI Agents.

Provides git commit, auto-commit, and dirty-check tools.
Designed for fully automated AI Agent use — no confirmation prompts.
"""

import logging
from pathlib import Path
from typing import List

from ._git_ops import (
    commit_files,
    find_git_repo,
    get_diff,
    get_dirty_files,
    get_head_info,
    is_dirty,
)

logger = logging.getLogger(__name__)


def git_commit_files(
    file_paths: List[str],
    message: str,
    repo_path: str = "",
) -> str:
    """
    Stage and commit specific files to git.

    This tool commits one or more files with the given message.
    If repo_path is not provided, it auto-discovers the git repository
    from the first file path.

    Args:
        file_paths: List of file paths to commit.
        message: Commit message describing the changes.
        repo_path: Git repository root path. Auto-detected if empty.

    Returns:
        A description of the result, e.g.
        "Committed 2 file(s): abc123def — Fix unit test imports".

    Raises:
        ValueError: If arguments are invalid or no git repo is found.
        subprocess.CalledProcessError: If git command fails.
    """
    if not file_paths:
        raise ValueError("file_paths cannot be empty")
    if not message or not message.strip():
        raise ValueError("commit message cannot be empty")

    # Auto-discover repo
    if not repo_path:
        repo_path = find_git_repo(file_paths[0])
        if not repo_path:
            raise ValueError(
                f"No git repository found for: {file_paths[0]}. "
                f"Make sure the file is inside a git repository."
            )

    sha, msg = commit_files(repo_path, file_paths, message)
    file_count = len(file_paths)
    logger.info(f"Committed {file_count} file(s): {sha} — {msg}")
    return f"Committed {file_count} file(s): {sha} — {msg}"


def git_auto_commit(
    file_paths: List[str],
    repo_path: str = "",
) -> str:
    """
    Auto-commit files with an automatically generated commit message.

    This tool stages and commits files, generating a concise commit
    message based on the diff content. Useful after code editing operations.

    Args:
        file_paths: List of file paths to commit.
        repo_path: Git repository root path. Auto-detected if empty.

    Returns:
        A description of the result, e.g.
        "Auto-committed 1 file(s): abc123def — Update code_edit_tool.py".

    Raises:
        ValueError: If arguments are invalid or no git repo is found.
    """
    if not file_paths:
        raise ValueError("file_paths cannot be empty")

    # Auto-discover repo
    if not repo_path:
        repo_path = find_git_repo(file_paths[0])
        if not repo_path:
            raise ValueError(
                f"No git repository found for: {file_paths[0]}. "
                f"Make sure the file is inside a git repository."
            )

    # Generate commit message from filenames
    basenames = [Path(f).name for f in file_paths]
    if len(basenames) == 1:
        auto_message = f"Update {basenames[0]}"
    elif len(basenames) <= 3:
        auto_message = f"Update {', '.join(basenames)}"
    else:
        auto_message = f"Update {len(basenames)} files"

    # Try to get diff for more context
    try:
        diff = get_diff(repo_path, file_paths)
        if diff:
            # Count additions and deletions
            additions = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
            deletions = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
            auto_message += f" (+{additions}/-{deletions})"
    except Exception:
        pass

    sha, msg = commit_files(repo_path, file_paths, auto_message)
    file_count = len(file_paths)
    logger.info(f"Auto-committed {file_count} file(s): {sha} — {msg}")
    return f"Auto-committed {file_count} file(s): {sha} — {msg}"


def git_check_dirty(
    file_path: str = "",
    repo_path: str = "",
) -> str:
    """
    Check git dirty (uncommitted changes) status.

    With file_path: checks if that specific file has uncommitted changes.
    Without file_path: returns a list of all dirty files in the repo.

    Args:
        file_path: Specific file to check. Empty string checks the entire repo.
        repo_path: Git repository root path. Auto-detected if empty.

    Returns:
        A description of the dirty status, e.g.
        "File src/main.py has uncommitted changes (modified)" or
        "3 dirty file(s): M src/a.py, A src/b.py, ? src/c.py" or
        "Working directory is clean".

    Raises:
        ValueError: If no git repo is found.
    """
    # Auto-discover repo
    target = file_path or repo_path or "."
    if not repo_path:
        repo_path = find_git_repo(target)
        if not repo_path:
            raise ValueError(
                f"No git repository found for: {target}. "
                f"Make sure the path is inside a git repository."
            )

    if file_path:
        # Check specific file
        dirty = is_dirty(repo_path, file_path)
        if dirty:
            return f"File {file_path} has uncommitted changes"
        else:
            return f"File {file_path} is clean (no uncommitted changes)"
    else:
        # Check entire repo
        dirty_files = get_dirty_files(repo_path)
        if not dirty_files:
            return "Working directory is clean"

        file_list = ", ".join(
            f"{f['status']} {f['path']}" for f in dirty_files[:20]
        )
        count = len(dirty_files)
        result = f"{count} dirty file(s): {file_list}"
        if count > 20:
            result += f" ... and {count - 20} more"
        return result
