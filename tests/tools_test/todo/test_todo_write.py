"""Behavioral contract tests for the task-scoped ``todo_write`` tool."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest


@pytest.fixture
def todo_runtime():
    from src.lib.todo import TodoStateProvider, bind_todo_state_provider
    from src.trace import bind_explicit_execution_context, capture_explicit_execution_context

    provider = TodoStateProvider()
    execution = replace(
        capture_explicit_execution_context(),
        runtime_agent_path="supervisor",
    )
    with bind_explicit_execution_context(execution):
        with bind_todo_state_provider(provider):
            yield provider


def _write(todos: Any) -> dict:
    from src.tools.todo import todo_write

    return json.loads(todo_write.forward(todos=todos))


def test_full_replace_returns_canonical_snapshot(todo_runtime) -> None:
    first = _write(
        [
            {"content": "Inspect current behavior", "status": "completed"},
            {"content": "Implement replacement", "status": "in_progress"},
        ]
    )
    second = _write([{"content": "Run verification", "status": "pending"}])

    assert first == {
        "revision": 1,
        "todos": [
            {"content": "Inspect current behavior", "status": "completed"},
            {"content": "Implement replacement", "status": "in_progress"},
        ],
        "counts": {
            "pending": 0,
            "in_progress": 1,
            "completed": 1,
            "cancelled": 0,
        },
    }
    assert second["revision"] == 2
    assert second["todos"] == [{"content": "Run verification", "status": "pending"}]
    assert todo_runtime.load("supervisor")["items"] == second["todos"]


def test_empty_list_explicitly_clears_state(todo_runtime) -> None:
    _write([{"content": "Temporary item", "status": "pending"}])

    result = _write([])

    assert result["revision"] == 2
    assert result["todos"] == []
    assert result["counts"] == {
        "pending": 0,
        "in_progress": 0,
        "completed": 0,
        "cancelled": 0,
    }


def test_cancelled_item_requires_reason(todo_runtime) -> None:
    result = _write(
        [
            {
                "content": "Replace the database",
                "status": "cancelled",
                "cancel_reason": "Root cause was configuration, so no database change is needed.",
            }
        ]
    )

    assert result["todos"][0]["status"] == "cancelled"
    assert result["counts"]["cancelled"] == 1


@pytest.mark.parametrize(
    "todos,match",
    [
        ([{"content": "Missing state"}], "status"),
        ([{"content": "Bad state", "status": "done"}], "status"),
        ([{"content": "   ", "status": "pending"}], "content"),
        ([{"content": "No reason", "status": "cancelled"}], "cancel_reason"),
        (
            [
                {
                    "content": "Unexpected reason",
                    "status": "pending",
                    "cancel_reason": "not allowed",
                }
            ],
            "cancel_reason",
        ),
        (
            [
                {"content": "First", "status": "in_progress"},
                {"content": "Second", "status": "in_progress"},
            ],
            "in_progress",
        ),
        ([{"content": "Extra", "status": "pending", "priority": "high"}], "unexpected"),
        ("not-an-array", "array"),
        (["not-an-object"], "object"),
        (
            [{"content": "x" * 2_001, "status": "pending"}],
            "2000 characters",
        ),
        (
            [
                {
                    "content": "Cancelled",
                    "status": "cancelled",
                    "cancel_reason": "x" * 2_001,
                }
            ],
            "2000 characters",
        ),
        (
            [{"content": f"Item {index}", "status": "pending"} for index in range(101)],
            "at most 100",
        ),
        (
            [{"content": f"{index}:" + "x" * 1_995, "status": "pending"} for index in range(40)],
            "65536 bytes",
        ),
    ],
)
def test_invalid_snapshot_is_rejected_atomically(todo_runtime, todos, match) -> None:
    _write([{"content": "Existing", "status": "pending"}])

    with pytest.raises(ValueError, match=match):
        _write(todos)

    snapshot = todo_runtime.load("supervisor")
    assert snapshot["revision"] == 1
    assert snapshot["items"] == [{"content": "Existing", "status": "pending"}]


def test_agent_scopes_do_not_share_memory_state(todo_runtime) -> None:
    _write([{"content": "Supervisor item", "status": "pending"}])

    assert todo_runtime.load("supervisor")["items"] == [{"content": "Supervisor item", "status": "pending"}]
    assert todo_runtime.load("supervisor/worker")["items"] == []


def test_tool_schema_accepts_structured_array() -> None:
    from src.tools.todo import todo_write

    assert todo_write.inputs["todos"]["type"] == "array"
