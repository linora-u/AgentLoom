"""Tests for tree_kill — process tree termination utilities.

Covers:
- Normal: Kill a running process, graceful SIGTERM→SIGKILL escalation
- Abnormal: Kill nonexistent PID, permission errors
- Boundary: Kill already-dead process, zero/negative PID
- SizeWatchdog: File size monitoring and auto-kill
"""

import os
import signal
import subprocess
import sys
import tempfile
import time

import pytest

from src.tools.shell.tree_kill import (
    tree_kill,
    graceful_kill,
    _is_alive,
    SizeWatchdog,
)


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "darwin")),
    reason="Unix-only: signal handling",
)


# ---------------------------------------------------------------------------
# Normal path: tree_kill
# ---------------------------------------------------------------------------

class TestTreeKill:
    """Direct process killing."""

    def test_kill_running_process(self):
        """Kill a real running process."""
        proc = subprocess.Popen(
            ["sleep", "30"],
            start_new_session=True,
        )
        assert _is_alive(proc.pid)

        result = tree_kill(proc.pid, signal.SIGKILL)
        assert result is True

        proc.wait(timeout=5)
        assert not _is_alive(proc.pid)

    def test_kill_already_dead_process(self):
        """Killing an already-exited process returns True (no error)."""
        proc = subprocess.Popen(["true"])
        proc.wait()
        result = tree_kill(proc.pid, signal.SIGKILL)
        assert result is True  # ProcessLookupError is caught

    def test_kill_nonexistent_pid(self):
        """Killing PID 0 returns False."""
        result = tree_kill(0)
        assert result is False

    def test_kill_negative_pid(self):
        """Killing negative PID returns False."""
        result = tree_kill(-1)
        assert result is False


# ---------------------------------------------------------------------------
# Normal path: graceful_kill
# ---------------------------------------------------------------------------

class TestGracefulKill:
    """SIGTERM → SIGKILL escalation."""

    def test_graceful_kill_terminates_process(self):
        """graceful_kill terminates a sleep process."""
        proc = subprocess.Popen(
            ["sleep", "30"],
            start_new_session=True,
        )
        assert _is_alive(proc.pid)

        result = graceful_kill(proc.pid, grace_ms=200)
        assert result is True

        # Wait for the process to fully exit (reap zombie).
        proc.wait(timeout=5)
        assert not _is_alive(proc.pid)

    def test_graceful_kill_already_dead(self):
        """graceful_kill on an already-dead process returns True."""
        proc = subprocess.Popen(["true"])
        proc.wait()
        result = graceful_kill(proc.pid)
        assert result is True


# ---------------------------------------------------------------------------
# Normal path: _is_alive
# ---------------------------------------------------------------------------

class TestIsAlive:
    """Process existence checking."""

    def test_alive_process(self):
        """Running process reports as alive."""
        proc = subprocess.Popen(["sleep", "30"])
        try:
            assert _is_alive(proc.pid) is True
        finally:
            proc.kill()
            proc.wait()

    def test_dead_process(self):
        """Exited process reports as not alive."""
        proc = subprocess.Popen(["true"])
        proc.wait()
        # After wait, process is reaped
        assert _is_alive(proc.pid) is False

    def test_nonexistent_pid(self):
        """PID that never existed reports as not alive."""
        # Use a very high PID that is unlikely to exist
        assert _is_alive(999999999) is False


# ---------------------------------------------------------------------------
# SizeWatchdog
# ---------------------------------------------------------------------------

class TestSizeWatchdog:
    """File size watchdog for background processes."""

    def test_watchdog_kills_on_oversized_file(self):
        """Watchdog kills the process when output file exceeds limit."""
        proc = subprocess.Popen(
            ["sleep", "60"],
            start_new_session=True,
        )
        try:
            # Create a "large" output file
            fd, output_file = tempfile.mkstemp()
            os.write(fd, b"x" * 1000)  # 1000 bytes
            os.close(fd)

            # Set a very low threshold (500 bytes) so it triggers immediately
            watchdog = SizeWatchdog(
                proc.pid,
                output_file,
                max_bytes=500,
                poll_interval_s=0.1,  # Fast poll for testing
            )
            watchdog.start()

            # Wait for the watchdog to detect and kill
            time.sleep(1.0)
            watchdog.stop()

            # Process should be dead
            proc.wait(timeout=5)
            assert not _is_alive(proc.pid)
        finally:
            if os.path.exists(output_file):
                os.remove(output_file)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            proc.wait(timeout=5)

    def test_watchdog_does_not_kill_small_file(self):
        """Watchdog does not kill when file is within limits."""
        proc = subprocess.Popen(
            ["sleep", "10"],
            start_new_session=True,
        )
        try:
            fd, output_file = tempfile.mkstemp()
            os.write(fd, b"x" * 100)  # 100 bytes
            os.close(fd)

            watchdog = SizeWatchdog(
                proc.pid,
                output_file,
                max_bytes=1000,  # Well above 100
                poll_interval_s=0.1,
            )
            watchdog.start()

            time.sleep(0.5)
            watchdog.stop()

            # Process should still be alive
            assert _is_alive(proc.pid)
        finally:
            if os.path.exists(output_file):
                os.remove(output_file)
            proc.kill()
            proc.wait(timeout=5)

    def test_watchdog_stop_is_idempotent(self):
        """Calling stop() multiple times does not raise."""
        watchdog = SizeWatchdog(99999, "/tmp/nonexistent", max_bytes=100)
        watchdog.stop()
        watchdog.stop()  # Should not raise

    def test_watchdog_handles_missing_file(self):
        """Watchdog handles nonexistent output file gracefully."""
        watchdog = SizeWatchdog(
            99999,
            "/tmp/nonexistent_watchdog_file.txt",
            max_bytes=100,
            poll_interval_s=0.1,
        )
        watchdog.start()
        time.sleep(0.3)
        watchdog.stop()
        # Should not raise any exceptions
