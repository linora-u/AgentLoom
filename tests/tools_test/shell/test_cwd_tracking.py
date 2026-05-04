"""Tests for CWD tracking in persistent shell sessions.

CWD is tracked out-of-band via a temp file (pwd -P >| cwd_file).
The CWD marker is never embedded in stdout.

Covers:
- Initial cwd is set after first command
- cd updates cwd
- Multiple cd commands track the last directory
- cd to non-existent directory does not change cwd
- CWD tracking data is invisible in user output
- Standalone mode does not track cwd
"""

import os
import sys

import pytest

from src.tools.shell.process import ShellProcess


@pytest.fixture
def persistent_shell():
    """Create a persistent ShellProcess and clean up after test."""
    proc = ShellProcess(persistent=True, load_profile=False)
    yield proc
    proc.cleanup()


# ---------------------------------------------------------------------------
# CWD tracking -- persistent mode
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="Unix persistent shell only",
)
class TestCwdTrackingPersistent:
    """CWD tracking in persistent sessions."""

    def test_initial_cwd_after_first_command(self, persistent_shell):
        """After the first command, cwd is set to the actual directory."""
        assert persistent_shell.cwd is None  # Before any command
        persistent_shell.run("echo init")
        assert persistent_shell.cwd is not None
        assert os.path.isdir(persistent_shell.cwd)

    def test_cd_updates_cwd(self, persistent_shell):
        """cd /tmp updates cwd to /tmp."""
        persistent_shell.run("cd /tmp")
        assert persistent_shell.cwd == "/tmp"

    def test_multiple_cd_tracks_last(self, persistent_shell):
        """Multiple cd commands -- cwd follows the last one."""
        persistent_shell.run("cd /tmp")
        assert persistent_shell.cwd == "/tmp"
        persistent_shell.run("cd /var")
        assert persistent_shell.cwd == "/var"
        persistent_shell.run("cd /usr")
        assert persistent_shell.cwd == "/usr"

    def test_cd_nonexistent_keeps_previous(self, persistent_shell):
        """cd to non-existent directory does not change cwd."""
        persistent_shell.run("cd /tmp")
        assert persistent_shell.cwd == "/tmp"
        persistent_shell.run("cd /nonexistent_dir_xyz 2>/dev/null || true")
        assert persistent_shell.cwd == "/tmp"  # Unchanged

    def test_tracking_invisible_in_output(self, persistent_shell):
        """CWD tracking data (file paths, pwd output) never appears in user output."""
        output = persistent_shell.run("echo HELLO_WORLD")
        # The CWD tracking is done out-of-band via temp files,
        # so NONE of the tracking artifacts should be in output.
        assert "HELLO_WORLD" in output
        assert "cwd.txt" not in output
        assert "agentloom_session" not in output
        assert "pwd" not in output.lower() or "pwd" in "echo HELLO_WORLD"


# ---------------------------------------------------------------------------
# CWD tracking -- standalone mode
# ---------------------------------------------------------------------------

class TestCwdTrackingStandalone:
    """Standalone mode should not track CWD (each run is independent)."""

    def test_standalone_cwd_none(self):
        """Standalone process has cwd = None even after run."""
        proc = ShellProcess(persistent=False)
        proc.run("echo test")
        assert proc.cwd is None
