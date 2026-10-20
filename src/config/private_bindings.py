"""Private Harness capability values, carried only by the trusted child pipe.

The resolver is inert during ordinary/static configuration reads. An active
Harness binding fails on an unresolved reference instead of sending a marker
as an API credential or reopening live source configuration.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import os
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
    from .config import current_harness_model_config
    from agentloom.runtimes.pi.credentials import current_pi_credentials
    import fcntl
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
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
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
    from .config import bind_harness_model_config
    from agentloom.runtimes.pi.credentials import bind_pi_credentials
    with bind_harness_credentials(private["credentials"]), bind_harness_model_config(private["model_config"]), bind_pi_credentials(private.get("pi_credentials")):
        yield
