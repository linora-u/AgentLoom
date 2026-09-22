"""MCP tool wrapping: name prefixing and description enrichment.

Applies the ``mcp__{server}__{tool}`` naming convention (configurable)
so that MCP tools coexist safely with local tools in the same agent.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

from agentloom.execution.logging import get_logger
from agentloom.execution.native_tools import ToolManifestEntry
from agentloom.execution.tool_gateway import ToolBinding

if TYPE_CHECKING:
    from agentloom.integrations.mcp.config import McpSettings

logger = get_logger(__name__)

_NAME_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]")


def _sanitize_name(name: str) -> str:
    """Replace any non-alphanumeric character with ``_``."""
    return _NAME_SANITIZE_RE.sub("_", name)


def wrap_mcp_tools(
    server_name: str,
    tools: list[ToolBinding],
    settings: McpSettings,
) -> list[ToolBinding]:
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
    list[ToolBinding]
        New definitions sharing only their connection-owned callable.
    """
    sanitized = _sanitize_name(server_name)
    wrapped: list[ToolBinding] = []

    for tool in tools:
        original_name = tool.definition.name
        name = f"mcp__{sanitized}__{original_name}" if settings.tool_name_prefix else original_name

        # Enrich description with server origin hint (helps LLM context).
        desc = tool.definition.description or ""
        origin_hint = f"[MCP:{server_name}] "
        if not desc.startswith(origin_hint):
            desc = f"{origin_hint}{desc}"

        definition = replace(tool.definition, name=name, description=desc)
        wrapped.append(replace(
            tool,
            definition=definition,
            manifest_entry=ToolManifestEntry(
                logical_name=name,
                visible_name=name,
                owner="external",
                provider=f"mcp:{server_name}",
                capability=f"mcp.{original_name}",
                operation="control",
                parameters=definition.parameters,
            ),
        ))
        logger.debug(
            "[MCP] Wrapped tool '%s' -> '%s'",
            original_name,
            name,
        )

    return wrapped
