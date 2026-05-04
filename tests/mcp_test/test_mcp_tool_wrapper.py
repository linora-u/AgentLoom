"""Unit tests for src.mcp.tool_wrapper — name prefixing, sanitization, description enrichment."""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest

from src.mcp.config import McpSettings, McpServerConfig
from src.mcp.tool_wrapper import wrap_mcp_tools, _sanitize_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_tool(name: str, description: str = "A tool") -> MagicMock:
    """Create a mock tool that behaves like a smolagents.Tool."""
    tool = MagicMock()
    tool.name = name
    tool.description = description
    return tool


def _make_settings(prefix: bool = True) -> McpSettings:
    return McpSettings(tool_name_prefix=prefix)


# ---------------------------------------------------------------------------
# _sanitize_name
# ---------------------------------------------------------------------------

class TestSanitizeName:

    def test_alphanumeric(self):
        assert _sanitize_name("abc123") == "abc123"

    def test_dashes(self):
        assert _sanitize_name("web-search") == "web_search"

    def test_dots(self):
        assert _sanitize_name("com.example") == "com_example"

    def test_mixed(self):
        assert _sanitize_name("my-tool.v2!") == "my_tool_v2_"


# ---------------------------------------------------------------------------
# wrap_mcp_tools — prefix enabled
# ---------------------------------------------------------------------------

class TestWrapMcpToolsPrefixEnabled:

    def test_name_prefix_applied(self):
        tools = [_make_fake_tool("read_file"), _make_fake_tool("write_file")]
        settings = _make_settings(prefix=True)
        wrapped = wrap_mcp_tools("filesystem", tools, settings)

        assert len(wrapped) == 2
        assert wrapped[0].name == "mcp__filesystem__read_file"
        assert wrapped[1].name == "mcp__filesystem__write_file"

    def test_server_name_sanitized(self):
        tools = [_make_fake_tool("query")]
        settings = _make_settings(prefix=True)
        wrapped = wrap_mcp_tools("web-search", tools, settings)
        assert wrapped[0].name == "mcp__web_search__query"

    def test_description_enriched(self):
        tools = [_make_fake_tool("query", description="Search the web")]
        settings = _make_settings(prefix=True)
        wrapped = wrap_mcp_tools("search", tools, settings)
        assert wrapped[0].description == "[MCP:search] Search the web"

    def test_no_double_enrichment(self):
        """If description already has origin hint, don't add again."""
        tools = [_make_fake_tool("q", description="[MCP:search] Already tagged")]
        settings = _make_settings(prefix=True)
        wrapped = wrap_mcp_tools("search", tools, settings)
        assert wrapped[0].description == "[MCP:search] Already tagged"

    def test_original_not_mutated(self):
        original = _make_fake_tool("read_file", "Read a file")
        settings = _make_settings(prefix=True)
        wrapped = wrap_mcp_tools("fs", [original], settings)
        # The wrapped tool should be a different object (copy).
        assert wrapped[0] is not original


# ---------------------------------------------------------------------------
# wrap_mcp_tools — prefix disabled
# ---------------------------------------------------------------------------

class TestWrapMcpToolsPrefixDisabled:

    def test_original_name_kept(self):
        tools = [_make_fake_tool("read_file")]
        settings = _make_settings(prefix=False)
        wrapped = wrap_mcp_tools("filesystem", tools, settings)
        assert wrapped[0].name == "read_file"

    def test_description_still_enriched(self):
        tools = [_make_fake_tool("query", "Search")]
        settings = _make_settings(prefix=False)
        wrapped = wrap_mcp_tools("search", tools, settings)
        assert wrapped[0].description.startswith("[MCP:search]")


# ---------------------------------------------------------------------------
# wrap_mcp_tools — edge cases
# ---------------------------------------------------------------------------

class TestWrapMcpToolsEdgeCases:

    def test_empty_tools_list(self):
        wrapped = wrap_mcp_tools("srv", [], _make_settings())
        assert wrapped == []

    def test_tool_with_empty_description(self):
        tools = [_make_fake_tool("t", description="")]
        wrapped = wrap_mcp_tools("s", tools, _make_settings())
        assert "[MCP:s]" in wrapped[0].description

    def test_tool_with_none_description(self):
        tool = _make_fake_tool("t")
        tool.description = None
        wrapped = wrap_mcp_tools("s", [tool], _make_settings())
        assert "[MCP:s]" in wrapped[0].description
