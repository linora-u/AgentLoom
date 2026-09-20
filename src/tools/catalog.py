"""Pure metadata catalog for AgentLoom's built-in tools.

Reading this module never imports a tool implementation.  Runtime code must
cross the explicit ``agentloom.tools.loader`` seam to turn an implementation
reference into a callable.
"""

from __future__ import annotations

from collections.abc import Iterable
from .catalog_types import ToolImplementation as ToolImplementation, ToolSpec


DEFAULT_TOOLSETS: tuple[str, ...] = (
    "core_shell",
    "core_file",
    "core_search",
    "context",
    "skills",
    "self_learning",
)

_CATALOG: dict[str, ToolSpec] | None = None
_TOOLSETS: dict[str, tuple[str, ...]] | None = None


def _build_catalog() -> dict[str, ToolSpec]:
    from agentloom.adapters.smolagents.tool_catalog import tool_specs as smol_specs
    from .platform_catalog import tool_specs as platform_specs
    from .optional_catalog import tool_specs as optional_specs

    catalog: dict[str, ToolSpec] = {}
    for spec in (*smol_specs(), *platform_specs(), *optional_specs()):
        if spec.name in catalog:
            raise ValueError(f"Duplicate tool name: {spec.name}")
        catalog[spec.name] = spec
    return catalog


def _ensure_catalog() -> dict[str, ToolSpec]:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = _build_catalog()
    return _CATALOG


def _ensure_toolsets() -> dict[str, tuple[str, ...]]:
    global _TOOLSETS
    if _TOOLSETS is None:
        grouped: dict[str, list[str]] = {}
        for spec in _ensure_catalog().values():
            grouped.setdefault(spec.toolset, []).append(spec.name)
        _TOOLSETS = {toolset: tuple(names) for toolset, names in grouped.items()}
    return _TOOLSETS


def get_tool_spec(tool_name: str) -> ToolSpec:
    name = str(tool_name or "").strip()
    spec = _ensure_catalog().get(name)
    if spec is None:
        available = ", ".join(sorted(_ensure_catalog()))
        raise ValueError(f"Tool '{tool_name}' is not a registered built-in tool. Available tools: {available}")
    return spec


def list_tool_specs() -> tuple[ToolSpec, ...]:
    return tuple(_ensure_catalog().values())


def list_toolsets() -> dict[str, tuple[str, ...]]:
    return dict(_ensure_toolsets())


def resolve_toolsets(toolsets: Iterable[str] | None) -> list[str]:
    raw_toolsets = DEFAULT_TOOLSETS if toolsets is None else tuple(toolsets)
    available_toolsets = _ensure_toolsets()
    result: list[str] = []
    seen: set[str] = set()
    for raw_name in raw_toolsets:
        name = str(raw_name or "").strip()
        if not name:
            continue
        tools = available_toolsets.get(name)
        if tools is None:
            available = ", ".join(sorted(available_toolsets))
            raise ValueError(f"Unknown toolset '{name}'. Available toolsets: {available}")
        for tool_name in tools:
            if tool_name not in seen:
                seen.add(tool_name)
                result.append(tool_name)
    return result
