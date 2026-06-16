import json

import pytest

from src.lib.smolagents.models.tool_call_parser import (
    ToolCallParseError,
    parse_json_with_repair,
    parse_structured_tool_call,
    repair_json_string_literals,
)


TOOL_NAMES = ["read_file", "write_file", "shell_tool", "final_answer"]


class TestJsonRepair:
    def test_valid_json_is_unchanged(self):
        raw = '{"content": "hello", "items": [1, 2]}'
        assert repair_json_string_literals(raw) == raw
        assert parse_json_with_repair(raw) == {"content": "hello", "items": [1, 2]}

    def test_raw_control_chars_inside_string_are_escaped(self):
        raw = '{"content": "line1\nline2"}'
        assert json.loads(repair_json_string_literals(raw)) == {"content": "line1\nline2"}
        assert parse_json_with_repair(raw) == {"content": "line1\nline2"}

    def test_invalid_backslash_inside_string_is_escaped(self):
        raw = '{"pattern": "a' + "\\" + 'qb"}'
        assert parse_json_with_repair(raw) == {"pattern": "a\\qb"}

    def test_invalid_unicode_escape_inside_string_is_escaped(self):
        raw = '{"pattern": "a' + "\\" + 'u12xZ"}'
        assert parse_json_with_repair(raw) == {"pattern": "a\\u12xZ"}


class TestExplicitToolCallContainers:
    def test_standard_json_tool_call(self):
        result = parse_structured_tool_call(
            '{"name": "read_file", "arguments": {"path": "/tmp/a.txt"}}',
            TOOL_NAMES,
        )
        assert result.name == "read_file"
        assert result.arguments == {"path": "/tmp/a.txt"}
        assert result.source == "json"

    def test_arguments_json_string_uses_repair(self):
        text = '{"name": "write_file", "arguments": "{\\"path\\": \\"/tmp/a.txt\\", \\"content\\": \\"a\nb\\"}"}'
        result = parse_structured_tool_call(text, TOOL_NAMES)
        assert result.name == "write_file"
        assert result.arguments == {"path": "/tmp/a.txt", "content": "a\nb"}

    def test_dumped_native_tool_calls_json(self):
        text = json.dumps(
            {
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "/tmp/a.txt"}),
                        },
                    }
                ]
            }
        )
        result = parse_structured_tool_call(text, TOOL_NAMES)
        assert result.id == "call_1"
        assert result.name == "read_file"
        assert result.arguments == {"path": "/tmp/a.txt"}

    def test_minimax_xml_wrapper_with_python_native_dump(self):
        text = (
            "<minimax:tool_call>"
            "[{'id': 'call_2', 'type': 'function', "
            "'function': {'name': 'read_file', 'arguments': {'path': '/tmp/b.txt'}}}]"
            "</minimax:tool_call>"
        )
        result = parse_structured_tool_call(text, TOOL_NAMES)
        assert result.id == "call_2"
        assert result.name == "read_file"
        assert result.arguments == {"path": "/tmp/b.txt"}
        assert result.source == "xml_native_dump"

    def test_xml_name_arguments_wrapper(self):
        text = '<tool_call><name>shell_tool</name><arguments>{"command": "pwd"}</arguments></tool_call>'
        result = parse_structured_tool_call(text, TOOL_NAMES)
        assert result.name == "shell_tool"
        assert result.arguments == {"command": "pwd"}

    def test_xml_invoke_parameters(self):
        text = '<invoke name="write_file"><parameter name="path">/tmp/x</parameter><parameter name="content">hi</parameter></invoke>'
        result = parse_structured_tool_call(text, TOOL_NAMES)
        assert result.name == "write_file"
        assert result.arguments == {"path": "/tmp/x", "content": "hi"}


class TestFailClosedBehavior:
    def test_free_text_calling_tool_is_rejected(self):
        text = 'Calling tool: read_file with arguments: {"path": "/tmp/a"}'
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_structured_tool_call(text, TOOL_NAMES)
        assert exc_info.value.error_category.value == "FORMAT_NOT_FOUND"

    def test_direct_python_dict_is_rejected(self):
        text = "{'name': 'read_file', 'arguments': {'path': '/tmp/a'}}"
        with pytest.raises(ToolCallParseError):
            parse_structured_tool_call(text, TOOL_NAMES)

    def test_unknown_tool_is_rejected(self):
        text = '{"name": "missing_tool", "arguments": {"path": "/tmp/a"}}'
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_structured_tool_call(text, TOOL_NAMES)
        assert exc_info.value.error_category.value == "UNKNOWN_TOOL"

    def test_multiple_text_fallback_tool_calls_are_rejected(self):
        text = json.dumps(
            {
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": {"path": "a"}}},
                    {"id": "b", "type": "function", "function": {"name": "read_file", "arguments": {"path": "b"}}},
                ]
            }
        )
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_structured_tool_call(text, TOOL_NAMES)
        assert "Multiple tool calls are not supported" in str(exc_info.value)

    def test_incomplete_final_json_is_rejected(self):
        with pytest.raises(ToolCallParseError):
            parse_structured_tool_call('{"name": "read_file", "arguments": {"path": "/tmp/a"', TOOL_NAMES)
