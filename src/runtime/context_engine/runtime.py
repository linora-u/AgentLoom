"""Runtime ContextVar access for the active ContextEngine."""

from __future__ import annotations

from contextvars import ContextVar
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Optional

from .engine import ContextEngine


_current_context_engine: ContextVar[Optional[ContextEngine]] = ContextVar(
    "_current_context_engine", default=None
)


def set_current_context_engine(engine: ContextEngine | None) -> None:
    _current_context_engine.set(engine)


def get_current_context_engine() -> ContextEngine | None:
    return _current_context_engine.get()


def get_active_context_engine() -> ContextEngine | None:
    """Return the active task-scoped ContextEngine.

    Storage belongs to an explicit Application task, independently of whether
    its Agent supports checkpoint/resume. This accessor never creates a
    process-level store or consults another task's context.
    """
    return get_current_context_engine()


def clear_current_context_engine(engine: ContextEngine | None = None) -> None:
    current = _current_context_engine.get()
    if engine is None or current is engine:
        _current_context_engine.set(None)


@contextmanager
def ensure_task_context_engine(effective_config: dict) -> Iterator[ContextEngine | None]:
    """Own a task store when no checkpoint coordinator has already bound one.

    Workers inherit the root's engine through ContextVars. Canonical storage
    paths and descriptor anchoring are the same as checkpoint-backed stores;
    activating this service does not create an Agent checkpoint or allow resume.
    """
    from agentloom.runtime import SecureDirectory, get_current_run_context
    from .config import ContextEngineConfig

    current = _current_context_engine.get()
    context = get_current_run_context()
    if current is not None or context is None:
        yield current
        return
    root = SecureDirectory(context.root_dir, create=False)
    try:
        storage = root.child(context.context_store_dir.relative_to(context.root_dir), create=True)
    finally:
        root.close()
    try:
        engine = ContextEngine(context.context_store_dir,
            config=ContextEngineConfig.from_mapping(effective_config.get('context_engine', {})), storage=storage)
    except BaseException:
        storage.close()
        raise
    token = _current_context_engine.set(engine)
    try:
        yield engine
    finally:
        _current_context_engine.reset(token)
        engine.close()
