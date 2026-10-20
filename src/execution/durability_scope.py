"""Trusted storage boundary prepared before inherited filesystem restriction.

Ordinary execution keeps its full ancestor fsync behavior. An isolated Host
first persists its already-created Trial ancestors while still trusted, then
native journals need only persist new descendants through that original root.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path

_BOUNDARY: ContextVar[Path | None] = ContextVar("agentloom_durability_boundary", default=None)


def current_durability_boundary():
    return _BOUNDARY.get()


@contextmanager
def bind_durability_boundary(root: Path):
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or root == Path("/"):
        raise ValueError("Invalid trusted Trial durability root")
    # This runs before installing the OS policy, without granting tools access
    # to any ancestor. Preserve the original power-loss persistence barrier.
    for directory in (root, *root.parents):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    token = _BOUNDARY.set(root)
    try:
        yield
    finally:
        _BOUNDARY.reset(token)
