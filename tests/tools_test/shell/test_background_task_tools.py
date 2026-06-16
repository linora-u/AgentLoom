"""Tests for background task agent tools."""

import os
import subprocess
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from src.tools.shell.background_task import BackgroundTaskRegistry
from src.tools.shell.background_task_tools import (
    check_background_task,
    kill_background_task,
    list_background_tasks,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset the singleton registry before and after each test."""
    BackgroundTaskRegistry._reset_instance()
    yield
    BackgroundTaskRegistry._reset_instance()


def _spawn_and_register(cmd_args, command_str="test cmd"):
    """Helper: spawn a process and register as background task."""
    fd, out_path = tempfile.mkstemp(prefix="test_bgt_", suffix=".txt")
    os.close(fd)
    out_fd = os.open(out_path, os.O_WRONLY | os.O_APPEND)
    proc = subprocess.Popen(
        cmd_args,
        stdout=out_fd,
        stderr=out_fd,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    os.close(out_fd)
    registry = BackgroundTaskRegistry.get_instance()
    tid = registry.register(proc, command_str, out_path)
    return proc, tid, out_path


class TestCheckBackgroundTask:
    """Tests for check_background_task tool."""

    def test_check_running_task(self):
        proc, tid, _ = _spawn_and_register(["sleep", "60"])
        try:
            result = check_background_task(tid)
            assert "running" in result.lower()
            assert tid in result
            assert "sleep 60" not in result or "test cmd" in result
        finally:
            proc.kill()
            proc.wait(timeout=2)

    def test_check_completed_task(self):
        proc, tid, out_path = _spawn_and_register(
            ["sh", "-c", "echo 'build successful'"],
            "echo build",
        )
        # Wait for completion.
        proc.wait(timeout=5)
        time.sleep(1.5)

        result = check_background_task(tid)
        assert "completed" in result.lower()
        assert "build successful" in result

    def test_check_invalid_task_id(self):
        result = check_background_task("nonexistent123")
        assert "error" in result.lower()
        assert "nonexistent123" in result

    def test_check_with_empty_task_id(self):
        result = check_background_task("")
        assert "error" in result.lower()

    def test_check_shows_available_ids(self):
        proc, tid, _ = _spawn_and_register(["sleep", "60"])
        try:
            result = check_background_task("wrong_id")
            assert tid in result  # Should list available IDs
        finally:
            proc.kill()
            proc.wait(timeout=2)

    def test_check_shows_output_tail(self):
        proc, tid, _ = _spawn_and_register(
            ["sh", "-c", "for i in $(seq 1 30); do echo line_$i; done"],
            "generate lines",
        )
        proc.wait(timeout=5)
        time.sleep(1.5)

        result = check_background_task(tid)
        assert "line_30" in result
        # Should not include very early lines if output > 20 lines.
        assert "Last 20 lines" in result

    def test_check_shows_stall_warning_from_watchdog(self):
        with patch("src.tools.shell.background_task.C") as mock_c:
            mock_c.get_nested = MagicMock(side_effect=lambda *args, **kwargs: {
                ("shell_settings", "background_tasks", "max_concurrent"): 10,
                ("shell_settings", "background_tasks", "stall_detection"): True,
                ("shell_settings", "background_tasks",
                 "stall_threshold_seconds"): 0.5,
            }.get(args, kwargs.get("default", None)))

            proc, tid, _ = _spawn_and_register(
                ["sh", "-c", 'printf "Continue? (y/n) "; sleep 300'],
                "interactive prompt",
            )

            try:
                deadline = time.monotonic() + 5
                result = ""
                while time.monotonic() < deadline:
                    result = check_background_task(tid)
                    if "STALL WARNING" in result:
                        break
                    time.sleep(0.2)

                assert "STALL WARNING" in result
                assert "interactive input" in result
                assert "Continue? (y/n)" in result
            finally:
                kill_background_task(tid)


class TestKillBackgroundTask:
    """Tests for kill_background_task tool."""

    def test_kill_running_task(self):
        proc, tid, _ = _spawn_and_register(["sleep", "300"])
        result = kill_background_task(tid)
        assert "killed" in result.lower() or "failed" in result.lower()
        assert tid in result

    def test_kill_already_completed(self):
        proc, tid, _ = _spawn_and_register(["echo", "done"])
        proc.wait(timeout=5)
        time.sleep(1.5)

        result = kill_background_task(tid)
        assert "already finished" in result.lower()

    def test_kill_invalid_task_id(self):
        result = kill_background_task("bogus_id")
        assert "error" in result.lower()

    def test_kill_with_empty_id(self):
        result = kill_background_task("")
        assert "error" in result.lower()

    def test_kill_returns_output(self):
        proc, tid, _ = _spawn_and_register(
            ["sh", "-c", "echo 'partial output'; sleep 300"],
            "partial cmd",
        )
        time.sleep(0.5)  # Let some output be written.
        result = kill_background_task(tid)
        # Should include partial output.
        assert "partial output" in result or "killed" in result.lower()


class TestListBackgroundTasks:
    """Tests for list_background_tasks tool."""

    def test_list_empty(self):
        result = list_background_tasks()
        assert "no background tasks" in result.lower()

    def test_list_with_tasks(self):
        proc1, tid1, _ = _spawn_and_register(["sleep", "60"], "cmd1")
        proc2, tid2, _ = _spawn_and_register(["sleep", "60"], "cmd2")
        try:
            result = list_background_tasks()
            assert tid1 in result
            assert tid2 in result
            assert "cmd1" in result
            assert "cmd2" in result
            assert "2 running" in result
        finally:
            proc1.kill()
            proc1.wait(timeout=2)
            proc2.kill()
            proc2.wait(timeout=2)

    def test_list_shows_completed_and_running(self):
        proc1, tid1, _ = _spawn_and_register(["echo", "fast"], "fast cmd")
        proc2, tid2, _ = _spawn_and_register(["sleep", "60"], "slow cmd")
        proc1.wait(timeout=5)
        time.sleep(1.5)
        try:
            result = list_background_tasks()
            assert "completed" in result.lower()
            assert "running" in result.lower()
            assert "1 running" in result
        finally:
            proc2.kill()
            proc2.wait(timeout=2)

    def test_list_format_has_header(self):
        proc, tid, _ = _spawn_and_register(["sleep", "60"], "header test")
        try:
            result = list_background_tasks()
            assert "TASK ID" in result
            assert "STATUS" in result
            assert "COMMAND" in result
        finally:
            proc.kill()
            proc.wait(timeout=2)
