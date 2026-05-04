"""MCP tool wrapping: name prefixing and description enrichment.

Applies the ``mcp__{server}__{tool}`` naming convention (configurable)
so that MCP tools coexist safely with local tools in the same agent.
"""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING

from src.lib.logging import get_logger

if TYPE_CHECKING:
    from smolagents import Tool

    from src.mcp.config import McpSettings

logger = get_logger(__name__)

_NAME_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")


def _sanitize_name(name: str) -> str:
    """Replace any non-alphanumeric character with ``_``."""
    return _NAME_SANITIZE_RE.sub("_", name)


def wrap_mcp_tools(
    server_name: str,
    tools: list["Tool"],
    settings: "McpSettings",
) -> list["Tool"]:
    """Apply name prefix and description enrichment to MCP tools.

    When ``settings.tool_name_prefix`` is ``True`` (default), each tool is
    renamed to ``mcp__{server}__{tool}`` to prevent collisions with local
    tools of the same name.

    Parameters
    ----------
    server_name:
        The MCP server name (from JSON key).
    tools:
        Raw tool list returned by ``MCPClient.get_tools()``.
    settings:
        Parsed :class:`McpSettings` controlling prefix behaviour.

    Returns
    -------
    list[Tool]
        Shallow-copied tools with updated names and descriptions.
    """
    sanitized = _sanitize_name(server_name)
    wrapped: list["Tool"] = []

    for tool in tools:
        # Shallow copy to avoid mutating the original MCPClient tool.
        t = copy.copy(tool)
        original_name = getattr(t, "name", "unknown")

        if settings.tool_name_prefix:
            prefixed_name = f"mcp__{sanitized}__{original_name}"
            t.name = prefixed_name

        # Enrich description with server origin hint (helps LLM context).
        desc = getattr(t, "description", "") or ""
        origin_hint = f"[MCP:{server_name}] "
        if not desc.startswith(origin_hint):
            t.description = f"{origin_hint}{desc}"

        wrapped.append(t)
        logger.debug(
            "[MCP] Wrapped tool '%s' -> '%s'",
            original_name,
            getattr(t, "name", original_name),
        )

    return wrapped
