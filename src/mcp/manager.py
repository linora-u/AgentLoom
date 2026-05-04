"""MCP connection lifecycle manager.

:class:`McpManager` creates one ``smolagents.MCPClient`` per configured MCP
server, aggregates the discovered tools, and ensures graceful shutdown.
A server that fails to connect is logged as a warning and skipped — it
never blocks agent startup.
"""

from __future__ import annotations

from typing import Any

from src.lib.logging import get_logger
from src.mcp.config import McpServerConfig, McpSettings, to_mcp_client_params
from src.mcp.tool_wrapper import wrap_mcp_tools

logger = get_logger(__name__)


class McpManager:
    """Manage multiple MCP server connections and aggregate their tools.

    Usage::

        manager = McpManager(settings)
        manager.connect_all()
        tools = manager.get_all_tools()
        # ... use tools ...
        manager.disconnect_all()

    Or as a context manager::

        with McpManager(settings) as manager:
            tools = manager.get_all_tools()
    """

    def __init__(self, settings: McpSettings) -> None:
        self._settings = settings
        # MCPClient instances keyed by server name.
        self._clients: dict[str, Any] = {}
        # Raw tools per server (before wrapping).
        self._raw_tools: dict[str, list] = {}
        # Connection errors per server.
        self._errors: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect_all(self) -> None:
        """Connect to all configured MCP servers.

        Failures are logged as warnings and recorded in ``_errors``.
        Successful servers populate ``_clients`` and ``_raw_tools``.
        """
        if not self._settings.configs:
            logger.debug("[MCP] No MCP servers configured")
            return

        for cfg in self._settings.configs:
            self._connect_one(cfg)

    def _connect_one(self, cfg: McpServerConfig) -> None:
        """Connect to a single MCP server.  Never raises."""
        try:
            from smolagents import MCPClient
        except ImportError:
            msg = (
                "smolagents[mcp] is required for MCP support. "
                "Install with: uv pip install 'smolagents[mcp]'"
            )
            logger.warning("[MCP] %s", msg)
            self._errors[cfg.name] = msg
            return

        try:
            params = to_mcp_client_params(cfg)
            client = MCPClient(params)
            tools = client.get_tools()
            self._clients[cfg.name] = client
            self._raw_tools[cfg.name] = list(tools)
            logger.info(
                "[MCP] Connected to '%s': %d tools", cfg.name, len(tools)
            )
        except Exception as exc:
            logger.warning("[MCP] Failed to connect to '%s': %s", cfg.name, exc)
            self._errors[cfg.name] = str(exc)

    # ------------------------------------------------------------------
    # Tool access
    # ------------------------------------------------------------------

    def get_all_tools(self) -> list:
        """Return flattened, wrapped tool list from all connected servers."""
        all_tools: list = []
        for server_name, raw_tools in self._raw_tools.items():
            wrapped = wrap_mcp_tools(server_name, raw_tools, self._settings)
            all_tools.extend(wrapped)
        return all_tools

    def get_server_status(self) -> dict[str, dict[str, Any]]:
        """Return per-server status.

        Returns a dict::

            {
                "server_name": {
                    "connected": bool,
                    "tool_count": int,
                    "error": str | None,
                },
                ...
            }
        """
        status: dict[str, dict[str, Any]] = {}
        all_names = {cfg.name for cfg in self._settings.configs}
        for name in all_names:
            connected = name in self._clients
            tool_count = len(self._raw_tools.get(name, []))
            error = self._errors.get(name)
            status[name] = {
                "connected": connected,
                "tool_count": tool_count,
                "error": error,
            }
        return status

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def disconnect_all(self) -> None:
        """Disconnect all MCP clients.  Idempotent — safe to call multiple times."""
        for name in list(self._clients.keys()):
            self._disconnect_one(name)

    def _disconnect_one(self, name: str) -> None:
        """Disconnect a single server.  Never raises."""
        client = self._clients.pop(name, None)
        if client is None:
            return
        try:
            client.disconnect()
            logger.info("[MCP] Disconnected from '%s'", name)
        except Exception as exc:
            logger.warning("[MCP] Error disconnecting from '%s': %s", name, exc)
        self._raw_tools.pop(name, None)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "McpManager":
        self.connect_all()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect_all()
