"""Execute claimed schedule jobs through AgentLoom's canonical CLI boundary."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .store import ScheduleStore

CommandFactory = Callable[[dict[str, Any]], list[str]]
ProgressCallback = Callable[[], None]
StopCallback = Callable[[], bool]


class _ExecutionInterrupted(Exception):
    """Internal control flow for a requested service shutdown."""


class ScheduleRunner:
    def __init__(
        self,
        store: ScheduleStore,
        *,
        command_factory: CommandFactory | None = None,
        poll_seconds: float = 0.25,
    ):
        self.store = store
        self._command_factory = command_factory
        self.poll_seconds = max(float(poll_seconds), 0.01)
        self.owner = f"{socket.gethostname()}:{os.getpid()}"

    def command_for(self, job: dict[str, Any]) -> list[str]:
        if self._command_factory is not None:
            return list(self._command_factory(job))
        return [
            sys.executable,
            "-I",
            "-m",
            "src",
            "run",
            str(job["yaml_path"]),
            "--output-format",
            "jsonl",
        ]

    def _goal_budget_event(self, execution_id: str) -> dict[str, Any] | None:
        """Return a canonical budget event emitted by the isolated JSONL CLI."""

        try:
            stdout = self.store.read_execution_stdout(execution_id)
        except (OSError, RuntimeError, ValueError):
            return None
        for line in reversed(stdout.splitlines()):
            try:
                payload = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict) or payload.get("event") != "run.budget_limited":
                continue
            goal = payload.get("goal")
            run = payload.get("run")
            if (
                isinstance(goal, dict)
                and goal.get("status") == "budget_limited"
                and isinstance(run, dict)
                and isinstance(run.get("task_id"), str)
            ):
                return payload
        return None

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    def _terminate_process_tree(cls, process: subprocess.Popen[bytes]) -> None:
        if os.name != "posix":
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            return

        # start_new_session=True makes the direct child PID the process-group ID.
        # Signalling that stable ID also reaches descendants if the group leader
        # exits between poll() and shutdown.
        process_group_id = process.pid
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass

        deadline = time.monotonic() + 1
        while cls._process_group_exists(process_group_id) and time.monotonic() < deadline:
            process.poll()  # Reap the direct child so a dead leader does not look live.
            time.sleep(0.01)

        if cls._process_group_exists(process_group_id):
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.wait(timeout=1)

    def run_now(
        self,
        job_id: str,
        *,
        now: datetime | None = None,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        claim = self.store.claim_now(job_id, owner=self.owner, now=now)
        return self.execute_claim(claim, progress=progress)

    def run_due(
        self,
        *,
        now: datetime | None = None,
        limit: int | None = None,
        progress: ProgressCallback | None = None,
        should_stop: StopCallback | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        while limit is None or len(results) < limit:
            # Do not create another durable claim after shutdown was requested.
            if should_stop is not None and should_stop():
                break
            claims = self.store.claim_due(now=now, owner=self.owner, limit=1)
            if not claims:
                break
            results.append(
                self.execute_claim(
                    claims[0],
                    progress=progress,
                    should_stop=should_stop,
                )
            )
        return results

    def execute_claim(
        self,
        claim: dict[str, Any],
        *,
        progress: ProgressCallback | None = None,
        should_stop: StopCallback | None = None,
    ) -> dict[str, Any]:
        job = claim["job"]
        execution_id = str(claim["execution"]["id"])
        command = self.command_for(job)
        stdout_relative = f".agentloom/schedules/executions/{execution_id}.stdout.log"
        stderr_relative = f".agentloom/schedules/executions/{execution_id}.stderr.log"
        process: subprocess.Popen[bytes] | None = None
        exit_code: int | None = None
        error: str | None = None
        terminal_status: str | None = None
        goal: dict[str, Any] | None = None
        heartbeat_every = max(0.1, min(30.0, self.store.claim_lease_seconds / 3))
        last_heartbeat = time.monotonic()

        try:
            with self.store.open_execution_logs(execution_id) as (stdout_handle, stderr_handle):
                if should_stop is not None and should_stop():
                    raise _ExecutionInterrupted
                popen_kwargs: dict[str, Any] = {}
                if os.name == "posix":
                    popen_kwargs["start_new_session"] = True
                process = subprocess.Popen(
                    command,
                    cwd=self.store.project_root,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    shell=False,
                    **popen_kwargs,
                )
                self.store.mark_running(
                    execution_id,
                    command=command,
                    pid=process.pid,
                    stdout_path=stdout_relative,
                    stderr_path=stderr_relative,
                )
                while True:
                    polled = process.poll()
                    if polled is not None:
                        exit_code = int(polled)
                        break
                    if should_stop is not None and should_stop():
                        raise _ExecutionInterrupted
                    if progress is not None:
                        progress()
                    monotonic_now = time.monotonic()
                    if monotonic_now - last_heartbeat >= heartbeat_every:
                        if not self.store.heartbeat_claim(execution_id):
                            raise RuntimeError("schedule execution lost its durable claim")
                        last_heartbeat = monotonic_now
                    time.sleep(self.poll_seconds)
            if exit_code != 0:
                error = f"process exited with status {exit_code}"
                budget_event = self._goal_budget_event(execution_id)
                if budget_event is not None:
                    terminal_status = "budget_limited"
                    goal = budget_event["goal"]
                    error = str(
                        budget_event.get("error")
                        or "Goal token budget exhausted; increase or remove token_budget and resume"
                    )
        except BaseException as exc:
            if process is not None:
                self._terminate_process_tree(process)
            if isinstance(exc, (KeyboardInterrupt, _ExecutionInterrupted)):
                error = "execution interrupted"
            else:
                error = f"{type(exc).__name__}: {exc}"
            if exit_code is None and process is not None and process.returncode is not None:
                exit_code = int(process.returncode)
        result = self.store.finish_execution(
            execution_id,
            exit_code=exit_code,
            stdout_path=stdout_relative,
            stderr_path=stderr_relative,
            error=error,
            terminal_status=terminal_status,
            goal=goal,
        )
        if progress is not None:
            progress()
        return result
