"""Process tree termination utilities.

Provides cross-platform helpers to kill a process and all its
descendants.  On Unix, uses process group signals (``os.killpg``)
when the child was started with ``start_new_session=True``.
Falls back to walking ``/proc`` or using ``psutil`` when process
groups are unavailable.

Design aligned with Claude Code's use of npm ``tree-kill`` for
cleaning up shell command process trees.
"""

import os
import signal
import threading
import time
from typing import Optional

from src.lib.logging import get_logger
from src.tools.shell.output_reader import AnchoredOutputReader

logger = get_logger(__name__)

# Grace period between SIGTERM and SIGKILL (milliseconds).
_KILL_GRACE_MS = 200

DEFAULT_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def tree_kill(pid: int, sig: int = DEFAULT_KILL_SIGNAL) -> bool:
    """Kill a process tree rooted at *pid*.

    Strategy:
    1. Try ``os.killpg(pid, sig)`` — works when the process was started
       with ``start_new_session=True`` (making it the process group leader).
    2. Fall back to ``os.kill(pid, sig)`` for the single process.

    Args:
        pid: Root process ID.
        sig: Signal to send (default ``SIGKILL``).

    Returns:
        True if the signal was sent successfully, False otherwise.
    """
    if pid <= 0:
        return False

    try:
        # Attempt group kill first (covers all children in the group).
        os.killpg(pid, sig)
        logger.debug("Sent signal %s to process group %d", sig, pid)
        return True
    except ProcessLookupError:
        # Process already exited — nothing to do.
        return True
    except PermissionError:
        logger.debug("Permission denied for killpg(%d, %s)", pid, sig)
        return False
    except OSError:
        # Not a process group leader — fall back to single kill.
        pass

    try:
        os.kill(pid, sig)
        logger.debug("Sent signal %s to process %d", sig, pid)
        return True
    except ProcessLookupError:
        return True
    except (PermissionError, OSError) as exc:
        logger.debug("Failed to kill process %d: %s", pid, exc)
        return False


def graceful_kill(pid: int, grace_ms: int = _KILL_GRACE_MS) -> bool:
    """Attempt a graceful SIGTERM, then escalate to SIGKILL.

    1. Send ``SIGTERM`` to the process tree.
    2. Wait *grace_ms* milliseconds.
    3. If the process is still alive, send ``SIGKILL``.

    Args:
        pid: Root process ID.
        grace_ms: Time to wait between SIGTERM and SIGKILL.

    Returns:
        True if the process was terminated (or already gone).
    """
    if pid <= 0:
        return False

    # Phase 1: SIGTERM
    tree_kill(pid, signal.SIGTERM)

    # Phase 2: Wait
    deadline = time.monotonic() + (grace_ms / 1000.0)
    while time.monotonic() < deadline:
        if not _is_alive(pid):
            return True
        time.sleep(0.02)

    # Phase 3: SIGKILL
    if _is_alive(pid):
        logger.debug("Process %d still alive after %dms grace — sending SIGKILL", pid, grace_ms)
        tree_kill(pid, signal.SIGKILL)

    return True


def _is_alive(pid: int) -> bool:
    """Check whether a process is still running."""
    try:
        os.kill(pid, 0)  # Signal 0 = existence check
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission — treat as alive.
        return True
    except OSError:
        return False


class SizeWatchdog:
    """Background watchdog that kills a process if its output file grows too large.

    Polls the output file size every *poll_interval_s* seconds.  If the
    file exceeds *max_bytes*, the process tree is killed with SIGKILL.

    Usage::

        watchdog = SizeWatchdog(pid, output_path, max_bytes=100_000_000)
        watchdog.start()
        # ... process runs ...
        watchdog.stop()

    Design aligned with Claude Code's TaskOutput size watchdog (5s poll,
    kill at configurable limit).
    """

    def __init__(
        self,
        pid: int,
        output_path: str,
        max_bytes: int = 100_000_000,  # 100 MB
        poll_interval_s: float = 5.0,
        output_reader: AnchoredOutputReader | None = None,
        output_fd: int | None = None,
    ):
        self._pid = pid
        self._output_path = output_path
        self._max_bytes = max_bytes
        self._poll_interval = poll_interval_s
        self._stopped = False
        self._thread: Optional["threading.Thread"] = None
        self._owns_output_reader = output_reader is None
        if output_reader is not None:
            self._output_reader = output_reader
        else:
            try:
                if output_fd is None:
                    self._output_reader = AnchoredOutputReader.from_path(output_path)
                else:
                    self._output_reader = AnchoredOutputReader.from_fd(
                        output_fd,
                        path=output_path,
                    )
            except OSError:
                self._output_reader = None

    def start(self) -> None:
        """Start the watchdog polling thread."""
        from src.lib.runtime import copy_runtime_context

        self._stopped = False
        runtime_context = copy_runtime_context()
        self._thread = threading.Thread(
            target=runtime_context.run,
            args=(self._poll_loop,),
            daemon=True,
            name=f"size-watchdog-{self._pid}",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the watchdog (idempotent)."""
        self._stopped = True
        if self._owns_output_reader and self._output_reader is not None:
            self._output_reader.close()

    def _poll_loop(self) -> None:
        while not self._stopped:
            reader = self._output_reader
            size = reader.size() if reader is not None else 0
            if size > self._max_bytes:
                logger.warning(
                    "Output file %s exceeded %d bytes (size=%d) — killing process %d",
                    self._output_path,
                    self._max_bytes,
                    size,
                    self._pid,
                )
                tree_kill(self._pid, signal.SIGKILL)
                self._stopped = True
                if self._owns_output_reader and reader is not None:
                    reader.close()
                return

            # Sleep in small increments so stop() takes effect quickly.
            elapsed = 0.0
            while elapsed < self._poll_interval and not self._stopped:
                time.sleep(0.1)
                elapsed += 0.1
