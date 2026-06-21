"""
Rules-driven tool access control hook (PRE_TOOL_USE).

Delegates all security checks (UNC, Windows, symlink, workspace boundary)
to the shared permissions library at ``src.lib.permissions``.
This module only handles:
  - Rule matching (which tools need hook-level validation)
  - Path parameter extraction from tool input
  - Delegating to the shared ``validate_path()``

For backward-compatibility, the security functions are re-exported so that
existing test imports continue to work.
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .types import HookContext, HookResult
from src.lib.config import C
from src.lib.logging import get_logger
from src.trace.task_context import get_current_agent_config

# Re-export from shared library for backward-compatibility with tests
from src.lib.permissions.path_validation import (  # noqa: F401
    has_suspicious_windows_pattern,
    is_vulnerable_unc_path,
    resolve_symlink_chain,
)

logger = get_logger(__name__)

# ============================================
# Default path param patterns (fallback when not configured)
# ============================================
DEFAULT_PATH_PARAM_PATTERNS: List[str] = [
    "file_path",
    "filePath",
    "directory_path",
    "directory",
    "dirPath",
    "repo_path",
    "path",
    "path_str",
    "file_paths",
    "filePaths",
    "fileUri",
]


def _resolve_tool_access_control_config() -> dict:
    """Resolve tool_access_control config from agent config or global C config."""
    agent_cfg = get_current_agent_config()
    if isinstance(agent_cfg, dict):
        tac_cfg = agent_cfg.get("tool_access_control")
        if isinstance(tac_cfg, dict):
            return tac_cfg
    fallback = C.get("tool_access_control", {})
    if isinstance(fallback, dict):
        return fallback
    return {}


def _find_rule_for_tool(tool_name: str, rules: list) -> Optional[dict]:
    """Find the first rule whose 'tools' list contains the given tool_name."""
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        tools_list = rule.get("tools", [])
        if isinstance(tools_list, list) and tool_name in tools_list:
            return rule
    return None


def _resolve_path_params(
    tool_name: str,
    tool_inputs_schema: Optional[Dict[str, Any]],
    path_param_patterns: List[str],
) -> List[str]:
    """Match tool parameter names against path_param_patterns."""
    try:
        from src.tools import get_tool_spec

        registry_params = list(get_tool_spec(tool_name).path_params)
    except ValueError:
        registry_params = []

    if not isinstance(tool_inputs_schema, dict) or not path_param_patterns:
        return registry_params
    pattern_set = set(path_param_patterns)
    schema_params = [name for name in tool_inputs_schema if name in pattern_set]
    merged = []
    for name in [*registry_params, *schema_params]:
        if name not in merged:
            merged.append(name)
    return merged


def _normalize_str_list(raw: Any, default: list | None = None) -> List[str]:
    """Normalize a value to a list of non-empty strings."""
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str) and item.strip()]
    return list(default) if default else []


def validate_workspace_path(context: HookContext) -> HookResult:
    """Rules-driven tool access control hook.

    Delegates to the shared permissions library for all security checks.

    Flow:
      1. Read tool_access_control.path_validation rules
      2. Find the first entry matching the tool → not found? allow
      3. Extract file path values from tool input
      4. For each path: call shared validate_path() with per-rule extras
    """
    from src.lib.permissions import validate_path

    tac_cfg = _resolve_tool_access_control_config()

    # Read path_validation list
    rules = tac_cfg.get("path_validation")
    if not isinstance(rules, list) or not rules:
        return HookResult(success=True, decision="allow")

    tool_name = context.tool_name

    # Find entry for this tool
    rule = _find_rule_for_tool(tool_name, rules)
    if rule is None:
        return HookResult(success=True, decision="allow")

    # path_param_patterns: use entry's if present, otherwise DEFAULT
    entry_patterns = _normalize_str_list(rule.get("path_param_patterns"))
    effective_patterns = entry_patterns if entry_patterns else list(DEFAULT_PATH_PARAM_PATTERNS)

    # Resolve path params from tool schema
    tool_inputs_schema = getattr(context, "tool_inputs_schema", None)
    path_params = _resolve_path_params(tool_name, tool_inputs_schema, effective_patterns)
    if not path_params:
        return HookResult(success=True, decision="allow")

    # Extract path values from tool_input
    tool_input = context.tool_input
    paths_to_check: List[str] = []
    for param in path_params:
        if param in tool_input:
            val = tool_input[param]
            if isinstance(val, str):
                paths_to_check.append(val)
            elif isinstance(val, list):
                paths_to_check.extend([str(v) for v in val if isinstance(v, str)])

    if not paths_to_check:
        return HookResult(success=True, decision="allow")

    # Validate each path via shared library.
    # include_paths / exclude_paths are resolved from path_validation rules
    # inside validate_path() using tool_name.
    for p_str in paths_to_check:
        try:
            result = validate_path(
                p_str,
                tool_name=tool_name,
            )
            if not result.allowed:
                return HookResult(
                    success=True,
                    decision="block",
                    reason=result.reason,
                )
        except Exception as e:
            logger.error("Error validating path %s: %s", p_str, e)
            return HookResult(
                success=True,
                decision="block",
                reason=f"Path validation error: {e}",
            )

    return HookResult(success=True, decision="allow")
