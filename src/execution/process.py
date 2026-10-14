"""Bounded subprocess capture with complete process-tree termination."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any

import psutil
from agentloom.execution.cleanup import close_preserving_error as _close_preserving_error

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


def _marked_processes(
    token: str, *, exclude: set[int], env_name: str = _PROCESS_RUN_TOKEN_ENV,
) -> dict[int, psutil.Process]:
    marked: dict[int, psutil.Process] = {}
    for candidate in psutil.process_iter():
        if candidate.pid in exclude:
            continue
        try:
            if candidate.environ().get(env_name) == token:
                marked[candidate.pid] = candidate
        except (psutil.Error, OSError):
            continue
    return marked


def terminate_process_tree(process: subprocess.Popen[Any], token: str) -> None:
    """Terminate a process group plus descendants that escaped its session."""
    from agentloom.execution.process_scope import _guardian_generation

    known: dict[int, psutil.Process] = {}
    frozen: set[int] = set()
    process_group_id: int | None = None
    if os.name == "posix" and process.poll() is None:
        try:
            # Resolve this before stopping the group: the group can outlive its
            # leader, but getpgid(leader_pid) cannot find it after leader exit.
            process_group_id = os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def discover_lineage() -> None:
        # A reaped Popen PID may have been reused by an unrelated process.
        # Remaining tagged descendants can still be found without that PID.
        if process.poll() is not None:
            return
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
                if _guardian_generation(candidate) is None:
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
    # An isolated Application's tools share its OS-owned group. Never stop or
    # kill the Application while closing just one tool process. Its host owns
    # whole-group termination; here the concrete child tree remains the target.
    shared_group = os.name == "posix" and process_group_id == os.getpgrp()
    if shared_group:
        process_group_id = None
        # Quiesce this child tree before killing its parent. Otherwise a child
        # with a deliberately clean env could be orphaned before discovery.
        while True:
            discover_lineage()
            if not freeze_newly_discovered():
                break
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
    elif not shared_group and process.poll() is None:  # pragma: no cover - Windows
        process.kill()

    discover()
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        discover()
        for candidate in tuple(known.values()):
            if candidate.pid == process.pid:
                continue
            try:
                if _guardian_generation(candidate) is None:
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
            if _guardian_generation(candidate) is None:
                candidate.kill()
        except psutil.Error:
            pass
    _gone, alive = psutil.wait_procs(tuple(known.values()), timeout=1)
    if alive:
        raise RuntimeError("owned process tree has not exited after termination")
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:  # pragma: no cover - defensive fallback
        process.kill()
        process.wait()


def spawn_registered_process(
    command: Any, *, env: dict[str, str], **options: Any,
) -> tuple[subprocess.Popen[Any], Callable[[], None]]:
    """Start a concrete process owned by this Run, retaining its close callback.

    Tools may transfer the Popen and callback to their background registry.
    The callback is idempotent only after actual tree termination succeeds;
    a failed callback retains its original resource owner for cleanup retry.
    """
    from agentloom.execution.managed_process import launch_owned_process, native_owner_active
    from agentloom.execution.process_scope import child_starts_new_session, managed_subprocess_env
    from agentloom.execution.resources import register_resource, unregister_resource

    token = uuid.uuid4().hex
    process_env = managed_subprocess_env({**env, _PROCESS_RUN_TOKEN_ENV: token})
    key = f"owned-process:{token}"
    process = None
    cancelled = False
    stopped = False
    managed = native_owner_active()
    admission = Lock()

    def close_process() -> None:
        nonlocal cancelled, stopped
        with admission:
            cancelled = True
            if process is not None and not stopped:
                if managed:
                    process.close()
                else:
                    terminate_process_tree(process, token)
                stopped = True

    register_resource(key, close_process)
    try:
        with admission:
            if cancelled:
                raise InterruptedError("Execution resources are closed")
            process = (launch_owned_process(command, env=process_env, **options) if managed else
                       subprocess.Popen(command, env=process_env,
                                        start_new_session=child_starts_new_session(), **options))
    except BaseException:
        close_process()
        unregister_resource(key)
        raise
    return process, close_process


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

    started = time.monotonic()
    with tempfile.TemporaryFile(mode="w+b") as stdin_stream, tempfile.TemporaryFile(
        mode="w+b"
    ) as stdout_stream, tempfile.TemporaryFile(mode="w+b") as stderr_stream:
        # A child that never reads stdin must not block a pipe write before
        # communicate can enforce its deadline. The complete payload is known.
        stdin_stream.write(stdin)
        stdin_stream.seek(0)
        process, close_process = spawn_registered_process(
            command, shell=shell, cwd=cwd, env=env,
            stdin=stdin_stream, stdout=stdout_stream, stderr=stderr_stream,
        )
        try:
            process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise CapturedProcessTimeout(f"process timed out after {timeout:g}s") from exc
        finally:
            _close_preserving_error(close_process)

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

    process, close_process = spawn_registered_process(
        command, shell=shell, cwd=cwd, env=env, stdout=stdout_fd, stderr=stderr_fd,
    )
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        timeout_text = "without a limit" if timeout is None else f"after {timeout:g}s"
        raise CapturedProcessTimeout(f"process timed out {timeout_text}") from exc
    finally:
        _close_preserving_error(close_process)


def run_registered_process(command, *, env=None, input=None, capture_output=False, timeout=None, check=False, **options):
    """Popen-compatible capture through the same trusted resource admission."""
    if input is not None:
        if options.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        options["stdin"] = subprocess.PIPE
    if capture_output:
        if options.get("stdout") is not None or options.get("stderr") is not None:
            raise ValueError("stdout/stderr arguments may not be used with capture_output")
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process, close = spawn_registered_process(command, env=dict(os.environ) if env is None else env, **options)
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
        returncode = process.returncode
        if check and returncode:
            raise subprocess.CalledProcessError(returncode, command, stdout, stderr)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)
    finally:
        _close_preserving_error(close)


__all__ = [
    "CapturedProcessResult",
    "CapturedProcessTimeout",
    "run_captured_process",
    "run_process_to_fds",
    "terminate_process_tree",
    "spawn_registered_process",
]
