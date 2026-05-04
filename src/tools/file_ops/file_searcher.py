"""
Cross-file content search tool for AI Agents.

Provides a high-level, agent-friendly wrapper around ripgrep for searching
file contents by regex pattern across a directory tree.  Falls back to a
pure-Python implementation when ripgrep is not available.
"""

from src.lib.logging import get_logger
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

logger = get_logger(__name__)

# Locate ripgrep binary
_VENV_BIN = os.path.dirname(sys.executable)
_RG_PATH = shutil.which("rg", path=_VENV_BIN) or shutil.which("rg")

# Directories to skip in pure-Python fallback
_DEFAULT_SKIP = frozenset({
    "node_modules", ".git", ".svn", ".hg", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".tox", ".venv", "venv", "env", "target", "build", "dist",
    "out", "bin", "obj", ".vscode", ".idea", ".vs",
})


def search_files(
    directory: str,
    pattern: str,
    file_pattern: str = "",
    context_lines: int = 2,
    max_results: int = 50,
) -> str:
    """
    Search file contents in a directory tree by regex pattern.

    This is a high-level tool designed for AI Agents to quickly locate code
    patterns (function definitions, TODOs, references, etc.) across a project.

    Uses **ripgrep** when available (fast, respects ``.gitignore``), otherwise
    falls back to a pure-Python ``re`` walker.

    Args:
        directory: Root directory to search recursively.
        pattern: Regex pattern to match (Rust/PCRE syntax with ripgrep,
            Python ``re`` syntax for fallback).
        file_pattern: Optional glob to filter files (e.g. ``"*.py"``,
            ``"*.{js,ts}"``).  Empty string means all files.
        context_lines: Number of context lines to show around each match
            (default 2).
        max_results: Maximum number of matches to return (default 50).

    Returns:
        Formatted search results with file paths, line numbers, matched
        lines, and surrounding context.  Returns a human-readable message
        if no matches are found.

    Raises:
        ValueError: If *directory* or *pattern* is empty.
        FileNotFoundError: If *directory* does not exist.

    Examples:
        >>> search_files("./src", r"def\\s+connect")
        >>> search_files("./src", "TODO:", file_pattern="*.py")
        >>> search_files(".", r"class\\s+\\w+Agent", file_pattern="*.py", context_lines=3)
    """
    if not directory or not directory.strip():
        raise ValueError("directory is required and cannot be empty")
    if not pattern or not pattern.strip():
        raise ValueError("pattern is required and cannot be empty")

    dir_path = Path(directory)
    if not dir_path.exists():
        raise FileNotFoundError(f"Directory not found: {directory}")
    if not dir_path.is_dir():
        raise ValueError(f"Path is not a directory: {directory}")

    if _RG_PATH:
        return _search_with_ripgrep(
            directory, pattern, file_pattern, context_lines, max_results,
        )
    return _search_with_python(
        directory, pattern, file_pattern, context_lines, max_results,
    )


# ---------------------------------------------------------------------------
# ripgrep implementation
# ---------------------------------------------------------------------------

def _search_with_ripgrep(
    directory: str,
    pattern: str,
    file_pattern: str,
    context_lines: int,
    max_results: int,
) -> str:
    cmd = [
        _RG_PATH,
        "--line-number",
        "--no-heading",
        "--color", "never",
        "--max-count", str(max_results),
        "-C", str(context_lines),
    ]
    if file_pattern:
        cmd.extend(["-g", file_pattern])
    cmd.append(pattern)
    cmd.append(directory)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return f"Search timed out after 30 seconds for pattern: {pattern}"
    except Exception as exc:
        return f"ripgrep error: {exc}"

    if result.returncode == 1:
        return f"No matches found for pattern '{pattern}' in {directory}"
    if result.returncode not in (0, 1):
        stderr = result.stderr.strip()
        return f"ripgrep error (exit {result.returncode}): {stderr}"

    output = result.stdout.strip()
    if not output:
        return f"No matches found for pattern '{pattern}' in {directory}"

    # Count actual match lines (non-separator, non-context)
    match_count = sum(
        1 for line in output.splitlines()
        if line and not line.startswith("--") and ":" in line
    )

    header = f"Found {match_count} match(es) for '{pattern}'"
    if file_pattern:
        header += f" in {file_pattern} files"
    header += f" under {directory}:\n"

    return header + output


# ---------------------------------------------------------------------------
# Pure-Python fallback
# ---------------------------------------------------------------------------

def _search_with_python(
    directory: str,
    pattern: str,
    file_pattern: str,
    context_lines: int,
    max_results: int,
) -> str:
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"Invalid regex pattern: {exc}"

    import fnmatch

    results: List[str] = []
    match_count = 0

    for root, dirs, files in os.walk(directory):
        # Skip common non-user directories
        dirs[:] = [d for d in dirs if d not in _DEFAULT_SKIP]

        for fname in sorted(files):
            if file_pattern and not fnmatch.fnmatch(fname, file_pattern):
                continue

            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
            except (OSError, PermissionError):
                continue

            for i, line in enumerate(lines):
                if regex.search(line):
                    match_count += 1
                    if match_count > max_results:
                        break

                    # Collect context
                    start = max(0, i - context_lines)
                    end = min(len(lines), i + context_lines + 1)

                    rel_path = os.path.relpath(fpath, directory)
                    results.append(f"\n{rel_path}:{i + 1}:")
                    for j in range(start, end):
                        prefix = ">" if j == i else " "
                        results.append(f"  {prefix} {j + 1:4d} | {lines[j].rstrip()}")

            if match_count > max_results:
                break
        if match_count > max_results:
            break

    if not results:
        return f"No matches found for pattern '{pattern}' in {directory}"

    header = f"Found {match_count} match(es) for '{pattern}'"
    if file_pattern:
        header += f" in {file_pattern} files"
    header += f" under {directory}:"
    if match_count > max_results:
        header += f"\n(showing first {max_results} results)"

    return header + "\n".join(results)
