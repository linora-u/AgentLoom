"""Resource ownership through a real Application in an isolated OS job."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from uuid import uuid4

import psutil
import pytest


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError("managed resource acceptance watchdog")


@pytest.mark.skipif(sys.platform != "darwin", reason="actual macOS launchd ownership acceptance")
@pytest.mark.parametrize("mode", ["normal", "timeout", "crash", "mcp-timeout", "mcp-cancel", "mcp-start-failure"])
def test_application_pi_mcp_and_tools_share_os_scope_and_exit(tmp_path, mode):
    label = "dev.agentloom.test." + uuid4().hex
    domain = f"gui/{os.getuid()}"
    project_root = Path(__file__).resolve().parents[2]
    plist = tmp_path / "job.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "test_support.managed_process_probe", str(tmp_path), mode],
        "WorkingDirectory": str(project_root),
        "EnvironmentVariables": {"PYTHONPATH": str(project_root), "PATH": os.environ["PATH"]},
        "RunAtLoad": True, "KeepAlive": False, "AbandonProcessGroup": False,
        "StandardOutPath": str(tmp_path / "stdout.log"), "StandardErrorPath": str(tmp_path / "stderr.log"),
    }))
    r = subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    evidence = None
    try:
        path = tmp_path / "resources.json"
        wait_for(path.exists)
        evidence = json.loads(path.read_text())
        assert "pi_handshake_method" in evidence, evidence
        assert evidence["pi_handshake_method"] == "handshake"
        assert evidence["mcp_answer"] == 42
        assert evidence["root_group"] == evidence["root_pid"]
        assert evidence["pi_group"] == evidence["mcp_group"] == evidence["tool_group"] == evidence["root_group"]
        if mode != "crash":
            wait_for(lambda: "outcome_status" in json.loads(path.read_text()))
            evidence = json.loads(path.read_text())
            assert evidence["outcome_status"] == "success"
            assert evidence["application_still_alive_after_tool"] is True
        if mode == "timeout":
            assert evidence["tool_timeout_detected"] is True
            assert evidence["timeout_child"]["group"] == evidence["root_group"]
        if mode in {"mcp-timeout", "mcp-cancel"}:
            assert evidence["mcp_call_errors"]
        if mode == "mcp-start-failure":
            assert evidence["mcp_start_error"]
        pids = [evidence["root_pid"], evidence["pi_pid"], evidence["mcp_pid"]]
        if "timeout_child" in evidence:
            pids.append(evidence["timeout_child"]["pid"])
        wait_for(lambda: all(not psutil.pid_exists(pid) for pid in pids))
        assert not any(psutil.pid_exists(pid) for pid in pids)
    finally:
        r = subprocess.run(["/bin/launchctl", "bootout", domain + "/" + label], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
