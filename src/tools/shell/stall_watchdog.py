"""Stall watchdog — detects background tasks waiting for interactive input.

Monitors a background task's output file for growth.  If the file
stops growing for a configurable duration and the last line of output
matches a known interactive-prompt pattern (e.g. ``(y/n)``,
``Continue?``), the watchdog sets a stall notification on the task.

Design aligned with Claude Code's startStallWatchdog() in
LocalShellTask.tsx (5s poll, 45s threshold, pattern-based detection).
"""

import re
import threading
import time
from typing import Callable, Optional

from src.lib.logging import get_logger
from src.lib.runtime import copy_runtime_context
from src.tools.shell.output_reader import AnchoredOutputReader

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


def _build_stall_message(task_id: str, last_line: str) -> str:
    return (
        f'Background task "{task_id}" appears to be '
        f"waiting for interactive input.\n"
        f"Last output line: {last_line.strip()}\n"
        f"Consider killing this task and re-running with "
        f"non-interactive flags (e.g. -y, --yes, --non-interactive)."
    )


def detect_stall_prompt(
    task_id: str,
    output_path: str,
    *,
    output_fd: int | None = None,
    output_reader: AnchoredOutputReader | None = None,
) -> Optional[str]:
    """Return a stall warning if the current output tail ends in a prompt."""
    owned_reader = None
    reader = output_reader
    if reader is None:
        try:
            if output_fd is None:
                owned_reader = AnchoredOutputReader.from_path(output_path)
            else:
                owned_reader = AnchoredOutputReader.from_fd(
                    output_fd,
                    path=output_path,
                )
        except OSError:
            return None
        reader = owned_reader

    try:
        tail = _read_tail_from_reader(reader)
        if not tail:
            return None

        last_line = tail.rstrip().rsplit("\n", 1)[-1]
        if _matches_prompt(last_line):
            return _build_stall_message(task_id, last_line)
        return None
    finally:
        if owned_reader is not None:
            owned_reader.close()


def _read_tail(output_path: str) -> str:
    """Read the last _TAIL_BYTES from the output file."""
    try:
        reader = AnchoredOutputReader.from_path(output_path)
    except OSError:
        return ""
    try:
        return _read_tail_from_reader(reader)
    finally:
        reader.close()


def _read_tail_from_reader(reader: AnchoredOutputReader) -> str:
    return reader.read_tail(_TAIL_BYTES).decode(errors="replace")


def _matches_prompt(line: str) -> bool:
    """Check if a line matches any known interactive-prompt pattern."""
    for pattern in PROMPT_PATTERNS:
        if pattern.search(line):
            return True
    return False


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
        on_stall: Optional[Callable[[str], None]] = None,
        output_reader: AnchoredOutputReader | None = None,
        output_fd: int | None = None,
    ):
        self._task_id = task_id
        self._output_path = output_path
        self._poll_interval = poll_interval
        self._stall_threshold = stall_threshold
        self._on_stall = on_stall
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

        self._stopped = False
        self._thread: Optional[threading.Thread] = None
        self.stall_message: Optional[str] = None

    def start(self) -> None:
        """Start the polling thread."""
        self._stopped = False
        runtime_context = copy_runtime_context()
        self._thread = threading.Thread(
            target=runtime_context.run,
            args=(self._poll_loop,),
            daemon=True,
            name=f"stall-watchdog-{self._task_id}",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the watchdog (idempotent)."""
        self._stopped = True
        if self._owns_output_reader and self._output_reader is not None:
            self._output_reader.close()

    def _poll_loop(self) -> None:
        """Main polling loop — runs on a daemon thread."""
        last_size = 0
        last_growth = time.monotonic()

        while not self._stopped:
            try:
                reader = self._output_reader
                if reader is None:
                    self._sleep(self._poll_interval)
                    continue
                current_size = reader.size() if reader is not None else 0

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
                message = detect_stall_prompt(
                    self._task_id,
                    self._output_path,
                    output_reader=reader,
                )
                if not message:
                    tail = _read_tail_from_reader(reader) if reader is not None else ""
                    if not tail:
                        # No output yet — reset and keep watching.
                        last_growth = time.monotonic()
                        self._sleep(self._poll_interval)
                        continue
                    # Output stalled but no prompt — silently keep watching.
                    # Reset growth time to avoid repeated checks on same data.
                    last_growth = time.monotonic()
                    self._sleep(self._poll_interval)
                    continue

                last_line = _read_tail_from_reader(reader).rstrip().rsplit("\n", 1)[-1] if reader is not None else ""
                self.stall_message = message
                if self._on_stall is not None:
                    try:
                        self._on_stall(message)
                    except Exception:
                        pass
                logger.info(
                    "Stall watchdog detected prompt for task %s: %s",
                    self._task_id,
                    last_line.strip()[:80],
                )
                # One-shot: stop after detection.
                self._stopped = True
                if self._owns_output_reader and reader is not None:
                    reader.close()
                return

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

    @staticmethod
    def _matches_prompt(line: str) -> bool:
        """Check if a line matches any known interactive-prompt pattern."""
        return _matches_prompt(line)
