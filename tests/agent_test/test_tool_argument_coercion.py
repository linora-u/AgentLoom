"""Generic schema-bound tool argument coercion tests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from src.lib.smolagents.agent.tool_argument_coercion import coerce_tool_arguments


def _tool_with_input(key: str, expected_type: str):
    tool = MagicMock()
    tool.inputs = {key: {"type": expected_type}}
    return tool


def test_list_to_string_is_not_coerced() -> None:
    tool = _tool_with_input("todos", "string")
    args = {"todos": [{"content": "Task A", "status": "pending"}]}

    coerce_tool_arguments(tool, args)

    assert args["todos"] == [{"content": "Task A", "status": "pending"}]


def test_dict_to_string_is_not_coerced() -> None:
    tool = _tool_with_input("config", "string")
    args = {"config": {"key": "value"}}

    coerce_tool_arguments(tool, args)

    assert args["config"] == {"key": "value"}


def test_string_to_array_is_coerced() -> None:
    tool = _tool_with_input("items", "array")
    args = {"items": "[1, 2, 3]"}

    coerce_tool_arguments(tool, args)

    assert args["items"] == [1, 2, 3]


def test_string_to_object_is_coerced() -> None:
    tool = _tool_with_input("data", "object")
    args = {"data": '{"a": 1}'}

    coerce_tool_arguments(tool, args)

    assert args["data"] == {"a": 1}


def test_double_serialized_json_is_coerced() -> None:
    tool = _tool_with_input("sections", "array")
    inner_json = json.dumps([{"heading": "Intro", "body": "Hello"}])
    args = {"sections": json.dumps(inner_json)}

    coerce_tool_arguments(tool, args)

    assert args["sections"][0]["heading"] == "Intro"


def test_values_with_correct_type_are_unchanged() -> None:
    tool = _tool_with_input("items", "array")
    original = [1, 2, 3]
    args = {"items": original, "unknown": {"kept": True}}

    coerce_tool_arguments(tool, args)

    assert args["items"] is original
    assert args["unknown"] == {"kept": True}
