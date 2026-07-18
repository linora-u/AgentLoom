from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path

import pytest

from src.lib.smolagents.hooks import HookContext, HookEvent, HookPlanCompiler, HookRun
from src.lib.smolagents.hooks.config import HookConfigLayer
from src.lib.smolagents.hooks.shell import ShellHookExecutionError


def _command(script: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


def _handler(tmp_path: Path, command: str, *, timeout: float = 2):
    layer = HookConfigLayer(
        "global",
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "id": "test.pre",
                        "command": command,
                        "timeout": timeout,
                    }
                ]
            }
        },
        tmp_path,
        tmp_path / "system.yaml",
        0,
    )
    return HookPlanCompiler().compile([layer]).handlers[0]


def _context(tmp_path: Path, *, payload: object | None = None) -> HookContext:
    return HookContext(
        local_run_id="local-123",
        root_run_id="root-456",
        cwd=str(tmp_path / "invocation"),
        project_root=str(tmp_path),
        runtime_agent_path="parent/worker",
        hook_event_name="PreToolUse",
        tool_name="write_file",
        tool_input={"payload": payload if payload is not None else "value"},
        tool_response={"result": "ok"},
        tool_inputs_schema={"type": "object"},
        step_number=7,
        task_id="task-1",
        sub_task_id="sub-2",
        agent_name="worker",
    )


def test_shell_executor_sends_exact_versioned_stdin_and_filters_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = tmp_path / "captured.json"
    script = tmp_path / "capture.py"
    script.write_text(
        """
import json
import os
import sys

payload = json.load(sys.stdin)
with open("captured.json", "w", encoding="utf-8") as stream:
    json.dump({"payload": payload, "has_secret": "OPENAI_API_KEY" in os.environ}, stream)
json.dump({"decision": "modify", "modified_input": {"normalized": True}, "telemetry": {"script": "ok"}}, sys.stdout)
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    handler = _handler(tmp_path, _command(script))

    result = handler.callback(_context(tmp_path))

    assert result is not None
    assert result.decision == "modify"
    assert result.modified_input == {"normalized": True}
    recorded = json.loads(captured.read_text(encoding="utf-8"))
    assert recorded["has_secret"] is False
    payload = recorded["payload"]
    assert set(payload) == {
        "schema_version",
        "hook_id",
        "hook_event_name",
        "local_run_id",
        "root_run_id",
        "agent_name",
        "runtime_agent_path",
        "task_id",
        "sub_task_id",
        "step_number",
        "project_root",
        "cwd",
        "tool_name",
        "tool_input",
        "tool_response",
        "tool_inputs_schema",
    }
    assert payload == {
        "schema_version": 1,
        "hook_id": "test.pre",
        "hook_event_name": "PreToolUse",
        "local_run_id": "local-123",
        "root_run_id": "root-456",
        "agent_name": "worker",
        "runtime_agent_path": "parent/worker",
        "task_id": "task-1",
        "sub_task_id": "sub-2",
        "step_number": 7,
        "project_root": str(tmp_path),
        "cwd": str(tmp_path / "invocation"),
        "tool_name": "write_file",
        "tool_input": {"payload": "value"},
        "tool_response": {"result": "ok"},
        "tool_inputs_schema": {"type": "object"},
    }
    assert result.telemetry["script"] == "ok"
    assert result.telemetry["shell_hook"]["hook_id"] == "test.pre"


def test_shell_executor_accepts_large_stdin_without_environment_transport(
    tmp_path: Path,
) -> None:
    script = tmp_path / "large.py"
    script.write_text(
        """
import json
import os
import sys

payload = json.load(sys.stdin)
assert len(payload["tool_input"]["payload"]) > 1_000_000
assert "HOOK_CONTEXT_JSON" not in os.environ
assert "HOOK_CONTEXT_JSON_FILE" not in os.environ
json.dump({"decision": "allow"}, sys.stdout)
""",
        encoding="utf-8",
    )
    handler = _handler(tmp_path, _command(script))

    result = handler.callback(_context(tmp_path, payload="x" * 1_100_000))

    assert result is not None
    assert result.decision == "allow"


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("", "must emit one JSON object"),
        ("[]", "must be a JSON object"),
        ('{"decision":"allow","unknown":true}', "unsupported field"),
        ('{"decision":"allow","user_message":1}', "user_message must be str"),
        ('{"decision":"invalid"}', "Unsupported hook decision"),
        ('{"decision":"modify"}', "modify requires modified_input"),
    ],
)
def test_shell_executor_rejects_invalid_stdout(
    tmp_path: Path,
    stdout: str,
    message: str,
) -> None:
    script = tmp_path / "invalid.py"
    script.write_text(
        f"import sys\nsys.stdin.read()\nsys.stdout.write({stdout!r})\n",
        encoding="utf-8",
    )
    handler = _handler(tmp_path, _command(script))

    with pytest.raises(ShellHookExecutionError, match=message):
        handler.callback(_context(tmp_path))


def test_shell_executor_rejects_nonzero_exit_even_with_json(tmp_path: Path) -> None:
    script = tmp_path / "failure.py"
    script.write_text(
        'import sys\nsys.stdin.read()\nsys.stdout.write(\'{"decision":"block"}\')\nsys.exit(2)\n',
        encoding="utf-8",
    )
    handler = _handler(tmp_path, _command(script))

    with pytest.raises(ShellHookExecutionError, match="exited with code 2"):
        handler.callback(_context(tmp_path))


def test_timeout_kills_background_descendants_before_delayed_side_effect(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "late.txt"
    script = tmp_path / "timeout.py"
    script.write_text(
        f"""
import subprocess
import sys
import time

sys.stdin.read()
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.35); open({str(marker)!r}, 'w').write('late')"], start_new_session=True)
time.sleep(5)
""",
        encoding="utf-8",
    )
    handler = _handler(tmp_path, _command(script), timeout=0.1)

    with pytest.raises(ShellHookExecutionError, match="timed out"):
        handler.callback(_context(tmp_path))
    time.sleep(0.55)

    assert not marker.exists()


def test_gate_runtime_fails_closed_on_shell_contract_error(tmp_path: Path) -> None:
    script = tmp_path / "empty.py"
    script.write_text("import sys\nsys.stdin.read()\n", encoding="utf-8")
    plan = HookPlanCompiler().compile(
        [
            HookConfigLayer(
                "global",
                {"hooks": {"PreToolUse": [{"id": "bad", "command": _command(script)}]}},
                tmp_path,
                tmp_path / "system.yaml",
                0,
            )
        ]
    )
    run = HookRun(plan, local_run_id="local", root_run_id="root")

    result = run.dispatch(HookEvent.PRE_TOOL_USE, "write_file", {})

    assert result.decision == "block"
    assert "must emit one JSON object" in (result.reason or "")
