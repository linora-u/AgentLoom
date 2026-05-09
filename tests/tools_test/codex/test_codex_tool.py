import os
import stat
from pathlib import Path

import pytest

from src.lib.config import C
from src.tools.codex.codex_tool import CodexExecRunner, CodexExecSettings


def _install_fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "args.txt"
    stdin_file = tmp_path / "stdin.txt"
    fake = bin_dir / "codex"
    fake.write_text(
        f"""#!/usr/bin/env bash
echo "CALL:$*" >> "{args_file}"
if [[ "$1" == "--version" ]]; then
  echo "codex-cli 9.9.9"
  exit 0
fi
if [[ "$1" == "login" && "$2" == "status" ]]; then
  if [[ "$FAKE_CODEX_LOGIN_FAIL" == "1" ]]; then
    echo "not logged in" >&2
    exit 1
  fi
  echo "Logged in"
  exit 0
fi
if [[ "$1" == "--search" ]]; then
  shift
fi
if [[ "$1" == "exec" ]]; then
  cat > "{stdin_file}"
  if [[ -n "$FAKE_CODEX_SLEEP" ]]; then
    sleep "$FAKE_CODEX_SLEEP"
  fi
  if [[ -n "$FAKE_CODEX_EXIT" ]]; then
    echo "codex failed" >&2
    exit "$FAKE_CODEX_EXIT"
  fi
  if [[ "$FAKE_CODEX_BIG" == "1" ]]; then
    python - <<'PY'
import json
print(json.dumps({{"type": "agent_message", "message": "X" * 50}}))
PY
    exit 0
  fi
  echo '{{"type":"agent_message","message":"Codex final output"}}'
  exit 0
fi
echo "unexpected invocation: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return fake, args_file, stdin_file


def _exec_call(args_file: Path) -> str:
    calls = args_file.read_text(encoding="utf-8").splitlines()
    matches = [
        line for line in calls
        if line.startswith("CALL:exec ") or line.startswith("CALL:--search exec ")
    ]
    assert matches
    return matches[-1]


def test_missing_runtime_returns_runtime_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    result = CodexExecRunner().run("hello", cwd=str(tmp_path))

    assert result["success"] is False
    assert result["error"]["type"] == "RuntimeNotFound"


def test_login_status_failure_returns_auth_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_codex(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CODEX_LOGIN_FAIL", "1")

    result = CodexExecRunner().run("hello", cwd=str(tmp_path))

    assert result["success"] is False
    assert result["error"]["type"] == "AuthRequired"
    assert "not logged in" in result["logs"]


def test_sandbox_empty_and_search_false_are_not_forwarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _fake, args_file, _stdin_file = _install_fake_codex(tmp_path, monkeypatch)

    result = CodexExecRunner().run("hello", cwd=str(tmp_path), sandbox="", search="false")

    assert result["success"] is True
    exec_call = _exec_call(args_file)
    assert "--sandbox" not in exec_call
    assert "--search" not in exec_call
    assert "--ask-for-approval" not in exec_call


@pytest.mark.parametrize("sandbox", ["read-only", "workspace-write", "danger-full-access"])
def test_sandbox_values_are_forwarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox: str):
    _fake, args_file, _stdin_file = _install_fake_codex(tmp_path, monkeypatch)

    result = CodexExecRunner().run("hello", cwd=str(tmp_path), sandbox=sandbox)

    assert result["success"] is True
    assert f"--sandbox {sandbox}" in _exec_call(args_file)


def test_invalid_sandbox_returns_invalid_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_codex(tmp_path, monkeypatch)

    result = CodexExecRunner().run("hello", cwd=str(tmp_path), sandbox="restricted")

    assert result["success"] is False
    assert result["error"]["type"] == "InvalidSandbox"


def test_search_true_is_forwarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _fake, args_file, _stdin_file = _install_fake_codex(tmp_path, monkeypatch)

    result = CodexExecRunner().run("hello", cwd=str(tmp_path), search="true")

    assert result["success"] is True
    assert "--search" in _exec_call(args_file)


def test_invalid_search_flag_returns_invalid_search_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_codex(tmp_path, monkeypatch)

    result = CodexExecRunner().run("hello", cwd=str(tmp_path), search="yes")

    assert result["success"] is False
    assert result["error"]["type"] == "InvalidSearchFlag"


def test_missing_cwd_returns_cwd_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_codex(tmp_path, monkeypatch)

    result = CodexExecRunner().run("hello", cwd=str(tmp_path / "missing"))

    assert result["success"] is False
    assert result["error"]["type"] == "CwdRejected"


def test_success_jsonl_becomes_success_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _fake, _args_file, stdin_file = _install_fake_codex(tmp_path, monkeypatch)

    result = CodexExecRunner().run("hello from AgentLoom", cwd=str(tmp_path))

    assert result["success"] is True
    assert result["output"] == "Codex final output"
    assert result["metadata"]["codex_version"] == "codex-cli 9.9.9"
    assert stdin_file.read_text(encoding="utf-8") == "hello from AgentLoom"


def test_default_cwd_uses_agentloom_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _fake, args_file, _stdin_file = _install_fake_codex(tmp_path, monkeypatch)

    result = CodexExecRunner().run("hello")

    assert result["success"] is True
    exec_call = _exec_call(args_file)
    assert f"--cd {C.agent_root}" in exec_call
    assert "--search" not in exec_call


def test_nonzero_exit_returns_execution_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_codex(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CODEX_EXIT", "7")

    result = CodexExecRunner().run("hello", cwd=str(tmp_path))

    assert result["success"] is False
    assert result["error"]["type"] == "ExecutionError"
    assert result["metadata"]["exit_code"] == 7


def test_timeout_returns_timeout_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_codex(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "2")

    result = CodexExecRunner().run("hello", cwd=str(tmp_path), timeout="1")

    assert result["success"] is False
    assert result["error"]["type"] == "TimeoutError"


def test_output_truncation_marks_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_codex(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CODEX_BIG", "1")
    settings = CodexExecSettings(max_output_chars=10)

    result = CodexExecRunner(settings=settings).run("hello", cwd=str(tmp_path))

    assert result["success"] is True
    assert result["output"] == "XXXXXXXXXX"
    assert result["metadata"]["truncated"] is True
