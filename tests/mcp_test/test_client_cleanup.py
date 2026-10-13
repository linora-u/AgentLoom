"""A real MCP connection must expose cleanup failures to its public caller."""
import json
import os
from pathlib import Path
import subprocess
import sys


def test_managed_disconnect_retains_transport_cleanup_failure(tmp_path):
    root = Path(__file__).resolve().parents[2]
    peer = root / "tests/mcp_test/fixtures/stdio_server.py"
    code = '''
from contextlib import asynccontextmanager
import json,os,sys
import psutil
from mcp import StdioServerParameters
from agentloom.execution.process_scope import bind_managed_process_scope
from agentloom.integrations.mcp.client import AgentLoomMCPClient
from agentloom.integrations.mcp import managed_stdio
original = managed_stdio.managed_stdio_tools
@asynccontextmanager
async def failing_cleanup(*args):
    async with original(*args) as connection:
        try:
            yield connection
        finally:
            raise RuntimeError("managed transport cleanup failed")
managed_stdio.managed_stdio_tools = failing_cleanup
def error_text(error):
    return str(error) + " " + " ".join(error_text(e) for e in getattr(error,"exceptions",()))
with bind_managed_process_scope():
    client = AgentLoomMCPClient(StdioServerParameters(command=sys.executable,args=[sys.argv[1]]))
    lookup = next(t for t in client.get_tools() if t.definition.name == "lookup")
    pid = lookup.forward(query="cleanup failure")["pid"]
    try:
        client.disconnect()
    except BaseException as error:
        result = {"error":error_text(error),"peer_alive":psutil.pid_exists(pid),"root_group":os.getpgrp()}
    else:
        raise AssertionError("disconnect hid the transport cleanup failure")
    client.disconnect()
print(json.dumps(result))
'''
    child = subprocess.run([sys.executable, "-c", code, str(peer)], cwd=root,
                           capture_output=True, text=True, timeout=15, start_new_session=True)
    assert child.returncode == 0, child.stdout + child.stderr
    result = json.loads(child.stdout)
    assert "managed transport cleanup failed" in result["error"]
    assert result["peer_alive"] is False


def test_managed_malformed_frame_retains_original_parse_diagnostic(tmp_path):
    root = Path(__file__).resolve().parents[2]
    peer = tmp_path / "invalid_peer.py"
    peer.write_text("import sys,time\nprint('not-json',flush=True)\ntime.sleep(60)\n")
    code = '''
import sys
from mcp import StdioServerParameters
from agentloom.execution.process_scope import bind_managed_process_scope
from agentloom.integrations.mcp.client import AgentLoomMCPClient
with bind_managed_process_scope():
    try:
        AgentLoomMCPClient(StdioServerParameters(command=sys.executable,args=[sys.argv[1]]),
                          adapter_kwargs={"client_session_timeout_seconds":0.25,"connect_timeout":2})
    except Exception:
        pass
    else:
        raise AssertionError("malformed-only peer unexpectedly initialized")
'''
    child = subprocess.run([sys.executable, "-c", code, str(peer)], cwd=root,
                           capture_output=True, text=True, timeout=15, start_new_session=True)
    assert child.returncode == 0, child.stdout + child.stderr
    assert "Failed to parse JSONRPC message from server" in child.stderr
    assert "ValidationError" in child.stderr and "not-json" in child.stderr
