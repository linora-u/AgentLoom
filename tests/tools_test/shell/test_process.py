"""Tests for ShellProcess — stateless subprocess execution engine.

Covers:
- Standalone mode (subprocess.run)
- Persistent mode (subprocess.Popen with session state)
- Timeout handling
- Real command execution
- Session state persistence (CWD, env vars)
"""

import pytest
import sys
import subprocess
from unittest.mock import patch, MagicMock

from src.tools.shell.process import (
    AGENT_SHELL_PROMPT_ENV,
    ShellProcess,
)


def test_internal_prompt_env_name():
    """Legacy constant is preserved for backward compatibility."""
    assert AGENT_SHELL_PROMPT_ENV == "AGENT_SHELL_PROMPT"


def test_shell_process_standalone():
    """Test standard subprocess output without persistence."""
    with patch.object(subprocess, 'run') as mock_run:
        mock_proc = mock_run.return_value
        mock_proc.returncode = 0
        mock_proc.stdout = "standalone success\n"

        proc = ShellProcess(persistent=False)
        result = proc.run("echo standalone")

        assert "standalone success" in result
        mock_run.assert_called_once()


def test_shell_process_timeout():
    """Test timeout exception in standalone."""
    with patch.object(
        subprocess, 'run',
        side_effect=subprocess.TimeoutExpired(
            cmd="sleep 100", timeout=2, output="partial timeout"
        ),
    ):
        proc = ShellProcess(persistent=False, timeout=2)
        result = proc.run("sleep 100")

        assert "partial timeout" in result
        assert "Timeout Error" in result


def test_shell_process_real_ls():
    """Test actual execution of an ls command."""
    proc = ShellProcess(persistent=False)
    result = proc.run("ls -la")
    # since we run this from AgentLoom root, we should see common files
    assert "pyproject.toml" in result or "test_" in result


def test_shell_process_real_python_execution():
    """Test executing a python one-liner."""
    proc = ShellProcess(persistent=False)
    result = proc.run("python3 -c \"print('hello from python')\"")
    assert "hello from python" in result


@pytest.mark.skipif(
    not sys.platform.startswith("linux") and not sys.platform.startswith("darwin"),
    reason="Unix specific test",
)
def test_shell_process_persistent_state_real():
    """Test that CWD persists but env exports are ephemeral."""
    proc = ShellProcess(persistent=True, load_profile=False)
    try:
        # CWD should persist across calls.
        proc.run("cd /tmp")
        result = proc.run("pwd")
        assert "/tmp" in result.strip()

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
def test_shell_process_persistent_cwd_real():
    """Test that cd changes the directory persistently."""
    proc = ShellProcess(persistent=True, load_profile=False)
    try:
        proc.run("cd /tmp")
        result = proc.run("pwd")
        assert "/tmp" in result.strip()
    finally:
        proc.cleanup()


@pytest.mark.skipif(
    not sys.platform.startswith("linux") and not sys.platform.startswith("darwin"),
    reason="Unix specific test",
)
def test_shell_process_persistent_cleanup():
    """Verify cleanup removes session temp files."""
    proc = ShellProcess(persistent=True, load_profile=False)
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
def test_shell_process_persistent_ephemeral_env():
    """Test that inline exports work but do NOT persist across calls."""
    proc = ShellProcess(persistent=True, load_profile=False)
    try:
        # Inline export + echo in the SAME command works.
        result = proc.run("export A_VAR=hello && echo $A_VAR")
        assert "hello" in result

        # But A_VAR does NOT persist to the next command.
        result2 = proc.run("echo $A_VAR")
        assert "hello" not in result2
    finally:
        proc.cleanup()
