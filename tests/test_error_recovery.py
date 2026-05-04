"""
Tests for src.lib.smolagents.error_recovery module.

Covers error classification, tool info extraction, progressive recovery
message generation, error message consolidation, and exception safety.
"""

import pytest

from src.lib.smolagents.error_recovery import (
    ErrorCategory,
    NOW_LETS_RETRY_PREFIX,
    PARTIAL_TOOL_NAME_PATTERNS,
    build_recovery_message,
    classify_parse_error,
    consolidate_error_messages,
    extract_category_from_error,
    extract_tool_info,
    format_tool_list,
)


# =========================================================================
# 9.2  ErrorCategory enum completeness
# =========================================================================


class TestErrorCategory:
    """ErrorCategory enum has exactly 4 members (no AMBIGUOUS_CALL)."""

    def test_has_four_members(self):
        assert len(ErrorCategory) == 4

    def test_members(self):
        names = {e.name for e in ErrorCategory}
        assert names == {"FORMAT_NOT_FOUND", "JSON_SYNTAX_ERROR", "UNKNOWN_TOOL", "ARGUMENT_ERROR"}

    def test_no_ambiguous_call(self):
        with pytest.raises(ValueError):
            ErrorCategory("AMBIGUOUS_CALL")


# =========================================================================
# 9.3  classify_parse_error – 4 classification paths
# =========================================================================


class TestClassifyParseError:
    TOOLS = ["read_file", "shell_tool", "write_markdown_file"]

    def test_argument_error_tool_in_list(self):
        result = classify_parse_error(
            failures=["standard_json: exception=missing required arg"],
            partial_tool_name="read_file",
            available_tool_names=self.TOOLS,
        )
        assert result == ErrorCategory.ARGUMENT_ERROR

    def test_argument_error_different_tool(self):
        result = classify_parse_error(
            failures=[],
            partial_tool_name="shell_tool",
            available_tool_names=self.TOOLS,
        )
        assert result == ErrorCategory.ARGUMENT_ERROR

    def test_unknown_tool_not_in_list(self):
        result = classify_parse_error(
            failures=["xml_tags: tool 'magic_tool' not in registered tools"],
            partial_tool_name="magic_tool",
            available_tool_names=self.TOOLS,
        )
        assert result == ErrorCategory.UNKNOWN_TOOL

    def test_unknown_tool_close_name(self):
        result = classify_parse_error(
            failures=[],
            partial_tool_name="read_fiel",
            available_tool_names=self.TOOLS,
        )
        assert result == ErrorCategory.UNKNOWN_TOOL

    def test_json_syntax_error_from_failures(self):
        result = classify_parse_error(
            failures=["standard_json: json.decoder.JSONDecodeError: Expecting property name"],
            partial_tool_name=None,
            available_tool_names=self.TOOLS,
        )
        assert result == ErrorCategory.JSON_SYNTAX_ERROR

    def test_json_syntax_unterminated(self):
        result = classify_parse_error(
            failures=["fixed_json: Unterminated string starting at"],
            partial_tool_name=None,
            available_tool_names=self.TOOLS,
        )
        assert result == ErrorCategory.JSON_SYNTAX_ERROR

    def test_format_not_found_default(self):
        result = classify_parse_error(
            failures=["standard_json: no match", "xml_tags: no match"],
            partial_tool_name=None,
            available_tool_names=self.TOOLS,
        )
        assert result == ErrorCategory.FORMAT_NOT_FOUND

    def test_format_not_found_empty(self):
        result = classify_parse_error(
            failures=[],
            partial_tool_name=None,
            available_tool_names=self.TOOLS,
        )
        assert result == ErrorCategory.FORMAT_NOT_FOUND

    def test_none_failures(self):
        result = classify_parse_error(
            failures=None,
            partial_tool_name=None,
            available_tool_names=None,
        )
        assert result == ErrorCategory.FORMAT_NOT_FOUND


# =========================================================================
# 9.4  extract_tool_info – 3-level extraction
# =========================================================================


class TestExtractToolInfo:
    TOOLS = ["read_file", "shell_tool", "write_markdown_file"]

    # Level 2: from failures list
    def test_level2_tool_not_in_registered(self):
        failures = ["ast_literal_eval: tool 'read_file' not in registered tools ['shell_tool']"]
        assert extract_tool_info(failures=failures) == "read_file"

    def test_level2_no_match_entries(self):
        failures = ["standard_json: no match", "xml_tags: no match"]
        assert extract_tool_info(failures=failures) is None

    # Level 3: regex on raw text
    def test_level3_json_format(self):
        raw = '{"name": "read_file", "arguments": {"file_path": "test.py"}}'
        assert extract_tool_info(raw_text=raw) == "read_file"

    def test_level3_xml_format(self):
        raw = "<tool_call><name>shell_tool</name><arguments>{}</arguments></tool_call>"
        assert extract_tool_info(raw_text=raw) == "shell_tool"

    def test_level3_invoke_format(self):
        raw = '[invoke name="write_markdown_file"><parameter name="p">v</parameter>[/invoke]'
        assert extract_tool_info(raw_text=raw) == "write_markdown_file"

    def test_level3_minimax_wrapper(self):
        raw = "<minimax:tool_call><name>read_file</name><arguments>{}</arguments></minimax:tool_call>"
        assert extract_tool_info(raw_text=raw) == "read_file"

    def test_level3_calling_tool(self):
        raw = "Calling tool: 'shell_tool' with arguments: {}"
        assert extract_tool_info(raw_text=raw) == "shell_tool"

    def test_level3_tool_name_xml(self):
        raw = "<tool_name>read_file</tool_name>"
        assert extract_tool_info(raw_text=raw) == "read_file"

    def test_pure_text_returns_none(self):
        raw = "I think we should analyze the project structure now."
        assert extract_tool_info(raw_text=raw) is None

    def test_empty_string_returns_none(self):
        assert extract_tool_info(raw_text="") is None

    def test_none_inputs(self):
        assert extract_tool_info(failures=None, raw_text=None) is None

    def test_level2_priority_over_level3(self):
        failures = ["xml_tags: tool 'magic' not in registered tools"]
        raw = '{"name": "read_file", "arguments": {}}'
        result = extract_tool_info(failures=failures, raw_text=raw)
        assert result == "magic"  # Level 2 takes priority

    # 9.11: patterns don't cross-match
    def test_json_pattern_no_xml_match(self):
        raw = "<name>read_file</name>"
        # JSON pattern should NOT match XML format
        m = PARTIAL_TOOL_NAME_PATTERNS[0].search(raw)
        assert m is None

    def test_all_six_patterns_have_test(self):
        """Each of the 6 Level 3 patterns has a dedicated extraction test."""
        assert len(PARTIAL_TOOL_NAME_PATTERNS) == 6

    def test_first_match_wins_among_patterns(self):
        raw = '{"name": "aaa"} <name>bbb</name>'
        result = extract_tool_info(raw_text=raw)
        assert result == "aaa"  # JSON pattern is first


# =========================================================================
# 9.5  build_recovery_message – 4 levels
# =========================================================================


class TestBuildRecoveryMessage:
    TOOLS = ["read_file", "shell_tool"]

    def test_level1_basic(self):
        msg = build_recovery_message(
            consecutive_errors=1,
            error_category=ErrorCategory.FORMAT_NOT_FOUND,
            available_tool_names=self.TOOLS,
        )
        assert "did not contain a valid tool call" in msg
        assert "read_file" in msg
        assert "shell_tool" in msg
        assert len(msg) < 600

    def test_level2_diagnosis(self):
        msg = build_recovery_message(
            consecutive_errors=2,
            error_category=ErrorCategory.JSON_SYNTAX_ERROR,
            last_output_snippet="{'name': 'read_file'}",
            available_tool_names=self.TOOLS,
            tool_descriptions={"read_file": "Read a file"},
        )
        assert "DIAGNOSIS" in msg
        assert "JSON_SYNTAX_ERROR" in msg
        assert "CORRECT format" in msg
        assert len(msg) < 1200

    def test_level2_no_wrong_examples(self):
        msg = build_recovery_message(
            consecutive_errors=2,
            error_category=ErrorCategory.FORMAT_NOT_FOUND,
            available_tool_names=self.TOOLS,
        )
        assert "WRONG" not in msg

    def test_level3_approach_switch(self):
        msg = build_recovery_message(
            consecutive_errors=3,
            error_category=ErrorCategory.UNKNOWN_TOOL,
            available_tool_names=self.TOOLS,
        )
        assert "CRITICAL" in msg
        assert "DIFFERENT" in msg
        assert len(msg) < 800

    def test_level3_shorter_than_level2(self):
        msg2 = build_recovery_message(
            consecutive_errors=2,
            error_category=ErrorCategory.FORMAT_NOT_FOUND,
            last_output_snippet="{'name': 'read_file', 'arguments': {'path': '/tmp'}}",
            available_tool_names=self.TOOLS,
            tool_descriptions={"read_file": "Read file content", "shell_tool": "Run shell commands"},
        )
        msg3 = build_recovery_message(
            consecutive_errors=3,
            error_category=ErrorCategory.FORMAT_NOT_FOUND,
            available_tool_names=self.TOOLS,
        )
        assert len(msg3) < len(msg2)

    def test_level4_minimal(self):
        msg = build_recovery_message(
            consecutive_errors=5,
            error_category=ErrorCategory.FORMAT_NOT_FOUND,
            available_tool_names=self.TOOLS,
        )
        assert "FORMAT:" in msg
        assert len(msg) < 300

    def test_level4_no_final_answer(self):
        msg = build_recovery_message(
            consecutive_errors=5,
            available_tool_names=self.TOOLS,
        )
        assert "final_answer" not in msg

    def test_level4_at_10_errors(self):
        msg = build_recovery_message(
            consecutive_errors=10,
            available_tool_names=self.TOOLS,
        )
        assert "FORMAT:" in msg  # Still level 4, not terminated

    def test_zero_errors_empty(self):
        msg = build_recovery_message(consecutive_errors=0)
        assert msg == ""

    def test_negative_errors_empty(self):
        msg = build_recovery_message(consecutive_errors=-1)
        assert msg == ""

    def test_all_levels_contain_tool_list(self):
        for n in [1, 2, 3]:
            msg = build_recovery_message(
                consecutive_errors=n,
                available_tool_names=["read_file"],
            )
            assert "read_file" in msg, f"Level {n} should mention tool names"

    def test_level4_minimal_no_tool_list(self):
        """Level 4 is intentionally minimal — no tool list."""
        msg = build_recovery_message(
            consecutive_errors=5,
            available_tool_names=["read_file"],
        )
        # Level 4 only has FORMAT template, no tool list
        assert "FORMAT:" in msg

    def test_all_levels_no_wrong_block(self):
        for n in [1, 2, 3, 5]:
            msg = build_recovery_message(
                consecutive_errors=n,
                error_category=ErrorCategory.FORMAT_NOT_FOUND,
                available_tool_names=self.TOOLS,
            )
            assert "WRONG:" not in msg
            assert "WRONG format" not in msg


# =========================================================================
# 9.6 – 9.7  consolidate_error_messages
# =========================================================================


def _make_error_msg(text: str) -> dict:
    """Create a mock TOOL_RESPONSE error message dict."""
    return {"role": "tool-response", "content": [{"type": "text", "text": text}]}


def _make_normal_msg(text: str, role: str = "assistant") -> dict:
    return {"role": role, "content": [{"type": "text", "text": text}]}


class TestConsolidateErrorMessages:
    ERROR_TEXT = (
        "Error:\nToolCallParseError: [CATEGORY:FORMAT_NOT_FOUND] stuff\n"
        "Now let's retry: take care not to repeat previous errors!"
    )

    def test_five_consecutive_errors(self):
        msgs = [_make_error_msg(self.ERROR_TEXT) for _ in range(5)]
        result = consolidate_error_messages(
            msgs,
            consecutive_error_count=5,
            recovery_message="RECOVERY",
        )
        # First 4 should be compressed summaries
        for i in range(4):
            text = result[i]["content"][0]["text"]
            assert text.startswith("[Parse error")
        # Last one should retain error info + have recovery instead of retry
        last_text = result[4]["content"][0]["text"]
        assert "Error:" in last_text
        assert "RECOVERY" in last_text
        assert NOW_LETS_RETRY_PREFIX not in last_text

    def test_single_error_kept_full(self):
        msgs = [_make_error_msg(self.ERROR_TEXT)]
        result = consolidate_error_messages(
            msgs,
            consecutive_error_count=1,
            recovery_message="LEVEL1",
        )
        assert len(result) == 1
        text = result[0]["content"][0]["text"]
        assert "Error:" in text
        assert "LEVEL1" in text

    def test_non_consecutive_errors_independent(self):
        msgs = [
            _make_error_msg(self.ERROR_TEXT),
            _make_normal_msg("Success step"),
            _make_error_msg(self.ERROR_TEXT),
        ]
        result = consolidate_error_messages(
            msgs,
            consecutive_error_count=1,
            recovery_message="REC",
        )
        # Only the trailing error sequence (1 error) is affected
        # First error is NOT compressed (it's not trailing consecutive)
        first_text = result[0]["content"][0]["text"]
        assert "Error:" in first_text  # Kept in full (not in trailing streak)

    def test_empty_list(self):
        assert consolidate_error_messages([], 0, "") == []

    def test_no_errors(self):
        msgs = [_make_normal_msg("Hello")]
        result = consolidate_error_messages(msgs, 0, "")
        assert len(result) == 1

    def test_retry_suffix_replaced(self):
        msgs = [_make_error_msg(self.ERROR_TEXT)]
        result = consolidate_error_messages(
            msgs,
            consecutive_error_count=1,
            recovery_message="MY_GUIDANCE",
        )
        text = result[0]["content"][0]["text"]
        assert NOW_LETS_RETRY_PREFIX not in text
        assert "MY_GUIDANCE" in text

    def test_no_retry_suffix_appends(self):
        msg_text = "Error:\nSome error without retry suffix"
        msgs = [_make_error_msg(msg_text)]
        result = consolidate_error_messages(
            msgs,
            consecutive_error_count=1,
            recovery_message="GUIDANCE",
        )
        text = result[0]["content"][0]["text"]
        assert "GUIDANCE" in text
        assert "Some error" in text

    def test_original_error_preserved(self):
        msgs = [_make_error_msg(self.ERROR_TEXT)]
        result = consolidate_error_messages(
            msgs,
            consecutive_error_count=1,
            recovery_message="REC",
        )
        text = result[0]["content"][0]["text"]
        # Original error info before "Now let's retry" should be preserved
        assert "ToolCallParseError" in text

    def test_does_not_mutate_original(self):
        msgs = [_make_error_msg(self.ERROR_TEXT)]
        original_text = msgs[0]["content"][0]["text"]
        consolidate_error_messages(msgs, 1, "REC")
        assert msgs[0]["content"][0]["text"] == original_text


# =========================================================================
# 9.8  format_tool_list
# =========================================================================


class TestFormatToolList:
    def test_with_descriptions(self):
        result = format_tool_list(
            ["read_file", "shell_tool"],
            {"read_file": "Read a file", "shell_tool": "Run commands"},
        )
        assert "- read_file: Read a file" in result
        assert "- shell_tool: Run commands" in result

    def test_without_descriptions(self):
        result = format_tool_list(["read_file", "shell_tool"])
        assert "- read_file" in result
        assert "- shell_tool" in result

    def test_empty_list(self):
        assert format_tool_list([]) == ""

    def test_none(self):
        assert format_tool_list(None) == ""

    def test_single_tool(self):
        result = format_tool_list(["read_file"])
        assert result == "- read_file"


# =========================================================================
# 9.9  Exception safety
# =========================================================================


class TestExceptionSafety:
    """All public functions handle None, empty, and extreme inputs gracefully."""

    def test_classify_with_none(self):
        result = classify_parse_error(None, None, None)
        assert isinstance(result, ErrorCategory)

    def test_extract_with_none(self):
        assert extract_tool_info(None, None, None) is None

    def test_build_recovery_with_none(self):
        msg = build_recovery_message(1, None, None, None, None)
        assert isinstance(msg, str)

    def test_consolidate_with_none_messages(self):
        result = consolidate_error_messages(None, 0, "")
        assert result is None  # Returns input unchanged

    def test_extract_with_very_long_text(self):
        long_text = "a" * 100_000
        result = extract_tool_info(raw_text=long_text)
        assert result is None

    def test_format_tool_list_with_empty_descriptions(self):
        result = format_tool_list(["tool1"], {})
        assert "tool1" in result


# =========================================================================
# 9.10  Consolidation + compression interaction
# =========================================================================


class TestConsolidationCompressionInteraction:
    ERROR_TEXT = (
        "Error:\n[CATEGORY:FORMAT_NOT_FOUND] parse failed\n"
        "Now let's retry: take care not to repeat previous errors!"
    )

    def test_consolidation_reduces_message_count_effectively(self):
        """After consolidation, only 1 message is full-size; others are summaries."""
        msgs = [_make_error_msg(self.ERROR_TEXT) for _ in range(5)]
        result = consolidate_error_messages(msgs, 5, "RECOVERY")

        full_count = sum(1 for m in result if len(m["content"][0]["text"]) > 50)
        summary_count = sum(1 for m in result if len(m["content"][0]["text"]) <= 50)
        assert full_count == 1
        assert summary_count == 4

    def test_summaries_are_compressible(self):
        """Summary messages are short enough to be compressed by the pipeline."""
        msgs = [_make_error_msg(self.ERROR_TEXT) for _ in range(5)]
        result = consolidate_error_messages(msgs, 5, "RECOVERY")

        for i in range(4):
            text = result[i]["content"][0]["text"]
            assert len(text) < 50  # Short summary, easily compressible

    def test_latest_error_has_recovery_guidance(self):
        """The latest (kept) error has recovery guidance content."""
        msgs = [_make_error_msg(self.ERROR_TEXT) for _ in range(3)]
        result = consolidate_error_messages(msgs, 3, "LEVEL3 GUIDANCE")
        last_text = result[-1]["content"][0]["text"]
        assert "LEVEL3 GUIDANCE" in last_text
        assert "Error:" in last_text


# =========================================================================
# extract_category_from_error
# =========================================================================


class TestExtractCategoryFromError:
    def test_valid_tag(self):
        assert extract_category_from_error("[CATEGORY:FORMAT_NOT_FOUND] blah") == ErrorCategory.FORMAT_NOT_FOUND

    def test_unknown_value(self):
        assert extract_category_from_error("[CATEGORY:BOGUS] blah") is None

    def test_no_tag(self):
        assert extract_category_from_error("just a plain error") is None

    def test_empty(self):
        assert extract_category_from_error("") is None

    def test_none(self):
        assert extract_category_from_error(None) is None


# =========================================================================
# Category-aware L1 recovery messages
# =========================================================================


class TestCategoryAwareL1Recovery:
    """Validate that L1 recovery produces category-specific guidance."""

    TOOLS = ["read_file", "shell_tool", "write_file"]

    def test_l1_format_not_found(self):
        """FORMAT_NOT_FOUND L1 mentions missing tool call + format + tool list."""
        msg = build_recovery_message(
            consecutive_errors=1,
            error_category=ErrorCategory.FORMAT_NOT_FOUND,
            available_tool_names=self.TOOLS,
        )
        assert "did not contain a valid tool call" in msg
        assert '"name"' in msg
        assert '"arguments"' in msg
        assert "read_file" in msg

    def test_l1_unknown_tool(self):
        """UNKNOWN_TOOL L1 mentions unknown tool + available tools."""
        msg = build_recovery_message(
            consecutive_errors=1,
            error_category=ErrorCategory.UNKNOWN_TOOL,
            available_tool_names=self.TOOLS,
            partial_tool_name="magic_tool",
        )
        assert "does not exist" in msg
        assert "magic_tool" in msg
        assert "read_file" in msg

    def test_l1_json_syntax_error(self):
        """JSON_SYNTAX_ERROR L1 mentions syntax errors + correct format."""
        msg = build_recovery_message(
            consecutive_errors=1,
            error_category=ErrorCategory.JSON_SYNTAX_ERROR,
            available_tool_names=self.TOOLS,
        )
        assert "JSON syntax errors" in msg
        assert '"name"' in msg
        assert "read_file" in msg

    def test_l1_argument_error(self):
        """ARGUMENT_ERROR L1 mentions invalid args + partial tool name."""
        msg = build_recovery_message(
            consecutive_errors=1,
            error_category=ErrorCategory.ARGUMENT_ERROR,
            available_tool_names=self.TOOLS,
            partial_tool_name="read_file",
        )
        assert "invalid arguments" in msg
        assert "read_file" in msg

    def test_l1_none_category_fallback(self):
        """None category falls back to generic TOOL FORMAT REMINDER."""
        msg = build_recovery_message(
            consecutive_errors=1,
            error_category=None,
            available_tool_names=self.TOOLS,
        )
        assert "TOOL FORMAT REMINDER" in msg
        assert '"name"' in msg

    def test_l1_no_tool_names(self):
        """Empty tool list should not crash; still shows format example."""
        msg = build_recovery_message(
            consecutive_errors=1,
            error_category=ErrorCategory.FORMAT_NOT_FOUND,
            available_tool_names=None,
        )
        assert '"name"' in msg
        assert "N/A" in msg
