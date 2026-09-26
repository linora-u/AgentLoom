"""Keep the MCP connection lifecycle's partial-failure contract observable."""

from unittest.mock import MagicMock, patch

from agentloom.execution.model_protocol import ToolDefinition
from agentloom.execution.tool_gateway import ToolBinding
from agentloom.integrations.mcp.config import McpServerConfig, McpSettings
from agentloom.integrations.mcp.manager import McpManager


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
