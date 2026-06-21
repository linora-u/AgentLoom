"""Tests for tool result handling in the hook shim.

Large string results are no longer persisted through a separate temp-file
compatibility path. During an AgentLoom task, the active ContextEngine owns
reversible compression and retrieval through ContextRef.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.lib.smolagents.hooks.tool_shim import inject_hooks
from src.lib.smolagents.hooks.types import HookResult
from src.tools.shell.output_interceptor import OutputInterceptor


def _make_mock_tool(name: str, forward_return_value):
    """Create a minimal mock Tool with controllable forward()."""
    tool = MagicMock()
    tool.name = name
    tool.inputs = {}
    tool._hooks_injected = False
    tool.forward = MagicMock(return_value=forward_return_value)
    tool.forward.__name__ = "forward"
    tool.forward.__wrapped__ = None
    return tool


def _make_passthrough_hook_result():
    return HookResult(success=True, decision="allow")


def _run_tool(tool_name: str, forward_return, context_engine=None):
    tool = _make_mock_tool(tool_name, forward_return)
    passthrough = _make_passthrough_hook_result()

    with patch(
        "src.lib.smolagents.hooks.tool_shim._resolve_hook_manager"
    ) as mock_hm, patch(
        "src.lib.context_engine.runtime.get_active_context_engine",
        return_value=context_engine,
    ):
        hm_instance = MagicMock()
        hm_instance.trigger_hooks.return_value = passthrough
        hm_instance.flush_user_messages = MagicMock()
        mock_hm.return_value = hm_instance

        inject_hooks(tool)
        result = tool.forward()
    return result


class TestContextEngineResultHandling:
    def test_large_result_uses_active_context_engine(self):
        big_text = "CTX" * 20000
        context_engine = MagicMock()
        context_engine.compress_tool_result.return_value = (
            "[ContextRef ctx_1234567890abcdef kind=text source=big_tool "
            "original_chars=60000 preview_chars=128]\n"
            'Use loom_retrieve_context(ref="ctx_1234567890abcdef", query="", offset=0, limit=200) '
            "to retrieve original content.\n\npreview"
        )

        result = _run_tool("big_tool", big_text, context_engine=context_engine)

        assert "[ContextRef ctx_1234567890abcdef" in result
        assert "loom_retrieve_context" in result
        assert "Full output saved to" not in result
        context_engine.compress_tool_result.assert_called_once_with(
            big_text,
            tool_name="big_tool",
            source="tool_result:big_tool",
        )

    def test_no_active_context_engine_leaves_result_unchanged(self):
        big_text = "B" * 50000

        result = _run_tool("big_tool", big_text, context_engine=None)

        assert result == big_text
        assert "Full output saved to" not in result

    def test_context_engine_failure_is_visible_and_has_no_alternate_retrieval_path(self):
        big_text = "C" * 50000
        context_engine = MagicMock()
        context_engine.compress_tool_result.side_effect = RuntimeError("store unavailable")

        with pytest.raises(RuntimeError, match="store unavailable"):
            _run_tool("big_tool", big_text, context_engine=context_engine)

    def test_small_result_unchanged_when_context_engine_declines_compression(self):
        small_text = "Hello world"
        context_engine = MagicMock()
        context_engine.compress_tool_result.return_value = None

        result = _run_tool("small_tool", small_text, context_engine=context_engine)

        assert result == small_text
        context_engine.compress_tool_result.assert_called_once_with(
            small_text,
            tool_name="small_tool",
            source="tool_result:small_tool",
        )

    def test_empty_result_protection_still_runs(self):
        result = _run_tool("empty_tool", "", context_engine=None)

        assert result == "(empty_tool completed with no output)"

    def test_shell_tool_notice_survives_without_outer_temp_file_path(self, tmp_path):
        interceptor = OutputInterceptor(preview_bytes=30000, storage_dir=str(tmp_path))
        interceptor.write("\n".join(str(i) for i in range(1, 12001)))
        shell_result = interceptor.finalize()

        assert "<system_notice>" in shell_result
        assert "FULL, unbroken output log" in shell_result

        result = _run_tool("shell_tool", shell_result, context_engine=None)

        assert result == shell_result
        assert "Full output saved to" not in result
