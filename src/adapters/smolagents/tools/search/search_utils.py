"""
Shared utilities for search tools (grep_search, glob_search).

Provides centralized exclude-path pattern generation from
``tool_access_control.path_validation`` configuration.  Both ripgrep-based
and Python-fallback search backends consume these patterns to ensure that
results from excluded directories are never returned to the LLM.

Architecture overview — two-layer exclude enforcement:

  Layer 1 (core guard): the non-configurable tool boundary blocks *direct*
      access to excluded paths after Hook transformations (for example,
      ``read_file("secrets/key.pem")``).

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


from agentloom.runtime.tool_governance.search import load_exclude_paths as _load_exclude_paths


def get_search_exclude_patterns(tool_name: str = "grep_search", root=None) -> List[str]:
    """Return ripgrep-compatible glob exclusion patterns.

    Format: ``["!**/secrets/**"]`` — the ``!**/`` prefix ensures the pattern
    works correctly with both ``rg`` content-search mode and ``rg --files``
    file-listing mode, even when the search target is an absolute path.

    This is the only format that reliably excludes directories across all
    ripgrep invocation modes (verified empirically; ``!secrets/**`` and
    ``!/secrets/**`` do NOT work with absolute search paths).
    """
    if root is None:
        return [f"!**/{d}/**" for d in _load_exclude_paths(tool_name)]
    from agentloom.runtime.tool_governance.search import search_excludes
    patterns = []
    for value in search_excludes(tool_name, root):
        if value == "*":
            patterns.append("!**")
        else:
            patterns.extend((f"!**/{value}", f"!**/{value}/**"))
    return patterns


def get_python_exclude_dirs(tool_name: str = "grep_search") -> frozenset:
    """Merge ``SKIP_DIRS`` with configured ``exclude_paths`` for Python fallback.

    Returns a frozenset of directory names to skip during ``os.walk()``.
    """
    extra = set(_load_exclude_paths(tool_name))
    return SKIP_DIRS | extra
