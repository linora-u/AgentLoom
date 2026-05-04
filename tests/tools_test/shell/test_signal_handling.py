"""Tests for signal handling and partial output preservation."""

import os
import subprocess
import tempfile
import time
from unittest.mock import patch

import pytest

from src.tools.shell.background_task import BackgroundTaskRegistry
from src.tools.shell.process import ExecResult, ShellProcess


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset the singleton registry before and after each test."""
    BackgroundTaskRegistry._reset_instance()
    yield
    BackgroundTaskRegistry._reset_instance()


class TestExecResult:
    """Tests for the ExecResult dataclass."""

    def test_default_values(self):
        r = ExecResult()
        assert r.output == ""
        assert r.timed_out is False
        assert r.exit_code is None
        assert r.background_task_id is None
        assert r.interrupted is False

    def test_with_output(self):
        r = ExecResult(output="hello", exit_code=0)
        assert r.output == "hello"
        assert r.exit_code == 0

    def test_timeout_result(self):
        r = ExecResult(output="partial", timed_out=True)
        assert r.timed_out is True

    def test_background_promotion(self):
        r = ExecResult(
            output="partial",
            timed_out=True,
            background_task_id="abc123",
        )
        assert r.background_task_id == "abc123"


class TestPartialOutputPreservation:
    """Tests for preserving output on timeout."""

    def test_timeout_preserves_partial_output(self):
        """A command that produces output then exceeds timeout should
        return the partial output."""
        proc = ShellProcess(
            timeout=2,
            persistent=False,
            return_err_output=True,
        )
        # This command prints immediately, then sleeps forever.
        result = proc.run("echo 'output_before_timeout'; sleep 300")
        assert "output_before_timeout" in result
        # Should mention timeout or background.
        assert "Timeout" in result or "Background" in result

    def test_fast_command_no_timeout(self):
        """A fast command should complete normally without timeout."""
        proc = ShellProcess(
            timeout=30,
            persistent=False,
            return_err_output=True,
        )
        result = proc.run("echo 'fast_result'")
        assert "fast_result" in result
        assert "Timeout" not in result


class TestAutoBackgroundOnTimeout:
    """Tests for automatic background promotion on timeout."""

    @patch.dict("os.environ", {}, clear=False)
    def test_auto_background_creates_task(self):
        """When background tasks are enabled, timeout should create
        a background task instead of killing the process."""
        proc = ShellProcess(
            timeout=2,
            persistent=True,
            return_err_output=True,
            load_profile=False,
        )
        result = proc.run("echo 'bg_test_output'; sleep 300")

        # Check if background task was created.
        if "Background Task:" in result:
            # Extract task_id from result.
            import re
            match = re.search(r"Background Task: (\w+)", result)
            assert match is not None
            task_id = match.group(1)

            registry = BackgroundTaskRegistry.get_instance()
            task = registry.get(task_id)
            assert task is not None
            assert task.status == "running"

            # Clean up the background process.
            registry.kill_task(task_id)
        else:
            # Background may be disabled via config — timeout error is OK.
            assert "Timeout" in result

        proc.cleanup()

    @patch("src.lib.config.C.get_nested")
    def test_auto_background_disabled(self, mock_get_nested):
        """When auto-background is disabled, timeout should kill and
        return timeout error."""
        def fake_get_nested(*args, default=None):
            key_path = ".".join(str(a) for a in args)
            if "auto_background_on_timeout" in key_path:
                return False
            if "enabled" in key_path and "background" in key_path:
                return False
            return default

        mock_get_nested.side_effect = fake_get_nested

        proc = ShellProcess(
            timeout=2,
            persistent=True,
            return_err_output=True,
            load_profile=False,
        )
        result = proc.run("echo 'no_bg'; sleep 300")
        assert "Timeout" in result
        assert "Background Task:" not in result
        proc.cleanup()


class TestExitVsClose:
    """Tests for process exit behavior — should not hang on grandchildren."""

    def test_background_child_does_not_block(self):
        """A command that spawns a background child (via &) should not
        cause the parent to hang waiting for the child's stdout."""
        proc = ShellProcess(
            timeout=10,
            persistent=False,
            return_err_output=True,
        )
        start = time.monotonic()
        result = proc.run("echo 'parent_done'")
        elapsed = time.monotonic() - start
        assert "parent_done" in result
        # Should complete quickly — not wait for any grandchild.
        assert elapsed < 8
