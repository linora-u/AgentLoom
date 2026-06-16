"""Tests verifying standalone execution uses the detected shell binary.

Standalone mode now routes through the same subprocess executor as
session-scoped mode.  The detected shell must still be used explicitly as
argv[0], rather than falling back to /bin/sh.

Covers:
- _exec_subprocess receives the detected shell as argv[0]
- executor argv shell matches the ShellProcess shell path
- Real execution uses bash/zsh (not /bin/sh)
"""

import sys
from unittest.mock import patch

import pytest

from src.tools.shell.process import ExecResult, ShellProcess


# ---------------------------------------------------------------------------
# executable parameter verification — 3 cases
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="Unix-specific test",
)
class TestExecutableParam:
    """Verify standalone mode invokes the detected shell."""

    def test_standalone_executor_receives_shell_path(self):
        """23a: _exec_subprocess is called with shell path as argv[0]."""
        proc = ShellProcess(session_scoped=False)

        with patch.object(
            ShellProcess,
            "_exec_subprocess",
            return_value=ExecResult(output="ok\n", exit_code=0),
        ) as mock_exec:
            proc.run("echo test")

            mock_exec.assert_called_once()
            shell_args = mock_exec.call_args.args[0]
            assert shell_args[0] == proc._shell_path
            assert shell_args[-2:] == ["-c", "echo test"]

    def test_executable_matches_detected_shell(self):
        """23b: The executor argv shell equals find_suitable_shell()."""
        proc = ShellProcess(session_scoped=False)
        expected_shell = proc._shell_path

        with patch.object(
            ShellProcess,
            "_exec_subprocess",
            return_value=ExecResult(output="ok\n", exit_code=0),
        ) as mock_exec:
            proc.run("echo test")

            shell_args = mock_exec.call_args.args[0]
            assert shell_args[0] == expected_shell

    def test_real_execution_uses_detected_shell(self):
        """23c: Real execution confirms bash/zsh is used (not /bin/sh)."""
        proc = ShellProcess(session_scoped=False)
        # $0 reports the shell binary that interpreted the command
        result = proc.run("echo $0")
        shell_name = result.strip().lower()
        assert "bash" in shell_name or "zsh" in shell_name, (
            f"Expected bash or zsh, got: {shell_name}"
        )

    def test_shell_path_stored_on_instance(self):
        """24a: ShellProcess stores _shell_path on init."""
        proc = ShellProcess(session_scoped=False)
        assert proc._shell_path is not None
        assert len(proc._shell_path) > 0
        assert "bash" in proc._shell_path or "zsh" in proc._shell_path

    def test_all_shells_unavailable_raises_error(self, monkeypatch):
        """24b: When no shell is found, ShellProcess.__init__ raises FileNotFoundError."""
        monkeypatch.delenv("SHELL", raising=False)
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        monkeypatch.setattr("src.tools.shell.process._is_executable", lambda p: False)
        with pytest.raises(FileNotFoundError):
            ShellProcess(session_scoped=False)
