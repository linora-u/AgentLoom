"""
GlobTool.

Features:
- Prefers ripgrep ``--files --glob`` for speed and .gitignore awareness.
- Falls back to Python ``glob.glob`` + pathspec .gitignore parsing.
- Results sorted by modification time (newest first) by default.
- Directory existence validation (raises FileNotFoundError).
- Returns only files (not directories).
- Truncation metadata in output footer.
"""

import glob as _glob_mod
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from src.lib.logging import get_logger
from src.tools.search.search_utils import (
    get_search_exclude_patterns,
    get_python_exclude_dirs,
)


logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# ripgrep discovery (shared logic with grep_tool)
# ---------------------------------------------------------------------------
_VENV_BIN = os.path.dirname(sys.executable)
_RG_PATH: Optional[str] = shutil.which("rg", path=_VENV_BIN) or shutil.which("rg")

_DEFAULT_MAX_RESULTS = 200
_TIMEOUT_SECONDS = 30


def _filter_excluded_paths(files: List[str]) -> List[str]:
    """Remove results whose path components match configured exclude directories.

    This is Layer 2 exclude enforcement: even when the search root is allowed,
    results from excluded sub-directories are hidden from the LLM.
    """
    skip_dirs = get_python_exclude_dirs(tool_name="glob_search")
    filtered = []
    for rel in files:
        parts = Path(rel).parts
        if any(part in skip_dirs for part in parts):
            continue
        filtered.append(rel)
    return filtered


# =========================================================================
# Public API
# =========================================================================

def glob_search(
    pattern: str,
    path: str = ".",
    max_results: int = _DEFAULT_MAX_RESULTS,
    sort_by: str = "mtime",
) -> str:
    """Find files by name/glob pattern (powered by ripgrep --files).

    Fast, recursive, respects .gitignore by default.
    Results sorted by modification time (newest first) or alphabetically.
    Returns relative file paths only (no directories).

    Examples:
        glob_search("**/*.py")
        glob_search("**/*.yaml", path="config/")
        glob_search("*.ts", sort_by="name")
        glob_search("**/*.go", max_results=50)

    Args:
        pattern: Glob pattern to match (e.g. ``"**/*.py"``, ``"src/**/*.ts"``).
        path: Root directory to search in (default: current directory).
        max_results: Maximum number of file paths to return (0 = unlimited, default: 200).
        sort_by: Sort order — ``"mtime"`` (modification time, newest first, default) or ``"name"`` (alphabetical).

    Returns:
        Newline-separated list of relative file paths with a metadata footer.

    Raises:
        ValueError: If *pattern* is empty or *sort_by* is invalid.
        FileNotFoundError: If *path* does not exist.
    """
    # ---- validation ------------------------------------------------------
    if not pattern:
        raise ValueError("pattern is required")
    if sort_by not in ("mtime", "name"):
        raise ValueError(f"sort_by must be 'mtime' or 'name', got '{sort_by}'")

    search_dir = Path(path).resolve()
    if not search_dir.exists():
        raise FileNotFoundError(f"Directory does not exist: {path}")
    if not search_dir.is_dir():
        raise ValueError(f"Path is not a directory: {path}")

    start_time = time.monotonic()

    # ---- dispatch --------------------------------------------------------
    if _RG_PATH and sort_by == "mtime":
        files = _glob_with_ripgrep(pattern, search_dir)
    else:
        files = _glob_with_python(pattern, search_dir, sort_by)

    # If ripgrep returns empty but Python might not, try Python as fallback
    if not files and _RG_PATH:
        files = _glob_with_python(pattern, search_dir, sort_by)

    # Post-filter: remove results from excluded directories.
    # This is the Layer 2 exclude enforcement for search tools.
    files = _filter_excluded_paths(files)

    duration_ms = int((time.monotonic() - start_time) * 1000)
    return _format_output(files, max_results, sort_by, duration_ms)


# =========================================================================
# ripgrep backend
# =========================================================================

def _glob_with_ripgrep(pattern: str, search_dir: Path) -> List[str]:
    """Use ``rg --files --glob`` for fast, gitignore-aware file listing."""
    args = [
        _RG_PATH,
        "--files",
        "--glob", pattern,
        "--sort=modified",
    ]

    # Inject configured exclude patterns from tool_access_control.
    # Uses !**/dir/** format — the only format that works reliably with
    # rg --files and absolute search paths.
    for excl_glob in get_search_exclude_patterns():
        args.extend(["--glob", excl_glob])

    args.append(str(search_dir))

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ripgrep glob timed out after %ds", _TIMEOUT_SECONDS)
        return []
    except OSError as exc:
        logger.error("Failed to run ripgrep: %s", exc)
        return []

    if result.returncode not in (0, 1):
        logger.debug("ripgrep exit %d: %s", result.returncode, result.stderr.strip())
        return []

    files = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        rel = _to_relative(line, search_dir)
        files.append(rel)

    return files


# =========================================================================
# Python fallback
# =========================================================================

def _glob_with_python(pattern: str, search_dir: Path, sort_by: str) -> List[str]:
    """Pure-Python glob fallback with optional .gitignore filtering."""
    raw = _glob_mod.glob(pattern, root_dir=str(search_dir), recursive=True)

    # Filter to files only (exclude filtering done by _filter_excluded_paths later)
    files: List[str] = []
    for rel in raw:
        full = search_dir / rel
        if full.is_file():
            files.append(rel)

    # Try .gitignore filtering via pathspec
    files = _filter_gitignore(files, search_dir)

    # Sort
    if sort_by == "mtime":
        files.sort(
            key=lambda f: _safe_mtime(search_dir / f),
            reverse=True,  # newest first
        )
    else:
        files.sort()

    return files


def _filter_gitignore(files: List[str], search_dir: Path) -> List[str]:
    """Filter out files matching .gitignore patterns.  No-op if pathspec unavailable."""
    gitignore_path = search_dir / ".gitignore"
    if not gitignore_path.exists():
        return files
    try:
        import pathspec
        with open(gitignore_path, "r", encoding="utf-8", errors="replace") as fh:
            spec = pathspec.PathSpec.from_lines("gitwildmatch", fh)
        return [f for f in files if not spec.match_file(f)]
    except ImportError:
        logger.debug("pathspec not available; skipping .gitignore filtering")
        return files
    except Exception as exc:
        logger.debug("gitignore filtering failed: %s", exc)
        return files


def _safe_mtime(p: Path) -> float:
    """Get modification time, returning 0.0 on error."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# =========================================================================
# Output formatting
# =========================================================================

def _format_output(
    files: List[str],
    max_results: int,
    sort_by: str,
    duration_ms: int,
) -> str:
    """Format file list with metadata footer."""
    total = len(files)
    truncated = False

    if max_results > 0 and total > max_results:
        files = files[:max_results]
        truncated = True

    if not files:
        return "No files found."

    lines = list(files)

    # Footer
    lines.append("")
    meta_parts = [f"{total} files found"]
    if truncated:
        meta_parts.append(f"showing first {max_results}")
        meta_parts.append("truncated: true")
    meta_parts.append(f"{duration_ms}ms")
    lines.append(f"[{', '.join(meta_parts)}]")

    return "\n".join(lines)


# =========================================================================
# Helpers
# =========================================================================

def _to_relative(abs_path: str, base: Path) -> str:
    """Convert an absolute path to one relative to *base*."""
    try:
        return str(Path(abs_path).relative_to(base))
    except ValueError:
        return abs_path
