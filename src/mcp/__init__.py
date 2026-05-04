"""MCP (Model Context Protocol) client integration for AgentLoom.

Provides configuration parsing, connection management, and tool wrapping
for external MCP servers.  Tools discovered from MCP servers are adapted
into standard smolagents ``Tool`` instances and injected alongside
locally-defined tools during agent startup.

Public API
----------
- :class:`McpServerConfig` / :class:`McpSettings` — configuration data classes
- :func:`parse_mcp_yaml_value` — parse ``mcp_servers`` YAML value
- :func:`merge_mcp_configs` — merge global + agent-level MCP settings
- :class:`McpManager` — lifecycle manager for MCP server connections
"""

from src.mcp.config import (
    McpServerConfig,
    McpSettings,
    parse_mcp_yaml_value,
    merge_mcp_configs,
)
from src.mcp.manager import McpManager

__all__ = [
    "McpServerConfig",
    "McpSettings",
    "McpManager",
    "parse_mcp_yaml_value",
    "merge_mcp_configs",
]
