"""
Workspace boundary management.

Provides the canonical source of allowed directories for all tools.
Path access rules are read from ``tool_access_control.path_validation``
entries — each rule specifies ``include_paths`` / ``exclude_paths`` for
the tools it covers.

Supports:
  - ``~`` expansion (tilde → home directory)
  - Glob patterns via ``fnmatch`` (e.g. ``/home/*/code``)
  - Wildcard ``"*"`` (match everything)
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import List, Optional

from src.lib.config import C
from src.lib.logging import get_logger

logger = get_logger(__name__)

# Sentinel value indicating "match all paths"
_WILDCARD_ALL = "*"


def _resolve_tool_access_control_config() -> dict:
    """Resolve tool_access_control config from agent config or global C config.

    Per-agent config takes precedence over global system.yaml.
    """
    try:
        from src.trace.task_context import get_current_agent_config
        agent_cfg = get_current_agent_config()
        if isinstance(agent_cfg, dict):
            tac_cfg = agent_cfg.get("tool_access_control")
            if isinstance(tac_cfg, dict):
                return tac_cfg
    except Exception:
        pass
    fallback = C.get("tool_access_control", {})
    if isinstance(fallback, dict):
        return fallback
    return {}


def get_workspace_root() -> Path:
    """Return the resolved workspace root directory.

    Uses ``C.agent_root`` which is auto-detected from the project
    structure (directory containing pyproject.toml).
    """
    return Path(C.agent_root).resolve()


# ---------------------------------------------------------------------------
# Rule-based path accessors
# ---------------------------------------------------------------------------

def _find_rules_for_tool(tool_name: str) -> List[dict]:
    """Find ALL path_validation rules whose ``tools`` list contains *tool_name*.

    A rule with ``"*"`` in its ``tools`` list matches all tools.
    Returns a (possibly empty) list of rule dicts.  A tool in multiple
    rules gets the union of their include/exclude paths.
    """
    tac_cfg = _resolve_tool_access_control_config()
    rules = tac_cfg.get("path_validation")
    if not isinstance(rules, list):
        return []
    matched: List[dict] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        tools_list = rule.get("tools", [])
        if not isinstance(tools_list, list):
            continue
        if tool_name in tools_list or _WILDCARD_ALL in tools_list:
            matched.append(rule)
    return matched


def _collect_paths_from_rules(rules: List[dict], key: str) -> List[str]:
    """Merge a path-list field from multiple rules (union, dedup, order-preserving)."""
    seen: set = set()
    result: List[str] = []
    for rule in rules:
        raw = rule.get(key, [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            continue
        for p in raw:
            if isinstance(p, str) and p.strip() and p not in seen:
                seen.add(p)
                result.append(p.strip())
    return result


def get_rule_include_paths(tool_name: str) -> List[str]:
    """Return merged ``include_paths`` from all matching rules for *tool_name*.

    Raw strings are returned (no expansion).  The caller should use
    ``match_path_pattern()`` for matching or ``_expand_path()`` for
    resolution.

    Returns ``["*"]`` when any matching rule contains ``"*"`` (allow all).
    """
    rules = _find_rules_for_tool(tool_name)
    paths = _collect_paths_from_rules(rules, "include_paths")
    # If any path is the wildcard, short-circuit
    if _WILDCARD_ALL in paths:
        return [_WILDCARD_ALL]
    return paths


def get_rule_exclude_paths(tool_name: str) -> List[str]:
    """Return merged ``exclude_paths`` from all matching rules for *tool_name*.

    Returns ``["*"]`` when any matching rule contains ``"*"`` (deny all).
    """
    rules = _find_rules_for_tool(tool_name)
    paths = _collect_paths_from_rules(rules, "exclude_paths")
    if _WILDCARD_ALL in paths:
        return [_WILDCARD_ALL]
    return paths


# ---------------------------------------------------------------------------
# Glob / pattern matching
# ---------------------------------------------------------------------------

def match_path_pattern(path_str: str, pattern: str) -> bool:
    """Check whether *path_str* matches *pattern*.

    Supports:
      - ``"*"``   → matches everything (wildcard sentinel)
      - ``~``     → expanded to home directory before matching
      - Glob patterns → ``fnmatch.fnmatch`` (e.g. ``/home/*/code``)
      - Exact prefix → ``path_str.startswith(expanded_pattern)``

    Both *path_str* and *pattern* are resolved through tilde expansion.
    """
    if pattern == _WILDCARD_ALL:
        return True

    expanded_pattern = os.path.expanduser(pattern.strip())
    expanded_path = os.path.expanduser(path_str.strip())

    # Glob match (fnmatch operates on full path)
    if fnmatch.fnmatch(expanded_path, expanded_pattern):
        return True
    # Also try fnmatch with pattern as a prefix (directory glob)
    if fnmatch.fnmatch(expanded_path, expanded_pattern + "/*"):
        return True
    if fnmatch.fnmatch(expanded_path, expanded_pattern + "/**"):
        return True

    # Exact prefix match (for precise directory paths)
    try:
        resolved_pattern = str(Path(expanded_pattern).resolve())
        resolved_path = str(Path(expanded_path).resolve())
        if resolved_path == resolved_pattern or resolved_path.startswith(
            resolved_pattern + os.sep
        ):
            return True
    except (OSError, ValueError):
        pass

    return False


def _expand_path(p: str) -> Optional[Path]:
    """Expand tilde and resolve to absolute Path, or None on error."""
    if not p or not isinstance(p, str):
        return None
    expanded = os.path.expanduser(p.strip())
    try:
        return Path(expanded).resolve()
    except (OSError, ValueError):
        logger.warning("Invalid path, skipping: %s", p)
        return None


# ---------------------------------------------------------------------------
# Allowed directory builders
# ---------------------------------------------------------------------------

def get_allowed_directories(
    tool_name: Optional[str] = None,
    extra_include: Optional[List[str]] = None,
) -> List[Path]:
    """Build the complete list of allowed directories.

    Combines:
    1. Workspace root (always included, auto-detected)
    2. ``include_paths`` from matching ``path_validation`` rules for *tool_name*
    3. Optional *extra_include* paths (caller-provided additive)

    When any include list contains ``"*"`` the return value is
    ``[Path("*")]`` — callers must check for this sentinel.

    Args:
        tool_name: Canonical tool name to look up rules for.
        extra_include: Additional paths (e.g. from hook context).

    Returns:
        List of resolved absolute Path objects (or ``[Path("*")]``).
    """
    roots: List[Path] = [get_workspace_root()]

    rule_includes: List[str] = []
    if tool_name:
        rule_includes = get_rule_include_paths(tool_name)

    all_includes = list(rule_includes)
    if extra_include:
        all_includes.extend(extra_include)

    # Wildcard check
    if _WILDCARD_ALL in all_includes:
        return [Path(_WILDCARD_ALL)]

    for p in all_includes:
        resolved = _expand_path(p)
        if resolved:
            roots.append(resolved)

    return roots


def path_in_allowed_directory(
    path: Path,
    allowed_dirs: List[Path],
) -> bool:
    """Check if *path* is within any allowed directory.

    Supports:
    - ``Path("*")`` sentinel → always True
    - Glob patterns in allowed_dirs → ``fnmatch`` matching
    - Standard containment via ``Path.relative_to()``

    Both *path* and *allowed_dirs* should already be resolved.
    """
    path_str = str(path)
    for root in allowed_dirs:
        root_str = str(root)
        # Wildcard sentinel
        if root_str == _WILDCARD_ALL:
            return True
        # Glob pattern (contains fnmatch special characters)
        if any(c in root_str for c in ("*", "?", "[")):
            if match_path_pattern(path_str, root_str):
                return True
            continue
        # Standard containment
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False
