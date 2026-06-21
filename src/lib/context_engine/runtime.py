"""Runtime ContextVar access for the active ContextEngine."""

from __future__ import annotations

from contextvars import ContextVar
import threading
from typing import Optional

from .engine import ContextEngine


_current_context_engine: ContextVar[Optional[ContextEngine]] = ContextVar(
    "_current_context_engine", default=None
)
_active_lock = threading.Lock()
_active_context_engine: Optional[ContextEngine] = None


def set_current_context_engine(engine: ContextEngine | None) -> None:
    global _active_context_engine
    _current_context_engine.set(engine)
    with _active_lock:
        _active_context_engine = engine


def get_current_context_engine() -> ContextEngine | None:
    engine = _current_context_engine.get()
    if engine is not None:
        return engine
    with _active_lock:
        return _active_context_engine


def get_active_context_engine() -> ContextEngine | None:
    """Return the active task-scoped ContextEngine.

    ContextEngine storage is part of the checkpoint/run contract. Creating an
    implicit process-level store here would make refs survive differently from
    checkpoint state, so callers must activate a task ContextEngine first.
    """
    return get_current_context_engine()


def clear_current_context_engine(engine: ContextEngine | None = None) -> None:
    global _active_context_engine
    current = _current_context_engine.get()
    if engine is None or current is engine:
        _current_context_engine.set(None)
    with _active_lock:
        if engine is None or _active_context_engine is engine:
            _active_context_engine = None
