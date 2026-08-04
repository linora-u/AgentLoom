"""Task-scoped Todo state contracts."""

from .model import (
    MAX_TODO_CONTENT_CHARS,
    MAX_TODO_ITEMS,
    MAX_TODO_REASON_CHARS,
    MAX_TODO_SNAPSHOT_BYTES,
    TODO_SCHEMA_VERSION,
    TODO_STATUSES,
    empty_todo_snapshot,
    todo_counts,
    validate_todo_document,
    validate_todo_items,
)
from .provider import (
    TodoStateProvider,
    bind_todo_state_provider,
    ensure_todo_state_provider,
    get_current_todo_provider,
)

__all__ = [
    "MAX_TODO_CONTENT_CHARS",
    "MAX_TODO_ITEMS",
    "MAX_TODO_REASON_CHARS",
    "MAX_TODO_SNAPSHOT_BYTES",
    "TODO_SCHEMA_VERSION",
    "TODO_STATUSES",
    "TodoStateProvider",
    "bind_todo_state_provider",
    "empty_todo_snapshot",
    "ensure_todo_state_provider",
    "get_current_todo_provider",
    "todo_counts",
    "validate_todo_document",
    "validate_todo_items",
]
