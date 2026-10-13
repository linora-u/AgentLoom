"""Formal Pi CLI/native tool path inside a trusted OS process group."""
import json
import os
from pathlib import Path
import shlex
import signal
import sys

import pytest

from tests.pi_test.test_application import model_service, project
from tests.pi_test.test_tools_application import select
from tests.pi_test.test_process_lifecycle import CapturedCli, node_launcher

pytestmark = pytest.mark.skipif(os.name != "posix", reason="managed process groups require POSIX")


def start_managed_cli(root, application, env):
    code = (
        "from pathlib import Path\nimport sys\n"
        "from agentloom.config.config import bind_config,load_project_config\n"
        "from agentloom.execution.process_scope import bind_managed_process_scope\n"
        "from agentloom.__main__ import main\n"
        "with bind_managed_process_scope(),bind_config(load_project_config(Path(sys.argv[1]))):\n"
        " main(['run',sys.argv[2],'--no-file-log','--output-format','jsonl'])\n"
    )
    return CapturedCli([sys.executable, "-c", code, str(root), str(application)], root, env)


@pytest.mark.parametrize("scenario", ["success", "timeout", "signal"])
def test_formal_pi_cli_uses_managed_native_executor_and_preserves_receipts(tmp_path, scenario):
    marker = tmp_path / "native-group.json"
    command_code = "import os,json;from pathlib import Path;Path(" + repr(str(marker)) + ").write_text(json.dumps({'pid':os.getpid(),'group':os.getpgrp()}));print('managed-native-proof')"
    if scenario == "timeout":
        command_code += ";import time;time.sleep(60)"
    command = shlex.join([sys.executable, "-c", command_code])
    if scenario == "signal":
        command += ";kill -KILL $$"
    args = {"command": command, "timeout": 0.3 if scenario == "timeout" else 5}
    with model_service(turns=[[("managed-native", "bash", args)]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{"name": "bash"}], shell_settings={
            "allowed_commands": ["*"], "allowed_operators": ["*"], "sandbox": {"enabled": False},
        })
        env, bridge_marker = node_launcher(tmp_path)
        process = start_managed_cli(tmp_path, app, env)
        try:
            stdout, stderr = process.communicate(timeout=15)
        finally:
            if process.poll() is None:
                # Controller cleanup of its own isolated test group is never
                # the success oracle; a watchdog timeout remains a failure.
                import signal
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
    assert requests, stderr
    assert marker.exists(), stdout + stderr
    group = json.loads(marker.read_text())
    assert group["group"] == process.pid
    events = [json.loads(line) for line in stdout.splitlines()]
    journals = [json.loads(p.read_text()) for p in tmp_path.rglob("native-tools/**/*.json")]
    assert len(journals) == 1
    if scenario == "timeout":
        assert process.returncode != 0
        assert events[-1]["event"] == "run.failed"
        assert journals[0]["state"] == "uncertain"
    else:
        record = journals[0]["record"]
        assert process.returncode == 0, stderr
        assert events[-1]["event"] == "run.completed"
        assert record["status"] == ("completed" if scenario == "success" else "error")
        assert record["metadata"]["native"]["logical_name"] == "shell_tool"
        assert "managed-native-proof" in json.dumps(record)
    with pytest.raises(ProcessLookupError):
        os.kill(int(bridge_marker.read_text()), 0)


@pytest.mark.parametrize("scenario", ["stdin-eof", "background", "invalid-timeout", "active-descendant-timeout"])
def test_managed_pi_cli_preserves_public_sdk_shell_contract(tmp_path, scenario):
    marker = tmp_path / "must-not-execute"
    command = {
        "stdin-eof": "cat; printf stdin-eof",
        "background": "sleep 5 & printf foreground-done",
        "invalid-timeout": "printf executed > " + shlex.quote(str(marker)),
        "active-descendant-timeout": "(while true; do printf active; sleep 0.04; done) & wait",
    }[scenario]
    args = {"command": command, "timeout": 0 if scenario == "invalid-timeout" else 1}
    with model_service(turns=[[("shell-contract", "bash", args)]]) as (url, requests):
        app = project(tmp_path, url)
        select(app, tools=[{"name": "bash"}], shell_settings={
            "allowed_commands": ["*"], "allowed_operators": ["*"], "sandbox": {"enabled": False},
        })
        env, bridge_marker = node_launcher(tmp_path)
        process = start_managed_cli(tmp_path, app, env)
        try:
            stdout, stderr = process.communicate(timeout=15)
            assert requests, stderr
            journals = [json.loads(p.read_text()) for p in tmp_path.rglob("native-tools/**/*.json")]
            assert len(journals) == 1
            if scenario in {"invalid-timeout", "active-descendant-timeout"}:
                assert process.returncode != 0
                assert not marker.exists()
                assert journals[0]["state"] == "uncertain"
                if scenario == "invalid-timeout":
                    assert journals[0]["final_arguments"]["timeout"] == 0
                else:
                    assert "active" in journals[0]["capture"]["raw_output"]
            else:
                assert process.returncode == 0, stdout + stderr
                assert journals[0]["state"] == "committed"
                assert journals[0]["record"]["status"] == "completed"
                expected = "stdin-eof" if scenario == "stdin-eof" else "foreground-done"
                assert expected in json.dumps(journals[0])
            with pytest.raises(ProcessLookupError):
                os.kill(int(bridge_marker.read_text()), 0)
        finally:
            # This test checks SDK tool semantics, not OS-owned task drain.
            # Reap the intentional background sleeper in our isolated group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
