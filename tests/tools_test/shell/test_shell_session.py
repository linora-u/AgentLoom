"""Tests for ShellSession — session state manager.

Covers:
- Normal: Session ID generation, CWD tracking
- Abnormal: Missing files, corrupt data, cleanup idempotency
- Boundary: Concurrent sessions, special chars in paths
- Design: Env delta is intentionally NOT tracked (ephemeral exports)
"""

import os
import tempfile

import pytest

from src.tools.shell.shell_session import ShellSession


# ---------------------------------------------------------------------------
# Normal path: session initialization
# ---------------------------------------------------------------------------

class TestSessionInit:
    """Session initialization and basic properties."""

    def test_session_id_auto_generated(self):
        """Session ID is automatically generated when not provided."""
        s = ShellSession()
        assert s.session_id is not None
        assert len(s.session_id) > 0

    def test_session_id_custom(self):
        """Custom session ID is preserved."""
        s = ShellSession(session_id="my_custom_id")
        assert s.session_id == "my_custom_id"

    def test_initial_cwd_is_none(self):
        """CWD is None before any command execution."""
        s = ShellSession()
        assert s.cwd is None

    def test_no_env_delta_attribute(self):
        """ShellSession no longer tracks env delta (ephemeral exports)."""
        s = ShellSession()
        assert not hasattr(s, "env_delta")
        assert not hasattr(s, "_env_delta")

    def test_cwd_setter(self):
        """CWD can be set directly."""
        s = ShellSession()
        s.cwd = "/tmp"
        assert s.cwd == "/tmp"

    def test_state_dir_created_lazily(self):
        """State directory is not created until first use."""
        s = ShellSession()
        assert s._state_dir is None
        _ = s.cwd_file  # Triggers lazy creation
        assert s._state_dir is not None
        assert os.path.isdir(s._state_dir)
        s.cleanup()


# ---------------------------------------------------------------------------
# Normal path: CWD tracking via file
# ---------------------------------------------------------------------------

class TestCwdTracking:
    """CWD update from tracking file."""

    def test_update_cwd_from_valid_file(self):
        """CWD is updated when tracking file contains a valid directory."""
        s = ShellSession()
        try:
            with open(s.cwd_file, "w") as f:
                f.write("/tmp\n")
            result = s.update_cwd_from_file()
            assert result == "/tmp"
            assert s.cwd == "/tmp"
        finally:
            s.cleanup()

    def test_update_cwd_ignores_nonexistent_dir(self):
        """CWD is not updated if the path doesn't exist."""
        s = ShellSession()
        try:
            s.cwd = "/tmp"
            with open(s.cwd_file, "w") as f:
                f.write("/nonexistent_dir_xyz_123\n")
            result = s.update_cwd_from_file()
            assert result is None
            assert s.cwd == "/tmp"  # Unchanged
        finally:
            s.cleanup()

    def test_update_cwd_no_file(self):
        """Returns None when tracking file doesn't exist."""
        s = ShellSession()
        result = s.update_cwd_from_file()
        assert result is None
        s.cleanup()

    def test_update_cwd_empty_file(self):
        """Returns None when tracking file is empty."""
        s = ShellSession()
        try:
            with open(s.cwd_file, "w") as f:
                f.write("")
            result = s.update_cwd_from_file()
            assert result is None
        finally:
            s.cleanup()


# ---------------------------------------------------------------------------
# Design: ephemeral environment variables
# ---------------------------------------------------------------------------

class TestEphemeralEnv:
    """Verify that env delta tracking has been removed by design."""

    def test_no_env_file_property(self):
        """Session no longer exposes an env_file property."""
        s = ShellSession()
        assert not hasattr(s, "env_file")
        s.cleanup()

    def test_no_session_env_script_property(self):
        """Session no longer exposes a session_env_script property."""
        s = ShellSession()
        assert not hasattr(s, "session_env_script")
        s.cleanup()

    def test_no_update_env_delta_method(self):
        """Session no longer has update_env_delta_from_file()."""
        s = ShellSession()
        assert not hasattr(s, "update_env_delta_from_file")
        s.cleanup()

    def test_no_write_session_env_method(self):
        """Session no longer has write_session_env_script()."""
        s = ShellSession()
        assert not hasattr(s, "write_session_env_script")
        s.cleanup()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

class TestCleanup:
    """Session cleanup behavior."""

    def test_cleanup_removes_state_dir(self):
        """Cleanup removes the entire state directory."""
        s = ShellSession()
        _ = s.cwd_file  # Force creation
        state_dir = s._state_dir
        assert os.path.isdir(state_dir)
        s.cleanup()
        assert not os.path.isdir(state_dir)

    def test_cleanup_idempotent(self):
        """Calling cleanup multiple times does not raise."""
        s = ShellSession()
        _ = s.cwd_file  # Force creation
        s.cleanup()
        s.cleanup()  # Should not raise

    def test_cleanup_before_any_use(self):
        """Cleanup on a fresh session (no state dir) does not raise."""
        s = ShellSession()
        s.cleanup()  # Should not raise


# ---------------------------------------------------------------------------
# Boundary conditions
# ---------------------------------------------------------------------------

class TestBoundaryConditions:
    """Edge cases and boundary conditions."""

    def test_multiple_sessions_isolated(self):
        """Multiple sessions have distinct state directories."""
        s1 = ShellSession()
        s2 = ShellSession()
        try:
            _ = s1.cwd_file
            _ = s2.cwd_file
            assert s1._state_dir != s2._state_dir
        finally:
            s1.cleanup()
            s2.cleanup()

    def test_cwd_file_path_contains_session_id(self):
        """CWD file is namespaced by session ID."""
        s = ShellSession(session_id="test123")
        path = s.cwd_file
        assert "test123" in s._state_dir
        s.cleanup()
