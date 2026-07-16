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

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Literal, Optional

from src.lib.config import C
from src.lib.logging import get_logger
from src.lib.runtime import copy_runtime_context, get_current_run_context
from src.tools.shell.output_reader import AnchoredOutputReader
from src.tools.shell.tree_kill import SizeWatchdog, graceful_kill

logger = get_logger(__name__)

# Default configuration values.
_DEFAULT_MAX_CONCURRENT = 10
_DEFAULT_MAX_OUTPUT_BYTES = 100_000_000  # 100 MB
_DEFAULT_STALL_THRESHOLD = 45  # seconds
RuntimeKey = tuple[str, str, str, str] | None


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

        A running task can also carry ``stall_message`` when its output
        appears to be waiting for interactive input.  That warning is
        orthogonal to terminal state; callers can inspect it and decide
        whether to kill the task.
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
    _runtime_key: RuntimeKey = field(default=None, repr=False)
    _output_reader: Optional[AnchoredOutputReader] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Directly-created states are supported by a few local callers.  Anchor
        # once here; all later reads are descriptor-based.  The registry passes
        # a reader derived from the writer FD and does not take this fallback.
        if self._output_reader is None:
            try:
                self._output_reader = AnchoredOutputReader.from_path(self.output_path)
            except OSError:
                self._output_reader = None

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
        reader = self._output_reader
        return reader.size() if reader is not None else 0

    def read_output_tail(self, n_lines: int = 20) -> str:
        """Read the last *n_lines* lines from the output file."""
        reader = self._output_reader
        if reader is None:
            return ""
        tail_text = reader.read_tail(65536).decode("utf-8", errors="replace")
        lines = tail_text.splitlines()
        return "\n".join(lines[-n_lines:])

    def close_output_reader(self) -> None:
        """Release the retained output inode capability."""

        reader, self._output_reader = self._output_reader, None
        if reader is not None:
            reader.close()


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
                inst._tasks: Dict[tuple[RuntimeKey, str], BackgroundTaskState] = {}
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
                        if not task.is_terminal:
                            try:
                                graceful_kill(task.pid, grace_ms=500)
                            except Exception:
                                pass
                        if task._size_watchdog:
                            task._size_watchdog.stop()
                        if task._stall_watchdog:
                            task._stall_watchdog.stop()
                        task.close_output_reader()
                    inst._tasks.clear()
            cls._instance = None

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cfg_max_concurrent() -> int:
        return C.get_nested(
            "shell_settings",
            "background_tasks",
            "max_concurrent",
            default=_DEFAULT_MAX_CONCURRENT,
        )

    @staticmethod
    def _cfg_enabled() -> bool:
        return C.get_nested(
            "shell_settings",
            "background_tasks",
            "enabled",
            default=True,
        )

    @staticmethod
    def _cfg_auto_background() -> bool:
        return C.get_nested(
            "shell_settings",
            "background_tasks",
            "auto_background_on_timeout",
            default=True,
        )

    @staticmethod
    def _cfg_max_output_bytes() -> int:
        return C.get_nested(
            "shell_settings",
            "background_tasks",
            "max_output_bytes",
            default=_DEFAULT_MAX_OUTPUT_BYTES,
        )

    @staticmethod
    def _cfg_stall_detection() -> bool:
        return C.get_nested(
            "shell_settings",
            "background_tasks",
            "stall_detection",
            default=True,
        )

    @staticmethod
    def _cfg_stall_threshold() -> int:
        return C.get_nested(
            "shell_settings",
            "background_tasks",
            "stall_threshold_seconds",
            default=_DEFAULT_STALL_THRESHOLD,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def _current_runtime_key() -> RuntimeKey:
        context = get_current_run_context()
        return context.runtime_key if context is not None else None

    @staticmethod
    def _task_key(task_id: str, runtime_key: RuntimeKey) -> tuple[RuntimeKey, str]:
        return runtime_key, task_id

    def register(
        self,
        process,
        command: str,
        output_path: str,
        description: str = "",
        size_watchdog: Optional[SizeWatchdog] = None,
        output_fd: Optional[int] = None,
    ) -> str:
        """Register a running process as a background task.

        Args:
            process: A ``subprocess.Popen`` instance (must be running).
            command: The shell command string.
            output_path: Path to the output file being written by the process.
            description: Human-readable description (defaults to command).
            size_watchdog: An existing SizeWatchdog instance to transfer.
            output_fd: Original output descriptor.  When supplied, registry
                reads stay attached to its inode even if the path is replaced.

        Returns:
            The task_id assigned to this background task.

        Raises:
            RuntimeError: If the max-concurrent limit is reached.
        """
        runtime_key = self._current_runtime_key()
        if output_fd is None:
            output_reader = AnchoredOutputReader.from_path(output_path)
        else:
            output_reader = AnchoredOutputReader.from_fd(
                output_fd,
                path=output_path,
            )
        try:
            with self._lock:
                running = sum(
                    1 for (owner, _), task in self._tasks.items() if owner == runtime_key and not task.is_terminal
                )
                task_id = uuid.uuid4().hex[:12]
                max_c = self._cfg_max_concurrent()
                if running >= max_c:
                    raise RuntimeError(
                        f"Maximum concurrent background tasks reached ({max_c}). "
                        "Kill or wait for existing tasks before starting new ones."
                    )

                while self._task_key(task_id, runtime_key) in self._tasks:
                    task_id = uuid.uuid4().hex[:12]
                task = BackgroundTaskState(
                    task_id=task_id,
                    command=command,
                    description=description or command[:80],
                    pid=process.pid,
                    output_path=output_path,
                    _process=process,
                    _size_watchdog=size_watchdog,
                    _runtime_key=runtime_key,
                    _output_reader=output_reader,
                )
                self._tasks[self._task_key(task_id, runtime_key)] = task
        except BaseException:
            output_reader.close()
            raise

        # Start stall watchdog if enabled.
        if self._cfg_stall_detection():
            from src.tools.shell.stall_watchdog import StallWatchdog

            def _record_stall(message: str) -> None:
                with self._lock:
                    current = self._tasks.get(self._task_key(task_id, runtime_key))
                    if current is not None and not current.is_terminal:
                        current.stall_message = message

            stall_threshold = self._cfg_stall_threshold()

            sw = StallWatchdog(
                task_id=task_id,
                output_path=output_path,
                output_reader=output_reader,
                poll_interval=max(0.2, min(5.0, stall_threshold / 2.0)),
                stall_threshold=stall_threshold,
                on_stall=_record_stall,
            )
            task._stall_watchdog = sw
            sw.start()

        # Start monitoring only after every watchdog is attached.  Otherwise a
        # fast child can exit between monitor start and stall-watchdog setup,
        # leaving the late watchdog alive forever.
        monitor_context = copy_runtime_context()
        monitor = threading.Thread(
            target=monitor_context.run,
            args=(self._monitor_task, task_id, runtime_key),
            daemon=True,
            name=f"bg-monitor-{task_id}",
        )
        monitor.start()

        logger.info(
            "Background task %s registered: pid=%d, command=%s",
            task_id,
            process.pid,
            command[:120],
        )
        return task_id

    def get(self, task_id: str) -> Optional[BackgroundTaskState]:
        """Return the task state, or None if not found."""
        runtime_key = self._current_runtime_key()
        with self._lock:
            return self._tasks.get(self._task_key(task_id, runtime_key))

    def remove(self, task_id: str) -> bool:
        """Remove a task from the registry. Returns True if found."""
        runtime_key = self._current_runtime_key()
        with self._lock:
            task = self._tasks.pop(self._task_key(task_id, runtime_key), None)
        if task is None:
            return False
        # Clean up watchdogs.
        if task._size_watchdog:
            task._size_watchdog.stop()
        if task._stall_watchdog:
            task._stall_watchdog.stop()
        task.close_output_reader()
        return True

    def list_all(self) -> List[BackgroundTaskState]:
        """Return a snapshot of all tracked tasks."""
        runtime_key = self._current_runtime_key()
        with self._lock:
            return [task for (owner, _), task in self._tasks.items() if owner == runtime_key]

    def list_running(self) -> List[BackgroundTaskState]:
        """Return only tasks that are still running."""
        runtime_key = self._current_runtime_key()
        with self._lock:
            return [
                task for (owner, _), task in self._tasks.items() if owner == runtime_key and task.status == "running"
            ]

    def cleanup_completed(self, max_age_seconds: float = 3600) -> int:
        """Remove completed/failed/killed tasks older than *max_age_seconds*.

        Returns the number of tasks evicted.
        """
        now = time.monotonic()
        to_remove: List[str] = []
        runtime_key = self._current_runtime_key()
        with self._lock:
            for (owner, tid), task in self._tasks.items():
                if owner != runtime_key:
                    continue
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
        task = self.get(task_id)
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

    def terminate_current_run(self) -> int:
        """Kill and forget every background task owned by the bound run.

        The registry is process-global, so teardown must select by the full
        runtime key.  A caller without an explicit ``RuntimeContext`` is not
        allowed to touch any task, including legacy no-context entries.
        """
        runtime_key = self._current_runtime_key()
        if runtime_key is None:
            return 0

        with self._lock:
            owned_keys = [key for key in self._tasks if key[0] == runtime_key]
            tasks = [self._tasks.pop(key) for key in owned_keys]
            now = time.monotonic()
            for task in tasks:
                if not task.is_terminal:
                    task.status = "killed"
                    task.end_time = now
                    task.exit_code = -15

        for task in tasks:
            if task._size_watchdog:
                try:
                    task._size_watchdog.stop()
                except Exception:
                    pass
            if task._stall_watchdog:
                try:
                    task._stall_watchdog.stop()
                except Exception:
                    pass
            process = task._process
            if process is not None and process.poll() is None:
                try:
                    graceful_kill(task.pid, grace_ms=500)
                except Exception as exc:
                    logger.warning(
                        "Failed to terminate background task %s for run teardown: %s",
                        task.task_id,
                        exc,
                    )
            task.close_output_reader()

        return len(tasks)

    # ------------------------------------------------------------------
    # Background monitoring
    # ------------------------------------------------------------------

    def _monitor_task(self, task_id: str, runtime_key: RuntimeKey) -> None:
        """Daemon thread: poll the process until it exits."""
        with self._lock:
            task = self._tasks.get(self._task_key(task_id, runtime_key))
        if task is None or task._process is None:
            return

        proc = task._process
        while True:
            ret = proc.poll()
            if ret is not None:
                # Process exited.
                if not task.is_terminal:
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
                    task_id,
                    ret,
                    task.elapsed_seconds,
                )
                break
            time.sleep(0.5)
