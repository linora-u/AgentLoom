"""Tests for CWD tracking in session-scoped shell sessions.

CWD is tracked out-of-band via a temp file (pwd >| cwd_file).
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
def session_shell():
    """Create a session-scoped ShellProcess and clean up after test."""
    proc = ShellProcess(session_scoped=True, load_profile=False)
    yield proc
    proc.cleanup()


def _logical(path: str) -> str:
    return os.path.normpath(path)


# ---------------------------------------------------------------------------
# CWD tracking -- session-scoped mode
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="Unix session-scoped shell only",
)
class TestCwdTrackingSessionScoped:
    """CWD tracking in session-scoped sessions."""

    def test_initial_cwd_after_first_command(self, session_shell):
        """After the first command, cwd is set to the actual directory."""
        assert session_shell.cwd is None  # Before any command
        session_shell.run("echo init")
        assert session_shell.cwd is not None
        assert os.path.isdir(session_shell.cwd)

    def test_cd_updates_cwd(self, session_shell):
        """cd /tmp updates cwd to the shell-facing logical path."""
        session_shell.run("cd /tmp")
        assert session_shell.cwd == _logical("/tmp")

    def test_multiple_cd_tracks_last(self, session_shell):
        """Multiple cd commands -- cwd follows the last one."""
        session_shell.run("cd /tmp")
        assert session_shell.cwd == _logical("/tmp")
        session_shell.run("cd /var")
        assert session_shell.cwd == _logical("/var")
        session_shell.run("cd /usr")
        assert session_shell.cwd == _logical("/usr")

    def test_cd_nonexistent_keeps_previous(self, session_shell):
        """cd to non-existent directory does not change cwd."""
        session_shell.run("cd /tmp")
        assert session_shell.cwd == _logical("/tmp")
        session_shell.run("cd /nonexistent_dir_xyz 2>/dev/null || true")
        assert session_shell.cwd == _logical("/tmp")  # Unchanged

    def test_tracking_invisible_in_output(self, session_shell):
        """CWD tracking data (file paths, pwd output) never appears in user output."""
        output = session_shell.run("echo HELLO_WORLD")
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
        proc = ShellProcess(session_scoped=False)
        proc.run("echo test")
        assert proc.cwd is None
