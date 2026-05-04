"""Tests for the system-reminder wrapping and step_number propagation.

Validates:
- Normal: content wrapped in <system-reminder> tags
- Idempotent: already-wrapped content not double-wrapped
- Boundary: empty/whitespace input returns empty string
- Integration: HookContext carries step_number
- Integration: _context_to_dict serializes step_number
- Integration: prompt YAML contains System Reminders section
- Integration: exec_command_hook env includes STEP_NUMBER
"""

import os
import unittest

from src.lib.smolagents.hooks.hook_manager import wrap_in_system_reminder, _context_to_dict
from src.lib.smolagents.hooks.types import HookContext


class TestWrapInSystemReminder(unittest.TestCase):
    """Unit tests for wrap_in_system_reminder()."""

    def test_basic_wrapping(self):
        """Plain text should be wrapped in <system-reminder> tags."""
        result = wrap_in_system_reminder("Update your trace.md file now.")
        self.assertTrue(result.startswith("<system-reminder>"))
        self.assertTrue(result.endswith("</system-reminder>"))
        self.assertIn("Update your trace.md file now.", result)

    def test_idempotent(self):
        """Already-wrapped content should not be double-wrapped."""
        already_wrapped = "<system-reminder>\nSome content\n</system-reminder>"
        result = wrap_in_system_reminder(already_wrapped)
        self.assertEqual(result, already_wrapped)
        # Verify no nested tags
        self.assertEqual(result.count("<system-reminder>"), 1)

    def test_empty_string_returns_empty(self):
        """Empty input should return empty string, not wrapped empty tags."""
        self.assertEqual(wrap_in_system_reminder(""), "")

    def test_whitespace_only_returns_empty(self):
        """Whitespace-only input should return empty string."""
        self.assertEqual(wrap_in_system_reminder("   \n\t  "), "")

    def test_none_like_input(self):
        """None-like empty string edge case."""
        self.assertEqual(wrap_in_system_reminder(""), "")

    def test_multiline_content(self):
        """Multi-line content should be wrapped correctly."""
        content = "Line 1\nLine 2\nLine 3"
        result = wrap_in_system_reminder(content)
        self.assertIn("Line 1", result)
        self.assertIn("Line 3", result)
        self.assertTrue(result.startswith("<system-reminder>"))

    def test_content_with_leading_trailing_whitespace(self):
        """Leading/trailing whitespace should be stripped before wrapping."""
        result = wrap_in_system_reminder("  \n  Hello  \n  ")
        self.assertIn("Hello", result)
        self.assertTrue(result.startswith("<system-reminder>"))

    def test_content_with_xml_like_tags(self):
        """Content containing other XML tags should still be wrapped."""
        content = "<some-tag>data</some-tag>"
        result = wrap_in_system_reminder(content)
        self.assertTrue(result.startswith("<system-reminder>"))
        self.assertIn("<some-tag>data</some-tag>", result)


class TestHookContextStepNumber(unittest.TestCase):
    """Tests for step_number field in HookContext and serialization."""

    def test_hook_context_has_step_number(self):
        """HookContext should accept and store step_number."""
        ctx = HookContext(
            session_id="test-session",
            cwd="/tmp",
            hook_event_name="PostToolUse",
            tool_name="grep",
            tool_input={"query": "test"},
            step_number=5,
        )
        self.assertEqual(ctx.step_number, 5)

    def test_hook_context_step_number_default_none(self):
        """step_number should default to None when not provided."""
        ctx = HookContext(
            session_id="test-session",
            cwd="/tmp",
            hook_event_name="PostToolUse",
            tool_name="grep",
            tool_input={},
        )
        self.assertIsNone(ctx.step_number)

    def test_context_to_dict_includes_step_number(self):
        """_context_to_dict should serialize step_number."""
        ctx = HookContext(
            session_id="test-session",
            cwd="/tmp",
            hook_event_name="PostToolUse",
            tool_name="grep",
            tool_input={},
            step_number=7,
        )
        d = _context_to_dict(ctx)
        self.assertEqual(d["step_number"], 7)

    def test_context_to_dict_step_number_none(self):
        """_context_to_dict should serialize None step_number."""
        ctx = HookContext(
            session_id="test-session",
            cwd="/tmp",
            hook_event_name="PostToolUse",
            tool_name="grep",
            tool_input={},
        )
        d = _context_to_dict(ctx)
        self.assertIsNone(d["step_number"])


class TestBuildHookEnvStepNumber(unittest.TestCase):
    """Tests for STEP_NUMBER in exec_command_hook environment."""

    def test_step_number_in_env(self):
        """_build_hook_env should include STEP_NUMBER from hook_input."""
        from src.lib.smolagents.hooks.exec_command_hook import _build_hook_env
        env = _build_hook_env({"tool_name": "test", "step_number": 3})
        self.assertEqual(env.get("STEP_NUMBER"), "3")

    def test_step_number_missing_in_hook_input(self):
        """STEP_NUMBER should be empty string when not in hook_input."""
        from src.lib.smolagents.hooks.exec_command_hook import _build_hook_env
        env = _build_hook_env({"tool_name": "test"})
        self.assertEqual(env.get("STEP_NUMBER"), "")

    def test_step_number_none_in_hook_input(self):
        """STEP_NUMBER should be empty string when step_number is None."""
        from src.lib.smolagents.hooks.exec_command_hook import _build_hook_env
        env = _build_hook_env({"tool_name": "test", "step_number": None})
        self.assertEqual(env.get("STEP_NUMBER"), "")


class TestPromptContainsSystemReminderSection(unittest.TestCase):
    """Verify all 4 prompt variant YAMLs contain the System Reminders section."""

    PROMPT_FILES = [
        "src/lib/smolagents/prompts/toolcalling_agent.yaml",
        "src/lib/smolagents/prompts/anthropic/toolcalling_agent.yaml",
        "src/lib/smolagents/prompts/openai/toolcalling_agent.yaml",
        "src/lib/smolagents/prompts/gemini/toolcalling_agent.yaml",
    ]

    def test_all_prompts_contain_system_reminder_section(self):
        """Each prompt YAML should contain the System Reminders section."""
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        for rel_path in self.PROMPT_FILES:
            path = os.path.join(project_root, rel_path)
            with self.subTest(path=rel_path):
                self.assertTrue(os.path.exists(path), f"File not found: {path}")
                content = open(path, encoding="utf-8").read()
                self.assertIn("# System Reminders", content)
                self.assertIn("<system-reminder>", content)


class TestHookManagerStepNumberAttribute(unittest.TestCase):
    """Verify HookManager has step_number attribute."""

    def test_hook_manager_has_step_number(self):
        """HookManager instance should have step_number initialized to 0."""
        from src.lib.smolagents.hooks.hook_manager import HookManager
        hm = HookManager()
        self.assertEqual(hm.step_number, 0)

    def test_hook_manager_step_number_settable(self):
        """step_number should be directly settable on HookManager."""
        from src.lib.smolagents.hooks.hook_manager import HookManager
        hm = HookManager()
        hm.step_number = 42
        self.assertEqual(hm.step_number, 42)


class TestProcessHookOutputAgentContextShorthand(unittest.TestCase):
    """Verify that top-level 'agent_context' JSON key from hook scripts
    is correctly routed into HookResult.agent_context.

    This tests the root cause fix: hook scripts output
    {"decision": "allow", "agent_context": "msg"} which used to be
    silently dropped because SyncHookOutput only recognized the nested
    hookSpecificOutput.additionalContext path.
    """

    def test_toplevel_agent_context_parsed(self):
        """SyncHookOutput should recognize top-level agent_context field."""
        from src.lib.smolagents.hooks.hook_schemas import SyncHookOutput
        output = SyncHookOutput.model_validate({
            "decision": "approve",
            "agent_context": "Update trace.md now",
        })
        self.assertEqual(output.agent_context, "Update trace.md now")

    def test_toplevel_agent_context_routed_to_hook_result(self):
        """process_hook_output should set HookResult.agent_context
        from top-level agent_context when hookSpecificOutput is absent."""
        from src.lib.smolagents.hooks.hook_schemas import (
            SyncHookOutput, process_hook_output,
        )
        sync_out = SyncHookOutput.model_validate({
            "decision": "approve",
            "agent_context": "WARNING: trace.md stale",
        })
        result = process_hook_output(sync_out, hook_event="PostToolUse")
        self.assertEqual(result.agent_context, "WARNING: trace.md stale")
        self.assertEqual(result.additional_context, "WARNING: trace.md stale")

    def test_nested_additional_context_takes_precedence(self):
        """hookSpecificOutput.additionalContext should take precedence
        over top-level agent_context when both are present."""
        from src.lib.smolagents.hooks.hook_schemas import (
            SyncHookOutput, process_hook_output,
        )
        sync_out = SyncHookOutput.model_validate({
            "decision": "approve",
            "agent_context": "top-level msg",
            "hookSpecificOutput": {
                "additionalContext": "nested msg",
            },
        })
        result = process_hook_output(sync_out, hook_event="PostToolUse")
        # Nested takes precedence
        self.assertEqual(result.agent_context, "nested msg")

    def test_no_agent_context_yields_none(self):
        """When neither agent_context nor additionalContext is provided,
        HookResult.agent_context should remain None."""
        from src.lib.smolagents.hooks.hook_schemas import (
            SyncHookOutput, process_hook_output,
        )
        sync_out = SyncHookOutput.model_validate({
            "decision": "approve",
        })
        result = process_hook_output(sync_out, hook_event="PostToolUse")
        self.assertIsNone(result.agent_context)

    def test_agent_context_queued_in_hook_manager(self):
        """End-to-end: agent_context from hook output should be queued
        in HookManager and consumable by the agent."""
        from src.lib.smolagents.hooks.hook_manager import HookManager
        from src.lib.smolagents.hooks.types import HookResult

        hm = HookManager()

        # Simulate what _merge_results does when a hook returns agent_context
        mock_result = HookResult(
            success=True,
            decision="allow",
            agent_context="Stale runtime files",
            additional_context="Stale runtime files",
        )
        hm.queue_agent_context(mock_result.agent_context)

        consumed = hm.consume_pending_agent_context()
        self.assertEqual(consumed, ["Stale runtime files"])
        # Second consume should be empty
        self.assertEqual(hm.consume_pending_agent_context(), [])

    def test_full_pipeline_parse_to_result(self):
        """Full pipeline: JSON string -> parse_hook_output -> process_hook_output
        -> HookResult with agent_context populated."""
        from src.lib.smolagents.hooks.hook_schemas import (
            SyncHookOutput, parse_hook_output, process_hook_output,
        )
        # This is the exact JSON format hook scripts output
        json_str = '{"decision": "approve", "agent_context": "Consider updating trace.md"}'
        parsed = parse_hook_output(json_str)
        self.assertIsInstance(parsed, SyncHookOutput)
        result = process_hook_output(parsed, hook_event="PostToolUse")
        self.assertEqual(result.agent_context, "Consider updating trace.md")


if __name__ == "__main__":
    unittest.main()
