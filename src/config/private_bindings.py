"""Private Harness capability values, carried only by the trusted child pipe.

The resolver is inert during ordinary/static configuration reads. An active
Harness binding fails on an unresolved reference instead of sending a marker
as an API credential or reopening live source configuration.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

_VALUES: ContextVar[Mapping[str, Any] | None] = ContextVar("agentloom_harness_private_capabilities", default=None)
_PREFIX = "harness-credential-ref:"
_FD_ENV = "AGENTLOOM_HARNESS_PRIVATE_FD"
_MAX_BYTES = 4 * 1024 * 1024


@contextmanager
def bind_harness_credentials(values: Mapping[str, Any]):
    token = _VALUES.set(deepcopy(dict(values)))
    try:
        yield
    finally:
        _VALUES.reset(token)


def capture_harness_bindings():
    """Capture only private capabilities for callbacks in a trusted executor.

    DBOS owns its callback threads and does not inherit the caller's context.
    Each invocation gets a fresh binding scope, including across cancellation;
    unrelated trace, resource and Application contexts are not transplanted.
    The captured values stay in memory and never enter a HostSpec or task.
    """
    values = _VALUES.get()
    if values is None:
        return nullcontext
    credentials = deepcopy(dict(values))
    from agentloom.runtimes.pi.credentials import bind_pi_credentials, current_pi_credentials

    from .config import bind_harness_model_config, current_harness_model_config
    model_config, pi_credentials = current_harness_model_config(), current_pi_credentials()

    @contextmanager
    def bind():
        with bind_harness_credentials(credentials), bind_harness_model_config(deepcopy(model_config)), bind_pi_credentials(pi_credentials):
            yield

    return bind


def require_harness_bindings():
    """Refuse a missing private transport before an isolated Host can import."""
    from agentloom.runtimes.pi.credentials import current_pi_credentials

    from .config import current_harness_model_config
    if _VALUES.get() is None or current_harness_model_config() is None or current_pi_credentials() is None:
        raise ValueError("Isolated Harness execution requires its trusted private bindings")


def resolve_bound_credentials(value: Any) -> Any:
    bindings = _VALUES.get()
    if bindings is None:
        return value
    if isinstance(value, str) and value.startswith(_PREFIX):
        reference = value[len(_PREFIX):]
        if reference not in bindings:
            raise ValueError("A frozen Harness capability reference has no trusted binding")
        return deepcopy(bindings[reference])
    if isinstance(value, Mapping):
        return {key: resolve_bound_credentials(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_bound_credentials(item) for item in value]
    return value


@contextmanager
def forward_harness_bindings():
    """Return ``(environment_overlay, pass_fds)`` for one trusted child.

    The sealed anonymous file is never archived or named in the filesystem.
    Its numeric descriptor alone enters the environment. Callers consume it
    before importing any Agent, model Runtime, Hook or business tool.
    """
    values = _VALUES.get()
    if values is None:
        yield {}, ()
        return
    import fcntl

    from agentloom.runtimes.pi.credentials import current_pi_credentials

    from .config import current_harness_model_config
    payload = json.dumps({"credentials": dict(values), "model_config": current_harness_model_config(), "pi_credentials": current_pi_credentials()}, allow_nan=False).encode()
    if len(payload) > _MAX_BYTES:
        raise ValueError("Private Harness transport exceeds its bounded capacity")
    if not hasattr(os, "memfd_create"):
        raise RuntimeError("Private Harness forwarding requires a validated anonymous descriptor backend")
    fd = os.memfd_create("agentloom-private-binding", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(os.dup(fd), "wb", closefd=True) as stream:
            stream.write(payload)
            stream.flush()
        os.lseek(fd, 0, os.SEEK_SET)
        # Linux UAPI: F_ADD_SEALS=1024+9, SEAL/SHRINK/GROW/WRITE=1|2|4|8.
        # Standalone CPython builds may omit these names, not the syscall.
        fcntl.fcntl(fd, getattr(fcntl, "F_ADD_SEALS", 1033), 0x000F)
        yield {_FD_ENV: str(fd)}, (fd,)
    finally:
        os.close(fd)


@contextmanager
def consume_harness_bindings():
    """Consume and close a trusted cross-exec capability before tools exist."""
    raw_fd = os.environ.pop(_FD_ENV, None)
    if raw_fd is None:
        yield
        return
    if not raw_fd.isdigit() or int(raw_fd) < 3:
        raise ValueError("Invalid private Harness descriptor")
    fd = int(raw_fd)
    with os.fdopen(fd, "rb", closefd=True) as stream:
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("Private Harness transport exceeds its bounded capacity")
    private = json.loads(raw)
    from agentloom.runtimes.pi.credentials import bind_pi_credentials

    from .config import bind_harness_model_config
    with bind_harness_credentials(private["credentials"]), bind_harness_model_config(private["model_config"]), bind_pi_credentials(private.get("pi_credentials")):
        yield
