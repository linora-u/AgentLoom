"""Actual installed Pi and MCP peer through the default Application resource owner."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.application_test.test_python_process_owner import _alive, _marker, _safety

pytestmark = pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="managed POSIX execution")


@pytest.mark.parametrize("mode", ["normal", "hang", "mcp-timeout", "mcp-cancel", "mcp-start-failure", "timeout"])
def test_default_application_owns_real_pi_mcp_and_tool_resources(tmp_path, mode):
    root = Path(__file__).resolve().parents[2]
    identities = []
    with (tmp_path / "host.log").open("wb") as log:
        host = subprocess.Popen([sys.executable, "-m", "test_support.managed_process_probe", str(tmp_path),
                                 "default-" + mode], cwd=root, stdout=log, stderr=log)
        try:
            evidence = _marker(tmp_path / "resources.json", host, identities)
            assert evidence["pi_handshake_method"] == "handshake"
            assert evidence["mcp_answer"] == 42
            for name in ("pi", "mcp"):
                identities.append({"pid": evidence[name + "_pid"], "creation": evidence[name + "_creation"]})
            descriptions = list(tmp_path.rglob("native_resources/*/description.json"))
            assert len(descriptions) >= 3, "Pi/MCP/tools must all use concrete native resource ownership"
            for description in descriptions:
                anchor = json.loads(description.read_text())["anchor"]
                identities.append({"pid": anchor["pid"], "creation": anchor["creation"]})
            if mode == "hang":
                os.kill(host.pid, signal.SIGKILL)
            else:
                deadline = time.monotonic() + 15
                while "outcome_status" not in evidence and time.monotonic() < deadline:
                    time.sleep(.02)
                    evidence = json.loads((tmp_path / "resources.json").read_text())
                assert evidence["outcome_status"] == "success", evidence
                if mode.startswith("mcp-") and mode != "mcp-start-failure":
                    assert evidence["mcp_call_errors"]
                if mode == "mcp-start-failure":
                    assert evidence["mcp_start_error"]
                if mode == "timeout":
                    assert evidence["tool_timeout_detected"]
            host.wait(timeout=15)
            assert host.returncode == (-signal.SIGKILL if mode == "hang" else 0)
            deadline = time.monotonic() + 10
            while any(_alive(identity) for identity in identities) and time.monotonic() < deadline:
                time.sleep(.02)
            assert not any(_alive(identity) for identity in identities), "Native resources survived the Application"
        finally:
            _safety(host, identities)
