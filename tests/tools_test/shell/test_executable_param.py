"""Tests verifying standalone subprocess uses the detected shell binary.

Before this fix, ``_run_standalone()`` called ``subprocess.run(cmd, shell=True)``
without passing ``executable=``, which meant the command was always interpreted
by ``/bin/sh`` regardless of the detected shell.

Now ``executable=self._shell_path`` is passed on Unix, ensuring the detected
bash/zsh is actually used.

Covers:
- subprocess.run receives non-empty executable (mock)
- executable matches find_suitable_shell() result (mock)
- Real execution uses bash/zsh (not /bin/sh)
"""

import subprocess
import sys
from unittest.mock import patch, MagicMock

import pytest

from src.tools.shell.process import ShellProcess, find_suitable_shell


# ---------------------------------------------------------------------------
# executable parameter verification — 3 cases
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="Unix-specific test",
)
class TestExecutableParam:
    """Verify standalone mode passes the detected shell as ``executable``."""

    def test_subprocess_run_receives_executable(self):
        """23a: subprocess.run is called with a non-empty 'executable' kwarg."""
        proc = ShellProcess(persistent=False)

        with patch.object(subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok\n", returncode=0)
            proc.run("echo test")

            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            # Check keyword arguments for 'executable'
            if call_kwargs.kwargs:
                assert "executable" in call_kwargs.kwargs
                assert call_kwargs.kwargs["executable"] is not None
                assert len(call_kwargs.kwargs["executable"]) > 0
            else:
                # Might be passed as part of **kwargs dict
                assert call_kwargs[1].get("executable") is not None

    def test_executable_matches_detected_shell(self):
        """23b: The executable value equals the shell detected by find_suitable_shell()."""
        proc = ShellProcess(persistent=False)
        expected_shell = proc._shell_path

        with patch.object(subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok\n", returncode=0)
            proc.run("echo test")

            call_kwargs = mock_run.call_args
            actual_executable = (
                call_kwargs.kwargs.get("executable")
                if call_kwargs.kwargs
                else call_kwargs[1].get("executable")
            )
            assert actual_executable == expected_shell

    def test_real_execution_uses_detected_shell(self):
        """23c: Real execution confirms bash/zsh is used (not /bin/sh)."""
        proc = ShellProcess(persistent=False)
        # $0 reports the shell binary that interpreted the command
        result = proc.run("echo $0")
        shell_name = result.strip().lower()
        assert "bash" in shell_name or "zsh" in shell_name, (
            f"Expected bash or zsh, got: {shell_name}"
        )

    def test_shell_path_stored_on_instance(self):
        """24a: ShellProcess stores _shell_path on init."""
        proc = ShellProcess(persistent=False)
        assert proc._shell_path is not None
        assert len(proc._shell_path) > 0
        assert "bash" in proc._shell_path or "zsh" in proc._shell_path

    def test_all_shells_unavailable_raises_error(self, monkeypatch):
        """24b: When no shell is found, ShellProcess.__init__ raises FileNotFoundError."""
        monkeypatch.delenv("SHELL", raising=False)
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        monkeypatch.setattr("src.tools.shell.process._is_executable", lambda p: False)
        with pytest.raises(FileNotFoundError):
            ShellProcess(persistent=False)
