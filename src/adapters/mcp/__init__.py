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

from src.adapters.mcp.config import (
    McpServerConfig,
    McpSettings,
    merge_mcp_configs,
    parse_mcp_yaml_value,
)


def __getattr__(name):
    if name == "McpManager":
        from src.adapters.mcp.manager import McpManager
        globals()[name] = McpManager
        return McpManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "McpServerConfig",
    "McpSettings",
    "McpManager",
    "parse_mcp_yaml_value",
    "merge_mcp_configs",
]
