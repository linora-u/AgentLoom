"""
Base heartbeat writer with daemon-thread + atomic-write infrastructure.

Subclasses only need to implement :meth:`_build_payload` (what data to
write) and optionally :meth:`_on_stopping` (cleanup before the final write).
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import time
from pathlib import Path


class BaseHeartbeatWriter:
    """Abstract base for all heartbeat writers.

    Provides:
    - A daemon thread that calls :meth:`_write_once` every *interval* seconds.
    - Atomic file writes (tmp → rename) so a crash never corrupts the file.
    - ``atexit`` hook that writes a final beat on interpreter shutdown.

    Subclasses **must** override:

    - ``_build_payload() -> dict``  – the JSON object to persist.
    - ``_on_stopping()``            – called inside ``stop()`` before the
      final write (e.g. set ``status = "stopped"``).
    """

    def __init__(self, path: Path, agent_name: str, interval: float = 5.0):
        self._path = Path(path)
        self._agent_name = agent_name
        self._interval = interval

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._registered_atexit = False

    # ── public API ───────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background heartbeat thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name=f"heartbeat-{self._agent_name}",
        )
        self._thread.start()

        if not self._registered_atexit:
            atexit.register(self._on_exit)
            self._registered_atexit = True

    def stop(self) -> None:
        """Stop the heartbeat thread and write a final beat."""
        self._on_stopping()
        self._write_once()
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def path(self) -> Path:
        return self._path

    # ── abstract hooks (override in subclasses) ──────────────────────

    def _build_payload(self) -> dict:
        """Return the JSON-serialisable dict to write.  **Must override.**"""
        raise NotImplementedError

    def _on_stopping(self) -> None:
        """Called inside ``stop()`` before the final write.  Override as needed."""

    def _on_exit(self) -> None:
        """Called via ``atexit``.  Override for custom shutdown behaviour."""

    # ── internals ────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._write_once()
            self._stop_event.wait(self._interval)

    def _write_once(self) -> None:
        try:
            data = self._build_payload()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.rename(self._path)
        except Exception:
            pass  # heartbeat must never crash the host process
