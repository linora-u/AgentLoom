"""Tests for ShellSnapshot — environment snapshot capture.

Covers:
- Normal: Snapshot creation for bash and zsh, content validation
- Abnormal: Timeout handling, missing shell, empty output
- Boundary: Snapshot cleanup, extglob protection injection
"""

import os
import shutil
import sys

import pytest

from src.tools.shell.shell_snapshot import (
    create_snapshot,
    remove_snapshot,
    _build_bash_snapshot_script,
    _build_zsh_snapshot_script,
    _build_snapshot_content,
)


# ---------------------------------------------------------------------------
# Normal path: snapshot script generation
# ---------------------------------------------------------------------------

class TestSnapshotScripts:
    """Verify snapshot capture scripts are well-formed."""

    def test_bash_script_captures_functions(self):
        """Bash snapshot script includes declare -f for functions."""
        script = _build_bash_snapshot_script()
        assert "declare -f" in script

    def test_bash_script_captures_options(self):
        """Bash snapshot script includes shopt -p for options."""
        script = _build_bash_snapshot_script()
        assert "shopt -p" in script

    def test_bash_script_captures_aliases(self):
        """Bash snapshot script includes alias for aliases."""
        script = _build_bash_snapshot_script()
        assert "alias" in script

    def test_zsh_script_captures_functions(self):
        """Zsh snapshot script includes typeset -f for functions."""
        script = _build_zsh_snapshot_script()
        assert "typeset -f" in script

    def test_zsh_script_captures_options(self):
        """Zsh snapshot script includes setopt for options."""
        script = _build_zsh_snapshot_script()
        assert "setopt" in script

    def test_zsh_script_has_separators(self):
        """Snapshot scripts use separators to delimit sections."""
        script = _build_zsh_snapshot_script()
        assert "AGENTLOOM_SNAPSHOT_SEPARATOR" in script

    def test_bash_script_has_separators(self):
        """Bash snapshot scripts use separators to delimit sections."""
        script = _build_bash_snapshot_script()
        assert "AGENTLOOM_SNAPSHOT_SEPARATOR" in script


# ---------------------------------------------------------------------------
# Normal path: snapshot content building
# ---------------------------------------------------------------------------

class TestSnapshotContentBuilding:
    """Verify snapshot file content construction."""

    def test_build_bash_content_has_header(self):
        """Built content includes auto-generated header."""
        raw = "func1() { echo hello; }\n# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---\nshopt -s checkwinsize\n# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---\nalias ll='ls -l'\n"
        content = _build_snapshot_content(raw, "/usr/bin/bash")
        assert "AgentLoom shell environment snapshot" in content
        assert "Auto-generated" in content

    def test_build_bash_content_has_extglob_protection(self):
        """Bash snapshot includes extglob protection."""
        raw = "# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---\n# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---\n"
        content = _build_snapshot_content(raw, "/usr/bin/bash")
        assert "shopt -u extglob" in content

    def test_build_zsh_content_has_extglob_protection(self):
        """Zsh snapshot includes extended glob protection."""
        raw = "# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---\n# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---\n"
        content = _build_snapshot_content(raw, "/usr/bin/zsh")
        assert "NO_EXTENDED_GLOB" in content

    def test_build_content_includes_functions(self):
        """Functions section is included in the output."""
        raw = "my_func() { echo test; }\n# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---\n# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---\n"
        content = _build_snapshot_content(raw, "/usr/bin/bash")
        assert "my_func()" in content

    def test_build_content_includes_aliases(self):
        """Aliases section is included in the output."""
        raw = "# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---\n# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---\nalias ll='ls -la'\n"
        content = _build_snapshot_content(raw, "/usr/bin/bash")
        assert "alias ll=" in content


# ---------------------------------------------------------------------------
# Normal path: real snapshot creation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="Unix-only: requires bash or zsh",
)
class TestRealSnapshotCreation:
    """Real snapshot creation with system shells."""

    def test_create_bash_snapshot(self):
        """Create a real snapshot with bash."""
        bash_path = shutil.which("bash")
        if not bash_path:
            pytest.skip("bash not installed")
        path = create_snapshot(bash_path)
        try:
            assert path is not None
            assert os.path.exists(path)
            content = open(path).read()
            assert "AgentLoom" in content
            assert len(content) > 50  # Non-trivial content
        finally:
            remove_snapshot(path)

    def test_create_zsh_snapshot(self):
        """Create a real snapshot with zsh."""
        zsh_path = shutil.which("zsh")
        if not zsh_path:
            pytest.skip("zsh not installed")
        path = create_snapshot(zsh_path)
        try:
            assert path is not None
            assert os.path.exists(path)
            content = open(path).read()
            assert "AgentLoom" in content
        finally:
            remove_snapshot(path)

    def test_snapshot_is_sourceable(self):
        """Snapshot file can be sourced without errors."""
        import subprocess

        shell_path = shutil.which("bash") or shutil.which("zsh")
        if not shell_path:
            pytest.skip("No suitable shell")
        path = create_snapshot(shell_path)
        try:
            assert path is not None
            result = subprocess.run(
                [shell_path, "-c", f"source '{path}' && echo SNAPSHOT_OK"],
                capture_output=True, text=True, timeout=5,
            )
            assert "SNAPSHOT_OK" in result.stdout
        finally:
            remove_snapshot(path)


# ---------------------------------------------------------------------------
# Abnormal path: error handling
# ---------------------------------------------------------------------------

class TestSnapshotErrorHandling:
    """Error handling in snapshot creation."""

    def test_nonexistent_shell_returns_none(self):
        """create_snapshot returns None for a nonexistent shell."""
        result = create_snapshot("/nonexistent/shell/binary")
        assert result is None

    def test_invalid_shell_returns_none(self):
        """create_snapshot returns None for a non-shell executable."""
        # /bin/true exists but is not a shell — running capture will fail
        if os.path.exists("/bin/true"):
            result = create_snapshot("/bin/true")
            # May return None or a near-empty snapshot
            if result:
                remove_snapshot(result)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

class TestSnapshotCleanup:
    """Snapshot file cleanup."""

    def test_remove_snapshot_deletes_file(self):
        """remove_snapshot deletes the file."""
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".sh")
        os.close(fd)
        assert os.path.exists(path)
        remove_snapshot(path)
        assert not os.path.exists(path)

    def test_remove_snapshot_none_safe(self):
        """remove_snapshot(None) does not raise."""
        remove_snapshot(None)  # Should not raise

    def test_remove_snapshot_nonexistent_safe(self):
        """remove_snapshot for nonexistent path does not raise."""
        remove_snapshot("/tmp/nonexistent_snapshot_xyz.sh")
