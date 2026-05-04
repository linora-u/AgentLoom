"""
Worker-level aggregated heartbeat writer.

Each **worker type** (e.g. ``scan_code``) gets **one** heartbeat file that
tracks all concurrent calls.  This avoids file-per-call proliferation when
``tool.batch()`` launches many parallel workers.

File layout::

    .runtime/{supervisor}/checkpoints/{task_id}/workers/{worker_name}/heartbeat.json

JSON payload::

    {
      "agent_name": "scan_code",
      "pid": 12345,
      "timestamp": 1711800000.0,
      "timestamp_iso": "2026-03-31T10:00:00Z",
      "calls": {
        "0": {"status": "running",   "thread_id": 456, "step": 3, "started_at": "..."},
        "1": {"status": "completed", "thread_id": 789, "step": 5, "finished_at": "..."}
      }
    }

Thread-safety: all mutations go through ``threading.Lock`` because
``ParallelAgentExecutor`` invokes workers from different threads.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from src.lib.heartbeat._base import BaseHeartbeatWriter


class WorkerHeartbeat(BaseHeartbeatWriter):
    """Aggregated heartbeat for one worker type across multiple calls.

    Unlike :class:`SupervisorHeartbeat` (one flat record), this writer
    maintains a ``calls`` dict keyed by ``call_index`` so the dashboard
    can inspect each concurrent invocation individually.
    """

    def __init__(
        self,
        path: Path,
        agent_name: str,
        interval: float = 5.0,
    ):
        super().__init__(path=path, agent_name=agent_name, interval=interval)
        self._lock = threading.Lock()
        self._calls: dict[str, dict] = {}

    # ── public API (thread-safe) ─────────────────────────────────────

    def register_call(self, call_index: int) -> None:
        """Register a new worker call.  Called at the start of execution."""
        key = str(call_index)
        with self._lock:
            self._calls[key] = {
                "status": "running",
                "thread_id": threading.get_ident(),
                "step": 0,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            }

    def update_call_status(self, call_index: int, status: str) -> None:
        """Update the status of an existing call (e.g. completed / failed)."""
        key = str(call_index)
        with self._lock:
            if key in self._calls:
                self._calls[key]["status"] = status
                if status in ("completed", "failed"):
                    self._calls[key]["finished_at"] = time.strftime(
                        "%Y-%m-%dT%H:%M:%S%z", time.localtime()
                    )

    def update_call_step(self, call_index: int, step: int) -> None:
        """Increment the step counter for a running call."""
        key = str(call_index)
        with self._lock:
            if key in self._calls:
                self._calls[key]["step"] = step

    def all_calls_terminal(self) -> bool:
        """Return *True* when every registered call is completed or failed."""
        with self._lock:
            if not self._calls:
                return False
            return all(
                c.get("status") in ("completed", "failed")
                for c in self._calls.values()
            )

    # ── BaseHeartbeatWriter hooks ────────────────────────────────────

    def _build_payload(self) -> dict:
        with self._lock:
            calls_snapshot = {k: dict(v) for k, v in self._calls.items()}
        return {
            "agent_name": self._agent_name,
            "pid": os.getpid(),
            "timestamp": time.time(),
            "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            "calls": calls_snapshot,
        }

    def _on_stopping(self) -> None:
        """Mark any still-running calls as 'stopped'."""
        with self._lock:
            for call in self._calls.values():
                if call.get("status") == "running":
                    call["status"] = "stopped"
                    call["finished_at"] = time.strftime(
                        "%Y-%m-%dT%H:%M:%S%z", time.localtime()
                    )

    def _on_exit(self) -> None:
        """atexit: write a final snapshot so crash detection can work."""
        with self._lock:
            has_running = any(
                c.get("status") == "running" for c in self._calls.values()
            )
        if has_running:
            try:
                self._write_once()
            except Exception:
                pass
