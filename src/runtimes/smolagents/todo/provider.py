"""Run-scoped access to checkpoint-backed or in-memory Todo state."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal, overload

from agentloom.runtime import safe_agent_path

from .model import empty_todo_snapshot, validate_todo_items
from .store import TodoStore


class TodoStateProvider:
    """Route current-task Todo operations to the active checkpoint when present."""

    def __init__(self) -> None:
        self._memory: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._store_owner: Any = None
        self._store: TodoStore | None = None

    @staticmethod
    def _coordinator() -> Any | None:
        from agentloom.runtime.checkpoint.coordinator import CheckpointCoordinator

        return CheckpointCoordinator.current()

    def _checkpoint_store(self, coordinator: Any) -> TodoStore:
        with self._lock:
            manager = coordinator.checkpoint_manager
            if self._store is None or self._store_owner is not manager:
                self._store = TodoStore(manager.task_storage)
                self._store_owner = manager
            return self._store

    def load(self, agent_path: str) -> dict[str, Any]:
        agent_path = safe_agent_path(agent_path)
        coordinator = self._coordinator()
        if coordinator is not None:
            return self._checkpoint_store(coordinator).load_todos(coordinator.task_id, agent_path)
        with self._lock:
            snapshot = self._memory.get(agent_path)
            if snapshot is None:
                return empty_todo_snapshot()
            return {
                "revision": snapshot["revision"],
                "items": [dict(item) for item in snapshot["items"]],
                "corrupt": False,
            }

    def replace(self, agent_path: str, items: Any) -> dict[str, Any]:
        agent_path = safe_agent_path(agent_path)
        canonical = validate_todo_items(items)
        coordinator = self._coordinator()
        if coordinator is not None:
            return self._checkpoint_store(coordinator).replace_todos(coordinator.task_id, agent_path, canonical)
        with self._lock:
            previous = self._memory.get(agent_path)
            revision = int(previous["revision"]) + 1 if previous else 1
            snapshot = {
                "revision": revision,
                "items": [dict(item) for item in canonical],
                "corrupt": False,
            }
            self._memory[agent_path] = snapshot
            return {
                "revision": revision,
                "items": [dict(item) for item in canonical],
                "corrupt": False,
            }


_CURRENT_TODO_PROVIDER: ContextVar[TodoStateProvider | None] = ContextVar(
    "_CURRENT_TODO_PROVIDER",
    default=None,
)


@overload
def get_current_todo_provider(*, required: Literal[True]) -> TodoStateProvider: ...


@overload
def get_current_todo_provider(*, required: Literal[False] = False) -> TodoStateProvider | None: ...


def get_current_todo_provider(*, required: bool = False) -> TodoStateProvider | None:
    provider = _CURRENT_TODO_PROVIDER.get()
    if provider is None and required:
        raise RuntimeError("no TodoStateProvider is bound to the current Agent run")
    return provider


@contextmanager
def bind_todo_state_provider(
    provider: TodoStateProvider | None = None,
) -> Iterator[TodoStateProvider]:
    resolved = provider or TodoStateProvider()
    token = _CURRENT_TODO_PROVIDER.set(resolved)
    try:
        yield resolved
    finally:
        _CURRENT_TODO_PROVIDER.reset(token)


@contextmanager
def ensure_todo_state_provider() -> Iterator[TodoStateProvider]:
    existing = get_current_todo_provider()
    if existing is not None:
        yield existing
        return
    with bind_todo_state_provider() as provider:
        yield provider
