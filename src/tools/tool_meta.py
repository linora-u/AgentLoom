"""
Tool metadata system — YAML-driven, convention-based tool resolution.

1. **Convention-based resolution**: ``resolve_tool_function(name)`` looks up
   the function via ``getattr(src.tools, name)``.
2. **YAML-driven metadata**: ``get_tool_meta(name)`` reads from
   ``config/system.yaml  tool_metadata`` with Agent YAML overrides.

Metadata precedence (high → low):
    Agent YAML  tools[].xxx  >  system.yaml  tool_metadata.<name>  >  default  >  hardcoded defaults
"""

from __future__ import annotations

import importlib
from src.lib.logging import get_logger
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from src.lib.config import C

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Hardcoded defaults (lowest precedence — overridden by YAML)
# ---------------------------------------------------------------------------
_HARDCODED_DEFAULTS: Dict[str, Any] = {
    "max_result_chars": 20000,
    "is_concurrency_safe": True,
    "category": "general",
    "disable_type_coercion": False,
}


# ---------------------------------------------------------------------------
# ToolMeta dataclass — runtime representation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolMeta:
    """Runtime metadata for a single tool, assembled from YAML config."""

    name: str
    max_result_chars: Optional[int] = 20000
    is_concurrency_safe: bool = True
    category: str = "general"
    disable_type_coercion: bool = False


# ---------------------------------------------------------------------------
# Tool function resolution
# ---------------------------------------------------------------------------

# The ``src.tools`` package imports every tool function at module
# level (e.g. ``from .search import grep_search``).  We leverage ``getattr``
# for convention-based lookup without a separate registry.
_TOOLS_MODULE = None


def _get_tools_module():
    """Lazy-import the tools package to avoid circular imports."""
    global _TOOLS_MODULE
    if _TOOLS_MODULE is None:
        _TOOLS_MODULE = importlib.import_module("src.tools")
    return _TOOLS_MODULE


def resolve_tool_function(tool_name: str) -> Callable:
    """Resolve a tool name to its callable function.

    Uses convention-based lookup: ``getattr(src.tools, tool_name)``.
    Raises ``ValueError`` if the tool cannot be found.

    Args:
        tool_name: Canonical tool function name (e.g. ``"grep_search"``).

    Returns:
        The callable tool function.

    Raises:
        ValueError: If the tool name does not exist in ``src.tools``.
    """
    tools_mod = _get_tools_module()
    func = getattr(tools_mod, tool_name, None)
    if func is not None and callable(func):
        return func

    # Provide a helpful error listing available tools
    available = sorted(
        name for name in dir(tools_mod)
        if not name.startswith("_") and callable(getattr(tools_mod, name, None))
    )
    raise ValueError(
        f"Tool '{tool_name}' not found in src.tools. "
        f"Available tools: {available[:20]}{'...' if len(available) > 20 else ''}"
    )


# ---------------------------------------------------------------------------
# Metadata loading from YAML
# ---------------------------------------------------------------------------

def _load_tool_metadata_from_config() -> Dict[str, Dict[str, Any]]:
    """Read the ``tool_metadata`` section from system config.

    Returns:
        A dict keyed by tool name, each value is a dict of metadata fields.
        Includes a ``"default"`` key if configured.
    """
    raw = C.get("tool_metadata", {})
    if not isinstance(raw, dict):
        logger.warning("tool_metadata config is not a dict, ignoring: %s", type(raw))
        return {}
    return raw


def get_tool_meta(
    tool_name: str,
    agent_tool_overrides: Optional[Dict[str, Any]] = None,
) -> ToolMeta:
    """Build a ``ToolMeta`` for *tool_name* by merging config layers.

    Precedence (high → low):
        1. *agent_tool_overrides* (from Agent YAML ``tools[].xxx``)
        2. ``config/system.yaml`` → ``tool_metadata.<tool_name>``
        3. ``config/system.yaml`` → ``tool_metadata.default``
        4. Hardcoded defaults in ``_HARDCODED_DEFAULTS``

    Args:
        tool_name: Canonical tool function name.
        agent_tool_overrides: Optional per-tool overrides from Agent YAML.

    Returns:
        A frozen ``ToolMeta`` instance.
    """
    yaml_meta = _load_tool_metadata_from_config()

    # Layer merging: hardcoded → default → per-tool → agent override
    merged: Dict[str, Any] = dict(_HARDCODED_DEFAULTS)

    # system.yaml default section
    default_section = yaml_meta.get("default")
    if isinstance(default_section, dict):
        merged.update({k: v for k, v in default_section.items() if v is not None})

    # system.yaml per-tool section
    tool_section = yaml_meta.get(tool_name)
    if isinstance(tool_section, dict):
        merged.update({k: v for k, v in tool_section.items() if v is not None})

    # Agent YAML per-tool overrides
    if isinstance(agent_tool_overrides, dict):
        merged.update({k: v for k, v in agent_tool_overrides.items() if v is not None})

    # Build ToolMeta, only passing fields the dataclass accepts
    known_fields = {f.name for f in ToolMeta.__dataclass_fields__.values()}
    filtered = {k: v for k, v in merged.items() if k in known_fields}
    filtered["name"] = tool_name

    return ToolMeta(**filtered)
