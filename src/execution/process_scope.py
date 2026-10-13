"""Process-group ownership installed by a trusted isolated Application host.

The OS host owns the group lifetime. Runtime/tool transports only decide
whether their child joins it; they do not acquire queue capacity or retry work.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import os

_PROCESS_SCOPE: ContextVar[int | None] = ContextVar("agentloom_managed_process_group", default=None)
PI_PROCESS_SCOPE_ENV = "AGENTLOOM_MANAGED_PROCESS_GROUP"


@contextmanager
def bind_managed_process_scope():
    """Bind one isolated process leader, before opening its Application.

    This is trusted host code, never a task-input option. A caller in an
    existing shared shell group cannot opt that group into task ownership.
    """
    if os.name != "posix" or os.getpgrp() != os.getpid():
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
