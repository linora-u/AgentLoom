"""Smol Todo persistence over AgentLoom's secure, task-scoped storage.

The format and quarantine behavior preserve existing todos.json checkpoints.
The platform supplies storage handles; it does not interpret the Todo schema.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from agentloom.runtime import SecureDirectory
from agentloom.runtime.logging import get_logger
from .model import TODO_SCHEMA_VERSION, empty_todo_snapshot, validate_todo_document, validate_todo_items

_logger = get_logger(__name__)


class TodoStore:
    def __init__(self, task_storage: Callable[[str], SecureDirectory]) -> None:
        self._task_storage = task_storage
        self._lock = threading.RLock()
        self._corrupt_scopes: set[tuple[str, str]] = set()

    def _empty_todo_document(self, task_id: str) -> dict[str, Any]:
        return {
            "schema_version": TODO_SCHEMA_VERSION,
            "task_id": task_id,
            "agents": {},
        }

    def _quarantine_corrupt_todos(
        self,
        storage: SecureDirectory,
        *,
        task_id: str,
        agent_path: str,
        error: BaseException,
    ) -> bool:
        quarantine_name = f"todos.corrupt.{time.time_ns()}.{uuid.uuid4().hex}.json"
        evidence_preserved = False
        try:
            storage.rename_file("todos.json", quarantine_name)
            evidence_preserved = True
        except FileNotFoundError:
            evidence_preserved = True
        except Exception as quarantine_error:
            _logger.warning(
                "Failed to quarantine corrupt Todo state: task_id=%s agent_path=%s error=%s",
                task_id,
                agent_path,
                quarantine_error,
            )
        _logger.warning(
            "Ignoring corrupt Todo state and continuing with an empty list: "
            "task_id=%s agent_path=%s error=%s",
            task_id,
            agent_path,
            error,
        )
        return evidence_preserved

    def _read_todo_document_locked(
        self,
        storage: SecureDirectory,
        *,
        task_id: str,
        agent_path: str,
    ) -> tuple[dict[str, Any], bool, bool]:
        scope = (task_id, agent_path)
        try:
            raw = storage.read_json("todos.json")
        except FileNotFoundError:
            return (
                self._empty_todo_document(task_id),
                scope in self._corrupt_scopes,
                True,
            )
        except (UnicodeError, json.JSONDecodeError, OSError, RuntimeError, ValueError) as exc:
            evidence_preserved = self._quarantine_corrupt_todos(
                storage,
                task_id=task_id,
                agent_path=agent_path,
                error=exc,
            )
            self._corrupt_scopes.add(scope)
            return self._empty_todo_document(task_id), True, evidence_preserved
        try:
            document = validate_todo_document(raw, task_id=task_id)
            self._corrupt_scopes.discard(scope)
            return document, False, True
        except (TypeError, ValueError) as exc:
            evidence_preserved = self._quarantine_corrupt_todos(
                storage,
                task_id=task_id,
                agent_path=agent_path,
                error=exc,
            )
            self._corrupt_scopes.add(scope)
            return self._empty_todo_document(task_id), True, evidence_preserved

    def load_todos(self, task_id: str, agent_path: str) -> dict[str, Any]:
        """Load one Agent's canonical current-task Todo snapshot."""

        from agentloom.runtime import safe_agent_path

        agent_path = safe_agent_path(agent_path)
        with self._lock:
            storage = self._task_storage(task_id)
            try:
                with storage.advisory_file_lock("todos.lock", create=True):
                    document, corrupt, _ = self._read_todo_document_locked(
                        storage,
                        task_id=task_id,
                        agent_path=agent_path,
                    )
            finally:
                storage.close()
        snapshot = document["agents"].get(agent_path)
        if snapshot is None:
            return empty_todo_snapshot(corrupt=corrupt)
        return {
            "revision": snapshot["revision"],
            "items": [dict(item) for item in snapshot["items"]],
            "corrupt": corrupt,
        }

    def replace_todos(
        self,
        task_id: str,
        agent_path: str,
        items: Any,
    ) -> dict[str, Any]:
        """Atomically replace one Agent's complete current-task Todo list."""

        from agentloom.runtime import safe_agent_path

        agent_path = safe_agent_path(agent_path)
        canonical = validate_todo_items(items)
        with self._lock:
            storage = self._task_storage(task_id)
            try:
                with storage.advisory_file_lock("todos.lock", create=True):
                    document, _, evidence_preserved = self._read_todo_document_locked(
                        storage,
                        task_id=task_id,
                        agent_path=agent_path,
                    )
                    if not evidence_preserved:
                        raise RuntimeError(
                            "cannot replace corrupt Todo state before its diagnostic evidence is preserved"
                        )
                    previous = document["agents"].get(agent_path)
                    revision = int(previous["revision"]) + 1 if previous else 1
                    document["agents"][agent_path] = {
                        "revision": revision,
                        "items": [dict(item) for item in canonical],
                    }
                    storage.atomic_write_json("todos.json", document)
                    self._corrupt_scopes.discard((task_id, agent_path))
            finally:
                storage.close()
        return {
            "revision": revision,
            "items": [dict(item) for item in canonical],
            "corrupt": False,
        }

