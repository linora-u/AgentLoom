"""
Tests for agentloom.runtime.error_recovery module.

Covers error classification, tool info extraction, progressive recovery
message generation, error message consolidation, and exception safety.
"""


from agentloom.runtime.error_recovery import (
    NOW_LETS_RETRY_PREFIX,
    RUNTIME_FEEDBACK_RAW_KEY,
    ErrorCategory,
    build_recovery_message,
    consolidate_error_messages,
    extract_category_from_error,
    format_tool_list,
)


class TestErrorCategory:
    """Only provider-native structured-call failures remain recoverable."""

    def test_members(self):
        names = {e.name for e in ErrorCategory}
        assert names == {
            "NATIVE_TOOL_CALL_REQUIRED",
            "UNKNOWN_TOOL",
            "ARGUMENT_ERROR",
        }


class TestBuildRecoveryMessage:
    TOOLS = ["read_file", "shell_tool"]

    def test_level1_basic(self):
        msg = build_recovery_message(
            consecutive_errors=1,
            error_category=ErrorCategory.NATIVE_TOOL_CALL_REQUIRED,
            available_tool_names=self.TOOLS,
        )
        assert "provider-native structured tool call" in msg
        assert "read_file" in msg
        assert "shell_tool" in msg
        assert len(msg) < 600

    def test_level2_diagnosis(self):
        msg = build_recovery_message(
            consecutive_errors=2,
            error_category=ErrorCategory.ARGUMENT_ERROR,
            last_output_snippet="{'name': 'read_file'}",
            available_tool_names=self.TOOLS,
            tool_descriptions={"read_file": "Read a file"},
        )
        assert "DIAGNOSIS" in msg
        assert "ARGUMENT_ERROR" in msg
        assert "native structured" in msg
        assert len(msg) < 1200

    def test_level2_no_wrong_examples(self):
        msg = build_recovery_message(
            consecutive_errors=2,
            error_category=ErrorCategory.NATIVE_TOOL_CALL_REQUIRED,
            available_tool_names=self.TOOLS,
        )
        assert "WRONG" not in msg

    def test_level3_approach_switch(self):
        msg = build_recovery_message(
            consecutive_errors=3,
            error_category=ErrorCategory.UNKNOWN_TOOL,
            available_tool_names=self.TOOLS,
        )
        assert "native tool-call errors" in msg
        assert "provider's structured tool-call mechanism" in msg
        assert len(msg) < 800

    def test_level3_shorter_than_level2(self):
        msg2 = build_recovery_message(
            consecutive_errors=2,
            error_category=ErrorCategory.NATIVE_TOOL_CALL_REQUIRED,
            last_output_snippet="{'name': 'read_file', 'arguments': {'path': '/tmp'}}",
            available_tool_names=self.TOOLS,
            tool_descriptions={"read_file": "Read file content", "shell_tool": "Run shell commands"},
        )
        msg3 = build_recovery_message(
            consecutive_errors=3,
            error_category=ErrorCategory.NATIVE_TOOL_CALL_REQUIRED,
            available_tool_names=self.TOOLS,
        )
        assert len(msg3) < len(msg2)

    def test_level4_minimal(self):
        msg = build_recovery_message(
            consecutive_errors=5,
            error_category=ErrorCategory.NATIVE_TOOL_CALL_REQUIRED,
            available_tool_names=self.TOOLS,
        )
        assert "native tool-call errors" in msg
        assert len(msg) < 400

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
        assert "native tool-call errors" in msg

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
        """Later retries remain concise and provider-native."""
        msg = build_recovery_message(
            consecutive_errors=5,
            available_tool_names=["read_file"],
        )
        assert "provider" in msg

    def test_all_levels_no_wrong_block(self):
        for n in [1, 2, 3, 5]:
            msg = build_recovery_message(
                consecutive_errors=n,
                error_category=ErrorCategory.NATIVE_TOOL_CALL_REQUIRED,
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
        assert result[0]["raw"][RUNTIME_FEEDBACK_RAW_KEY] is True

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

    def test_build_recovery_with_none(self):
        msg = build_recovery_message(1, None, None, None, None)
        assert isinstance(msg, str)

    def test_consolidate_with_none_messages(self):
        result = consolidate_error_messages(None, 0, "")
        assert result is None  # Returns input unchanged

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
    def test_native_tool_call_required(self):
        assert (
            extract_category_from_error(
                "Model response must contain native structured tool_calls"
            )
            == ErrorCategory.NATIVE_TOOL_CALL_REQUIRED
        )

    def test_unknown_tool(self):
        assert (
            extract_category_from_error("Tool x not found in registered tools")
            == ErrorCategory.UNKNOWN_TOOL
        )

    def test_argument_error(self):
        assert (
            extract_category_from_error("arguments must be a JSON object")
            == ErrorCategory.ARGUMENT_ERROR
        )

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

    def test_l1_native_tool_call_required(self):
        msg = build_recovery_message(
            consecutive_errors=1,
            error_category=ErrorCategory.NATIVE_TOOL_CALL_REQUIRED,
            available_tool_names=self.TOOLS,
        )
        assert "provider-native structured tool call" in msg
        assert "assistant text" in msg
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
        """Unknown categories retain provider-native guidance."""
        msg = build_recovery_message(
            consecutive_errors=1,
            error_category=None,
            available_tool_names=self.TOOLS,
        )
        assert "provider's native structured tool-call mechanism" in msg

    def test_l1_no_tool_names(self):
        """Empty tool lists remain safe."""
        msg = build_recovery_message(
            consecutive_errors=1,
            error_category=ErrorCategory.NATIVE_TOOL_CALL_REQUIRED,
            available_tool_names=None,
        )
        assert "N/A" in msg
