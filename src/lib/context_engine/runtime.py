"""Runtime ContextVar access for the active ContextEngine."""

from __future__ import annotations

from contextvars import ContextVar
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

    ContextEngine storage is part of the checkpoint/run contract. Creating an
    implicit process-level store here would make refs survive differently from
    checkpoint state, so callers must activate a task ContextEngine first.
    """
    return get_current_context_engine()


def clear_current_context_engine(engine: ContextEngine | None = None) -> None:
    current = _current_context_engine.get()
    if engine is None or current is engine:
        _current_context_engine.set(None)
