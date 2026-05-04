"""Unit tests for src.mcp.manager — McpManager lifecycle, tool aggregation, graceful degradation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.mcp.config import McpServerConfig, McpSettings
from src.mcp.manager import McpManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stdio_config(name: str = "test_server", command: str = "echo") -> McpServerConfig:
    return McpServerConfig(name=name, type="stdio", command=command)


def _settings(*configs: McpServerConfig, prefix: bool = True) -> McpSettings:
    return McpSettings(configs=list(configs), tool_name_prefix=prefix)


def _fake_tool(name: str = "tool_a") -> MagicMock:
    tool = MagicMock()
    tool.name = name
    tool.description = "A tool"
    return tool


def _mock_mcp_client(tools: list | None = None):
    """Create a mock MCPClient that returns the given tools."""
    client = MagicMock()
    client.get_tools.return_value = tools or [_fake_tool()]
    return client


# ---------------------------------------------------------------------------
# connect_all
# ---------------------------------------------------------------------------

class TestConnectAll:

    @patch("src.mcp.manager.to_mcp_client_params")
    @patch("smolagents.MCPClient")
    def test_success(self, MockMCPClient, mock_params):
        mock_params.return_value = MagicMock()
        fake_tools = [_fake_tool("t1"), _fake_tool("t2")]
        MockMCPClient.return_value = _mock_mcp_client(fake_tools)

        cfg = _stdio_config("srv")
        manager = McpManager(_settings(cfg))
        manager.connect_all()

        assert "srv" in manager._clients
        assert len(manager._raw_tools["srv"]) == 2
        assert not manager._errors

    @patch("src.mcp.manager.to_mcp_client_params")
    @patch("smolagents.MCPClient", side_effect=RuntimeError("boom"))
    def test_failure_graceful(self, MockMCPClient, mock_params):
        mock_params.return_value = MagicMock()

        cfg = _stdio_config("bad")
        manager = McpManager(_settings(cfg))
        manager.connect_all()

        assert "bad" not in manager._clients
        assert "bad" in manager._errors
        assert "boom" in manager._errors["bad"]

    @patch("src.mcp.manager.to_mcp_client_params")
    @patch("smolagents.MCPClient")
    def test_partial_failure(self, MockMCPClient, mock_params):
        """One server succeeds, one fails — both recorded correctly."""
        mock_params.return_value = MagicMock()
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _mock_mcp_client([_fake_tool("ok_tool")])
            raise ConnectionError("timeout")

        MockMCPClient.side_effect = side_effect

        good = _stdio_config("good")
        bad = _stdio_config("bad")
        manager = McpManager(_settings(good, bad))
        manager.connect_all()

        assert "good" in manager._clients
        assert "bad" not in manager._clients
        assert "bad" in manager._errors

    def test_no_configs(self):
        manager = McpManager(McpSettings(configs=[]))
        manager.connect_all()
        assert not manager._clients
        assert not manager._errors


# ---------------------------------------------------------------------------
# get_all_tools
# ---------------------------------------------------------------------------

class TestGetAllTools:

    @patch("src.mcp.manager.to_mcp_client_params")
    @patch("smolagents.MCPClient")
    def test_tools_from_multiple_servers(self, MockMCPClient, mock_params):
        mock_params.return_value = MagicMock()
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _mock_mcp_client([_fake_tool("tool_a")])
            return _mock_mcp_client([_fake_tool("tool_b")])

        MockMCPClient.side_effect = side_effect

        a = _stdio_config("srv_a")
        b = _stdio_config("srv_b")
        manager = McpManager(_settings(a, b))
        manager.connect_all()

        tools = manager.get_all_tools()
        assert len(tools) == 2
        names = {t.name for t in tools}
        assert "mcp__srv_a__tool_a" in names
        assert "mcp__srv_b__tool_b" in names

    def test_no_connected_servers(self):
        manager = McpManager(McpSettings(configs=[]))
        assert manager.get_all_tools() == []


# ---------------------------------------------------------------------------
# get_server_status
# ---------------------------------------------------------------------------

class TestGetServerStatus:

    @patch("src.mcp.manager.to_mcp_client_params")
    @patch("smolagents.MCPClient")
    def test_status_mixed(self, MockMCPClient, mock_params):
        mock_params.return_value = MagicMock()
        call_count = [0]
        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return _mock_mcp_client([_fake_tool()])
            raise RuntimeError("fail")

        MockMCPClient.side_effect = side_effect

        a = _stdio_config("ok_srv")
        b = _stdio_config("bad_srv")
        manager = McpManager(_settings(a, b))
        manager.connect_all()

        status = manager.get_server_status()
        assert status["ok_srv"]["connected"] is True
        assert status["ok_srv"]["tool_count"] == 1
        assert status["ok_srv"]["error"] is None

        assert status["bad_srv"]["connected"] is False
        assert status["bad_srv"]["tool_count"] == 0
        assert status["bad_srv"]["error"] is not None


# ---------------------------------------------------------------------------
# disconnect_all
# ---------------------------------------------------------------------------

class TestDisconnectAll:

    @patch("src.mcp.manager.to_mcp_client_params")
    @patch("smolagents.MCPClient")
    def test_disconnect_cleans_up(self, MockMCPClient, mock_params):
        mock_params.return_value = MagicMock()
        mock_client = _mock_mcp_client()
        MockMCPClient.return_value = mock_client

        cfg = _stdio_config("srv")
        manager = McpManager(_settings(cfg))
        manager.connect_all()
        assert "srv" in manager._clients

        manager.disconnect_all()
        assert "srv" not in manager._clients
        mock_client.disconnect.assert_called_once()

    def test_disconnect_idempotent(self):
        """Calling disconnect_all twice should not raise."""
        manager = McpManager(McpSettings(configs=[]))
        manager.disconnect_all()
        manager.disconnect_all()  # second call — should not error

    @patch("src.mcp.manager.to_mcp_client_params")
    @patch("smolagents.MCPClient")
    def test_disconnect_error_suppressed(self, MockMCPClient, mock_params):
        mock_params.return_value = MagicMock()
        mock_client = _mock_mcp_client()
        mock_client.disconnect.side_effect = RuntimeError("disconnect error")
        MockMCPClient.return_value = mock_client

        cfg = _stdio_config("srv")
        manager = McpManager(_settings(cfg))
        manager.connect_all()

        # Should not raise despite disconnect error.
        manager.disconnect_all()
        assert "srv" not in manager._clients


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------

class TestContextManager:

    @patch("src.mcp.manager.to_mcp_client_params")
    @patch("smolagents.MCPClient")
    def test_context_manager(self, MockMCPClient, mock_params):
        mock_params.return_value = MagicMock()
        MockMCPClient.return_value = _mock_mcp_client()

        cfg = _stdio_config("srv")
        settings = _settings(cfg)

        with McpManager(settings) as manager:
            assert "srv" in manager._clients
            tools = manager.get_all_tools()
            assert len(tools) == 1

        # After exit, clients should be disconnected.
        assert "srv" not in manager._clients
