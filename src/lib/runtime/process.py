"""Bounded subprocess capture with complete process-tree termination."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import Any

import psutil

_PROCESS_RUN_TOKEN_ENV = "AGENTLOOM_SUBPROCESS_RUN_TOKEN"


class CapturedProcessTimeout(TimeoutError):
    """Raised after a timed-out process and all descendants are terminated."""


@dataclass(frozen=True, slots=True)
class CapturedProcessResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    duration_seconds: float


def _read_preview(stream: Any, limit: int) -> tuple[str, int, bool]:
    stream.flush()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(0)
    payload = stream.read(min(size, limit + 1))
    if isinstance(payload, str):
        payload = payload.encode("utf-8", errors="replace")
    return payload[:limit].decode("utf-8", errors="replace"), size, size > limit


def _marked_processes(token: str, *, exclude: set[int]) -> dict[int, psutil.Process]:
    marked: dict[int, psutil.Process] = {}
    for candidate in psutil.process_iter():
        if candidate.pid in exclude:
            continue
        try:
            if candidate.environ().get(_PROCESS_RUN_TOKEN_ENV) == token:
                marked[candidate.pid] = candidate
        except (psutil.Error, OSError):
            continue
    return marked


def terminate_process_tree(process: subprocess.Popen[Any], token: str) -> None:
    """Terminate a process group plus descendants that escaped its session."""

    known: dict[int, psutil.Process] = {}
    frozen: set[int] = set()
    process_group_id: int | None = None
    if os.name == "posix":
        try:
            # Resolve this before stopping the group: the group can outlive its
            # leader, but getpgid(leader_pid) cannot find it after leader exit.
            process_group_id = os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def discover_lineage() -> None:
        try:
            parent = psutil.Process(process.pid)
            known[parent.pid] = parent
            known.update({child.pid: child for child in parent.children(recursive=True)})
        except (psutil.Error, OSError):
            pass

    def discover() -> None:
        discover_lineage()
        known.update(_marked_processes(token, exclude={os.getpid()}))

    def freeze_newly_discovered() -> bool:
        newly_discovered = [candidate for pid, candidate in known.items() if pid not in frozen]
        for candidate in newly_discovered:
            try:
                candidate.suspend()
            except psutil.Error:
                pass
            frozen.add(candidate.pid)
        return bool(newly_discovered)

    # Quiesce the process group first, then freeze its complete descendant tree
    # while the parent relationship is still intact. A child may have escaped
    # into a new session, but it remains discoverable through the live parent;
    # killing the parent before this snapshot would orphan that child and leave
    # it running during a potentially slow system-wide token scan.
    # Group-directed SIGKILL is not delivered atomically on every POSIX kernel:
    # killing a sleeping child can briefly wake its shell parent before that
    # parent receives SIGKILL. SIGSTOP first closes that late-side-effect race.
    # A timed-out process has no cleanup grace; token discovery below exists
    # only to find descendants that escaped the original process group.
    if process_group_id is not None:
        try:
            os.killpg(process_group_id, signal.SIGSTOP)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        while True:
            discover_lineage()
            if not freeze_newly_discovered():
                break
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    elif process.poll() is None:  # pragma: no cover - Windows
        process.kill()

    discover()
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        discover()
        for candidate in tuple(known.values()):
            if candidate.pid == process.pid:
                continue
            try:
                candidate.terminate()
            except psutil.Error:
                pass
        time.sleep(0.01)

    # Freeze every still-marked process before the final scan. This closes the
    # race where a TERM handler forks a detached descendant during cleanup.
    while True:
        discover()
        if not freeze_newly_discovered():
            break

    if process_group_id is not None:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    elif process.poll() is None:  # pragma: no cover - Windows
        process.kill()
    for candidate in reversed(tuple(known.values())):
        try:
            candidate.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(tuple(known.values()), timeout=1)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive fallback
        process.kill()
        process.wait()


def run_captured_process(
    command: str,
    *,
    cwd: str,
    env: dict[str, str],
    stdin: bytes,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
    shell: bool = True,
) -> CapturedProcessResult:
    """Run one process with disk-backed capture and bounded memory previews."""

    run_token = uuid.uuid4().hex
    process_env = dict(env)
    process_env[_PROCESS_RUN_TOKEN_ENV] = run_token
    started = time.monotonic()
    with tempfile.TemporaryFile(mode="w+b") as stdout_stream, tempfile.TemporaryFile(
        mode="w+b"
    ) as stderr_stream:
        process = subprocess.Popen(
            command,
            shell=shell,
            cwd=cwd,
            env=process_env,
            stdin=subprocess.PIPE,
            stdout=stdout_stream,
            stderr=stderr_stream,
            start_new_session=os.name == "posix",
        )
        try:
            process.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            terminate_process_tree(process, run_token)
            raise CapturedProcessTimeout(f"process timed out after {timeout:g}s") from exc
        except BaseException:
            if process.poll() is None:
                terminate_process_tree(process, run_token)
            raise

        stdout, stdout_bytes, stdout_truncated = _read_preview(
            stdout_stream,
            stdout_limit,
        )
        stderr, stderr_bytes, stderr_truncated = _read_preview(
            stderr_stream,
            stderr_limit,
        )
    return CapturedProcessResult(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        duration_seconds=round(time.monotonic() - started, 6),
    )


def run_process_to_fds(
    command: str,
    *,
    cwd: str,
    env: dict[str, str],
    stdout_fd: int,
    stderr_fd: int,
    timeout: float | None,
    shell: bool = True,
) -> int:
    """Run a process whose output is already bound to caller-owned file descriptors.

    This is the low-level seam for runtimes that own durable output files and
    therefore do not need :func:`run_captured_process` to create temporary
    capture streams. Timeout still guarantees complete process-tree teardown.
    """

    run_token = uuid.uuid4().hex
    process_env = dict(env)
    process_env[_PROCESS_RUN_TOKEN_ENV] = run_token
    process = subprocess.Popen(
        command,
        shell=shell,
        cwd=cwd,
        env=process_env,
        stdout=stdout_fd,
        stderr=stderr_fd,
        start_new_session=os.name == "posix",
    )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        terminate_process_tree(process, run_token)
        timeout_text = "without a limit" if timeout is None else f"after {timeout:g}s"
        raise CapturedProcessTimeout(f"process timed out {timeout_text}") from exc
    except BaseException:
        if process.poll() is None:
            terminate_process_tree(process, run_token)
        raise


__all__ = [
    "CapturedProcessResult",
    "CapturedProcessTimeout",
    "run_captured_process",
    "run_process_to_fds",
    "terminate_process_tree",
]
