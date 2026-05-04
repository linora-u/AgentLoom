"""Background task state machine and registry.

Provides first-class management of long-running shell commands that
have been promoted from foreground to background execution.  Each
background task tracks its process, output file, and lifecycle state.

The BackgroundTaskRegistry is a thread-safe singleton that monitors
running tasks via daemon threads and enforces resource limits
(max concurrent tasks, max output size).

Design aligned with Claude Code's LocalShellTask + ShellCommand
state machine (LocalShellTask.tsx, ShellCommand.ts).
"""

import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

from src.lib.config import C
from src.lib.logging import get_logger
from src.tools.shell.tree_kill import SizeWatchdog, graceful_kill

logger = get_logger(__name__)

# Default configuration values.
_DEFAULT_MAX_CONCURRENT = 10
_DEFAULT_MAX_OUTPUT_BYTES = 100_000_000  # 100 MB
_DEFAULT_STALL_THRESHOLD = 45  # seconds


# ---------------------------------------------------------------------------
# Background task state
# ---------------------------------------------------------------------------

@dataclass
class BackgroundTaskState:
    """Snapshot of a background task's lifecycle.

    State transitions::

        running  ──►  completed   (exit code 0)
        running  ──►  failed      (exit code != 0)
        running  ──►  killed      (explicit kill or size watchdog)
    """

    task_id: str
    command: str
    description: str
    pid: int
    output_path: str
    start_time: float = field(default_factory=time.monotonic)
    status: Literal["running", "completed", "failed", "killed"] = "running"
    end_time: Optional[float] = None
    exit_code: Optional[int] = None
    stall_message: Optional[str] = None

    # Internal references — not serialised.
    _process: Optional[object] = field(default=None, repr=False)
    _size_watchdog: Optional[SizeWatchdog] = field(default=None, repr=False)
    _stall_watchdog: Optional[object] = field(default=None, repr=False)

    @property
    def elapsed_seconds(self) -> float:
        """Seconds since the task started."""
        end = self.end_time if self.end_time is not None else time.monotonic()
        return end - self.start_time

    @property
    def is_terminal(self) -> bool:
        """True if the task has reached a final state."""
        return self.status in ("completed", "failed", "killed")

    @property
    def output_size(self) -> int:
        """Current size of the output file in bytes."""
        try:
            return os.path.getsize(self.output_path)
        except OSError:
            return 0

    def read_output_tail(self, n_lines: int = 20) -> str:
        """Read the last *n_lines* lines from the output file."""
        try:
            if not os.path.exists(self.output_path):
                return ""
            with open(self.output_path, "r", errors="replace") as f:
                # Seek to the last 64 KB for efficiency.
                try:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 65536))
                except OSError:
                    f.seek(0)
                tail_text = f.read()
            lines = tail_text.splitlines()
            return "\n".join(lines[-n_lines:])
        except (OSError, IOError):
            return ""


# ---------------------------------------------------------------------------
# Background task registry (singleton)
# ---------------------------------------------------------------------------

class BackgroundTaskRegistry:
    """Thread-safe singleton registry for background shell tasks.

    Each background task is monitored by a daemon thread that polls the
    process status and updates the task state when it exits.
    """

    _instance: Optional["BackgroundTaskRegistry"] = None
    _instance_lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "BackgroundTaskRegistry":
        with cls._instance_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._tasks: Dict[str, BackgroundTaskState] = {}
                inst._lock = threading.Lock()
                cls._instance = inst
        return cls._instance

    @classmethod
    def get_instance(cls) -> "BackgroundTaskRegistry":
        """Return the global singleton."""
        return cls()

    @classmethod
    def _reset_instance(cls) -> None:
        """Reset the singleton (for testing only)."""
        with cls._instance_lock:
            if cls._instance is not None:
                inst = cls._instance
                with inst._lock:
                    # Stop all watchdogs.
                    for task in inst._tasks.values():
                        if task._size_watchdog:
                            task._size_watchdog.stop()
                        if task._stall_watchdog:
                            task._stall_watchdog.stop()
                    inst._tasks.clear()
            cls._instance = None

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cfg_max_concurrent() -> int:
        return C.get_nested(
            "shell_settings", "background_tasks", "max_concurrent",
            default=_DEFAULT_MAX_CONCURRENT,
        )

    @staticmethod
    def _cfg_enabled() -> bool:
        return C.get_nested(
            "shell_settings", "background_tasks", "enabled",
            default=True,
        )

    @staticmethod
    def _cfg_auto_background() -> bool:
        return C.get_nested(
            "shell_settings", "background_tasks", "auto_background_on_timeout",
            default=True,
        )

    @staticmethod
    def _cfg_max_output_bytes() -> int:
        return C.get_nested(
            "shell_settings", "background_tasks", "max_output_bytes",
            default=_DEFAULT_MAX_OUTPUT_BYTES,
        )

    @staticmethod
    def _cfg_stall_detection() -> bool:
        return C.get_nested(
            "shell_settings", "background_tasks", "stall_detection",
            default=True,
        )

    @staticmethod
    def _cfg_stall_threshold() -> int:
        return C.get_nested(
            "shell_settings", "background_tasks", "stall_threshold_seconds",
            default=_DEFAULT_STALL_THRESHOLD,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(
        self,
        process,
        command: str,
        output_path: str,
        description: str = "",
        size_watchdog: Optional[SizeWatchdog] = None,
    ) -> str:
        """Register a running process as a background task.

        Args:
            process: A ``subprocess.Popen`` instance (must be running).
            command: The shell command string.
            output_path: Path to the output file being written by the process.
            description: Human-readable description (defaults to command).
            size_watchdog: An existing SizeWatchdog instance to transfer.

        Returns:
            The task_id assigned to this background task.

        Raises:
            RuntimeError: If the max-concurrent limit is reached.
        """
        with self._lock:
            running = sum(1 for t in self._tasks.values() if not t.is_terminal)
            max_c = self._cfg_max_concurrent()
            if running >= max_c:
                raise RuntimeError(
                    f"Maximum concurrent background tasks reached ({max_c}). "
                    "Kill or wait for existing tasks before starting new ones."
                )

            task_id = uuid.uuid4().hex[:12]
            task = BackgroundTaskState(
                task_id=task_id,
                command=command,
                description=description or command[:80],
                pid=process.pid,
                output_path=output_path,
                _process=process,
                _size_watchdog=size_watchdog,
            )
            self._tasks[task_id] = task

        # Start monitoring thread (outside lock).
        monitor = threading.Thread(
            target=self._monitor_task,
            args=(task_id,),
            daemon=True,
            name=f"bg-monitor-{task_id}",
        )
        monitor.start()

        # Start stall watchdog if enabled.
        if self._cfg_stall_detection():
            from src.tools.shell.stall_watchdog import StallWatchdog

            sw = StallWatchdog(
                task_id=task_id,
                output_path=output_path,
                stall_threshold=self._cfg_stall_threshold(),
            )
            task._stall_watchdog = sw
            sw.start()

        logger.info(
            "Background task %s registered: pid=%d, command=%s",
            task_id, process.pid, command[:120],
        )
        return task_id

    def get(self, task_id: str) -> Optional[BackgroundTaskState]:
        """Return the task state, or None if not found."""
        with self._lock:
            return self._tasks.get(task_id)

    def remove(self, task_id: str) -> bool:
        """Remove a task from the registry. Returns True if found."""
        with self._lock:
            task = self._tasks.pop(task_id, None)
        if task is None:
            return False
        # Clean up watchdogs.
        if task._size_watchdog:
            task._size_watchdog.stop()
        if task._stall_watchdog:
            task._stall_watchdog.stop()
        return True

    def list_all(self) -> List[BackgroundTaskState]:
        """Return a snapshot of all tracked tasks."""
        with self._lock:
            return list(self._tasks.values())

    def list_running(self) -> List[BackgroundTaskState]:
        """Return only tasks that are still running."""
        with self._lock:
            return [t for t in self._tasks.values() if t.status == "running"]

    def cleanup_completed(self, max_age_seconds: float = 3600) -> int:
        """Remove completed/failed/killed tasks older than *max_age_seconds*.

        Returns the number of tasks evicted.
        """
        now = time.monotonic()
        to_remove: List[str] = []
        with self._lock:
            for tid, task in self._tasks.items():
                if task.is_terminal and task.end_time is not None:
                    if (now - task.end_time) > max_age_seconds:
                        to_remove.append(tid)
        count = 0
        for tid in to_remove:
            if self.remove(tid):
                count += 1
        return count

    def kill_task(self, task_id: str) -> Optional[BackgroundTaskState]:
        """Kill a running background task.

        Returns the updated task state, or None if not found.
        """
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return None
        if task.is_terminal:
            return task

        # Send SIGTERM → SIGKILL.
        graceful_kill(task.pid, grace_ms=2000)

        # Wait briefly for the monitor thread to pick up the exit.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not task.is_terminal:
            time.sleep(0.1)

        # If still running, force-update state.
        if not task.is_terminal:
            task.status = "killed"
            task.end_time = time.monotonic()
            task.exit_code = -9

        return task

    # ------------------------------------------------------------------
    # Background monitoring
    # ------------------------------------------------------------------

    def _monitor_task(self, task_id: str) -> None:
        """Daemon thread: poll the process until it exits."""
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None or task._process is None:
            return

        proc = task._process
        while True:
            ret = proc.poll()
            if ret is not None:
                # Process exited.
                task.exit_code = ret
                task.end_time = time.monotonic()
                task.status = "completed" if ret == 0 else "failed"

                # Stop watchdogs.
                if task._size_watchdog:
                    task._size_watchdog.stop()
                if task._stall_watchdog:
                    task._stall_watchdog.stop()

                logger.info(
                    "Background task %s finished: exit_code=%d, elapsed=%.1fs",
                    task_id, ret, task.elapsed_seconds,
                )
                break
            time.sleep(0.5)
