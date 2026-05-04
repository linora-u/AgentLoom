"""Async hook registry for tracking background hook processes.

Aligned with upstream ``AsyncHookRegistry.ts``:
- Tracks pending background hook processes by process ID
- **Stores ``subprocess.Popen`` handles** to enable real process control
- Provides polling mechanism to check for completed responses
- Kills timed-out processes via SIGTERM->SIGKILL escalation
- Supports asyncRewake: exit code 2 triggers task notification
- Cleanup on shutdown
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.lib.logging import get_logger

from .types import HookResult

logger = get_logger(__name__)

# Default timeout for async hooks before they are considered stale.
DEFAULT_ASYNC_TIMEOUT_MS: int = 15_000  # 15 seconds


@dataclass
class PendingAsyncHook:
    """Tracks a single background hook process.

    The ``process_handle`` field stores the live ``subprocess.Popen``
    object so the registry can:
    - Poll for natural completion (``proc.poll()``)
    - Collect stdout/stderr on completion (``proc.communicate()``)
    - Kill the entire process group on timeout
    """
    process_id: str
    hook_id: str
    hook_event: str
    hook_name: str
    command: str
    start_time: float = field(default_factory=time.monotonic)
    timeout_ms: int = DEFAULT_ASYNC_TIMEOUT_MS
    response_sent: bool = False
    result: Optional[HookResult] = None
    completed: bool = False
    process_handle: Optional[subprocess.Popen] = None


class AsyncHookRegistry:
    """Global registry for background hook processes.

    Thread-safe singleton that tracks all async hooks and provides
    a polling mechanism to retrieve completed results.  Holds live
    ``Popen`` handles so timed-out processes can be force-killed.
    """

    _instance: Optional["AsyncHookRegistry"] = None
    _lock = threading.RLock()

    def __init__(self) -> None:
        self._data_lock = threading.RLock()
        self._pending: Dict[str, PendingAsyncHook] = {}

    @classmethod
    def get_instance(cls) -> "AsyncHookRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def register(self, hook: PendingAsyncHook) -> None:
        """Register a new pending async hook."""
        with self._data_lock:
            self._pending[hook.process_id] = hook
        logger.debug(
            "Registered async hook: %s (event=%s, timeout=%dms, has_handle=%s)",
            hook.process_id, hook.hook_event, hook.timeout_ms,
            hook.process_handle is not None,
        )

    def mark_completed(
        self,
        process_id: str,
        result: HookResult,
    ) -> None:
        """Mark a pending hook as completed with a result."""
        with self._data_lock:
            hook = self._pending.get(process_id)
            if hook is not None:
                hook.completed = True
                hook.result = result
                logger.debug("Async hook completed: %s", process_id)

    def check_for_responses(self) -> List[PendingAsyncHook]:
        """Check for completed async hooks that haven't been delivered yet.

        For hooks with a live ``process_handle``:
        - If the process exited naturally, collect its stdout/stderr and
          build a result from exit code + output.
        - If the timeout has elapsed, **kill the entire process group**
          via ``kill_hook_process_group()`` and return a timeout result.

        Returns a list of completed hooks whose responses have not yet
        been sent.  Marks returned hooks as ``response_sent = True``.
        """
        from .hook_helpers import kill_hook_process_group

        ready: List[PendingAsyncHook] = []
        now = time.monotonic()

        with self._data_lock:
            for hook in self._pending.values():
                if hook.completed and not hook.response_sent:
                    hook.response_sent = True
                    ready.append(hook)
                    continue

                if hook.completed:
                    continue

                proc = hook.process_handle
                elapsed_ms = (now - hook.start_time) * 1000

                # Check if process finished naturally
                if proc is not None and proc.poll() is not None:
                    hook.completed = True
                    hook.result = self._collect_process_result(hook)
                    hook.response_sent = True
                    ready.append(hook)
                    logger.debug(
                        "Async hook %s completed naturally (exit=%d)",
                        hook.process_id, proc.returncode,
                    )
                    continue

                # Check timeout
                if elapsed_ms > hook.timeout_ms:
                    # Kill the process group if we have a handle
                    if proc is not None and proc.poll() is None:
                        logger.warning(
                            "Async hook %s timed out after %dms, killing process group",
                            hook.process_id, hook.timeout_ms,
                        )
                        kill_hook_process_group(proc)

                    hook.completed = True
                    hook.result = HookResult(
                        success=False,
                        decision="allow",
                        outcome="cancelled",
                        reason=f"Async hook timed out after {hook.timeout_ms}ms",
                    )
                    hook.response_sent = True
                    ready.append(hook)

        return ready

    @staticmethod
    def _collect_process_result(hook: PendingAsyncHook) -> HookResult:
        """Collect stdout/stderr from a naturally-completed process."""
        proc = hook.process_handle
        if proc is None:
            return HookResult(
                success=True, decision="allow", outcome="success",
            )

        try:
            stdout, stderr = proc.communicate(timeout=2)
        except (subprocess.TimeoutExpired, Exception):
            stdout, stderr = "", ""

        exit_code = proc.returncode
        if exit_code == 0:
            return HookResult(success=True, decision="allow", outcome="success")
        if exit_code == 2:
            return HookResult(
                success=False,
                decision="block",
                outcome="blocking",
                reason=stderr.strip() or "Async hook exited with blocking error",
                blocking_error={
                    "blocking_error": stderr.strip() or "exit code 2",
                    "command": hook.command,
                },
            )
        return HookResult(
            success=False,
            decision="allow",
            outcome="non_blocking_error",
            reason=stderr.strip() or f"Async hook exited with code {exit_code}",
        )

    def remove_delivered(self) -> int:
        """Remove all hooks that have been delivered.  Returns count removed."""
        with self._data_lock:
            to_remove = [
                pid for pid, hook in self._pending.items()
                if hook.response_sent
            ]
            for pid in to_remove:
                del self._pending[pid]
            return len(to_remove)

    def finalize_all(self) -> None:
        """Kill all pending processes and mark hooks as completed (shutdown)."""
        from .hook_helpers import kill_hook_process_group

        with self._data_lock:
            for hook in self._pending.values():
                if not hook.completed:
                    # Kill the process if still running
                    if hook.process_handle is not None and hook.process_handle.poll() is None:
                        kill_hook_process_group(hook.process_handle)
                    hook.completed = True
                    hook.result = HookResult(
                        success=False,
                        decision="allow",
                        outcome="cancelled",
                        reason="Shutdown: async hook finalized",
                    )
            logger.debug(
                "Finalized %d pending async hooks", len(self._pending),
            )

    @property
    def pending_count(self) -> int:
        with self._data_lock:
            return sum(1 for h in self._pending.values() if not h.completed)

    def clear(self) -> None:
        """Kill all tracked processes and clear the registry."""
        from .hook_helpers import kill_hook_process_group

        with self._data_lock:
            for hook in self._pending.values():
                if hook.process_handle is not None and hook.process_handle.poll() is None:
                    kill_hook_process_group(hook.process_handle)
            self._pending.clear()
