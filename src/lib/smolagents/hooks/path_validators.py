"""Rules-driven, non-configurable CoreToolGuard path enforcement.

Delegates all security checks (UNC, Windows, symlink, workspace boundary)
to the shared permissions library at ``src.lib.permissions``.
This module only handles:
  - Rule matching (which tools need final-boundary validation)
  - Path parameter extraction from tool input
  - Delegating to the shared ``validate_path()``

For backward-compatibility, the security functions are re-exported so that
existing test imports continue to work.
"""

from typing import Any

from src.lib.config import C
from src.lib.logging import get_logger

# Re-export from shared library for backward-compatibility with tests
from src.lib.permissions.path_validation import (  # noqa: F401
    has_suspicious_windows_pattern,
    is_vulnerable_unc_path,
    resolve_symlink_chain,
)
from src.trace.task_context import get_current_agent_config

from .types import HookContext, HookResult

logger = get_logger(__name__)

# ============================================
# Default path param patterns (fallback when not configured)
# ============================================
DEFAULT_PATH_PARAM_PATTERNS: list[str] = [
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


def _find_rules_for_tool(tool_name: str, rules: list) -> list[dict]:
    """Return every rule applying to *tool_name* in declaration order."""

    matched: list[dict] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        tools_list = rule.get("tools", [])
        if isinstance(tools_list, list) and (tool_name in tools_list or "*" in tools_list):
            matched.append(rule)
    return matched


def _find_rule_for_tool(tool_name: str, rules: list) -> dict | None:
    """Compatibility helper returning the first matching rule, if any."""

    matched = _find_rules_for_tool(tool_name, rules)
    return matched[0] if matched else None


def _collect_rule_values(rules: list[dict], key: str) -> list[str]:
    """Merge one string-list field across matching rules without reordering."""

    values: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        for raw in _normalize_str_list(rule.get(key)):
            value = raw.strip()
            if value and value not in seen:
                seen.add(value)
                values.append(value)
    return values


def _resolve_path_params(
    tool_name: str,
    tool_inputs_schema: dict[str, Any] | None,
    path_param_patterns: list[str],
) -> list[str]:
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


def _normalize_str_list(raw: Any, default: list | None = None) -> list[str]:
    """Normalize a value to a list of non-empty strings."""
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, str) and item.strip()]
    return list(default) if default else []


def _evaluate_workspace_path(
    context: HookContext,
    *,
    explicit_runtime_policy: bool = False,
) -> HookResult:
    """Evaluate rules-driven tool access control for one final tool input.

    Delegates to the shared permissions library for all security checks.

    Flow:
      1. Read tool_access_control.path_validation rules
      2. Find every entry matching the tool → not found? allow
      3. Extract file path values from tool input
      4. For each path: call shared validate_path() with per-rule extras
    """
    from src.lib.permissions import validate_path

    tool_name = context.tool_name
    tool_inputs_schema = getattr(context, "tool_inputs_schema", None)
    potential_path_params = _resolve_path_params(
        tool_name,
        tool_inputs_schema,
        list(DEFAULT_PATH_PARAM_PATTERNS),
    )
    if explicit_runtime_policy:
        if not isinstance(context.agent_config, dict):
            if not potential_path_params:
                return HookResult(decision="allow")
            return HookResult(decision="block", reason="Core tool guard is missing explicit Agent config")
        if not isinstance(context.project_root, str) or not context.project_root.strip():
            if not potential_path_params:
                return HookResult(decision="allow")
            return HookResult(decision="block", reason="Core tool guard is missing explicit project root")
        tac_cfg = context.agent_config.get("tool_access_control", {})
        if not isinstance(tac_cfg, dict):
            return HookResult(decision="block", reason="Invalid explicit tool_access_control config")
    else:
        tac_cfg = _resolve_tool_access_control_config()

    # Read path_validation list
    rules = tac_cfg.get("path_validation")
    if not isinstance(rules, list) or not rules:
        return HookResult(decision="allow")

    matching_rules = _find_rules_for_tool(tool_name, rules)
    if not matching_rules:
        return HookResult(decision="allow")

    # Each matching rule contributes its path parameters. A rule omitting the
    # field retains the documented defaults, so a narrower later rule cannot
    # accidentally disable validation established by an earlier rule.
    effective_patterns: list[str] = []
    for rule in matching_rules:
        patterns = _normalize_str_list(rule.get("path_param_patterns"))
        for pattern in patterns or DEFAULT_PATH_PARAM_PATTERNS:
            if pattern not in effective_patterns:
                effective_patterns.append(pattern)

    include_paths = _collect_rule_values(matching_rules, "include_paths")
    exclude_paths = _collect_rule_values(matching_rules, "exclude_paths")

    # Resolve path params from tool schema
    path_params = _resolve_path_params(tool_name, tool_inputs_schema, effective_patterns)
    if not path_params:
        return HookResult(decision="allow")

    # Extract path values from tool_input
    tool_input = context.tool_input
    paths_to_check: list[str] = []
    for param in path_params:
        if param in tool_input:
            val = tool_input[param]
            if isinstance(val, str):
                paths_to_check.append(val)
            elif isinstance(val, list):
                paths_to_check.extend([str(v) for v in val if isinstance(v, str)])

    if not paths_to_check:
        return HookResult(decision="allow")

    # Validate each path via shared library.
    # include_paths / exclude_paths are resolved from path_validation rules
    # inside validate_path() using tool_name.
    for p_str in paths_to_check:
        try:
            result = validate_path(
                p_str,
                tool_name=None if explicit_runtime_policy else tool_name,
                explicit_include_paths=(
                    include_paths if explicit_runtime_policy else None
                ),
                explicit_exclude_paths=(
                    exclude_paths if explicit_runtime_policy else None
                ),
                explicit_workspace_root=(context.project_root if explicit_runtime_policy else None),
            )
            if not result.allowed:
                return HookResult(
                    decision="block",
                    reason=result.reason,
                )
        except Exception as e:
            logger.error("Error validating path %s: %s", p_str, e)
            return HookResult(
                decision="block",
                reason=f"Path validation error: {e}",
            )

    return HookResult(decision="allow")


def enforce_core_tool_guard(context: HookContext) -> HookResult:
    """Run the non-configurable final path guard.

    The tool runtime calls this function directly after all configurable
    ``PreToolUse`` transformations. It is intentionally not registered as a
    ``HookHandler`` and therefore cannot be removed, reordered, or replaced by
    configured Hook declarations.
    """

    return _evaluate_workspace_path(context, explicit_runtime_policy=True)


def validate_workspace_path(context: HookContext) -> HookResult:
    """Compatibility API for direct validation tests and legacy callers."""

    return _evaluate_workspace_path(context)
