"""Keep MCP partial-failure and real stdio reconnect behavior observable."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest
from agentloom.execution.model_protocol import ToolDefinition
from agentloom.execution.tool_gateway import ToolBinding
from agentloom.integrations.mcp.client import AgentLoomMCPClient
from agentloom.integrations.mcp.config import McpServerConfig, McpSettings
from agentloom.integrations.mcp.manager import McpManager
from mcp import StdioServerParameters


def test_partial_connection_preserves_tools_and_disconnect_is_idempotent():
    tool = ToolBinding(
        definition=ToolDefinition(
            name="search", description="Search", parameters={"type": "object", "properties": {}}
        ),
        forward=lambda: "ok",
        inputs_schema={},
    )
    good_client = MagicMock()
    good_client.get_tools.return_value = [tool]
    with patch("agentloom.integrations.mcp.manager.MCPClient") as client_class:
        client_class.side_effect = [good_client, ConnectionError("unavailable")]
        manager = McpManager(McpSettings(configs=[
            McpServerConfig(name="good", type="stdio", command="echo"),
            McpServerConfig(name="bad", type="stdio", command="echo"),
        ]))
        manager.connect_all()

    status = manager.get_server_status()
    assert status["good"] == {"connected": True, "tool_count": 1, "error": None}
    assert status["bad"]["connected"] is False
    assert "unavailable" in status["bad"]["error"]
    assert [item.definition.name for item in manager.get_all_tools()] == [
        "mcp__good__search"
    ]

    good_client.disconnect.side_effect = RuntimeError("disconnect failed")
    manager.disconnect_all()
    manager.disconnect_all()
    good_client.disconnect.assert_called_once()
    assert manager.get_all_tools() == []

    server = Path(__file__).parent / "fixtures" / "stdio_server.py"
    client = AgentLoomMCPClient(
        StdioServerParameters(command=sys.executable, args=[str(server)])
    )
    try:
        first = next(item for item in client.get_tools() if item.definition.name == "lookup")
        pid = first.forward(query="before disconnect")["pid"]
        client.disconnect()
        client.disconnect()
        assert not psutil.pid_exists(pid)
        with pytest.raises(ValueError, match="connect"):
            client.get_tools()
        client.connect()
        fresh = next(item for item in client.get_tools() if item.definition.name == "lookup")
        assert fresh.forward(query="after reconnect")["calls"] == 1
        with pytest.raises(RuntimeError, match="closed"):
            first.forward(query="stale binding")
    finally:
        client.disconnect()
