"""
Tests for enhanced multi-strategy tool call text parsing.

Tests cover:
- _extract_balanced_patch() — bracket-depth matching
- _try_structural_extract() — structural extraction fallback
- xml_tags + nested delegation (MiniMax XML wrapper format)
- Large string handling (>2KB quote conversion skip)
- Integration: parse_tool_call_resilient() end-to-end
"""

import json
import pytest

from src.lib.smolagents.monkey_patch.tool_call_parsing_patch import (
    ParsedToolCall,
    _extract_balanced_patch,
    _strategy_nested_tool_calls,
    _strategy_xml_tags,
    _try_structural_extract,
    parse_tool_call_resilient,
)


# ===================================================================
#  _extract_balanced_patch() tests
# ===================================================================

class TestExtractBalancedPatch:
    """Tests for the patch-module bracket-depth extraction."""

    def test_basic(self):
        result = _extract_balanced_patch('{"a": 1}', 0, "{", "}")
        assert result == '{"a": 1}'

    def test_nested(self):
        result = _extract_balanced_patch('{"a": {"b": [1,2]}}', 0, "{", "}")
        assert result == '{"a": {"b": [1,2]}}'


# ===================================================================
#  Phase 2: _try_structural_extract() tests
# ===================================================================

class TestTryStructuralExtract:
    """Tests for structural extraction in the text parsing fallback."""

    def test_basic_function_call(self):
        text = "{'id': 'c1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': {'file_path': '/a.h'}}}"
        result = _try_structural_extract(text)
        assert result is not None
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["function"]["name"] == "read_file"

    def test_no_function_key(self):
        text = "{'name': 'read_file', 'arguments': {'path': '/a.h'}}"
        result = _try_structural_extract(text)
        assert result is None

    def test_large_content(self):
        """Structural extraction should handle large content without quote conversion."""
        big_arg = "a" * 5000
        text = (
            "{'function': {'name': 'shell_tool', 'arguments': "
            "{'command': '" + big_arg + "'}}}"
        )
        result = _try_structural_extract(text)
        assert result is not None
        assert result[0]["function"]["name"] == "shell_tool"


# ===================================================================
#  Phase 2: xml_tags + nested delegation integration tests
# ===================================================================

class TestXmlTagsNestedDelegation:
    """Test that xml_tags delegates to nested_tool_calls when wrapper
    contains a list/dict format."""

    def test_minimax_wrapper_delegates_to_nested(self):
        text = (
            '<minimax:tool_call>\n'
            "[{'id': 'c1', 'type': 'function', 'function': {'name': 'read_file', "
            "'arguments': {'file_path': '/a.h'}}}]\n"
            '</minimax:tool_call>'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "read_file"
        assert "nested_delegate" in result.strategy or result.strategy == "xml_tags"

    def test_regular_xml_still_works(self):
        """Ensure regular XML format is not broken by the delegation."""
        text = '<tool_call><name>shell_tool</name><arguments>{"command": "ls"}</arguments></tool_call>'
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments.get("command") == "ls"


# ===================================================================
#  Phase 2: nested_tool_calls with structural fallback
# ===================================================================

class TestNestedToolCallsStructuralFallback:
    """Test that nested_tool_calls falls back to structural extraction."""

    def test_normal_small_content_still_works(self):
        text = "[{'id': 'c1', 'type': 'function', 'function': {'name': 'read_file', 'arguments': {'file_path': '/a.h'}}}]"
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert result.name == "read_file"

    def test_large_content_uses_structural(self):
        """Large content that breaks quote conversion should still work via structural extraction."""
        # Build a large argument value without embedded apostrophes or problematic escapes
        # that would break the bracket-depth parser. The key test is that the string
        # is > 2KB so quote conversion is skipped, forcing structural extraction.
        big_body = "table row data " * 200  # ~3KB, no apostrophes
        text = (
            "[{'id': 'c1', 'type': 'function', 'function': {'name': 'write_markdown_file', "
            "'arguments': {'file_path': './report.md', 'sections': [{'body': '"
            + big_body
            + "'}]}}}]"
        )
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert result.name == "write_markdown_file"

    def test_malformed_parallel_calls_structural_fallback(self):
        """MiniMax malformed parallel calls — each dict missing one closing brace."""
        text = (
            "[{'id': 'c1', 'type': 'function', 'function': {'name': 'read_file', "
            "'arguments': {'file_path': '/a.h'}}, "
            "{'id': 'c2', 'type': 'function', 'function': {'name': 'read_file', "
            "'arguments': {'file_path': '/b.h'}}]"
        )
        result = _strategy_nested_tool_calls(text)
        # Should extract at least the first tool call via structural fallback
        assert result is not None
        assert result.name == "read_file"


# ===================================================================
#  Integration: parse_tool_call_resilient() end-to-end
# ===================================================================

class TestParseToolCallResilientIntegration:
    """End-to-end tests for the full parsing chain."""

    def test_minimax_wrapper_with_nested_list(self):
        text = (
            '<minimax:tool_call>\n'
            "[{'id': 'c1', 'type': 'function', 'function': {'name': 'read_file', "
            "'arguments': {'file_path': '/test.h'}}}]\n"
            '</minimax:tool_call>'
        )
        result = parse_tool_call_resilient(
            text, available_tool_names=["read_file"]
        )
        assert result.name == "read_file"
        assert result.arguments["file_path"] == "/test.h"

    def test_standard_json_still_works(self):
        text = '{"name": "shell_tool", "arguments": {"command": "pwd"}}'
        result = parse_tool_call_resilient(
            text, available_tool_names=["shell_tool"]
        )
        assert result.name == "shell_tool"

    def test_large_write_markdown_file_call(self):
        """The exact scenario that was causing FORMAT_NOT_FOUND errors."""
        sections = [
            {"heading": "Report", "level": 1, "body": "| " + "c|" * 50 + "\\n"},
            {"heading": "Details", "level": 2, "body": "Content " * 100},
        ]
        text = (
            '<minimax:tool_call>\n'
            "[{'id': 'call_big', 'type': 'function', 'function': "
            "{'name': 'write_markdown_file', 'arguments': "
            "{'file_path': './temp/CAN_RiskList.md', 'overwrite': True, "
            f"'sections': {sections}" + "}}}]\n"
            '</minimax:tool_call>'
        )
        result = parse_tool_call_resilient(
            text, available_tool_names=["write_markdown_file"]
        )
        assert result.name == "write_markdown_file"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
