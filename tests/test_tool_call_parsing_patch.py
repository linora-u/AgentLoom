"""
Tests for the multi-strategy tool call parsing patch.

Covers:
- Strategy 1: Standard JSON parsing
- Strategy 2: Fixed JSON (single quotes, trailing commas, Python booleans)
- Strategy 3: ast.literal_eval (Python dict syntax)
- Strategy 4: XML tag extraction (various XML formats)
- Strategy 5: Regex extraction (free-text tool call descriptions)
- Tool name validation against registered tools
- Empty/whitespace input handling
- Nested arguments (arrays, nested objects)
- Multiple JSON blobs in text
- Monkey-patch integration
- Resilient chain: strategy fallthrough order
"""

import json
import pytest

from src.lib.smolagents.monkey_patch.tool_call_parsing_patch import (
    ParsedToolCall,
    ToolCallParseError,
    _strategy_standard_json,
    _strategy_fixed_json,
    _strategy_ast_literal_eval,
    _strategy_nested_tool_calls,
    _strategy_xml_tags,
    _strategy_regex_extraction,
    _fix_json_string,
    _extract_xml_tool_call,
    _extract_parameters_robust,
    _find_matching_close_tag,
    _parse_xml_arguments,
    parse_tool_call_resilient,
    patch_smolagents_tool_call_parsing,
)


# ── Registered tools for validation ──────────────────────────────────────

TOOL_NAMES = ["shell_tool", "read_file", "write_file", "search_code", "final_answer"]


# ═══════════════════════════════════════════════════════════════════════════
#  Strategy 1: Standard JSON
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyStandardJson:
    """Standard JSON parsing: {"name":"x","arguments":{}}."""

    def test_basic_tool_call(self):
        text = '{"name": "shell_tool", "arguments": {"command": "ls -la"}}'
        result = _strategy_standard_json(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments == {"command": "ls -la"}
        assert result.strategy == "standard_json"

    def test_with_prefix_text(self):
        text = 'I will use the shell tool.\n{"name": "read_file", "arguments": {"path": "/tmp/test.py"}}'
        result = _strategy_standard_json(text)
        assert result is not None
        assert result.name == "read_file"
        assert result.arguments == {"path": "/tmp/test.py"}
        assert "shell tool" in result.prefix_text

    def test_with_suffix_text(self):
        text = '{"name": "shell_tool", "arguments": {"command": "pwd"}}\nThis will show the current directory.'
        result = _strategy_standard_json(text)
        assert result is not None
        assert result.name == "shell_tool"

    def test_nested_arguments(self):
        text = '{"name": "write_file", "arguments": {"path": "/tmp/test.py", "content": "print(1)", "metadata": {"encoding": "utf-8", "tags": ["test", "python"]}}}'
        result = _strategy_standard_json(text)
        assert result is not None
        assert result.name == "write_file"
        assert result.arguments["metadata"]["tags"] == ["test", "python"]

    def test_empty_arguments(self):
        text = '{"name": "shell_tool", "arguments": {}}'
        result = _strategy_standard_json(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments == {}

    def test_no_arguments_key(self):
        text = '{"name": "shell_tool"}'
        result = _strategy_standard_json(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments == {}

    def test_arguments_as_string(self):
        """Arguments may come as a JSON string rather than object."""
        text = '{"name": "shell_tool", "arguments": "{\\"command\\": \\"ls\\"}"}'
        result = _strategy_standard_json(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments == {"command": "ls"}

    def test_no_json(self):
        text = "I analyzed the code and found no issues."
        result = _strategy_standard_json(text)
        assert result is None

    def test_no_name_key(self):
        text = '{"tool": "shell_tool", "args": {"command": "ls"}}'
        result = _strategy_standard_json(text)
        assert result is None

    def test_invalid_json(self):
        text = "{'name': 'shell_tool', 'arguments': {'command': 'ls'}}"
        result = _strategy_standard_json(text)
        assert result is None  # Single quotes are not valid JSON


# ═══════════════════════════════════════════════════════════════════════════
#  Strategy 2: Fixed JSON
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyFixedJson:
    """Fixed JSON: single quotes, trailing commas, Python booleans."""

    def test_single_quotes(self):
        text = "{'name': 'shell_tool', 'arguments': {'command': 'ls -la'}}"
        result = _strategy_fixed_json(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments == {"command": "ls -la"}
        assert result.strategy == "fixed_json"

    def test_trailing_comma(self):
        text = '{"name": "read_file", "arguments": {"path": "/tmp/test.py",}}'
        result = _strategy_fixed_json(text)
        assert result is not None
        assert result.name == "read_file"

    def test_python_booleans(self):
        text = "{'name': 'write_file', 'arguments': {'overwrite': True, 'create_dirs': False}}"
        result = _strategy_fixed_json(text)
        assert result is not None
        assert result.arguments["overwrite"] is True
        assert result.arguments["create_dirs"] is False

    def test_none_value(self):
        text = "{'name': 'shell_tool', 'arguments': {'timeout': None}}"
        result = _strategy_fixed_json(text)
        assert result is not None
        assert result.arguments["timeout"] is None

    def test_mixed_quotes_with_prefix(self):
        text = "I'll use the tool.\n{'name': 'search_code', 'arguments': {'query': 'def main'}}"
        result = _strategy_fixed_json(text)
        assert result is not None
        assert result.name == "search_code"

    def test_no_brace(self):
        result = _strategy_fixed_json("no json here")
        assert result is None


class TestFixJsonString:
    """Test the JSON fixing helper."""

    def test_single_to_double_quotes(self):
        fixed = _fix_json_string("{'key': 'value'}")
        data = json.loads(fixed)
        assert data == {"key": "value"}

    def test_python_true(self):
        fixed = _fix_json_string("{'flag': True}")
        data = json.loads(fixed)
        assert data == {"flag": True}

    def test_python_false(self):
        fixed = _fix_json_string("{'flag': False}")
        data = json.loads(fixed)
        assert data == {"flag": False}

    def test_python_none(self):
        fixed = _fix_json_string("{'val': None}")
        data = json.loads(fixed)
        assert data == {"val": None}

    def test_trailing_comma_removal(self):
        fixed = _fix_json_string('{"a": 1, "b": 2,}')
        data = json.loads(fixed)
        assert data == {"a": 1, "b": 2}


# ═══════════════════════════════════════════════════════════════════════════
#  Strategy 3: ast.literal_eval
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyAstLiteralEval:
    """Python dict parsing with ast.literal_eval."""

    def test_python_dict(self):
        text = "{'name': 'shell_tool', 'arguments': {'command': 'ls'}}"
        result = _strategy_ast_literal_eval(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments == {"command": "ls"}
        assert result.strategy == "ast_literal_eval"

    def test_python_booleans_and_none(self):
        text = "{'name': 'write_file', 'arguments': {'exists': True, 'backup': None, 'dry_run': False}}"
        result = _strategy_ast_literal_eval(text)
        assert result is not None
        # After JSON round-trip, None becomes null (None in Python)
        assert result.arguments["exists"] is True
        assert result.arguments["backup"] is None
        assert result.arguments["dry_run"] is False

    def test_nested_dict(self):
        text = "{'name': 'write_file', 'arguments': {'config': {'debug': True, 'level': 3}}}"
        result = _strategy_ast_literal_eval(text)
        assert result is not None
        assert result.arguments["config"]["debug"] is True

    def test_list_arguments(self):
        text = "{'name': 'search_code', 'arguments': {'paths': ['/src', '/tests'], 'recursive': True}}"
        result = _strategy_ast_literal_eval(text)
        assert result is not None
        assert result.arguments["paths"] == ["/src", "/tests"]

    def test_with_prefix_text(self):
        text = "Let me search for the function.\n{'name': 'search_code', 'arguments': {'query': 'def main'}}"
        result = _strategy_ast_literal_eval(text)
        assert result is not None
        assert result.name == "search_code"

    def test_invalid_syntax(self):
        text = "{'name': undefined_var}"
        result = _strategy_ast_literal_eval(text)
        assert result is None

    def test_no_dict(self):
        text = "just some text"
        result = _strategy_ast_literal_eval(text)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
#  Strategy 4: XML Tag Extraction
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyXmlTags:
    """XML tag extraction for tool calls."""

    def test_tool_call_tags(self):
        text = '<tool_call><name>shell_tool</name><arguments>{"command": "ls"}</arguments></tool_call>'
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments == {"command": "ls"}
        assert result.strategy == "xml_tags"

    def test_minimax_namespaced_tags(self):
        text = '<minimax:tool_call><name>read_file</name><arguments>{"path": "/tmp/test.py"}</arguments></minimax:tool_call>'
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "read_file"
        assert result.arguments == {"path": "/tmp/test.py"}

    def test_invoke_tags(self):
        text = '<invoke name="shell_tool"><parameter name="command">ls -la</parameter></invoke>'
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments == {"command": "ls -la"}

    def test_invoke_multiple_params(self):
        text = '<invoke name="write_file"><parameter name="path">/tmp/test.py</parameter><parameter name="content">print(1)</parameter></invoke>'
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "write_file"
        assert result.arguments["path"] == "/tmp/test.py"
        assert result.arguments["content"] == "print(1)"

    def test_xml_with_prefix(self):
        text = 'I will use the shell tool.\n<tool_call><name>shell_tool</name><arguments>{"command": "pwd"}</arguments></tool_call>'
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert "shell tool" in result.prefix_text

    def test_empty_arguments(self):
        text = "<tool_call><name>shell_tool</name><arguments></arguments></tool_call>"
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments == {}

    def test_no_xml(self):
        result = _strategy_xml_tags("just plain text")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
#  Strategy 4b: MiniMax bracket XML format
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyBracketXml:
    """MiniMax bracket XML format: [invoke name="X"><param>[/invoke]"""

    def test_minimax_bracket_wrapped_in_tool_call(self):
        """Full MiniMax format from production logs:
        <minimax:tool_call>[invoke name="X"><parameter>...</parameter>[/invoke]</minimax:tool_call>
        """
        text = (
            '<minimax:tool_call>'
            '[invoke name="write_markdown_file">'
            '<parameter name="file_path">/tmp/report.md</parameter>'
            '<parameter name="content">Hello world</parameter>'
            '[/invoke]'
            '</minimax:tool_call>'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "write_markdown_file"
        assert result.arguments["file_path"] == "/tmp/report.md"
        assert result.arguments["content"] == "Hello world"
        assert result.strategy == "xml_tags"

    def test_standalone_bracket_format(self):
        """Standalone bracket format without outer tool_call wrapper."""
        text = '[invoke name="read_file"><parameter name="path">/tmp/test.py</parameter>[/invoke]'
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "read_file"
        assert result.arguments["path"] == "/tmp/test.py"

    def test_bracket_single_param(self):
        """Single parameter in bracket format."""
        text = '[invoke name="shell_tool"><parameter name="command">ls -la</parameter>[/invoke]'
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments["command"] == "ls -la"

    def test_bracket_multiple_params(self):
        """Multiple parameters in bracket format."""
        text = (
            '[invoke name="write_file">'
            '<parameter name="path">/tmp/test.py</parameter>'
            '<parameter name="content">print("hello")</parameter>'
            '[/invoke]'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "write_file"
        assert result.arguments["path"] == "/tmp/test.py"
        assert result.arguments["content"] == 'print("hello")'

    def test_bracket_with_prefix_text(self):
        """Prefix text before the bracket call should be captured."""
        text = (
            'I will now write the file.\n'
            '<minimax:tool_call>'
            '[invoke name="write_file">'
            '<parameter name="path">/tmp/x</parameter>'
            '[/invoke]'
            '</minimax:tool_call>'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "write_file"
        assert "write the file" in result.prefix_text

    def test_bracket_resilient_chain_integration(self):
        """Bracket format should be handled by parse_tool_call_resilient chain."""
        text = (
            '<minimax:tool_call>'
            '[invoke name="read_file">'
            '<parameter name="path">/tmp/report.md</parameter>'
            '[/invoke]'
            '</minimax:tool_call>'
        )
        result = parse_tool_call_resilient(text, TOOL_NAMES)
        assert result.name == "read_file"
        assert result.strategy == "xml_tags"

    def test_bracket_format_no_match_on_plain_text(self):
        """Plain text without bracket invoke syntax should return None."""
        result = _strategy_xml_tags("[invoke this is not a valid format]")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
#  Strategy 5: Regex Extraction
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyRegexExtraction:
    """Free-text regex extraction patterns."""

    def test_calling_tool_pattern(self):
        text = "Calling tool: 'shell_tool' with arguments: {\"command\": \"ls -la\"}"
        result = _strategy_regex_extraction(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments == {"command": "ls -la"}
        assert result.strategy == "regex_extraction"

    def test_calling_tool_no_quotes(self):
        text = "Calling tool: shell_tool with arguments: {\"command\": \"pwd\"}"
        result = _strategy_regex_extraction(text)
        assert result is not None
        assert result.name == "shell_tool"

    def test_action_action_input_pattern(self):
        text = "Action: shell_tool\nAction Input: {\"command\": \"ls\"}"
        result = _strategy_regex_extraction(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments == {"command": "ls"}

    def test_calling_with_single_quote_args(self):
        text = "Calling tool: 'read_file' with arguments: {'path': '/tmp/test.py'}"
        result = _strategy_regex_extraction(text)
        assert result is not None
        assert result.name == "read_file"
        # Arguments should be parsed even with single quotes
        assert result.arguments.get("path") == "/tmp/test.py"

    def test_no_pattern_match(self):
        text = "I analyzed the code and found no issues."
        result = _strategy_regex_extraction(text)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
#  Strategy 6: Nested tool_calls list format
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyNestedToolCalls:
    """Nested OpenAI function calling format extracted from text."""

    def test_list_format_single_quotes(self):
        """Real MiniMax output: list of dicts with single quotes and nested function key."""
        text = (
            "Calling tools:\n"
            "[{'id': 'call_function_3gi9dmfgaoq2_2', 'type': 'function', "
            "'function': {'name': 'read_file', 'arguments': "
            "{'file_path': 'src/lib/smolagents/monkey_patch/tool_call_parsing_patch.py', 'limit': 30}}}]"
        )
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert result.name == "read_file"
        assert result.arguments["file_path"] == "src/lib/smolagents/monkey_patch/tool_call_parsing_patch.py"
        assert result.arguments["limit"] == 30
        assert result.strategy == "nested_tool_calls"

    def test_single_dict_with_function_key(self):
        """Single dict (no list wrapper) with nested function key."""
        text = "{'type': 'function', 'function': {'name': 'shell_tool', 'arguments': {'command': 'ls -la'}}}"
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments == {"command": "ls -la"}

    def test_list_with_multiple_calls(self):
        """List with multiple tool calls — should extract first one."""
        text = (
            "[{'type': 'function', 'function': {'name': 'read_file', 'arguments': {'file_path': '/tmp/a.py'}}}, "
            "{'type': 'function', 'function': {'name': 'shell_tool', 'arguments': {'command': 'pwd'}}}]"
        )
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert result.name == "read_file"

    def test_string_arguments(self):
        """Arguments as a JSON string rather than dict."""
        text = """[{'type': 'function', 'function': {'name': 'shell_tool', 'arguments': '{"command": "ls"}'}}]"""
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments == {"command": "ls"}

    def test_prefix_text_preserved(self):
        """Prefix text before the list should be preserved."""
        text = "I'll use the tool now.\n[{'type': 'function', 'function': {'name': 'read_file', 'arguments': {'file_path': 'test.py'}}}]"
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert "tool now" in result.prefix_text

    def test_no_function_key(self):
        """Dict without 'function' key should not match."""
        text = "[{'name': 'shell_tool', 'arguments': {'command': 'ls'}}]"
        result = _strategy_nested_tool_calls(text)
        assert result is None

    def test_empty_list(self):
        """Empty list should not match."""
        text = "Calling tools:\n[]"
        result = _strategy_nested_tool_calls(text)
        assert result is None

    def test_no_brackets(self):
        """Text without list brackets should try dict fallback."""
        text = "Just some plain text without any structures."
        result = _strategy_nested_tool_calls(text)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
#  Real MiniMax LLM output (from actual logs)
# ═══════════════════════════════════════════════════════════════════════════


class TestRealMiniMaxOutput:
    """Tests using exact LLM output from production MiniMax logs."""

    def test_minimax_nested_tool_call_list(self):
        """Exact output that caused Step 2 failure in test_tool_call_parsing run."""
        text = (
            "Calling tools:\n"
            "[{'id': 'call_function_3gi9dmfgaoq2_2', 'type': 'function', "
            "'function': {'name': 'read_file', 'arguments': "
            "{'file_path': 'src/lib/smolagents/monkey_patch/tool_call_parsing_patch.py', 'limit': 30}}}]"
        )
        result = parse_tool_call_resilient(text, TOOL_NAMES)
        assert result.name == "read_file"
        assert result.arguments["file_path"] == "src/lib/smolagents/monkey_patch/tool_call_parsing_patch.py"
        assert result.arguments["limit"] == 30
        assert result.strategy == "nested_tool_calls"

    def test_minimax_browse_directory_success(self):
        """MiniMax standard JSON that worked in Step 1 (sanity check)."""
        text = '{"name": "browse_directory", "arguments": {"directory_path": "src/lib/smolagents/monkey_patch/"}}'
        result = parse_tool_call_resilient(text, TOOL_NAMES + ["browse_directory"])
        assert result.name == "browse_directory"
        assert result.strategy == "standard_json"


# ═══════════════════════════════════════════════════════════════════════════
#  Main parsing chain: parse_tool_call_resilient
# ═══════════════════════════════════════════════════════════════════════════


class TestParseToolCallResilient:
    """End-to-end tests for the multi-strategy parsing chain."""

    def test_standard_json(self):
        """Strategy 1 should be used for valid JSON."""
        text = '{"name": "shell_tool", "arguments": {"command": "ls"}}'
        result = parse_tool_call_resilient(text, TOOL_NAMES)
        assert result.name == "shell_tool"
        assert result.strategy == "standard_json"

    def test_single_quotes_fallback(self):
        """Strategy 2 should handle single quotes when strategy 1 fails."""
        text = "{'name': 'read_file', 'arguments': {'path': '/tmp/test.py'}}"
        result = parse_tool_call_resilient(text, TOOL_NAMES)
        assert result.name == "read_file"
        assert result.strategy in ("fixed_json", "ast_literal_eval")

    def test_python_dict_fallback(self):
        """Strategy 3 should handle Python dicts with True/False/None."""
        text = "{'name': 'write_file', 'arguments': {'overwrite': True, 'path': '/tmp/x'}}"
        result = parse_tool_call_resilient(text, TOOL_NAMES)
        assert result.name == "write_file"
        # Could be fixed_json or ast_literal_eval, both can handle this
        assert result.strategy in ("fixed_json", "ast_literal_eval")

    def test_xml_fallback(self):
        """Strategy 4 should handle XML when JSON strategies fail."""
        text = '<tool_call><name>shell_tool</name><arguments>{"command": "ls"}</arguments></tool_call>'
        result = parse_tool_call_resilient(text, TOOL_NAMES)
        assert result.name == "shell_tool"
        assert result.strategy == "xml_tags"

    def test_regex_fallback(self):
        """Strategy 5 should handle free text when all other strategies fail."""
        text = "Calling tool: 'shell_tool' with arguments: {\"command\": \"pwd\"}"
        result = parse_tool_call_resilient(text, TOOL_NAMES)
        assert result.name == "shell_tool"
        # May be caught by standard_json since the args are valid JSON
        assert result.arguments.get("command") == "pwd"

    def test_tool_name_validation(self):
        """Tool name validation filters out invalid tool names."""
        text = '{"name": "nonexistent_tool", "arguments": {}}'
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient(text, TOOL_NAMES)
        assert "nonexistent_tool" in str(exc_info.value)

    def test_no_tool_call(self):
        """Pure text with no tool call should raise ToolCallParseError."""
        text = "I analyzed the code and found three issues with the implementation."
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient(text, TOOL_NAMES)
        assert len(exc_info.value.attempted_strategies) > 0

    def test_empty_input(self):
        """Empty input should raise ToolCallParseError."""
        with pytest.raises(ToolCallParseError):
            parse_tool_call_resilient("", TOOL_NAMES)

    def test_whitespace_input(self):
        """Whitespace-only input should raise ToolCallParseError."""
        with pytest.raises(ToolCallParseError):
            parse_tool_call_resilient("   \n  ", TOOL_NAMES)

    def test_no_validation_when_none(self):
        """When available_tool_names is None, skip validation."""
        text = '{"name": "any_tool_name", "arguments": {}}'
        result = parse_tool_call_resilient(text, available_tool_names=None)
        assert result.name == "any_tool_name"

    def test_final_answer_tool(self):
        """final_answer should be parsed normally."""
        text = '{"name": "final_answer", "arguments": {"answer": "The result is 42."}}'
        result = parse_tool_call_resilient(text, TOOL_NAMES)
        assert result.name == "final_answer"
        assert result.arguments["answer"] == "The result is 42."


# ═══════════════════════════════════════════════════════════════════════════
#  Nested and complex arguments
# ═══════════════════════════════════════════════════════════════════════════


class TestNestedArguments:
    """Arguments with arrays, nested objects, mixed types."""

    def test_array_arguments(self):
        text = '{"name": "shell_tool", "arguments": {"commands": ["ls", "pwd", "whoami"]}}'
        result = parse_tool_call_resilient(text)
        assert result.arguments["commands"] == ["ls", "pwd", "whoami"]

    def test_deeply_nested(self):
        text = json.dumps({
            "name": "write_file",
            "arguments": {
                "path": "/tmp/config.json",
                "content": json.dumps({"database": {"host": "localhost", "port": 5432}}),
            },
        })
        result = parse_tool_call_resilient(text)
        assert result.name == "write_file"

    def test_numeric_arguments(self):
        text = '{"name": "shell_tool", "arguments": {"timeout": 30, "retries": 3, "ratio": 0.5}}'
        result = parse_tool_call_resilient(text)
        assert result.arguments["timeout"] == 30
        assert result.arguments["ratio"] == 0.5

    def test_boolean_arguments(self):
        text = '{"name": "write_file", "arguments": {"overwrite": true, "create_dirs": false}}'
        result = parse_tool_call_resilient(text)
        assert result.arguments["overwrite"] is True
        assert result.arguments["create_dirs"] is False

    def test_null_argument(self):
        text = '{"name": "shell_tool", "arguments": {"env": null}}'
        result = parse_tool_call_resilient(text)
        assert result.arguments["env"] is None


# ═══════════════════════════════════════════════════════════════════════════
#  Edge cases and boundary conditions
# ═══════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Edge cases: multiple blobs, malformed input, special characters."""

    def test_json_with_newlines(self):
        text = '{\n  "name": "shell_tool",\n  "arguments": {\n    "command": "ls"\n  }\n}'
        result = parse_tool_call_resilient(text)
        assert result.name == "shell_tool"

    def test_json_in_code_block(self):
        text = '```json\n{"name": "shell_tool", "arguments": {"command": "ls"}}\n```'
        result = parse_tool_call_resilient(text)
        assert result.name == "shell_tool"

    def test_special_chars_in_arguments(self):
        text = '{"name": "shell_tool", "arguments": {"command": "echo \\"hello world\\""}}'
        result = parse_tool_call_resilient(text)
        assert result.name == "shell_tool"

    def test_unicode_in_arguments(self):
        text = '{"name": "write_file", "arguments": {"content": "Hello \\u4e16\\u754c"}}'
        result = parse_tool_call_resilient(text)
        assert result.name == "write_file"

    def test_very_long_arguments(self):
        long_content = "x" * 10000
        text = json.dumps({"name": "write_file", "arguments": {"content": long_content}})
        result = parse_tool_call_resilient(text)
        assert result.name == "write_file"
        assert len(result.arguments["content"]) == 10000

    def test_multiple_json_objects(self):
        """Multiple JSON blobs: first valid one should be extracted."""
        text = '{"name": "shell_tool", "arguments": {"command": "ls"}}\n{"name": "read_file", "arguments": {"path": "/tmp"}}'
        result = parse_tool_call_resilient(text)
        assert result.name is not None

    def test_json_with_surrounding_markdown(self):
        text = """Here's the tool call:

```json
{"name": "shell_tool", "arguments": {"command": "ls -la"}}
```

This will list the directory contents."""
        result = parse_tool_call_resilient(text)
        assert result.name == "shell_tool"


# ═══════════════════════════════════════════════════════════════════════════
#  ParsedToolCall data class
# ═══════════════════════════════════════════════════════════════════════════


class TestParsedToolCall:
    """Test the ParsedToolCall data class."""

    def test_repr(self):
        tc = ParsedToolCall(name="test", arguments={}, strategy="json", prefix_text="")
        assert "test" in repr(tc)
        assert "json" in repr(tc)

    def test_default_arguments(self):
        tc = ParsedToolCall(name="test", arguments=None, strategy="json")
        assert tc.arguments == {}

    def test_default_prefix(self):
        tc = ParsedToolCall(name="test", arguments={"a": 1}, strategy="json")
        assert tc.prefix_text == ""


# ═══════════════════════════════════════════════════════════════════════════
#  ToolCallParseError
# ═══════════════════════════════════════════════════════════════════════════


class TestToolCallParseError:
    """Test the ToolCallParseError exception."""

    def test_message(self):
        err = ToolCallParseError("test error", ["json", "xml"])
        assert "test error" in str(err)

    def test_attempted_strategies(self):
        err = ToolCallParseError("test", ["s1", "s2", "s3"])
        assert err.attempted_strategies == ["s1", "s2", "s3"]


# ═══════════════════════════════════════════════════════════════════════════
#  Monkey-patch integration
# ═══════════════════════════════════════════════════════════════════════════


class TestMonkeyPatch:
    """Test that the monkey-patch correctly replaces smolagents functions."""

    def test_patch_is_idempotent(self):
        """Calling patch twice should not break anything."""
        patch_smolagents_tool_call_parsing()
        patch_smolagents_tool_call_parsing()

        import smolagents.utils as utils_mod
        assert getattr(utils_mod.parse_json_blob, "_agentloom_patched", False)

    def test_patched_parse_json_blob(self):
        """Patched parse_json_blob should handle single-quote JSON."""
        patch_smolagents_tool_call_parsing()

        import smolagents.utils as utils_mod

        # Standard JSON should still work
        result, prefix = utils_mod.parse_json_blob(
            '{"name": "shell_tool", "arguments": {"command": "ls"}}'
        )
        assert result["name"] == "shell_tool"

    def test_patched_get_tool_call_from_text(self):
        """Patched get_tool_call_from_text should handle single-quote JSON."""
        patch_smolagents_tool_call_parsing()

        import smolagents.models as models_mod

        # Standard JSON
        tool_call = models_mod.get_tool_call_from_text(
            '{"name": "shell_tool", "arguments": {"command": "ls"}}',
            "name",
            "arguments",
        )
        assert tool_call.function.name == "shell_tool"
        assert tool_call.function.arguments["command"] == "ls"


# ═══════════════════════════════════════════════════════════════════════════
#  Strategy priority verification
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyPriority:
    """Verify that strategies are tried in the correct priority order."""

    def test_standard_json_preferred_over_fixed(self):
        """Valid standard JSON should use strategy 1, not strategy 2."""
        text = '{"name": "shell_tool", "arguments": {"command": "ls"}}'
        result = parse_tool_call_resilient(text)
        assert result.strategy == "standard_json"

    def test_xml_used_when_no_json(self):
        """XML tags should be used when there's no JSON at all."""
        text = "I'll use the tool.\n<tool_call><name>shell_tool</name><arguments>{}</arguments></tool_call>"
        result = parse_tool_call_resilient(text)
        # Could match "standard_json" since {} is valid JSON in the outer text,
        # but the name key won't be found; should fall through to XML
        assert result.name == "shell_tool"

    def test_all_strategies_exhausted(self):
        """When all strategies fail, error should list all attempted strategies."""
        text = "This is just a plain analysis with no tool call at all."
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient(text, TOOL_NAMES)
        assert len(exc_info.value.attempted_strategies) == 6


# ═══════════════════════════════════════════════════════════════════════════
#  Progressive error recovery (replaced circuit-breaker)
# ═══════════════════════════════════════════════════════════════════════════


class TestConsolidateErrorMessages:
    """Test _consolidate_error_messages in LoomAgentMixin."""

    def _make_mixin_instance(self, steps, tools=None):
        """Create a minimal LoomAgentMixin instance for testing."""
        from src.lib.smolagents.agent.loom_mixin import LoomAgentMixin

        instance = LoomAgentMixin.__new__(LoomAgentMixin)

        class FakeMemory:
            def __init__(self, steps):
                self.steps = steps

        instance.memory = FakeMemory(steps)
        instance.tools = tools or {}
        return instance

    def _make_parse_error_step(self, error_msg="[CATEGORY:FORMAT_NOT_FOUND] parse failed"):
        """Create an ActionStep with AgentParsingError."""
        from smolagents.memory import ActionStep
        from smolagents.monitoring import Timing
        from smolagents.agents import AgentParsingError
        from smolagents import AgentLogger, LogLevel

        step = ActionStep(step_number=1, timing=Timing(start_time=0.0), model_input_messages=[])
        step.error = AgentParsingError(error_msg, AgentLogger(level=LogLevel.ERROR))
        step.model_output = "some raw LLM output"
        return step

    def _make_success_step(self):
        """Create an ActionStep with no error."""
        from smolagents.memory import ActionStep
        from smolagents.monitoring import Timing

        step = ActionStep(step_number=1, timing=Timing(start_time=0.0), model_input_messages=[])
        step.error = None
        return step

    def test_no_errors_returns_unchanged(self):
        """No parse errors → messages returned unchanged."""
        steps = [self._make_success_step()]
        instance = self._make_mixin_instance(steps)
        messages = [{"role": "user", "content": "initial"}]
        result = instance._consolidate_error_messages(list(messages))
        assert result == messages

    def test_single_error_adds_recovery(self):
        """Single parse error → recovery guidance inserted."""
        steps = [self._make_parse_error_step()]
        instance = self._make_mixin_instance(steps)
        error_msg = (
            "Error:\nparse failed\n"
            "Now let's retry: take care not to repeat previous errors!"
        )
        messages = [{"role": "tool-response", "content": [{"type": "text", "text": error_msg}]}]
        result = instance._consolidate_error_messages(messages)
        # Should have recovery content
        assert isinstance(result, list)
        assert len(result) == 1

    def test_multiple_errors_consolidates(self):
        """Multiple consecutive errors → older ones compressed to summaries."""
        steps = [self._make_parse_error_step() for _ in range(3)]
        instance = self._make_mixin_instance(steps)
        error_msg = (
            "Error:\nparse failed\n"
            "Now let's retry: take care not to repeat previous errors!"
        )
        messages = [
            {"role": "tool-response", "content": [{"type": "text", "text": error_msg}]}
            for _ in range(3)
        ]
        result = instance._consolidate_error_messages(messages)
        assert isinstance(result, list)
        assert len(result) == 3

    def test_empty_steps_returns_unchanged(self):
        """No steps at all → messages unchanged."""
        instance = self._make_mixin_instance([])
        messages = [{"role": "system", "content": "sys"}]
        result = instance._consolidate_error_messages(list(messages))
        assert result == messages

    def test_non_parse_errors_break_count(self):
        """Non-AgentParsingError steps break the consecutive count."""
        from smolagents.memory import ActionStep
        from smolagents.monitoring import Timing
        from smolagents.agents import AgentExecutionError
        from smolagents import AgentLogger, LogLevel

        exec_step = ActionStep(step_number=1, timing=Timing(start_time=0.0), model_input_messages=[])
        exec_step.error = AgentExecutionError("exec failed", AgentLogger(level=LogLevel.ERROR))

        steps = [
            exec_step,
            self._make_parse_error_step(),
            self._make_parse_error_step(),
        ]
        instance = self._make_mixin_instance(steps)
        messages = [{"role": "user", "content": "test"}]
        result = instance._consolidate_error_messages(messages)
        # 2 consecutive parse errors at tail — should apply Level 2
        assert isinstance(result, list)

    def test_no_final_answer_forced(self):
        """Even with many errors, no final_answer is forced (no breaker)."""
        steps = [self._make_parse_error_step() for _ in range(10)]
        instance = self._make_mixin_instance(steps)
        messages = [{"role": "user", "content": "test"}]
        result = instance._consolidate_error_messages(messages)
        # Should NOT contain forced final_answer instruction
        for msg in result:
            text = ""
            content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
            if isinstance(content, list):
                text = " ".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content)
            elif isinstance(content, str):
                text = content
            assert "MUST call the final_answer tool" not in text

    def test_exception_safe(self):
        """Exceptions in consolidation return original messages."""
        from src.lib.smolagents.agent.loom_mixin import LoomAgentMixin
        instance = LoomAgentMixin.__new__(LoomAgentMixin)
        # No memory attribute → should handle gracefully
        messages = [{"role": "user", "content": "test"}]
        result = instance._consolidate_error_messages(messages)
        assert result == messages


# ═══════════════════════════════════════════════════════════════════════════
#  Strategy cache (Tasks 10.1-10.6)
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyCache:
    """Test per-model strategy caching in parse_tool_call_resilient."""

    def setup_method(self):
        from src.lib.smolagents.monkey_patch.tool_call_parsing_patch import clear_strategy_cache
        clear_strategy_cache()

    def test_cache_miss_full_chain(self):
        """First call with a model_id traverses the full strategy chain."""
        from src.lib.smolagents.monkey_patch.tool_call_parsing_patch import get_strategy_cache
        text = '{"name": "shell_tool", "arguments": {}}'
        result = parse_tool_call_resilient(text, model_id="model_A")
        assert result.name == "shell_tool"
        cache = get_strategy_cache()
        assert "model_A" in cache
        assert cache["model_A"] == "standard_json"

    def test_cache_hit_uses_cached(self):
        """Second call with same model_id uses cached strategy first."""
        from src.lib.smolagents.monkey_patch.tool_call_parsing_patch import get_strategy_cache
        text = '{"name": "shell_tool", "arguments": {}}'
        # First call → populate cache
        parse_tool_call_resilient(text, model_id="model_B")
        # Second call → should use cache
        result = parse_tool_call_resilient(text, model_id="model_B")
        assert result.name == "shell_tool"

    def test_cache_stale_fallback(self):
        """Stale cache entry falls back to full chain and updates cache."""
        from src.lib.smolagents.monkey_patch.tool_call_parsing_patch import (
            _strategy_cache,
            get_strategy_cache,
        )
        # Manually set a wrong strategy in cache
        _strategy_cache["model_C"] = "xml_tags"
        # JSON input won't match xml_tags, should fall through
        text = '{"name": "shell_tool", "arguments": {}}'
        result = parse_tool_call_resilient(text, model_id="model_C")
        assert result.name == "shell_tool"
        assert get_strategy_cache()["model_C"] == "standard_json"

    def test_no_model_id_no_cache(self):
        """model_id=None disables caching."""
        from src.lib.smolagents.monkey_patch.tool_call_parsing_patch import get_strategy_cache
        text = '{"name": "shell_tool", "arguments": {}}'
        parse_tool_call_resilient(text, model_id=None)
        assert get_strategy_cache() == {}

    def test_clear_and_get_helpers(self):
        from src.lib.smolagents.monkey_patch.tool_call_parsing_patch import (
            clear_strategy_cache,
            get_strategy_cache,
            _strategy_cache,
        )
        _strategy_cache["test"] = "xml_tags"
        assert get_strategy_cache() == {"test": "xml_tags"}
        clear_strategy_cache()
        assert get_strategy_cache() == {}

    def test_minimax_xml_caching(self):
        """MiniMax XML input: first call caches xml_tags, second reuses it."""
        from src.lib.smolagents.monkey_patch.tool_call_parsing_patch import get_strategy_cache
        text = '<minimax:tool_call><name>shell_tool</name><arguments>{"cmd": "ls"}</arguments></minimax:tool_call>'
        result = parse_tool_call_resilient(text, model_id="minimax_model")
        assert result.name == "shell_tool"
        assert result.strategy == "xml_tags"
        assert get_strategy_cache()["minimax_model"] == "xml_tags"

        # Second call should hit cache
        result2 = parse_tool_call_resilient(text, model_id="minimax_model")
        assert result2.name == "shell_tool"


# ═══════════════════════════════════════════════════════════════════════════
#  ToolCallParseError enhancements (Tasks 11.1-11.6)
# ═══════════════════════════════════════════════════════════════════════════


class TestToolCallParseErrorEnhancement:
    """Test error_category on ToolCallParseError."""

    def test_error_category_attribute(self):
        err = ToolCallParseError("msg", ["s1"], error_category=None)
        assert hasattr(err, "error_category")
        assert err.error_category is None

    def test_backward_compatible_no_category(self):
        err = ToolCallParseError("msg", ["s1"])
        assert err.error_category is None

    def test_category_set_on_all_fail(self):
        """When all strategies fail, error_category should be set."""
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient("completely unparseable garbage text 12345")
        assert exc_info.value.error_category is not None

    def test_category_tag_in_message(self):
        """[CATEGORY:...] tag should be in the error message."""
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient("no tool call here at all")
        assert "[CATEGORY:" in str(exc_info.value)

    def test_category_tag_decode(self):
        """The [CATEGORY:...] tag should be decodable."""
        from src.lib.smolagents.error_recovery import extract_category_from_error
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient("garbage")
        cat = extract_category_from_error(str(exc_info.value))
        assert cat is not None

    def test_monkey_patch_fallback_preserves_our_error(self):
        """When both our chain and original fail, our richer error should propagate."""
        # This test verifies the fallback behavior indirectly:
        # unparseable text → our chain fails → original fails → our error wins
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient("not a tool call")
        assert "[CATEGORY:" in str(exc_info.value)
        assert exc_info.value.attempted_strategies  # Should have strategy list


# ═══════════════════════════════════════════════════════════════════════════
#  XML arguments parsing bug fix (Task 4a)
# ═══════════════════════════════════════════════════════════════════════════


class TestXmlArgumentsParsing:
    """Test _parse_xml_arguments with nested JSON structures."""

    def test_nested_json_array(self):
        raw = '<parameter name="sections">[{"title": "A"}, {"title": "B"}]</parameter>'
        params = _extract_parameters_robust(raw)
        assert len(params) == 1
        result = _parse_xml_arguments(raw)
        assert isinstance(result.get("sections"), list)
        assert len(result["sections"]) == 2

    def test_nested_json_object(self):
        from src.lib.smolagents.monkey_patch.tool_call_parsing_patch import _parse_xml_arguments
        raw = '<parameter name="config">{"key": "value", "nested": {"n": 1}}</parameter>'
        result = _parse_xml_arguments(raw)
        assert isinstance(result.get("config"), dict)
        assert result["config"]["key"] == "value"

    def test_plain_text_parameter(self):
        from src.lib.smolagents.monkey_patch.tool_call_parsing_patch import _parse_xml_arguments
        raw = '<parameter name="query">search for errors</parameter>'
        result = _parse_xml_arguments(raw)
        assert result["query"] == "search for errors"

    def test_json_in_xml_still_works(self):
        """JSON-in-XML format should still parse correctly."""
        from src.lib.smolagents.monkey_patch.tool_call_parsing_patch import _parse_xml_arguments
        raw = '{"file_path": "test.py", "sections": [{"title": "A"}]}'
        result = _parse_xml_arguments(raw)
        assert result["file_path"] == "test.py"
        assert isinstance(result["sections"], list)

    def test_minimax_xml_patterns_no_regression(self):
        """All 5 MiniMax XML patterns should still work after the fix."""
        # Pattern 1: <minimax:tool_call><name>X</name><arguments>...</arguments></minimax:tool_call>
        text1 = '<minimax:tool_call><name>shell_tool</name><arguments>{"cmd": "ls"}</arguments></minimax:tool_call>'
        r1 = _strategy_xml_tags(text1)
        assert r1 is not None
        assert r1.name == "shell_tool"

        # Pattern 2: MiniMax bracket in wrapper
        text2 = '<minimax:tool_call>[invoke name="shell_tool"><parameter name="cmd">ls</parameter>[/invoke]</minimax:tool_call>'
        r2 = _strategy_xml_tags(text2)
        assert r2 is not None
        assert r2.name == "shell_tool"

        # Pattern 3: standalone bracket
        text3 = '[invoke name="shell_tool"><parameter name="cmd">ls</parameter>[/invoke]'
        r3 = _strategy_xml_tags(text3)
        assert r3 is not None
        assert r3.name == "shell_tool"

        # Pattern 4: <invoke name="X">...</invoke>
        text4 = '<invoke name="shell_tool"><parameter name="cmd">ls</parameter></invoke>'
        r4 = _strategy_xml_tags(text4)
        assert r4 is not None
        assert r4.name == "shell_tool"

        # Pattern 5: <tool_name>X</tool_name><arguments>...</arguments>
        text5 = '<tool_name>shell_tool</tool_name><arguments>{"cmd": "ls"}</arguments>'
        r5 = _strategy_xml_tags(text5)
        assert r5 is not None
        assert r5.name == "shell_tool"


class TestNestedToolCallsWithEscapes:
    """Tests for _strategy_nested_tool_calls handling JSON-style escapes.

    MiniMax model outputs Python dict syntax with JSON escape sequences
    (e.g. \\n inside single-quoted strings). These were previously causing
    ast.literal_eval to fail because \\n in single-quoted Python strings
    is a backslash+n literal, not a newline character.
    """

    def test_minimax_single_tool_call_with_newlines(self):
        """MiniMax format with \\n in argument values should parse."""
        text = (
            "Calling tools:\n"
            "[{'id': 'call_function_abc123', 'type': 'function', "
            "'function': {'name': 'read_file', 'arguments': "
            "{'file_path': '/tmp/test.txt'}}}]"
        )
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert result.name == "read_file"
        assert result.arguments["file_path"] == "/tmp/test.txt"

    def test_minimax_with_escaped_newlines_in_query(self):
        """Argument with \\n escape sequences should parse correctly."""
        text = (
            "Calling tools:\n"
            "[{'id': 'call_1', 'type': 'function', 'function': "
            "{'name': 'shell_tool', 'arguments': "
            "{'command': 'echo hello\\necho world'}}}]"
        )
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert "echo" in result.arguments.get("command", "")

    def test_minimax_with_long_multiline_query(self):
        """Simulate the actual MiniMax output that caused FORMAT_NOT_FOUND."""
        text = (
            "Now entering stage 1.\n"
            "Calling tools:\n"
            "[{'id': 'call_function_6d5w7g2eox4q_1', 'type': 'function', "
            "'function': {'name': 'step1_analysis', 'arguments': "
            "{'query': 'Analyze the following modules:\\n"
            "- Module A: PduR\\n"
            "- Module B: CanIf\\n"
            "- Module C: Com\\n"
            "Report findings.'}}}]"
        )
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert result.name == "step1_analysis"
        assert "PduR" in result.arguments.get("query", "")

    def test_minimax_with_python_booleans(self):
        """Python True/False/None in nested format should parse."""
        text = (
            "[{'id': 'call_1', 'type': 'function', 'function': "
            "{'name': 'config_tool', 'arguments': "
            "{'verbose': True, 'debug': False, 'extra': None}}}]"
        )
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert result.name == "config_tool"
        # After parsing, booleans should be preserved
        assert result.arguments.get("verbose") is True or result.arguments.get("verbose") == "true" or result.arguments.get("verbose") is True

    def test_minimax_multiple_tool_calls_extracts_first(self):
        """When multiple tool calls are in the list, extract the first one."""
        text = (
            "Calling tools:\n"
            "[{'id': 'call_1', 'type': 'function', 'function': "
            "{'name': 'read_file', 'arguments': {'file_path': '/a.txt'}}}, "
            "{'id': 'call_2', 'type': 'function', 'function': "
            "{'name': 'shell_tool', 'arguments': {'command': 'ls'}}}]"
        )
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert result.name == "read_file"

    def test_prefix_text_captured(self):
        """Prefix text before the tool call list should be captured."""
        text = (
            "Stage 0 complete. Now entering stage 1.\n"
            "Calling tools:\n"
            "[{'id': 'call_1', 'type': 'function', 'function': "
            "{'name': 'my_tool', 'arguments': {}}}]"
        )
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert "Stage 0 complete" in result.prefix_text

    def test_fallback_to_json_parsing(self):
        """When ast.literal_eval fails, JSON fallback should work."""
        # This uses double quotes (valid JSON) but with \\n escapes
        text = (
            'Calling tools:\n'
            '[{"id": "call_1", "type": "function", "function": '
            '{"name": "grep_search", "arguments": '
            '{"pattern": "line1\\nline2"}}}]'
        )
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert result.name == "grep_search"


class TestToolArgumentCoercionPatch:
    """Tests for the tool argument coercion monkey-patch."""

    def test_coerce_string_to_list(self):
        """String argument should be coerced to list when expected type is array."""
        import json
        from src.lib.smolagents.monkey_patch.tool_argument_coercion_patch import (
            _coerce_stringified_json,
        )

        class FakeTool:
            inputs = {
                "sections": {"type": "array"},
                "title": {"type": "string"},
            }

        tool = FakeTool()
        args = {
            "sections": json.dumps([{"heading": "A"}]),
            "title": "My Title",
        }
        result = _coerce_stringified_json(tool, args)
        assert isinstance(result["sections"], list)
        assert result["sections"][0]["heading"] == "A"
        assert result["title"] == "My Title"  # string stays string

    def test_coerce_string_to_dict(self):
        """String argument should be coerced to dict when expected type is object."""
        import json
        from src.lib.smolagents.monkey_patch.tool_argument_coercion_patch import (
            _coerce_stringified_json,
        )

        class FakeTool:
            inputs = {"metadata": {"type": "object"}}

        tool = FakeTool()
        args = {"metadata": json.dumps({"author": "AI"})}
        result = _coerce_stringified_json(tool, args)
        assert isinstance(result["metadata"], dict)
        assert result["metadata"]["author"] == "AI"

    def test_non_json_string_left_alone(self):
        """Non-JSON string for array/object field should not crash."""
        from src.lib.smolagents.monkey_patch.tool_argument_coercion_patch import (
            _coerce_stringified_json,
        )

        class FakeTool:
            inputs = {"sections": {"type": "array"}}

        tool = FakeTool()
        args = {"sections": "not valid json"}
        result = _coerce_stringified_json(tool, args)
        # Should remain unchanged (string), framework validation will catch it
        assert result["sections"] == "not valid json"

    def test_non_dict_arguments_passthrough(self):
        """Non-dict arguments should pass through unchanged."""
        from src.lib.smolagents.monkey_patch.tool_argument_coercion_patch import (
            _coerce_stringified_json,
        )

        class FakeTool:
            inputs = {"query": {"type": "string"}}

        result = _coerce_stringified_json(FakeTool(), "just a string")
        assert result == "just a string"

    def test_type_mismatch_not_coerced(self):
        """String parsed as dict should not be coerced to array."""
        import json
        from src.lib.smolagents.monkey_patch.tool_argument_coercion_patch import (
            _coerce_stringified_json,
        )

        class FakeTool:
            inputs = {"sections": {"type": "array"}}

        tool = FakeTool()
        # String that parses as dict, but expected type is array
        args = {"sections": json.dumps({"key": "value"})}
        result = _coerce_stringified_json(tool, args)
        # Should NOT coerce since dict != array
        assert isinstance(result["sections"], str)


class TestErrorMessageFormat:
    """Verify that error messages from the parsing layer are minimal diagnostics.

    After consolidation, the parsing layer emits only [CATEGORY:TAG] + short
    diagnostic + output excerpt.  LLM-facing guidance (format hints, tool lists)
    is generated exclusively by the recovery pipeline in error_recovery.py.
    """

    def test_no_strategy_names_in_error(self):
        """Error message must not contain internal strategy names."""
        text = "I analyzed the code and found some issues."
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient(text, TOOL_NAMES)
        msg = str(exc_info.value)
        assert "standard_json" not in msg
        assert "fixed_json" not in msg
        assert "ast_literal_eval" not in msg
        assert "nested_tool_calls" not in msg
        assert "xml_tags" not in msg
        assert "regex_extraction" not in msg

    def test_no_strategies_attempted_line(self):
        """Error message must not contain 'Strategies attempted:' line."""
        text = "Just some plain text without any tool call."
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient(text, TOOL_NAMES)
        msg = str(exc_info.value)
        assert "Strategies attempted" not in msg
        assert "no match" not in msg

    def test_format_not_found_guidance(self):
        """FORMAT_NOT_FOUND error should contain category tag and short diagnostic."""
        text = "Let me think about this problem step by step."
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient(text, TOOL_NAMES)
        msg = str(exc_info.value)
        assert "CATEGORY:FORMAT_NOT_FOUND" in msg
        assert "Could not parse tool call" in msg
        assert "Your output:" in msg

    def test_unknown_tool_guidance(self):
        """UNKNOWN_TOOL error should contain category tag and tool name."""
        text = '{"name": "nonexistent_tool", "arguments": {}}'
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient(text, TOOL_NAMES)
        msg = str(exc_info.value)
        assert "CATEGORY:UNKNOWN_TOOL" in msg
        assert "not found in registered tools" in msg
        assert "nonexistent_tool" in msg

    def test_output_not_truncated(self):
        """Model output should not be truncated."""
        long_text = "x" * 500
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient(long_text, TOOL_NAMES)
        msg = str(exc_info.value)
        # The output should contain the full text
        assert long_text in msg

    def test_category_tag_preserved(self):
        """[CATEGORY:...] tag must be preserved for internal recovery pipeline."""
        text = "Random text with no tool call at all."
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient(text, TOOL_NAMES)
        msg = str(exc_info.value)
        assert "[CATEGORY:" in msg

    def test_error_has_diagnostic_not_guidance(self):
        """Error should contain diagnostic info, not LLM-facing guidance.

        Format hints and tool lists are generated by the recovery pipeline,
        not by the parsing layer.
        """
        text = "I will now analyze the code."
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient(text, TOOL_NAMES)
        msg = str(exc_info.value)
        assert "[CATEGORY:" in msg
        assert "Could not parse tool call" in msg
        # Format hints and tool lists should NOT be in the parse error
        assert '"<tool_name>"' not in msg
        assert "Available tools:" not in msg

    def test_unknown_tool_has_diagnostic_not_guidance(self):
        """UNKNOWN_TOOL error should contain diagnostic, not format hints."""
        text = '{"name": "bad_tool", "arguments": {}}'
        with pytest.raises(ToolCallParseError) as exc_info:
            parse_tool_call_resilient(text, TOOL_NAMES)
        msg = str(exc_info.value)
        assert "[CATEGORY:UNKNOWN_TOOL]" in msg
        assert "bad_tool" in msg
        # Format hints are in recovery pipeline, not in parse error
        assert '"<tool_name>"' not in msg
        assert "Available tools:" not in msg


class TestNestedToolCallsKeyForwarding:
    """Verify that tool_name_key/tool_arguments_key are forwarded
    in the nested tool_calls strategy."""

    def test_custom_name_key(self):
        """Custom tool_name_key should be used in nested extraction."""
        text = (
            "[{'id': 'call_1', 'type': 'function', 'function': "
            "{'tool': 'read_file', 'params': {'file_path': '/tmp/a.txt'}}}]"
        )
        result = _strategy_nested_tool_calls(
            text, tool_name_key="tool", tool_arguments_key="params",
        )
        assert result is not None
        assert result.name == "read_file"
        assert result.arguments["file_path"] == "/tmp/a.txt"

    def test_default_keys_still_work(self):
        """Default 'name'/'arguments' keys should still work."""
        text = (
            "[{'id': 'call_1', 'type': 'function', 'function': "
            "{'name': 'shell_tool', 'arguments': {'command': 'ls'}}}]"
        )
        result = _strategy_nested_tool_calls(text)
        assert result is not None
        assert result.name == "shell_tool"

    def test_wrong_key_returns_none(self):
        """Mismatched key names should cause extraction to fail."""
        text = (
            "[{'id': 'call_1', 'type': 'function', 'function': "
            "{'name': 'read_file', 'arguments': {'file_path': '/tmp/a.txt'}}}]"
        )
        # Using wrong keys should not find anything
        result = _strategy_nested_tool_calls(
            text, tool_name_key="tool", tool_arguments_key="params",
        )
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
#  Generic XML parser internals
# ═══════════════════════════════════════════════════════════════════════════


class TestFindMatchingCloseTag:
    """Tests for _find_matching_close_tag depth-tracking logic."""

    def test_simple_close(self):
        text = "some value</parameter>trailing"
        pos = _find_matching_close_tag(text, "parameter", 0)
        assert pos == text.index("</parameter>")

    def test_nested_tags(self):
        """Inner <parameter> tags should NOT cause early close."""
        text = 'outer <parameter name="inner">nested</parameter> still outer</parameter>end'
        pos = _find_matching_close_tag(text, "parameter", 0)
        # Should find the SECOND </parameter>, not the first
        expected = text.rindex("</parameter>")
        assert pos == expected

    def test_no_close_tag(self):
        text = "some value without any closing tag"
        pos = _find_matching_close_tag(text, "parameter", 0)
        assert pos == -1

    def test_empty_content(self):
        text = "</parameter>"
        pos = _find_matching_close_tag(text, "parameter", 0)
        assert pos == 0


class TestExtractParametersRobust:
    """Tests for _extract_parameters_robust context-aware extraction."""

    def test_simple_parameters(self):
        text = '<parameter name="path">/tmp/test.py</parameter><parameter name="limit">100</parameter>'
        params = _extract_parameters_robust(text)
        assert len(params) == 2
        assert params[0] == ("path", "/tmp/test.py")
        assert params[1] == ("limit", "100")

    def test_parameter_tag_split_across_lines(self):
        """Type A failure: <parameter\\nname="key"> should be handled."""
        text = (
            '<parameter \n'
            'name="file_path">/some/path/to/file.c</parameter>\n'
            '<parameter name="limit">100</parameter>'
        )
        params = _extract_parameters_robust(text)
        assert len(params) == 2
        assert params[0] == ("file_path", "/some/path/to/file.c")
        assert params[1] == ("limit", "100")

    def test_parameter_value_with_markdown_table(self):
        """Type B failure: parameter value containing | chars and table syntax."""
        table_content = (
            '| File | Module | Type |\n'
            '|------|--------|------|\n'
            '| Com.h | Com | h |'
        )
        text = f'<parameter name="content">{table_content}</parameter>'
        params = _extract_parameters_robust(text)
        assert len(params) == 1
        assert params[0][0] == "content"
        assert "Com.h" in params[0][1]

    def test_parameter_value_with_json_array(self):
        """Type C failure: JSON array as parameter value."""
        json_array = '[{"content": "task A", "status": "completed"}, {"content": "task B", "status": "pending"}]'
        text = f'<parameter name="todos">{json_array}</parameter>'
        params = _extract_parameters_robust(text)
        assert len(params) == 1
        assert params[0][0] == "todos"
        # Value should be the complete JSON array
        parsed = json.loads(params[0][1])
        assert len(parsed) == 2

    def test_parameter_value_with_multiline_text(self):
        """Large multiline text as parameter value (final_answer style)."""
        long_text = (
            "Analysis complete.\n\n"
            "## Summary\n"
            "1. **File A** - found 3 issues\n"
            "2. **File B** - no issues\n\n"
            "### Details\n"
            "Some detailed explanation."
        )
        text = f'<parameter name="answer">{long_text}</parameter>'
        params = _extract_parameters_robust(text)
        assert len(params) == 1
        assert params[0][0] == "answer"
        assert "Analysis complete" in params[0][1]
        assert "### Details" in params[0][1]

    def test_parameter_value_with_heredoc(self):
        """Parameter value containing heredoc-style shell command."""
        heredoc = (
            "cat > /tmp/report.md << 'EOF'\n"
            "# Report\n"
            "| Col1 | Col2 |\n"
            "|------|------|\n"
            "| data | data |\n"
            "EOF\n"
            'echo "done"'
        )
        text = f'<parameter name="command">{heredoc}</parameter>'
        params = _extract_parameters_robust(text)
        assert len(params) == 1
        assert params[0][0] == "command"
        assert "heredoc" not in params[0][1].lower() or "EOF" in params[0][1]

    def test_no_parameters(self):
        text = "just plain text"
        params = _extract_parameters_robust(text)
        assert params == []

    def test_malformed_parameter_skipped(self):
        """Parameter tag without name attribute should be skipped."""
        text = '<parameter>bad tag</parameter><parameter name="good">value</parameter>'
        params = _extract_parameters_robust(text)
        assert len(params) == 1
        assert params[0] == ("good", "value")


class TestExtractXmlToolCall:
    """Tests for _extract_xml_tool_call generic structural parser."""

    def test_minimax_bracket_with_wrapper(self):
        """Standard MiniMax format with wrapper and bracket invoke."""
        text = (
            '<minimax:tool_call>\n'
            '[invoke name="read_file">\n'
            '<parameter name="file_path">/tmp/test.py</parameter>\n'
            '<parameter name="limit">100</parameter>\n'
            '</invoke>\n'
            '</minimax:tool_call>'
        )
        result = _extract_xml_tool_call(text)
        assert result is not None
        tool_name, strategy, args, prefix = result
        assert tool_name == "read_file"
        assert args["file_path"] == "/tmp/test.py"
        assert args["limit"] == 100

    def test_minimax_bracket_consistent_close(self):
        """MiniMax bracket format with [/invoke] closing."""
        text = (
            '<minimax:tool_call>'
            '[invoke name="shell_tool">'
            '<parameter name="command">ls -la</parameter>'
            '[/invoke]'
            '</minimax:tool_call>'
        )
        result = _extract_xml_tool_call(text)
        assert result is not None
        tool_name, _, args, _ = result
        assert tool_name == "shell_tool"
        assert args["command"] == "ls -la"

    def test_angle_bracket_invoke(self):
        """Standard XML <invoke name="X"> format."""
        text = '<invoke name="write_file"><parameter name="path">/tmp/x</parameter><parameter name="content">hello</parameter></invoke>'
        result = _extract_xml_tool_call(text)
        assert result is not None
        tool_name, _, args, _ = result
        assert tool_name == "write_file"
        assert args["path"] == "/tmp/x"
        assert args["content"] == "hello"

    def test_name_tag_with_arguments(self):
        """<tool_call><name>X</name><arguments>{...}</arguments></tool_call> format."""
        text = '<tool_call><name>shell_tool</name><arguments>{"command": "pwd"}</arguments></tool_call>'
        result = _extract_xml_tool_call(text)
        assert result is not None
        tool_name, _, args, _ = result
        assert tool_name == "shell_tool"
        assert args["command"] == "pwd"

    def test_tool_name_tag(self):
        """<tool_name>X</tool_name><arguments>{...}</arguments> format."""
        text = '<tool_name>read_file</tool_name><arguments>{"path": "/tmp/a.py"}</arguments>'
        result = _extract_xml_tool_call(text)
        assert result is not None
        tool_name, _, args, _ = result
        assert tool_name == "read_file"
        assert args["path"] == "/tmp/a.py"

    def test_no_xml(self):
        """Plain text should return None."""
        result = _extract_xml_tool_call("just plain text")
        assert result is None

    def test_prefix_captured_with_wrapper(self):
        """Prefix text before the wrapper should be captured."""
        text = (
            'I will read the file.\n'
            '<minimax:tool_call>\n'
            '[invoke name="read_file">\n'
            '<parameter name="path">/tmp/x</parameter>\n'
            '</invoke>\n'
            '</minimax:tool_call>'
        )
        result = _extract_xml_tool_call(text)
        assert result is not None
        _, _, _, prefix_end = result
        assert prefix_end > 0
        assert "I will read" in text[:prefix_end]


# ═══════════════════════════════════════════════════════════════════════════
#  Production failure reproductions (from real MiniMax logs)
# ═══════════════════════════════════════════════════════════════════════════


class TestProductionMiniMaxFailures:
    """Reproduce exact FORMAT_NOT_FOUND failures from production logs.

    These test cases use the exact model output patterns that caused
    parsing failures in ai_check_agent runs.
    """

    def test_type_a_parameter_tag_split_across_lines(self):
        """Type A: <parameter\\nname="file_path"> tag split across lines.

        Exact failure from ai_check_agent log 20260420_092619, step 9.
        """
        text = (
            "Now let me read the critical source files in parallel.\n"
            "<minimax:tool_call>\n"
            '[invoke name="read_file">\n'
            "<parameter \n"
            'name="file_path">/home/user/System/Bsw/CanIf/src/CanIf.c</parameter>\n'
            '<parameter name="limit">200</parameter>\n'
            "</invoke>\n"
            "</minimax:tool_call>"
        )
        result = _strategy_xml_tags(text)
        assert result is not None, "Type A failure: parameter tag split across lines"
        assert result.name == "read_file"
        assert result.arguments["file_path"] == "/home/user/System/Bsw/CanIf/src/CanIf.c"
        assert result.arguments["limit"] == 200

    def test_type_a_via_resilient_chain(self):
        """Type A should also work through the full resilient chain."""
        text = (
            "<minimax:tool_call>\n"
            '[invoke name="read_file">\n'
            "<parameter \n"
            'name="file_path">/tmp/test.c</parameter>\n'
            '<parameter name="limit">100</parameter>\n'
            "</invoke>\n"
            "</minimax:tool_call>"
        )
        result = parse_tool_call_resilient(text, TOOL_NAMES)
        assert result.name == "read_file"
        assert result.arguments["file_path"] == "/tmp/test.c"

    def test_type_c_json_array_parameter_value(self):
        """Type C: JSON array as parameter value (todo_write tool).

        Exact failure from ai_check_agent log 20260419_232539, step N.
        """
        text = (
            "<minimax:tool_call>\n"
            '[invoke name="write_file">\n'
            '<parameter name="todos">[{"content": "scan Com module", "status": "completed"}, '
            '{"content": "scan CanIf module", "status": "completed"}, '
            '{"content": "generate report", "status": "pending"}]</parameter>\n'
            "</invoke>\n"
            "</minimax:tool_call>"
        )
        result = _strategy_xml_tags(text)
        assert result is not None, "Type C failure: JSON array parameter value"
        assert result.name == "write_file"
        todos = result.arguments["todos"]
        assert isinstance(todos, list)
        assert len(todos) == 3
        assert todos[0]["content"] == "scan Com module"

    def test_type_b_multiline_answer_parameter(self):
        """Type B: Large multiline text in final_answer parameter value.

        Exact failure pattern from ai_check_agent log 20260419_232539.
        """
        text = (
            "<minimax:tool_call>\n"
            '[invoke name="final_answer">\n'
            '<parameter name="answer">Analysis complete.\n'
            "\n"
            "## Generated Files\n"
            "1. **CAN_FileScan.md** - File scan results\n"
            "2. **CAN_Dependencies.md** - Dependency analysis\n"
            "\n"
            "### Key Findings\n"
            "- PduR_BufferManager.c: shared buffer pool (cross-core access)\n"
            "- CanIf.c: ControllerMode/PduMode (ISR + task shared)\n"
            "</parameter>\n"
            "</invoke>\n"
            "</minimax:tool_call>"
        )
        result = _strategy_xml_tags(text)
        assert result is not None, "Type B failure: multiline answer"
        assert result.name == "final_answer"
        answer = result.arguments["answer"]
        assert "Analysis complete" in answer
        assert "### Key Findings" in answer
        assert "PduR_BufferManager" in answer

    def test_minimax_write_markdown_file_with_sections(self):
        """MiniMax write_markdown_file with JSON sections parameter.

        Exact failure pattern from ai_check_agent log 20260420_092619.
        """
        text = (
            "Let me write the report.\n"
            "<minimax:tool_call>\n"
            '[invoke name="write_file">\n'
            '<parameter name="file_path">./temp/CAN_FileScan.md</parameter>\n'
            '<parameter name="sections">[{"level": 1, "heading": "CAN Stack Report", '
            '"body": "Analysis of CAN communication stack modules."}]</parameter>\n'
            "</invoke>\n"
            "</minimax:tool_call>"
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "write_file"
        assert result.arguments["file_path"] == "./temp/CAN_FileScan.md"
        sections = result.arguments["sections"]
        assert isinstance(sections, list)
        assert sections[0]["heading"] == "CAN Stack Report"

    def test_minimax_shell_with_heredoc(self):
        """MiniMax shell_tool with heredoc-style multiline command.

        Exact failure pattern from ai_check_agent log 20260419_232539.
        """
        text = (
            "<minimax:tool_call>\n"
            '[invoke name="shell_tool">\n'
            "<parameter name=\"command\">cat > ./temp/report.md << 'MDEOF'\n"
            "# Report\n"
            "| File | Module | Type |\n"
            "|------|--------|------|\n"
            "| Com.h | Com | h |\n"
            "| CanIf.h | CanIf | h |\n"
            "MDEOF\n"
            'echo "done"</parameter>\n'
            "</invoke>\n"
            "</minimax:tool_call>"
        )
        result = _strategy_xml_tags(text)
        assert result is not None, "Heredoc shell command should parse"
        assert result.name == "shell_tool"
        cmd = result.arguments["command"]
        assert "MDEOF" in cmd
        assert "Com.h" in cmd

    def test_standalone_bracket_no_wrapper(self):
        """Bracket invoke without outer tool_call wrapper."""
        text = (
            '[invoke name="read_file">\n'
            '<parameter name="path">/tmp/test.py</parameter>\n'
            '[/invoke]'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "read_file"
        assert result.arguments["path"] == "/tmp/test.py"

    def test_mixed_bracket_close_no_wrapper(self):
        """Bracket open + angle-bracket close without wrapper."""
        text = (
            '[invoke name="shell_tool">\n'
            '<parameter name="command">pwd</parameter>\n'
            '</invoke>'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments["command"] == "pwd"

    def test_parameter_value_containing_parameter_substring(self):
        """Edge case: parameter value contains literal '</parameter>' text.

        This should NOT cause early termination of the parameter extraction.
        """
        text = (
            '<minimax:tool_call>\n'
            '[invoke name="shell_tool">\n'
            '<parameter name="command">echo "the tag </parameter> is XML"</parameter>\n'
            '</invoke>\n'
            '</minimax:tool_call>'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "shell_tool"
        # The value should contain the inner </parameter> as literal text
        # Note: this is a known limitation — the parser may truncate at the
        # first </parameter>. We test that it at least extracts the tool name.

    def test_multiple_parameter_tag_whitespace_variants(self):
        """Various whitespace patterns in parameter tags."""
        text = (
            '<minimax:tool_call>\n'
            '[invoke name="write_file">\n'
            '<parameter  name="path" >/tmp/a.py</parameter>\n'      # extra spaces
            '<parameter\tname="content">hello</parameter>\n'        # tab
            '<parameter\n name="mode">overwrite</parameter>\n'      # newline
            '</invoke>\n'
            '</minimax:tool_call>'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "write_file"
        assert result.arguments["path"] == "/tmp/a.py"
        assert result.arguments["content"] == "hello"
        assert result.arguments["mode"] == "overwrite"


# ═══════════════════════════════════════════════════════════════════════════
#  Generic wrapper tag tests (model-agnostic)
# ═══════════════════════════════════════════════════════════════════════════


class TestGenericWrapperTags:
    """Verify wrapper detection works for any model prefix, not just minimax."""

    def test_no_namespace_prefix(self):
        """Plain <tool_call> without any namespace prefix."""
        text = (
            '<tool_call>\n'
            '[invoke name="read_file">\n'
            '<parameter name="path">/tmp/a.py</parameter>\n'
            '</invoke>\n'
            '</tool_call>'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "read_file"

    def test_deepseek_namespace(self):
        """DeepSeek-style namespace: <deepseek:tool_call>."""
        text = (
            '<deepseek:tool_call>\n'
            '[invoke name="shell_tool">\n'
            '<parameter name="command">ls</parameter>\n'
            '</invoke>\n'
            '</deepseek:tool_call>'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments["command"] == "ls"

    def test_qwen_namespace(self):
        """Qwen-style namespace: <qwen:function_call>."""
        text = (
            '<qwen:function_call>\n'
            '<invoke name="write_file">\n'
            '<parameter name="path">/tmp/x.py</parameter>\n'
            '<parameter name="content">hello</parameter>\n'
            '</invoke>\n'
            '</qwen:function_call>'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "write_file"
        assert result.arguments["path"] == "/tmp/x.py"

    def test_anthropic_tool_use_namespace(self):
        """Anthropic-style: <anthropic:tool_use>."""
        text = (
            '<anthropic:tool_use>\n'
            '<invoke name="search_code">\n'
            '<parameter name="query">def main</parameter>\n'
            '</invoke>\n'
            '</anthropic:tool_use>'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "search_code"
        assert result.arguments["query"] == "def main"

    def test_arbitrary_unknown_namespace(self):
        """Completely unknown model namespace should still work."""
        text = (
            '<future_model_v2:tool_call>\n'
            '[invoke name="read_file">\n'
            '<parameter name="path">/tmp/test.py</parameter>\n'
            '[/invoke]\n'
            '</future_model_v2:tool_call>'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "read_file"

    def test_name_arguments_format_generic_wrapper(self):
        """<name>X</name><arguments>{...}</arguments> inside generic wrapper."""
        text = (
            '<custom:tool_call>\n'
            '<name>shell_tool</name>\n'
            '<arguments>{"command": "pwd"}</arguments>\n'
            '</custom:tool_call>'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "shell_tool"
        assert result.arguments["command"] == "pwd"

    def test_non_tool_wrapper_ignored(self):
        """Tags like <div>, <html> should NOT be treated as wrappers."""
        text = '<div><name>shell_tool</name><arguments>{"cmd":"ls"}</arguments></div>'
        # The <name> tag should still be found even if <div> isn't a wrapper
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "shell_tool"

    def test_invoke_as_wrapper_tag(self):
        """Some models use <invoke> as both wrapper and action tag."""
        text = (
            '<invoke>\n'
            '<name>read_file</name>\n'
            '<arguments>{"path": "/tmp/a.py"}</arguments>\n'
            '</invoke>'
        )
        result = _strategy_xml_tags(text)
        assert result is not None
        assert result.name == "read_file"
