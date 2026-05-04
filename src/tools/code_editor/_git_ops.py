"""
Git operations for code editing.

Provides low-level git operations used by git_tool.py:
- Repository discovery
- Dirty file checking
- Commit operations
- Diff generation
- Cherry-pick assisted replacement

Inspired by Aider's repo.py. Uses subprocess for git commands —
no dependency on GitPython.
"""

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repository discovery
# ---------------------------------------------------------------------------

def find_git_repo(path: str) -> Optional[str]:
    """
    Find the git repository root for a given path.

    Walks up the directory tree looking for a .git directory.

    Args:
        path: A file or directory path inside the repo.

    Returns:
        Absolute path to the repo root, or None if not in a git repo.
    """
    current = Path(path).resolve()
    if current.is_file():
        current = current.parent

    while current != current.parent:
        if (current / ".git").exists():
            return str(current)
        current = current.parent

    # Check root
    if (current / ".git").exists():
        return str(current)

    return None


# ---------------------------------------------------------------------------
# Status / Dirty checking
# ---------------------------------------------------------------------------

def _run_git(args: List[str], cwd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    cmd = ["git"] + args
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    )


def is_dirty(repo_path: str, file_path: Optional[str] = None) -> bool:
    """
    Check if a file (or the entire repo) has uncommitted changes.

    Args:
        repo_path: Path to the git repository root.
        file_path: Specific file to check. None checks the entire repo.

    Returns:
        True if there are uncommitted changes.
    """
    args = ["status", "--porcelain"]
    if file_path:
        # Make path relative to repo root
        try:
            rel_path = str(Path(file_path).resolve().relative_to(Path(repo_path).resolve()))
            args.append(rel_path)
        except ValueError:
            args.append(file_path)

    result = _run_git(args, cwd=repo_path, check=False)
    return bool(result.stdout.strip())


def get_dirty_files(repo_path: str) -> List[Dict[str, str]]:
    """
    Get a list of all files with uncommitted changes.

    Args:
        repo_path: Path to the git repository root.

    Returns:
        List of dicts with 'status' and 'path' keys.
        Status codes: M=modified, A=added, D=deleted, ?=untracked, etc.
    """
    result = _run_git(["status", "--porcelain"], cwd=repo_path, check=False)
    files = []
    for line in result.stdout.strip().splitlines():
        if len(line) >= 4:
            status = line[:2].strip()
            filepath = line[3:]
            files.append({"status": status, "path": filepath})
    return files


# ---------------------------------------------------------------------------
# Commit operations
# ---------------------------------------------------------------------------

def commit_files(
    repo_path: str,
    file_paths: List[str],
    message: str,
) -> Tuple[str, str]:
    """
    Stage and commit specific files.

    Args:
        repo_path: Path to the git repository root.
        file_paths: List of file paths to stage and commit.
        message: Commit message.

    Returns:
        Tuple of (commit_sha, commit_message).

    Raises:
        subprocess.CalledProcessError: If git command fails.
    """
    repo = Path(repo_path).resolve()

    # Stage files
    for fpath in file_paths:
        try:
            rel_path = str(Path(fpath).resolve().relative_to(repo))
        except ValueError:
            rel_path = fpath
        _run_git(["add", rel_path], cwd=repo_path)

    # Commit
    _run_git(["commit", "-m", message, "--no-verify"], cwd=repo_path)

    # Get the commit SHA
    result = _run_git(["rev-parse", "HEAD"], cwd=repo_path)
    sha = result.stdout.strip()[:12]

    return sha, message


# ---------------------------------------------------------------------------
# HEAD info
# ---------------------------------------------------------------------------

def get_head_info(repo_path: str) -> Dict[str, str]:
    """
    Get HEAD commit information.

    Args:
        repo_path: Path to the git repository root.

    Returns:
        Dict with 'sha', 'message', 'author', 'date' keys.
    """
    fmt = "%H%n%s%n%an%n%ai"
    result = _run_git(
        ["log", "-1", f"--format={fmt}"],
        cwd=repo_path,
        check=False,
    )
    lines = result.stdout.strip().splitlines()
    if len(lines) >= 4:
        return {
            "sha": lines[0][:12],
            "message": lines[1],
            "author": lines[2],
            "date": lines[3],
        }
    return {"sha": "", "message": "", "author": "", "date": ""}


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

def get_diff(
    repo_path: str,
    file_paths: Optional[List[str]] = None,
    staged: bool = False,
) -> str:
    """
    Get the diff for specified files.

    Args:
        repo_path: Path to the git repository root.
        file_paths: Specific files to diff. None for all changes.
        staged: If True, show staged changes. If False, show working dir changes.

    Returns:
        Unified diff string.
    """
    args = ["diff"]
    if staged:
        args.append("--cached")

    if file_paths:
        args.append("--")
        repo = Path(repo_path).resolve()
        for fpath in file_paths:
            try:
                rel = str(Path(fpath).resolve().relative_to(repo))
            except ValueError:
                rel = fpath
            args.append(rel)

    result = _run_git(args, cwd=repo_path, check=False)
    return result.stdout


# ---------------------------------------------------------------------------
# Cherry-pick assisted replacement (reference: aider search_replace.py)
# ---------------------------------------------------------------------------

def git_cherry_pick_apply(
    original: str,
    search: str,
    replace: str,
) -> Optional[str]:
    """
    Use git cherry-pick to apply a search/replace operation.

    Creates a temporary git repo with:
    1. Base commit containing the search text
    2. Feature commit with the replacement
    3. Cherry-picks the feature onto the original content

    This leverages git's merge machinery to handle whitespace and
    indentation differences.

    Args:
        original: The complete original file content.
        search: The text to search for.
        replace: The replacement text.

    Returns:
        New content with replacement applied, or None if cherry-pick fails.
    """
    if not search.strip() or not original:
        return None

    tmpdir = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="code_edit_cp_")
        target = os.path.join(tmpdir, "target.txt")

        # Init repo
        _run_git(["init", "-q"], cwd=tmpdir)
        _run_git(["config", "user.email", "agent@local"], cwd=tmpdir)
        _run_git(["config", "user.name", "Agent"], cwd=tmpdir)

        # Commit 1: original with search text as-is
        Path(target).write_text(search, encoding="utf-8")
        _run_git(["add", "."], cwd=tmpdir)
        _run_git(["commit", "-q", "-m", "base", "--no-verify"], cwd=tmpdir)

        # Commit 2: replace text
        Path(target).write_text(replace, encoding="utf-8")
        _run_git(["add", "."], cwd=tmpdir)
        _run_git(["commit", "-q", "-m", "edit", "--no-verify"], cwd=tmpdir)
        edit_sha = _run_git(["rev-parse", "HEAD"], cwd=tmpdir).stdout.strip()

        # Reset to base, write the actual original content
        _run_git(["checkout", "-q", "HEAD~1"], cwd=tmpdir)
        Path(target).write_text(original, encoding="utf-8")
        _run_git(["add", "."], cwd=tmpdir)
        _run_git(["commit", "-q", "-m", "original", "--no-verify"], cwd=tmpdir)

        # Cherry-pick the edit
        result = _run_git(
            ["cherry-pick", "--no-commit", edit_sha],
            cwd=tmpdir,
            check=False,
        )
        if result.returncode != 0:
            # Cherry-pick failed (conflict)
            _run_git(["cherry-pick", "--abort"], cwd=tmpdir, check=False)
            return None

        return Path(target).read_text(encoding="utf-8")

    except Exception as e:
        logger.debug(f"Cherry-pick apply failed: {e}")
        return None
    finally:
        if tmpdir:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
