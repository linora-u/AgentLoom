"""Exercise real MCP discovery, calls and shutdown over child-process stdio."""

import json
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest
from agentloom.integrations.mcp.client import AgentLoomMCPClient
from agentloom.integrations.mcp.config import McpServerConfig, McpSettings
from agentloom.integrations.mcp.manager import McpManager
from mcp import StdioServerParameters

SERVER = Path(__file__).parent / "fixtures" / "stdio_server.py"


def test_connect_is_idempotent_and_disconnected_client_can_reconnect():
    client = AgentLoomMCPClient(StdioServerParameters(command=sys.executable, args=[str(SERVER)]))
    first = next(tool for tool in client.get_tools() if tool.definition.name == "lookup")
    pid = first.forward(query="first")["pid"]
    try:
        client.connect()
        again = next(tool for tool in client.get_tools() if tool.definition.name == "lookup")
        assert again.forward(query="again") == {"query": "again", "pid": pid, "calls": 2, "answer": 42}
        client.disconnect()
        assert not psutil.pid_exists(pid)
        client.disconnect()
        with pytest.raises(ValueError, match="connect"):
            client.get_tools()
        client.connect()
        fresh = next(tool for tool in client.get_tools() if tool.definition.name == "lookup")
        assert fresh.forward(query="fresh")["calls"] == 1
        with pytest.raises(RuntimeError, match="closed"):
            first.forward(query="stale binding")
    finally:
        client.disconnect()


def test_managers_isolate_sessions_and_closing_one_cancels_its_pending_call(tmp_path):
    events = tmp_path / "events.jsonl"
    settings = McpSettings(configs=[McpServerConfig(
        name="facts", type="stdio", command=sys.executable, args=[str(SERVER), str(events)],
    )])
    first = McpManager(settings)
    second = McpManager(settings)
    errors = []
    try:
        first.connect_all()
        second.connect_all()
        tools = {tool.definition.name: tool for tool in first.get_all_tools()}
        other = {tool.definition.name: tool for tool in second.get_all_tools()}
        pid = tools["mcp__facts__lookup"].forward(query="first")["pid"]
        other_pid = other["mcp__facts__lookup"].forward(query="second")["pid"]
        assert other_pid != pid
        first.connect_all()
        again = {tool.definition.name: tool for tool in first.get_all_tools()}
        assert again["mcp__facts__lookup"].forward(query="again")["pid"] == pid

        def pending_call():
            try:
                tools["mcp__facts__slow_lookup"].forward(query="cancel this call")
            except BaseException as error:
                errors.append(error)

        pending = threading.Thread(target=pending_call, daemon=True)
        pending.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if any(json.loads(line)["event"] == "slow_started" for line in events.read_text().splitlines()):
                break
            time.sleep(0.02)
        else:
            pytest.fail("MCP call did not reach the server")
        first.disconnect_all()
        pending.join(timeout=5)
        assert not pending.is_alive(), "Closing MCP must finish its pending calls"
        assert errors, "An interrupted tool must not report success"
        assert not psutil.pid_exists(pid)
        assert other["mcp__facts__lookup"].forward(query="still running") == {
            "query": "still running", "pid": other_pid, "calls": 2, "answer": 42,
        }
    finally:
        first.disconnect_all()
        second.disconnect_all()
    assert not psutil.pid_exists(other_pid)


def test_connection_timeout_releases_the_started_server(tmp_path):
    events = tmp_path / "timeout.jsonl"
    with pytest.raises(TimeoutError, match="connecting"):
        AgentLoomMCPClient(
            StdioServerParameters(command=sys.executable, args=[str(SERVER), str(events), "--stall"]),
            adapter_kwargs={"connect_timeout": 1.5},
        )
    pid = json.loads(events.read_text().splitlines()[0])["pid"]
    assert not psutil.pid_exists(pid)


def test_yaml_tool_loading_rejects_duplicate_mcp_names_and_closes_both_servers(tmp_path):
    from agentloom.runtime.factory import YamlAgentFactory

    events = tmp_path / "duplicates.jsonl"
    configuration = tmp_path / "mcp.json"
    server = {"command": sys.executable, "args": [str(SERVER), str(events)]}
    configuration.write_text(json.dumps({"mcpServers": {"one": server, "two": server}}))
    manager = None
    try:
        with pytest.raises(ValueError, match="Duplicate tool name: lookup"):
            _, manager = YamlAgentFactory.get_tools_from_config(
                {"tools": [], "mcp_servers": {"path": str(configuration), "tool_name_prefix": False}},
                effective_agent_config={"default_toolsets": []},
            )
    finally:
        if manager is not None:
            manager.disconnect_all()
    pids = {json.loads(line)["pid"] for line in events.read_text().splitlines()}
    assert len(pids) == 2
    assert not any(psutil.pid_exists(pid) for pid in pids)
