"""Unit tests for MCP tool loading integration in YamlAgentFactory.get_tools_from_config().

Verifies that get_tools_from_config() returns (tools, McpManager | None)
and that MCP tools are correctly appended when mcp_servers is configured.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.mcp.config import McpSettings, McpServerConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_tool(name: str) -> MagicMock:
    tool = MagicMock()
    tool.name = name
    tool.description = f"Tool {name}"
    tool.__name__ = name
    return tool


def _make_mcp_json(tmp_path: Path, servers: dict) -> Path:
    """Create a .mcp.json file and return its path."""
    data = {"mcpServers": servers}
    path = tmp_path / ".mcp.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Return type is tuple
# ---------------------------------------------------------------------------

class TestReturnType:

    def test_returns_tuple_without_mcp(self):
        """When no mcp_servers configured, returns (tools, None)."""
        from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

        result = YamlAgentFactory.get_tools_from_config(
            {"tools": []},
            effective_agent_config={"default_toolsets": []},
        )
        assert isinstance(result, tuple)
        assert len(result) == 2
        tools, mcp_mgr = result
        assert isinstance(tools, list)
        assert mcp_mgr is None

    def test_returns_tuple_no_tools_key(self):
        """Config without 'tools' key still returns tuple."""
        from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

        result = YamlAgentFactory.get_tools_from_config(
            {},
            effective_agent_config={"default_toolsets": []},
        )
        assert isinstance(result, tuple)
        tools, mcp_mgr = result
        assert tools == []
        assert mcp_mgr is None


# ---------------------------------------------------------------------------
# MCP tools appended (mocked)
# ---------------------------------------------------------------------------

class TestMcpToolsLoading:

    @patch("src.mcp.manager.McpManager")
    @patch("src.mcp.config.parse_mcp_yaml_value")
    @patch("src.mcp.config.merge_mcp_configs")
    def test_mcp_tools_appended(
        self, mock_merge, mock_parse, MockManager, tmp_path
    ):
        """When mcp_servers is configured and servers connect, tools are appended."""
        from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

        # Set up mocks
        fake_mcp_tool = _fake_tool("mcp__srv__search")
        mock_settings = McpSettings(
            configs=[McpServerConfig(name="srv", type="stdio", command="echo")]
        )
        mock_parse.return_value = mock_settings
        mock_merge.return_value = mock_settings

        mock_mgr_instance = MagicMock()
        mock_mgr_instance.get_all_tools.return_value = [fake_mcp_tool]
        MockManager.return_value = mock_mgr_instance

        config = {
            "tools": [],
            "mcp_servers": "config/.mcp.json",
        }
        tools, mcp_mgr = YamlAgentFactory.get_tools_from_config(
            config,
            effective_agent_config={"default_toolsets": []},
        )

        assert any(getattr(t, "name", None) == "mcp__srv__search" for t in tools)
        assert mcp_mgr is mock_mgr_instance
        mock_mgr_instance.connect_all.assert_called_once()


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

class TestNoMcpServers:

    def test_no_mcp_servers_loads_default_toolsets(self):
        from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory

        config = {}  # no tools, no mcp_servers
        tools, mcp_mgr = YamlAgentFactory.get_tools_from_config(
            config,
            effective_agent_config={"default_toolsets": ["core_shell"]},
        )
        assert mcp_mgr is None
        tool_names = {getattr(tool, "__name__", getattr(tool, "name", None)) for tool in tools}
        assert tool_names == {
            "shell_tool",
            "check_background_task",
            "kill_background_task",
            "list_background_tasks",
        }
