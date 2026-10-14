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

_PROCESS_SCOPE: ContextVar[int | None] = ContextVar("agentloom_managed_process_group", default=None)
PI_PROCESS_SCOPE_ENV = "AGENTLOOM_MANAGED_PROCESS_GROUP"
NATIVE_SCOPE_FD_ENV = "AGENTLOOM_NATIVE_SCOPE_FD"


@contextmanager
def bind_managed_process_scope():
    """Bind one isolated process leader, before opening its Application.

    This is trusted host code, never a task-input option. A caller in an
    existing shared shell group cannot opt that group into task ownership.
    """
    if os.name != "posix":
        raise RuntimeError("managed Application process requires POSIX")
    descriptor = os.environ.pop(NATIVE_SCOPE_FD_ENV, None)
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
        parent = psutil.Process(anchor["pid"])
        entry = str(Path(__file__).with_name("_process_guardian.py").resolve())
        command = parent.cmdline()
        if (child["pid"] != os.getpid() or child["creation"] != psutil.Process().create_time()
                or os.getppid() != anchor["pid"] or os.getpgrp() != anchor["group"]
                or anchor["pid"] != anchor["group"] or parent.create_time() != anchor["creation"]
                or len(command) < 2 or str(Path(command[1]).resolve()) != entry):
            raise RuntimeError("Native scope bootstrap does not match its actual guardian identity")
    elif os.getpgrp() != os.getpid():
        raise RuntimeError("managed Application process must lead its own process group")
    token = _PROCESS_SCOPE.set(os.getpgrp())
    try:
        yield
    finally:
        _PROCESS_SCOPE.reset(token)


def managed_process_group() -> int | None:
    group = _PROCESS_SCOPE.get()
    if group is not None and group != os.getpgrp():
        raise RuntimeError("managed Application left its process group")
    return group


def child_starts_new_session() -> bool:
    """Ordinary Runs keep existing groups; isolated Runs inherit their owner."""
    return managed_process_group() is None
