"""Persistent schedule ticker and liveness status."""

from __future__ import annotations

import json
import os
import signal
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from threading import Event
from typing import Any

from .runner import ScheduleRunner, StopCallback
from .schedule import parse_datetime
from .store import ScheduleStore


class ScheduleServerAlreadyRunning(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ScheduleService:
    def __init__(self, store: ScheduleStore, *, runner: ScheduleRunner | None = None):
        self.store = store
        self.runner = runner or ScheduleRunner(store)
        self.heartbeat_path = store.schedules_dir / "serve-status.json"
        self.server_lock_path = store.schedules_dir / "serve.lock"
        self._started_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error: str | None = None
        self._tick_seconds = 1.0
        self._last_heartbeat_monotonic = 0.0

    def _write_heartbeat(self, *, force: bool = False) -> None:
        monotonic_now = time.monotonic()
        if not force and monotonic_now - self._last_heartbeat_monotonic < 1.0:
            return
        self._last_heartbeat_monotonic = monotonic_now
        payload = {
            "pid": os.getpid(),
            "started_at": self._started_at,
            "last_tick_at": _utc_now().isoformat(),
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "tick_seconds": self._tick_seconds,
            "stopped_at": None,
        }
        self.store.write_state_json("serve-status.json", payload)

    def tick(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
        should_stop: StopCallback | None = None,
    ) -> list[dict[str, Any]]:
        self._write_heartbeat()
        executions = self.runner.run_due(
            now=now,
            limit=limit,
            progress=self._write_heartbeat,
            should_stop=should_stop,
        )
        self._last_success_at = _utc_now().isoformat()
        self._last_error = None
        self._write_heartbeat(force=True)
        return executions

    def run_now(self, job_id: str) -> dict[str, Any]:
        return self.runner.run_now(job_id, progress=self._write_heartbeat)

    def serve(
        self,
        *,
        tick_seconds: float = 1.0,
        stop_event: Event | None = None,
        max_ticks: int | None = None,
    ) -> None:
        """Run a foreground persistent ticker until signalled or stopped."""
        with ExitStack() as stack:
            try:
                stack.enter_context(self.store.file_lock("serve.lock", exclusive=True, blocking=False))
            except BlockingIOError as exc:
                raise ScheduleServerAlreadyRunning(f"A schedule server already owns {self.server_lock_path}") from exc
            self._tick_seconds = max(float(tick_seconds), 0.1)
            self._started_at = _utc_now().isoformat()
            stopping = stop_event or Event()
            previous_handlers: dict[int, Any] = {}

            def request_stop(_signum, _frame) -> None:
                stopping.set()

            if stop_event is None:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    try:
                        previous_handlers[signum] = signal.signal(signum, request_stop)
                    except ValueError:
                        pass
            ticks = 0
            self._write_heartbeat(force=True)
            try:
                while not stopping.is_set():
                    try:
                        self.tick(should_stop=stopping.is_set)
                    except Exception as exc:
                        self._last_error = f"{type(exc).__name__}: {exc}"
                        self._write_heartbeat(force=True)
                    ticks += 1
                    if max_ticks is not None and ticks >= max_ticks:
                        break
                    stopping.wait(self._tick_seconds)
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
                self._write_stopped_heartbeat()

    def _write_stopped_heartbeat(self) -> None:
        payload = {
            "pid": None,
            "started_at": self._started_at,
            "last_tick_at": _utc_now().isoformat(),
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "tick_seconds": self._tick_seconds,
            "stopped_at": _utc_now().isoformat(),
        }
        self.store.write_state_json("serve-status.json", payload)

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False

    def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        checked_at = (now or _utc_now()).astimezone(UTC)
        snapshot = self.store.snapshot()
        jobs = snapshot["jobs"]
        heartbeat: dict[str, Any] = {}
        try:
            stored_heartbeat = self.store.read_state_json("serve-status.json")
            if isinstance(stored_heartbeat, dict):
                heartbeat = stored_heartbeat
        except (FileNotFoundError, json.JSONDecodeError, OSError, RuntimeError):
            pass
        pid = int(heartbeat.get("pid") or 0)
        last_tick_raw = heartbeat.get("last_tick_at")
        recent = False
        if last_tick_raw:
            try:
                age = (checked_at - parse_datetime(str(last_tick_raw))).total_seconds()
                recent = age <= max(float(heartbeat.get("tick_seconds") or 1.0) * 3, 5.0)
            except (TypeError, ValueError):
                recent = False
        alive = self._pid_is_alive(pid)
        if heartbeat.get("stopped_at"):
            state = "stopped"
        elif alive and recent:
            state = "running"
        elif heartbeat:
            state = "stale"
        else:
            state = "stopped"
        due_count = 0
        claimed_count = 0
        for job in jobs:
            claim = job.get("claim")
            claim_is_live = False
            if isinstance(claim, dict) and claim.get("expires_at"):
                try:
                    claim_is_live = parse_datetime(str(claim["expires_at"])) > checked_at
                except ValueError:
                    claim_is_live = False
            if claim_is_live:
                claimed_count += 1
            if not claim_is_live and job.get("state") == "scheduled" and job.get("next_run_at"):
                try:
                    if parse_datetime(str(job["next_run_at"])) <= checked_at:
                        due_count += 1
                except ValueError:
                    continue
        return {
            "state": state,
            "pid": pid or None,
            "heartbeat": heartbeat or None,
            "job_count": len(jobs),
            "due_count": due_count,
            "claimed_count": claimed_count,
            "execution_count": len(snapshot["executions"]),
            "store_path": str(self.store.jobs_path),
        }
