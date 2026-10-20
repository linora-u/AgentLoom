"""Process-group ownership installed by a trusted isolated Application host.

The OS host owns the group lifetime. Runtime/tool transports only decide
whether their child joins it; they do not acquire queue capacity or retry work.
"""
from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import psutil

_PROCESS_SCOPE: ContextVar[tuple[int, str | None] | None] = ContextVar("agentloom_managed_process_group", default=None)
PI_PROCESS_SCOPE_ENV = "AGENTLOOM_MANAGED_PROCESS_GROUP"
NATIVE_SCOPE_FD_ENV = "AGENTLOOM_NATIVE_SCOPE_FD"
NATIVE_SCOPE_GENERATION_ENV = "AGENTLOOM_NATIVE_SCOPE_GENERATION"


@contextmanager
def bind_managed_process_scope(*, guardian_entry: Path | None = None):
    """Bind one isolated process leader, before opening its Application.

    This is trusted host code, never a task-input option. A caller in an
    existing shared shell group cannot opt that group into task ownership.
    """
    if os.name != "posix":
        raise RuntimeError("managed Application process requires POSIX")
    descriptor = os.environ.pop(NATIVE_SCOPE_FD_ENV, None)
    generation = None
    if descriptor is not None:
        fd = int(descriptor)
        try:
            if not stat.S_ISFIFO(os.fstat(fd).st_mode):
                raise RuntimeError("Native scope metadata must use the trusted bootstrap pipe")
            payload = os.read(fd, 8192)
            identity = json.loads(payload)
        finally:
            os.close(fd)
        anchor, child = identity["anchor"], identity["child"]
        generation = identity.get("generation")
        parent = psutil.Process(anchor["pid"])
        entry = str((guardian_entry or Path(__file__).with_name("_process_guardian.py")).resolve())
        command = parent.cmdline()
        if (child["pid"] != os.getpid() or child["creation"] != psutil.Process().create_time()
                or os.getppid() != anchor["pid"] or os.getpgrp() != anchor["group"]
                or anchor["pid"] != anchor["group"] or parent.create_time() != anchor["creation"]
                or len(command) < 2 or str(Path(command[1]).resolve()) != entry):
            raise RuntimeError("Native scope bootstrap does not match its actual guardian identity")
        if generation is not None and os.environ.get(NATIVE_SCOPE_GENERATION_ENV) != generation:
            raise RuntimeError("Native scope generation does not match its trusted bootstrap")
    elif os.getpgrp() != os.getpid():
        raise RuntimeError("managed Application process must lead its own process group")
    token = _PROCESS_SCOPE.set((os.getpgrp(), generation))
    try:
        yield
    finally:
        _PROCESS_SCOPE.reset(token)


def managed_process_group() -> int | None:
    scope = _PROCESS_SCOPE.get()
    group = scope[0] if scope is not None else None
    if group is not None and group != os.getpgrp():
        raise RuntimeError("managed Application left its process group")
    return group


def managed_process_generation() -> str | None:
    """Return only the generation received through the verified owner pipe."""
    managed_process_group()
    scope = _PROCESS_SCOPE.get()
    return scope[1] if scope is not None else None


def managed_subprocess_env(env: dict[str, str]) -> dict[str, str]:
    """Propagate only scope authority obtained from the trusted bootstrap."""
    result = dict(env)
    result.pop(PI_PROCESS_SCOPE_ENV, None)
    result.pop(NATIVE_SCOPE_GENERATION_ENV, None)
    group = managed_process_group()
    if group is not None:
        result[PI_PROCESS_SCOPE_ENV] = str(group)
        generation = managed_process_generation()
        if generation is not None:
            result[NATIVE_SCOPE_GENERATION_ENV] = generation
    return result


def guardian_subprocess_env() -> dict[str, str]:
    """Bind a new native owner only to verified enclosing process custody.

    A raw nested CLI has no ContextVar bootstrap, but its actual guardian
    ancestry still proves the enclosing scope. Arbitrary ambient markers
    cannot attach a separate caller to a live sibling's cleanup barrier.
    """
    result = managed_subprocess_env(dict(os.environ))
    # Observers have their own private control channel. An enclosing Pi/tool
    # instance's legacy cleanup token must not authorize killing this owner.
    result.pop("AGENTLOOM_SUBPROCESS_RUN_TOKEN", None)
    if managed_process_group() is not None:
        return result
    caller = psutil.Process()
    parents = caller.parents()
    try:
        parents.insert(0, psutil.Process(os.getpgrp()))
    except psutil.NoSuchProcess:
        pass  # A vanished ordinary group leader establishes no custody.
    for parent in parents:
        generation = _guardian_generation(parent)
        if generation is not None:
            result[PI_PROCESS_SCOPE_ENV] = str(parent.pid)
            result[NATIVE_SCOPE_GENERATION_ENV] = generation
            return result
    if os.getppid() == 1 and os.environ.get(NATIVE_SCOPE_GENERATION_ENV):
        raise RuntimeError("Orphaned managed caller cannot admit a new native owner without verified guardian custody")
    return result


def _guardian_generation(process: psutil.Process) -> str | None:
    """Recognize one live exact guardian identity, not an ambient marker."""
    try:
        command = process.cmdline()
        entry = str(Path(__file__).with_name("_process_guardian.py").resolve())
        if (process.is_running() and os.getpgid(process.pid) == process.pid and len(command) == 4
                and str(Path(command[1]).resolve()) == entry):
            return command[3]
    except (ProcessLookupError, psutil.NoSuchProcess):
        pass
    except psutil.AccessDenied:
        if process.uids().real == os.getuid():
            raise  # An unreadable owned observer cannot become an ordinary child.
    return None


def child_starts_new_session() -> bool:
    """Ordinary Runs keep existing groups; isolated Runs inherit their owner."""
    return managed_process_group() is None
