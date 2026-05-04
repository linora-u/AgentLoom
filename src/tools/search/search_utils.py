"""
Shared utilities for search tools (grep_search, glob_search).

Provides centralized exclude-path pattern generation from
``tool_access_control.path_validation`` configuration.  Both ripgrep-based
and Python-fallback search backends consume these patterns to ensure that
results from excluded directories are never returned to the LLM.

Architecture overview — two-layer exclude enforcement:

  Layer 1 (hook):  ``path_validators.validate_workspace_path`` blocks *direct*
      access to excluded paths (e.g. ``read_file("secrets/key.pem")``).

  Layer 2 (search-result filter):  This module provides patterns so that
      *directory-scanning* tools (grep, glob) silently omit results from
      excluded sub-directories (e.g. ``grep_search("password", path="src/")``
      won't return matches from ``src/secrets/``).
"""

import logging
from typing import List

logger = logging.getLogger(__name__)

# Directories unconditionally skipped by the Python-fallback search backends.
SKIP_DIRS = frozenset({
    ".git", ".svn", ".hg", ".bzr", ".jj",
    "node_modules", "__pycache__", ".venv", "venv",
})


def _load_exclude_paths(tool_name: str = "grep_search") -> List[str]:
    """Read ``exclude_paths`` from ``path_validation`` rules for *tool_name*.

    Uses the unified permissions library to get exclude_paths from
    all matching rules for the given tool.  Supports glob patterns
    and ``"*"`` (deny all).

    Returns plain directory names, e.g. ``["secrets", "build/dist"]``.
    """
    dirs: List[str] = []

    try:
        from src.lib.permissions.workspace import get_rule_exclude_paths
        for excl in get_rule_exclude_paths(tool_name):
            if excl.strip():
                dirs.append(excl.strip().rstrip("/"))
    except ImportError:
        pass

    return list(dict.fromkeys(dirs))  # deduplicate preserving order


def get_search_exclude_patterns(tool_name: str = "grep_search") -> List[str]:
    """Return ripgrep-compatible glob exclusion patterns.

    Format: ``["!**/secrets/**"]`` — the ``!**/`` prefix ensures the pattern
    works correctly with both ``rg`` content-search mode and ``rg --files``
    file-listing mode, even when the search target is an absolute path.

    This is the only format that reliably excludes directories across all
    ripgrep invocation modes (verified empirically; ``!secrets/**`` and
    ``!/secrets/**`` do NOT work with absolute search paths).
    """
    return [f"!**/{d}/**" for d in _load_exclude_paths(tool_name)]


def get_python_exclude_dirs(tool_name: str = "grep_search") -> frozenset:
    """Merge ``SKIP_DIRS`` with configured ``exclude_paths`` for Python fallback.

    Returns a frozenset of directory names to skip during ``os.walk()``.
    """
    extra = set(_load_exclude_paths(tool_name))
    return SKIP_DIRS | extra
