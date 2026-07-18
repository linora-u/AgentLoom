"""Bounded subprocess capture used by explicit Skill script execution."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.lib.runtime.process import CapturedProcessTimeout, run_process_to_fds

OUTPUT_PREVIEW_MAX_BYTES = 4000


@dataclass(frozen=True, slots=True)
class SkillOutputSnapshot:
    stdout: str
    stderr: str
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool


@dataclass(frozen=True, slots=True)
class SkillProcessResult:
    returncode: int | None
    timed_out: bool


class SkillSubprocessCapture:
    """Redirect a Skill subprocess to bounded run-owned output files.

    Outside a bound run, anonymous temporary files preserve the same bounded
    memory behavior without creating artifacts in an unrelated run directory.
    """

    def __init__(self, audit_dir: Path | None = None) -> None:
        from src.lib.runtime import get_current_run_context

        self.runtime_context = get_current_run_context()
        self.audit_dir = audit_dir
        self.stdout_path: Path | None = None
        self.stderr_path: Path | None = None
        self._temporary_files: list[Any] = []
        self._closed = False

        if audit_dir is not None:
            if self.runtime_context is None:
                raise RuntimeError("skill audit directory requires a bound run context")
            self.stdout_path = audit_dir / "stdout.txt"
            self.stderr_path = audit_dir / "stderr.txt"
            self.stdout_fd = self.runtime_context.create_run_file(self.stdout_path)
            try:
                self.stderr_fd = self.runtime_context.create_run_file(self.stderr_path)
            except BaseException:
                os.close(self.stdout_fd)
                raise
        else:
            stdout_file = tempfile.TemporaryFile(mode="w+b", buffering=0)
            try:
                stderr_file = tempfile.TemporaryFile(mode="w+b", buffering=0)
            except BaseException:
                stdout_file.close()
                raise
            self._temporary_files = [stdout_file, stderr_file]
            self.stdout_fd = stdout_file.fileno()
            self.stderr_fd = stderr_file.fileno()

    def snapshot(
        self,
        *,
        stdout_limit: int,
        stderr_limit: int,
    ) -> SkillOutputSnapshot:
        stdout, stdout_size, stdout_truncated = _read_fd_preview(
            self.stdout_fd,
            stdout_limit,
        )
        stderr, stderr_size, stderr_truncated = _read_fd_preview(
            self.stderr_fd,
            stderr_limit,
        )
        return SkillOutputSnapshot(
            stdout=stdout,
            stderr=stderr,
            stdout_bytes=stdout_size,
            stderr_bytes=stderr_size,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._temporary_files:
            for stream in self._temporary_files:
                stream.close()
            self._temporary_files.clear()
            return
        first_error: OSError | None = None
        for fd in (self.stdout_fd, self.stderr_fd):
            try:
                os.close(fd)
            except OSError as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def __enter__(self) -> SkillSubprocessCapture:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _read_fd_preview(fd: int, limit: int) -> tuple[str, int, bool]:
    limit = max(0, int(limit))
    size = os.fstat(fd).st_size
    remaining = min(size, limit)
    chunks: list[bytes] = []
    offset = 0
    while remaining:
        try:
            chunk = os.pread(fd, remaining, offset)
        except InterruptedError:
            continue
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    return payload.decode("utf-8", errors="replace"), size, size > limit


def run_skill_subprocess(
    command: str,
    *,
    cwd: str,
    env: dict[str, str],
    stdout_fd: int,
    stderr_fd: int,
    timeout: float | None,
) -> SkillProcessResult:
    """Run one Skill command through the shared process-containment runtime."""
    try:
        returncode = run_process_to_fds(
            command,
            cwd=cwd,
            env=env,
            stdout_fd=stdout_fd,
            stderr_fd=stderr_fd,
            timeout=timeout,
        )
    except CapturedProcessTimeout:
        return SkillProcessResult(returncode=None, timed_out=True)
    return SkillProcessResult(returncode=returncode, timed_out=False)
