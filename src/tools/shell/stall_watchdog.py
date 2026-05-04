"""Stall watchdog — detects background tasks waiting for interactive input.

Monitors a background task's output file for growth.  If the file
stops growing for a configurable duration and the last line of output
matches a known interactive-prompt pattern (e.g. ``(y/n)``,
``Continue?``), the watchdog sets a stall notification on the task.

Design aligned with Claude Code's startStallWatchdog() in
LocalShellTask.tsx (5s poll, 45s threshold, pattern-based detection).
"""

import os
import re
import threading
import time
from typing import Optional

from src.lib.logging import get_logger

logger = get_logger(__name__)

# Patterns that indicate the process is waiting for user input.
PROMPT_PATTERNS = [
    re.compile(r"\(y/n\)", re.IGNORECASE),
    re.compile(r"\[y/n\]", re.IGNORECASE),
    re.compile(r"\(yes/no\)", re.IGNORECASE),
    re.compile(
        r"\b(?:Do you|Would you|Shall I|Are you sure|Ready to)\b.*\?\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"Press (?:any key|Enter)", re.IGNORECASE),
    re.compile(r"Continue\?", re.IGNORECASE),
    re.compile(r"Overwrite\?", re.IGNORECASE),
    re.compile(r"Proceed\?", re.IGNORECASE),
    re.compile(r"\[Y/n\]"),
    re.compile(r"\[yes/No\]", re.IGNORECASE),
]

# How many bytes of the tail to read when checking for prompts.
_TAIL_BYTES = 1024


class StallWatchdog:
    """Detect background tasks stalled on interactive prompts.

    Usage::

        sw = StallWatchdog(task_id="abc123", output_path="/tmp/out.txt")
        sw.start()
        # ... later ...
        if sw.stall_message:
            print("Task appears stalled:", sw.stall_message)
        sw.stop()
    """

    def __init__(
        self,
        task_id: str,
        output_path: str,
        poll_interval: float = 5.0,
        stall_threshold: float = 45.0,
    ):
        self._task_id = task_id
        self._output_path = output_path
        self._poll_interval = poll_interval
        self._stall_threshold = stall_threshold

        self._stopped = False
        self._thread: Optional[threading.Thread] = None
        self.stall_message: Optional[str] = None

    def start(self) -> None:
        """Start the polling thread."""
        self._stopped = False
        self._thread = threading.Thread(
            target=self._poll_loop,
            daemon=True,
            name=f"stall-watchdog-{self._task_id}",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the watchdog (idempotent)."""
        self._stopped = True

    def _poll_loop(self) -> None:
        """Main polling loop — runs on a daemon thread."""
        last_size = 0
        last_growth = time.monotonic()

        while not self._stopped:
            try:
                if os.path.exists(self._output_path):
                    current_size = os.path.getsize(self._output_path)
                else:
                    current_size = 0

                if current_size > last_size:
                    last_size = current_size
                    last_growth = time.monotonic()
                    # Output growing — sleep and recheck.
                    self._sleep(self._poll_interval)
                    continue

                stall_duration = time.monotonic() - last_growth
                if stall_duration < self._stall_threshold:
                    self._sleep(self._poll_interval)
                    continue

                # Output has been stalled long enough — check for prompt.
                tail = self._read_tail()
                if not tail:
                    # No output yet — reset and keep watching.
                    last_growth = time.monotonic()
                    self._sleep(self._poll_interval)
                    continue

                last_line = tail.rstrip().rsplit("\n", 1)[-1]
                if self._matches_prompt(last_line):
                    self.stall_message = (
                        f'Background task "{self._task_id}" appears to be '
                        f"waiting for interactive input.\n"
                        f"Last output line: {last_line.strip()}\n"
                        f"Consider killing this task and re-running with "
                        f"non-interactive flags (e.g. -y, --yes, --non-interactive)."
                    )
                    logger.info(
                        "Stall watchdog detected prompt for task %s: %s",
                        self._task_id,
                        last_line.strip()[:80],
                    )
                    # One-shot: stop after detection.
                    self._stopped = True
                    return
                else:
                    # Output stalled but no prompt — silently keep watching.
                    # Reset growth time to avoid repeated checks on same data.
                    last_growth = time.monotonic()

            except Exception:
                # Never crash the watchdog thread.
                pass

            self._sleep(self._poll_interval)

    def _sleep(self, seconds: float) -> None:
        """Sleep in small increments so stop() takes effect quickly."""
        elapsed = 0.0
        while elapsed < seconds and not self._stopped:
            time.sleep(min(0.2, seconds - elapsed))
            elapsed += 0.2

    def _read_tail(self) -> str:
        """Read the last _TAIL_BYTES from the output file."""
        try:
            with open(self._output_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - _TAIL_BYTES))
                data = f.read()
            return data.decode(errors="replace")
        except (OSError, IOError):
            return ""

    @staticmethod
    def _matches_prompt(line: str) -> bool:
        """Check if a line matches any known interactive-prompt pattern."""
        for pattern in PROMPT_PATTERNS:
            if pattern.search(line):
                return True
        return False
