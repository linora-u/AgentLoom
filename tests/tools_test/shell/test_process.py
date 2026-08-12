"""Tests for ShellProcess — stateless subprocess execution engine.

Covers:
- Standalone mode (unified subprocess executor)
- Session-scoped mode (subprocess.Popen with session state)
- Timeout handling
- Real command execution
- Session-scoped state (CWD persists, env exports are ephemeral)
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from src.tools.shell.process import (
    AGENT_SHELL_PROMPT_ENV,
    ShellProcess,
)


def test_internal_prompt_env_name():
    """Legacy constant is preserved for backward compatibility."""
    assert AGENT_SHELL_PROMPT_ENV == "AGENT_SHELL_PROMPT"


def test_shell_process_standalone():
    """Test standard subprocess output without session state."""
    proc = ShellProcess(session_scoped=False, load_profile=False)
    result = proc.run("echo standalone success")

    assert "standalone success" in result


def test_shell_process_timeout():
    """Test standalone timeout when auto-background is disabled."""
    with patch("src.tools.shell.process.C") as mock_c:
        mock_c.get_nested = MagicMock(side_effect=lambda *args, **kwargs: {
            ("shell_settings", "background_tasks", "enabled"): False,
            ("shell_settings", "background_tasks",
             "auto_background_on_timeout"): False,
            ("shell_settings", "background_tasks",
             "stall_threshold_seconds"): 45,
        }.get(args, kwargs.get("default", None)))

        proc = ShellProcess(session_scoped=False, timeout=2)
        result = proc.run("echo partial timeout && sleep 100")

    assert "partial timeout" in result
    assert "Timeout Error" in result


def test_shell_process_real_ls():
    """Test actual execution of an ls command."""
    proc = ShellProcess(session_scoped=False)
    result = proc.run("ls -la")
    # since we run this from AgentLoom root, we should see common files
    assert "pyproject.toml" in result or "test_" in result


def test_shell_process_real_python_execution():
    """Test executing a python one-liner."""
    proc = ShellProcess(session_scoped=False)
    result = proc.run("python3 -c \"print('hello from python')\"")
    assert "hello from python" in result


def test_shell_process_marks_spawn_failure_as_interrupted(monkeypatch):
    proc = ShellProcess(session_scoped=False, load_profile=False)
    monkeypatch.setattr(proc, "_exec_subprocess", MagicMock(side_effect=OSError("spawn unavailable")))

    result = proc.execute("echo unreachable")

    assert result.interrupted is True
    assert "spawn unavailable" in result.output


@pytest.mark.skipif(
    not sys.platform.startswith("linux") and not sys.platform.startswith("darwin"),
    reason="Unix specific test",
)
def test_shell_process_session_scoped_state_real():
    """Test that CWD persists but env exports are ephemeral."""
    proc = ShellProcess(session_scoped=True, load_profile=False)
    try:
        # CWD should persist across calls.
        proc.run("cd /tmp")
        result = proc.run("pwd")
        assert result.strip() == os.path.normpath("/tmp")

        # Environment exports are ephemeral — they do NOT persist.
        # This matches the stateless subprocess design.
        proc.run("export TEST_VAR=42")
        result = proc.run("echo $TEST_VAR")
        # TEST_VAR should be empty (not persisted).
        assert "42" not in result
    finally:
        proc.cleanup()


@pytest.mark.skipif(
    not sys.platform.startswith("linux") and not sys.platform.startswith("darwin"),
    reason="Unix specific test",
)
def test_shell_process_session_scoped_cwd_real():
    """Test that cd changes the session working directory."""
    proc = ShellProcess(session_scoped=True, load_profile=False)
    try:
        proc.run("cd /tmp")
        result = proc.run("pwd")
        assert result.strip() == os.path.normpath("/tmp")
    finally:
        proc.cleanup()


@pytest.mark.skipif(
    not sys.platform.startswith("linux") and not sys.platform.startswith("darwin"),
    reason="Unix specific test",
)
def test_shell_process_session_scoped_cleanup():
    """Verify cleanup removes session temp files."""
    proc = ShellProcess(session_scoped=True, load_profile=False)
    proc.run("echo init")

    # Session should exist
    assert proc._session is not None
    state_dir = proc._session._state_dir
    assert state_dir is not None

    proc.cleanup()
    assert proc._session is None


@pytest.mark.skipif(
    not sys.platform.startswith("linux") and not sys.platform.startswith("darwin"),
    reason="Unix specific test",
)
def test_shell_process_session_scoped_ephemeral_env():
    """Test that inline exports work but do NOT persist across calls."""
    proc = ShellProcess(session_scoped=True, load_profile=False)
    try:
        # Inline export + echo in the SAME command works.
        result = proc.run("export A_VAR=hello && echo $A_VAR")
        assert "hello" in result

        # But A_VAR does NOT persist to the next command.
        result2 = proc.run("echo $A_VAR")
        assert "hello" not in result2
    finally:
        proc.cleanup()
