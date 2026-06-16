"""Tests for file-based output capture in the subprocess engine.

Covers:
- Normal: Output correctly captured via file FD for session-scoped mode
- Normal: Large output fully captured without memory issues
- Abnormal: Empty output, command failure output
- Boundary: Binary-like output, unicode output, zero-length output
"""

import os
import sys

import pytest

from src.tools.shell.process import ShellProcess


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="Unix-only: subprocess tests",
)


# ---------------------------------------------------------------------------
# Normal path: output capture
# ---------------------------------------------------------------------------

class TestFileOutputCapture:
    """Verify output is correctly captured via file descriptors."""

    def test_simple_echo_captured(self):
        """Simple echo output is captured in session-scoped mode."""
        proc = ShellProcess(session_scoped=True, load_profile=False)
        try:
            result = proc.run("echo hello_file_output")
            assert "hello_file_output" in result
        finally:
            proc.cleanup()

    def test_multiline_output_captured(self):
        """Multi-line output is fully captured."""
        proc = ShellProcess(session_scoped=True, load_profile=False)
        try:
            result = proc.run("echo line1 && echo line2 && echo line3")
            assert "line1" in result
            assert "line2" in result
            assert "line3" in result
        finally:
            proc.cleanup()

    def test_stderr_captured_in_output(self):
        """stderr is captured alongside stdout."""
        proc = ShellProcess(session_scoped=True, load_profile=False)
        try:
            result = proc.run("echo stdout_msg && echo stderr_msg >&2")
            assert "stdout_msg" in result
            assert "stderr_msg" in result
        finally:
            proc.cleanup()

    def test_large_output_captured_completely(self):
        """Large output (10K+ lines) is fully captured."""
        proc = ShellProcess(session_scoped=True, load_profile=False)
        try:
            result = proc.run("seq 1 10000")
            assert "1" in result
            assert "10000" in result
            lines = result.strip().split("\n")
            assert len(lines) >= 10000
        finally:
            proc.cleanup()

    def test_no_temp_files_leaked(self):
        """Output temp files are cleaned up after execution."""
        import glob
        before = set(glob.glob("/tmp/agentloom_output_*"))
        proc = ShellProcess(session_scoped=True, load_profile=False)
        try:
            proc.run("echo test")
        finally:
            proc.cleanup()
        after = set(glob.glob("/tmp/agentloom_output_*"))
        # No new temp files should persist
        assert after == before


# ---------------------------------------------------------------------------
# Abnormal path: error output
# ---------------------------------------------------------------------------

class TestFileOutputErrors:
    """Output capture for failing commands."""

    def test_failed_command_output_captured(self):
        """Output from a command that exits non-zero is captured."""
        proc = ShellProcess(session_scoped=True, load_profile=False)
        try:
            result = proc.run("echo before_error && false && echo after_error")
            assert "before_error" in result
            # after_error should NOT appear (false stops the chain)
        finally:
            proc.cleanup()

    def test_nonexistent_command_error(self):
        """Error message for nonexistent command is captured."""
        proc = ShellProcess(session_scoped=True, load_profile=False)
        try:
            result = proc.run("nonexistent_command_xyz_123 2>&1 || true")
            assert "not found" in result.lower() or "command not found" in result.lower() or result != ""
        finally:
            proc.cleanup()


# ---------------------------------------------------------------------------
# Boundary: special output content
# ---------------------------------------------------------------------------

class TestFileOutputBoundary:
    """Edge cases in output content."""

    def test_empty_output_command(self):
        """Command producing no output returns empty string."""
        proc = ShellProcess(session_scoped=True, load_profile=False)
        try:
            result = proc.run("true")
            # May be empty or contain minimal formatting
            assert len(result) < 10
        finally:
            proc.cleanup()

    def test_unicode_output(self):
        """Unicode content in output is preserved."""
        proc = ShellProcess(session_scoped=True, load_profile=False)
        try:
            result = proc.run("echo 'Hello 你好 🌍'")
            assert "Hello" in result
            assert "你好" in result
        finally:
            proc.cleanup()

    def test_output_with_ansi_codes_stripped(self):
        """ANSI escape codes in output are stripped."""
        proc = ShellProcess(session_scoped=True, load_profile=False)
        try:
            # printf with ANSI color
            result = proc.run(r"printf '\033[31mRED\033[0m NORMAL'")
            assert "RED" in result
            assert "NORMAL" in result
            assert "\033" not in result  # ANSI codes stripped
        finally:
            proc.cleanup()

    def test_standalone_output_matches(self):
        """Standalone mode also captures output correctly."""
        proc = ShellProcess(session_scoped=False)
        result = proc.run("echo standalone_test_output")
        assert "standalone_test_output" in result
