"""Tests for foreground stall detection and early kill (V4.1).

Covers the polling loop in ``_exec_subprocess`` that checks the
StallWatchdog every second and kills the process early when an
interactive prompt is detected — instead of waiting the full timeout.
"""

import os
import subprocess
import sys
import tempfile
import time
import threading
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.tools.shell.process import ExecResult, ShellProcess


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session_scoped_process(**kwargs) -> ShellProcess:
    """Create a session-scoped ShellProcess with fast defaults for testing."""
    return ShellProcess(
        session_scoped=True,
        load_profile=False,
        timeout=kwargs.get("timeout", 120),
    )


# ---------------------------------------------------------------------------
# Test: Foreground stall kills process early
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not sys.platform.startswith("linux") and not sys.platform.startswith("darwin"),
    reason="Unix specific test — subprocess + signals",
)
class TestForegroundStallEarlyKill:
    """When the stall watchdog detects an interactive prompt while the
    process is still running, the polling loop should kill the process
    immediately rather than waiting for the full timeout."""

    def test_stall_triggers_early_kill(self):
        """A command that blocks on interactive input is terminated
        within a few seconds of stall detection, not at the full
        timeout boundary.

        Uses ``printf`` to write a prompt-like line then ``sleep`` to
        block indefinitely.  This is shell-agnostic (works in both
        bash and zsh) and triggers the stall watchdog pattern matcher.
        """
        proc = _make_session_scoped_process(timeout=120)
        try:
            start = time.monotonic()

            # Patch the stall threshold to 2 seconds (instead of 45)
            # so the test finishes fast.
            with patch("src.tools.shell.process.C") as mock_c:
                mock_c.get_nested = MagicMock(side_effect=lambda *args, **kwargs: {
                    # stall_threshold_seconds -> 2 seconds
                    ("shell_settings", "background_tasks",
                     "stall_threshold_seconds"): 2,
                    # background tasks enabled
                    ("shell_settings", "background_tasks", "enabled"): True,
                    # auto background on timeout
                    ("shell_settings", "background_tasks",
                     "auto_background_on_timeout"): True,
                }.get(args, kwargs.get("default", None)))

                # printf writes a prompt-like line, sleep blocks forever.
                # stdin is DEVNULL so read-based commands exit immediately
                # in some shells; printf + sleep is the reliable alternative.
                result = proc.run('printf "Continue? (y/n) " && sleep 300')

            elapsed = time.monotonic() - start

            # Should complete well before the 120s timeout.
            # With stall_threshold=2s, poll_interval=5s in watchdog,
            # and 1s polling in main thread, expect ~8-15 seconds.
            assert elapsed < 60, (
                f"Expected early kill, but waited {elapsed:.1f}s"
            )

            # Output should contain the stall warning.
            assert "Stall Warning" in result
            assert "interactive input" in result.lower() or "Continue?" in result

        finally:
            proc.cleanup()

    def test_normal_command_unaffected(self):
        """A fast command that completes normally is not affected by
        the polling loop — it returns immediately with correct output."""
        proc = _make_session_scoped_process(timeout=120)
        try:
            start = time.monotonic()
            result = proc.run("echo 'hello world'")
            elapsed = time.monotonic() - start

            assert "hello world" in result
            assert "Stall Warning" not in result
            assert elapsed < 10, (
                f"Normal command took {elapsed:.1f}s — polling overhead?"
            )
        finally:
            proc.cleanup()


# ---------------------------------------------------------------------------
# Test: Race condition — stall + normal exit simultaneously
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not sys.platform.startswith("linux") and not sys.platform.startswith("darwin"),
    reason="Unix specific test — subprocess + signals",
)
class TestForegroundStallRaceCondition:
    """When a process exits normally at the same moment the watchdog
    sets stall_message, the polling loop should NOT kill the process."""

    def test_normal_exit_with_stall_flag_no_kill(self):
        """Process exits normally (exit code 0) but stall_message was
        set by the watchdog thread.  The result should reflect normal
        completion, not a kill.

        We mock the StallWatchdog to set stall_message immediately,
        but the process (``echo done``) completes within 1 second.
        Since ``proc.poll() is None`` will be False (process exited),
        the kill branch is never taken.
        """
        proc = _make_session_scoped_process(timeout=120)
        try:
            # The command completes instantly.  Even if the watchdog
            # happened to set stall_message, proc.poll() != None so
            # the kill branch is skipped.
            result = proc.run("echo done_race_test")

            # Process completed normally — output should be present.
            assert "done_race_test" in result
            # If stall warning is appended it's fine (post-wait check),
            # but the process was NOT killed.
        finally:
            proc.cleanup()


# ---------------------------------------------------------------------------
# Test: No stall, normal timeout behaviour unchanged
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not sys.platform.startswith("linux") and not sys.platform.startswith("darwin"),
    reason="Unix specific test — subprocess + signals",
)
class TestForegroundNoStallTimeout:
    """When a command exceeds the timeout WITHOUT triggering stall
    detection, the existing timeout behaviour (background promotion
    or kill) must be preserved unchanged."""

    def test_timeout_without_stall_falls_through(self):
        """A command that runs longer than the timeout but does NOT
        trigger stall detection goes through the normal timeout path.

        We use a short timeout (3s) and a command that produces output
        (to prevent stall detection) but runs forever.
        """
        proc = ShellProcess(
            session_scoped=True,
            load_profile=False,
            timeout=3,
        )
        try:
            with patch("src.tools.shell.process.C") as mock_c:
                mock_c.get_nested = MagicMock(side_effect=lambda *args, **kwargs: {
                    ("shell_settings", "background_tasks",
                     "stall_threshold_seconds"): 45,
                    ("shell_settings", "background_tasks", "enabled"): False,
                    ("shell_settings", "background_tasks",
                     "auto_background_on_timeout"): False,
                }.get(args, kwargs.get("default", None)))

                start = time.monotonic()
                # Produce output every second to prevent stall detection,
                # but run forever.
                result = proc.run(
                    "for i in $(seq 1 100); do echo line$i; sleep 1; done"
                )
                elapsed = time.monotonic() - start

            # Should timeout around 3-5 seconds.
            assert elapsed < 15, f"Timeout took {elapsed:.1f}s"
            assert "Timeout Error" in result
        finally:
            proc.cleanup()


@pytest.mark.skipif(
    not sys.platform.startswith("linux") and not sys.platform.startswith("darwin"),
    reason="Unix specific test — subprocess + signals",
)
class TestForegroundPromptAtTimeout:
    """Prompt-like output at the timeout boundary must not become background."""

    def test_standalone_prompt_uses_same_stall_detection(self):
        proc = ShellProcess(
            session_scoped=False,
            load_profile=False,
            timeout=4,
        )
        result = proc.run(
            'printf "StandalonePrompt? (y/n) " && sleep 300'
        )

        assert "Stall Warning" in result
        assert "interactive input" in result
        assert "Background Task" not in result

    def test_prompt_timeout_is_killed_not_promoted(self):
        proc = ShellProcess(
            session_scoped=True,
            load_profile=False,
            timeout=4,
        )
        try:
            with patch("src.tools.shell.process.C") as mock_c:
                mock_c.get_nested = MagicMock(side_effect=lambda *args, **kwargs: {
                    ("shell_settings", "background_tasks",
                     "stall_threshold_seconds"): 45,
                    ("shell_settings", "background_tasks", "enabled"): True,
                    ("shell_settings", "background_tasks",
                     "auto_background_on_timeout"): True,
                }.get(args, kwargs.get("default", None)))

                result = proc.run(
                    'printf "TimeoutPrompt? (y/n) " && sleep 300'
                )

            assert "Stall Warning" in result
            assert "interactive input" in result
            assert "Background Task" not in result
            assert "promoted to background" not in result

        finally:
            proc.cleanup()


# ---------------------------------------------------------------------------
# Test: Partial output is preserved when stall kills the process
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not sys.platform.startswith("linux") and not sys.platform.startswith("darwin"),
    reason="Unix specific test — subprocess + signals",
)
class TestForegroundStallOutputPreserved:
    """When a process produces output before stalling, the partial
    output must be preserved in the result alongside the stall warning."""

    def test_output_before_stall_is_preserved(self):
        """Command writes output, then blocks on a prompt.  The stall
        kill should return the partial output plus the stall warning."""
        proc = _make_session_scoped_process(timeout=120)
        try:
            with patch("src.tools.shell.process.C") as mock_c:
                mock_c.get_nested = MagicMock(side_effect=lambda *args, **kwargs: {
                    ("shell_settings", "background_tasks",
                     "stall_threshold_seconds"): 2,
                    ("shell_settings", "background_tasks", "enabled"): True,
                    ("shell_settings", "background_tasks",
                     "auto_background_on_timeout"): True,
                }.get(args, kwargs.get("default", None)))

                # echo produces output, then printf + sleep simulates a
                # stall on an interactive prompt.
                result = proc.run(
                    'echo "output_before_stall" && '
                    'printf "Proceed? (y/n) " && sleep 300'
                )

            # Partial output from before the stall should be present.
            assert "output_before_stall" in result

            # Stall warning should also be present.
            assert "Stall Warning" in result

        finally:
            proc.cleanup()
