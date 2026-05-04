"""Unit tests for src.mcp.config — JSON loading, path resolution, parsing, merging."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from src.mcp.config import (
    McpServerConfig,
    McpSettings,
    load_mcp_json,
    merge_mcp_configs,
    parse_mcp_servers_from_json,
    parse_mcp_yaml_value,
    resolve_mcp_json_path,
    sanitize_server_name,
    to_mcp_client_params,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_root(tmp_path):
    """Create a temporary agent root directory."""
    return tmp_path


@pytest.fixture
def valid_mcp_json(tmp_path):
    """Create a valid .mcp.json file with stdio + sse servers."""
    data = {
        "mcpServers": {
            "filesystem": {
                "type": "stdio",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
                "env": {"NODE_ENV": "production"},
            },
            "web-search": {
                "type": "sse",
                "url": "http://localhost:8080/mcp",
                "headers": {"Authorization": "Bearer token123"},
            },
        }
    }
    path = tmp_path / "config" / ".mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def http_mcp_json(tmp_path):
    """Create a .mcp.json with http type server."""
    data = {
        "mcpServers": {
            "remote-api": {
                "type": "http",
                "url": "https://api.example.com/mcp",
            }
        }
    }
    path = tmp_path / "http.mcp.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# resolve_mcp_json_path
# ---------------------------------------------------------------------------

class TestResolveMcpJsonPath:

    def test_relative_path(self, agent_root):
        result = resolve_mcp_json_path("config/.mcp.json", agent_root)
        expected = (agent_root / "config" / ".mcp.json").resolve()
        assert result == expected

    def test_absolute_path(self, agent_root):
        abs_path = "/opt/mcp/config.json"
        result = resolve_mcp_json_path(abs_path, agent_root)
        assert result == Path(abs_path)

    def test_tilde_expansion(self, agent_root):
        result = resolve_mcp_json_path("~/mcp/config.json", agent_root)
        assert result == Path.home() / "mcp" / "config.json"


# ---------------------------------------------------------------------------
# load_mcp_json
# ---------------------------------------------------------------------------

class TestLoadMcpJson:

    def test_valid_json(self, valid_mcp_json):
        data = load_mcp_json(valid_mcp_json)
        assert "mcpServers" in data
        assert "filesystem" in data["mcpServers"]
        assert "web-search" in data["mcpServers"]

    def test_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="not found"):
            load_mcp_json(tmp_path / "nonexistent.json")

    def test_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("not json {{{", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON"):
            load_mcp_json(bad)

    def test_not_a_dict(self, tmp_path):
        arr = tmp_path / "array.json"
        arr.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError, match="Expected JSON object"):
            load_mcp_json(arr)

    def test_missing_mcp_servers_key(self, tmp_path):
        no_key = tmp_path / "nokey.json"
        no_key.write_text('{"other": {}}', encoding="utf-8")
        with pytest.raises(ValueError, match="Missing 'mcpServers'"):
            load_mcp_json(no_key)


# ---------------------------------------------------------------------------
# parse_mcp_servers_from_json
# ---------------------------------------------------------------------------

class TestParseMcpServersFromJson:

    def test_stdio_server(self):
        data = {
            "mcpServers": {
                "fs": {
                    "type": "stdio",
                    "command": "npx",
                    "args": ["-y", "fs-server"],
                    "env": {"KEY": "val"},
                }
            }
        }
        configs = parse_mcp_servers_from_json(data)
        assert len(configs) == 1
        c = configs[0]
        assert c.name == "fs"
        assert c.type == "stdio"
        assert c.command == "npx"
        assert c.args == ["-y", "fs-server"]
        assert c.env == {"KEY": "val"}

    def test_sse_server(self):
        data = {
            "mcpServers": {
                "search": {
                    "type": "sse",
                    "url": "http://localhost:8080/mcp",
                    "headers": {"Auth": "Bearer x"},
                }
            }
        }
        configs = parse_mcp_servers_from_json(data)
        assert len(configs) == 1
        c = configs[0]
        assert c.name == "search"
        assert c.type == "sse"
        assert c.url == "http://localhost:8080/mcp"
        assert c.headers == {"Auth": "Bearer x"}

    def test_http_normalized_to_streamable_http(self):
        data = {"mcpServers": {"api": {"type": "http", "url": "https://x.com/mcp"}}}
        configs = parse_mcp_servers_from_json(data)
        assert configs[0].type == "streamable-http"

    def test_default_type_is_stdio(self):
        """When 'type' is omitted, default to stdio."""
        data = {"mcpServers": {"tool": {"command": "mytool"}}}
        configs = parse_mcp_servers_from_json(data)
        assert configs[0].type == "stdio"
        assert configs[0].command == "mytool"

    def test_missing_command_for_stdio(self):
        """Missing command for stdio should be skipped with warning."""
        data = {"mcpServers": {"bad": {"type": "stdio"}}}
        configs = parse_mcp_servers_from_json(data)
        assert len(configs) == 0

    def test_missing_url_for_sse(self):
        """Missing url for sse should be skipped with warning."""
        data = {"mcpServers": {"bad": {"type": "sse"}}}
        configs = parse_mcp_servers_from_json(data)
        assert len(configs) == 0

    def test_unknown_transport_type(self):
        """Unsupported type should be skipped."""
        data = {"mcpServers": {"bad": {"type": "ws", "url": "ws://x"}}}
        configs = parse_mcp_servers_from_json(data)
        assert len(configs) == 0

    def test_non_dict_server_entry_skipped(self):
        data = {"mcpServers": {"bad": "not_a_dict"}}
        configs = parse_mcp_servers_from_json(data)
        assert len(configs) == 0

    def test_non_dict_mcp_servers_value(self):
        data = {"mcpServers": [1, 2, 3]}
        configs = parse_mcp_servers_from_json(data)
        assert len(configs) == 0

    def test_multiple_servers(self):
        data = {
            "mcpServers": {
                "a": {"type": "stdio", "command": "cmd_a"},
                "b": {"type": "sse", "url": "http://b/mcp"},
            }
        }
        configs = parse_mcp_servers_from_json(data)
        assert len(configs) == 2


# ---------------------------------------------------------------------------
# sanitize_server_name
# ---------------------------------------------------------------------------

class TestSanitizeServerName:

    def test_alphanumeric_unchanged(self):
        assert sanitize_server_name("myServer123") == "myServer123"

    def test_dashes_replaced(self):
        assert sanitize_server_name("web-search") == "web_search"

    def test_dots_replaced(self):
        assert sanitize_server_name("com.example.tool") == "com_example_tool"

    def test_spaces_replaced(self):
        assert sanitize_server_name("my tool") == "my_tool"


# ---------------------------------------------------------------------------
# parse_mcp_yaml_value
# ---------------------------------------------------------------------------

class TestParseMcpYamlValue:

    def test_none_returns_none(self, agent_root):
        assert parse_mcp_yaml_value(None, agent_root) is None

    def test_empty_string_returns_none(self, agent_root):
        assert parse_mcp_yaml_value("", agent_root) is None

    def test_string_path(self, valid_mcp_json, agent_root):
        # Place the JSON file relative to agent_root
        rel = valid_mcp_json.relative_to(agent_root)
        settings = parse_mcp_yaml_value(str(rel), agent_root)
        assert settings is not None
        assert len(settings.configs) == 2

    def test_list_of_paths(self, valid_mcp_json, http_mcp_json, agent_root):
        rel1 = valid_mcp_json.relative_to(agent_root)
        rel2 = http_mcp_json.relative_to(agent_root)
        settings = parse_mcp_yaml_value([str(rel1), str(rel2)], agent_root)
        assert settings is not None
        assert len(settings.configs) == 3  # 2 from valid + 1 from http

    def test_dict_with_path(self, valid_mcp_json, agent_root):
        rel = valid_mcp_json.relative_to(agent_root)
        raw = {
            "path": str(rel),
            "timeout": 15,
            "tool_timeout": 45,
            "tool_name_prefix": False,
        }
        settings = parse_mcp_yaml_value(raw, agent_root)
        assert settings is not None
        assert settings.timeout == 15
        assert settings.tool_timeout == 45
        assert settings.tool_name_prefix is False
        assert len(settings.configs) == 2

    def test_dict_with_paths(self, valid_mcp_json, http_mcp_json, agent_root):
        rel1 = valid_mcp_json.relative_to(agent_root)
        rel2 = http_mcp_json.relative_to(agent_root)
        raw = {"paths": [str(rel1), str(rel2)]}
        settings = parse_mcp_yaml_value(raw, agent_root)
        assert settings is not None
        assert len(settings.configs) == 3

    def test_dict_without_path_returns_none(self, agent_root):
        assert parse_mcp_yaml_value({"timeout": 10}, agent_root) is None

    def test_unsupported_type_returns_none(self, agent_root):
        assert parse_mcp_yaml_value(42, agent_root) is None

    def test_nonexistent_file_returns_empty_configs(self, agent_root):
        settings = parse_mcp_yaml_value("nonexistent/.mcp.json", agent_root)
        assert settings is not None
        assert len(settings.configs) == 0

    def test_empty_list_returns_none(self, agent_root):
        assert parse_mcp_yaml_value([], agent_root) is None


# ---------------------------------------------------------------------------
# merge_mcp_configs
# ---------------------------------------------------------------------------

class TestMergeMcpConfigs:

    def _make_settings(self, servers: dict[str, str], **kwargs) -> McpSettings:
        configs = [
            McpServerConfig(name=name, type="stdio", command=cmd)
            for name, cmd in servers.items()
        ]
        return McpSettings(configs=configs, **kwargs)

    def test_both_none(self):
        assert merge_mcp_configs(None, None) is None

    def test_global_only(self):
        g = self._make_settings({"a": "cmd_a"})
        result = merge_mcp_configs(g, None)
        assert result is g

    def test_agent_only(self):
        a = self._make_settings({"b": "cmd_b"})
        result = merge_mcp_configs(None, a)
        assert result is a

    def test_agent_overrides_global(self):
        g = self._make_settings({"srv": "old_cmd"})
        a = self._make_settings({"srv": "new_cmd"})
        result = merge_mcp_configs(g, a)
        assert len(result.configs) == 1
        assert result.configs[0].command == "new_cmd"

    def test_agent_adds_new_server(self):
        g = self._make_settings({"srv_a": "cmd_a"})
        a = self._make_settings({"srv_b": "cmd_b"})
        result = merge_mcp_configs(g, a)
        names = {c.name for c in result.configs}
        assert names == {"srv_a", "srv_b"}

    def test_agent_options_take_precedence(self):
        g = self._make_settings({"a": "cmd"}, timeout=10, tool_timeout=30, tool_name_prefix=True)
        a = self._make_settings({}, timeout=20, tool_timeout=90, tool_name_prefix=False)
        result = merge_mcp_configs(g, a)
        assert result.timeout == 20
        assert result.tool_timeout == 90
        assert result.tool_name_prefix is False


# ---------------------------------------------------------------------------
# to_mcp_client_params
# ---------------------------------------------------------------------------

class TestToMcpClientParams:

    def test_stdio_params(self):
        cfg = McpServerConfig(
            name="fs", type="stdio", command="npx",
            args=["-y", "fs-server"], env={"K": "V"},
        )
        params = to_mcp_client_params(cfg)
        # Should be StdioServerParameters
        assert hasattr(params, "command")
        assert params.command == "npx"
        assert params.args == ["-y", "fs-server"]
        assert params.env == {"K": "V"}

    def test_stdio_params_no_env(self):
        cfg = McpServerConfig(name="fs", type="stdio", command="npx")
        params = to_mcp_client_params(cfg)
        assert params.command == "npx"

    def test_sse_params(self):
        cfg = McpServerConfig(
            name="search", type="sse",
            url="http://localhost:8080/mcp",
            headers={"Auth": "x"},
        )
        params = to_mcp_client_params(cfg)
        assert isinstance(params, dict)
        assert params["url"] == "http://localhost:8080/mcp"
        assert params["transport"] == "sse"
        assert params["headers"] == {"Auth": "x"}

    def test_streamable_http_params(self):
        cfg = McpServerConfig(
            name="api", type="streamable-http",
            url="https://api.example.com/mcp",
        )
        params = to_mcp_client_params(cfg)
        assert isinstance(params, dict)
        assert params["transport"] == "streamable-http"

    def test_http_no_headers(self):
        cfg = McpServerConfig(
            name="api", type="streamable-http",
            url="https://api.example.com/mcp",
        )
        params = to_mcp_client_params(cfg)
        assert "headers" not in params
