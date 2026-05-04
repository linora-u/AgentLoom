"""Tests for background task state machine and registry."""

import os
import subprocess
import tempfile
import threading
import time

import pytest

from src.tools.shell.background_task import (
    BackgroundTaskRegistry,
    BackgroundTaskState,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset the singleton registry before and after each test."""
    BackgroundTaskRegistry._reset_instance()
    yield
    BackgroundTaskRegistry._reset_instance()


# ---------------------------------------------------------------------------
# BackgroundTaskState unit tests
# ---------------------------------------------------------------------------

class TestBackgroundTaskState:
    """Unit tests for the BackgroundTaskState dataclass."""

    def test_initial_state_is_running(self):
        task = BackgroundTaskState(
            task_id="abc123",
            command="sleep 60",
            description="test",
            pid=12345,
            output_path="/tmp/test.txt",
        )
        assert task.status == "running"
        assert task.exit_code is None
        assert task.end_time is None
        assert not task.is_terminal

    def test_elapsed_seconds_while_running(self):
        task = BackgroundTaskState(
            task_id="t1",
            command="echo hi",
            description="test",
            pid=1,
            output_path="/tmp/x",
            start_time=time.monotonic() - 10,
        )
        assert task.elapsed_seconds >= 9.5

    def test_elapsed_seconds_after_completion(self):
        now = time.monotonic()
        task = BackgroundTaskState(
            task_id="t2",
            command="echo hi",
            description="test",
            pid=1,
            output_path="/tmp/x",
            start_time=now - 5,
            end_time=now - 2,
            status="completed",
        )
        # Elapsed should be ~3 seconds (end - start).
        assert 2.5 <= task.elapsed_seconds <= 3.5

    def test_is_terminal_states(self):
        for status in ("completed", "failed", "killed"):
            task = BackgroundTaskState(
                task_id="t",
                command="x",
                description="",
                pid=1,
                output_path="/tmp/x",
                status=status,
            )
            assert task.is_terminal

    def test_output_size_missing_file(self):
        task = BackgroundTaskState(
            task_id="t",
            command="x",
            description="",
            pid=1,
            output_path="/nonexistent/path/file.txt",
        )
        assert task.output_size == 0

    def test_output_size_with_file(self, tmp_path):
        out = tmp_path / "out.txt"
        out.write_text("hello world\n")
        task = BackgroundTaskState(
            task_id="t",
            command="x",
            description="",
            pid=1,
            output_path=str(out),
        )
        assert task.output_size == len("hello world\n")

    def test_read_output_tail(self, tmp_path):
        out = tmp_path / "out.txt"
        lines = [f"line {i}" for i in range(50)]
        out.write_text("\n".join(lines) + "\n")
        task = BackgroundTaskState(
            task_id="t",
            command="x",
            description="",
            pid=1,
            output_path=str(out),
        )
        tail = task.read_output_tail(n_lines=5)
        assert "line 49" in tail
        assert "line 45" in tail
        assert "line 0" not in tail

    def test_read_output_tail_empty_file(self, tmp_path):
        out = tmp_path / "out.txt"
        out.write_text("")
        task = BackgroundTaskState(
            task_id="t",
            command="x",
            description="",
            pid=1,
            output_path=str(out),
        )
        assert task.read_output_tail() == ""

    def test_read_output_tail_missing_file(self):
        task = BackgroundTaskState(
            task_id="t",
            command="x",
            description="",
            pid=1,
            output_path="/nonexistent/file.txt",
        )
        assert task.read_output_tail() == ""


# ---------------------------------------------------------------------------
# BackgroundTaskRegistry tests
# ---------------------------------------------------------------------------

class TestBackgroundTaskRegistry:
    """Tests for the registry lifecycle and thread safety."""

    def _spawn_sleeper(self, seconds=60):
        """Spawn a long-running subprocess for testing."""
        fd, output_path = tempfile.mkstemp(prefix="test_bg_", suffix=".txt")
        os.close(fd)
        out_fd = os.open(output_path, os.O_WRONLY | os.O_APPEND)
        proc = subprocess.Popen(
            ["sleep", str(seconds)],
            stdout=out_fd,
            stderr=out_fd,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        os.close(out_fd)
        return proc, output_path

    def _cleanup_proc(self, proc):
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass

    def test_register_and_get(self):
        registry = BackgroundTaskRegistry.get_instance()
        proc, out = self._spawn_sleeper()
        try:
            tid = registry.register(proc, "sleep 60", out)
            assert len(tid) == 12
            task = registry.get(tid)
            assert task is not None
            assert task.status == "running"
            assert task.pid == proc.pid
            assert task.command == "sleep 60"
        finally:
            self._cleanup_proc(proc)

    def test_get_nonexistent_returns_none(self):
        registry = BackgroundTaskRegistry.get_instance()
        assert registry.get("nonexistent") is None

    def test_list_all_and_list_running(self):
        registry = BackgroundTaskRegistry.get_instance()
        proc1, out1 = self._spawn_sleeper()
        proc2, out2 = self._spawn_sleeper()
        try:
            tid1 = registry.register(proc1, "cmd1", out1)
            tid2 = registry.register(proc2, "cmd2", out2)
            assert len(registry.list_all()) == 2
            assert len(registry.list_running()) == 2
        finally:
            self._cleanup_proc(proc1)
            self._cleanup_proc(proc2)

    def test_remove_task(self):
        registry = BackgroundTaskRegistry.get_instance()
        proc, out = self._spawn_sleeper()
        try:
            tid = registry.register(proc, "sleep 60", out)
            assert registry.remove(tid) is True
            assert registry.get(tid) is None
            assert registry.remove(tid) is False
        finally:
            self._cleanup_proc(proc)

    def test_kill_task(self):
        registry = BackgroundTaskRegistry.get_instance()
        proc, out = self._spawn_sleeper()
        tid = registry.register(proc, "sleep 60", out)

        task = registry.kill_task(tid)
        assert task is not None
        assert task.status in ("killed", "failed")
        assert task.is_terminal

    def test_kill_nonexistent_returns_none(self):
        registry = BackgroundTaskRegistry.get_instance()
        assert registry.kill_task("nope") is None

    def test_kill_already_completed(self):
        registry = BackgroundTaskRegistry.get_instance()
        # Use a fast command that exits immediately.
        fd, out_path = tempfile.mkstemp(prefix="test_bg_", suffix=".txt")
        os.close(fd)
        out_fd = os.open(out_path, os.O_WRONLY | os.O_APPEND)
        proc = subprocess.Popen(
            ["echo", "done"],
            stdout=out_fd,
            stderr=out_fd,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        os.close(out_fd)
        proc.wait(timeout=5)

        tid = registry.register(proc, "echo done", out_path)
        # Wait for the monitor thread to detect completion.
        time.sleep(1.5)
        task = registry.get(tid)
        assert task is not None
        assert task.is_terminal

        # Kill should return the already-terminal task.
        result = registry.kill_task(tid)
        assert result.is_terminal

    def test_monitor_detects_completion(self):
        registry = BackgroundTaskRegistry.get_instance()
        fd, out_path = tempfile.mkstemp(prefix="test_bg_", suffix=".txt")
        os.close(fd)
        out_fd = os.open(out_path, os.O_WRONLY | os.O_APPEND)
        proc = subprocess.Popen(
            ["echo", "hello"],
            stdout=out_fd,
            stderr=out_fd,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        os.close(out_fd)

        tid = registry.register(proc, "echo hello", out_path)
        # Wait for monitor thread to detect the exit.
        time.sleep(2)
        task = registry.get(tid)
        assert task is not None
        assert task.status == "completed"
        assert task.exit_code == 0
        assert task.end_time is not None

    def test_monitor_detects_failure(self):
        registry = BackgroundTaskRegistry.get_instance()
        fd, out_path = tempfile.mkstemp(prefix="test_bg_", suffix=".txt")
        os.close(fd)
        out_fd = os.open(out_path, os.O_WRONLY | os.O_APPEND)
        proc = subprocess.Popen(
            ["false"],  # exits with code 1
            stdout=out_fd,
            stderr=out_fd,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        os.close(out_fd)

        tid = registry.register(proc, "false", out_path)
        time.sleep(2)
        task = registry.get(tid)
        assert task is not None
        assert task.status == "failed"
        assert task.exit_code == 1

    def test_cleanup_completed(self):
        registry = BackgroundTaskRegistry.get_instance()
        fd, out_path = tempfile.mkstemp(prefix="test_bg_", suffix=".txt")
        os.close(fd)
        out_fd = os.open(out_path, os.O_WRONLY | os.O_APPEND)
        proc = subprocess.Popen(
            ["echo", "x"],
            stdout=out_fd,
            stderr=out_fd,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        os.close(out_fd)

        tid = registry.register(proc, "echo x", out_path)
        time.sleep(2)

        # With max_age_seconds=0, everything completed should be evicted.
        removed = registry.cleanup_completed(max_age_seconds=0)
        assert removed == 1
        assert registry.get(tid) is None

    def test_max_concurrent_enforcement(self):
        registry = BackgroundTaskRegistry.get_instance()
        procs = []
        try:
            # Fill up to max_concurrent (default 10).
            for i in range(10):
                proc, out = self._spawn_sleeper()
                procs.append(proc)
                registry.register(proc, f"sleep {i}", out)

            # 11th should raise.
            proc11, out11 = self._spawn_sleeper()
            procs.append(proc11)
            with pytest.raises(RuntimeError, match="Maximum concurrent"):
                registry.register(proc11, "sleep 11", out11)
        finally:
            for p in procs:
                self._cleanup_proc(p)

    def test_thread_safety_concurrent_register(self):
        """Register tasks from multiple threads simultaneously."""
        registry = BackgroundTaskRegistry.get_instance()
        results = []
        errors = []
        procs = []

        def register_one(idx):
            try:
                proc, out = self._spawn_sleeper()
                procs.append(proc)
                tid = registry.register(proc, f"sleep {idx}", out)
                results.append(tid)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=register_one, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        try:
            assert len(errors) == 0
            assert len(results) == 5
            # All task IDs should be unique.
            assert len(set(results)) == 5
        finally:
            for p in procs:
                self._cleanup_proc(p)

    def test_singleton_identity(self):
        r1 = BackgroundTaskRegistry.get_instance()
        r2 = BackgroundTaskRegistry.get_instance()
        assert r1 is r2
