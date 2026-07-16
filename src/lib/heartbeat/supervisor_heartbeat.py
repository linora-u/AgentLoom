"""
Supervisor-level heartbeat writer.

Periodically writes a JSON file so that external monitors (scripts, UIs)
can determine whether the supervisor agent process is alive – even when it
is blocked on a long-running LLM call with no log output.

Usage::

    hb = SupervisorHeartbeat(
        path=runtime_context.heartbeat_path,
        agent_name="code_review_agent",
        run_id=runtime_context.run_id,
    )
    hb.start()
    ...
    hb.update_step(3)
    ...
    hb.stop()
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from src.lib.heartbeat._base import BaseHeartbeatWriter
from src.lib.runtime import SecureDirectory


class SupervisorHeartbeat(BaseHeartbeatWriter):
    """Daemon thread that writes ``heartbeat.json`` every *interval* seconds.

    The file contains::

        {"pid": 12345, "timestamp": 1711800000.0, "timestamp_iso": "...",
         "status": "running", "step": 0, "agent_name": "..."}

    ``status`` is one of ``"running"``, ``"stopped"``, ``"exited"``.
    """

    def __init__(
        self,
        path: Path,
        agent_name: str,
        run_id: str,
        interval: float = 5.0,
        storage: SecureDirectory | None = None,
    ):
        super().__init__(
            path=path,
            agent_name=agent_name,
            run_id=run_id,
            interval=interval,
            storage=storage,
        )
        self._step: int = 0
        self._status: str = "running"

    # ── public API ───────────────────────────────────────────────────

    def update_step(self, step: int) -> None:
        self._step = step

    def update_status(self, status: str) -> None:
        self._status = status

    # ── BaseHeartbeatWriter hooks ────────────────────────────────────

    def _build_payload(self) -> dict:
        return {
            "pid": os.getpid(),
            "timestamp": time.time(),
            "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            "status": self._status,
            "step": self._step,
            "agent_name": self._agent_name,
            "run_id": self._run_id,
        }

    def _on_stopping(self) -> None:
        self._status = "stopped"

    def _on_exit(self) -> None:
        if self._status == "running":
            self._status = "exited"
            try:
                self._write_once()
            except Exception:
                pass
