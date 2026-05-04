"""Tests for the YAML config -> HookManager bridge.

Validates:
- Normal: CommandHook config -> register -> trigger_hooks -> executed
- Normal: Config hooks and function hooks merge correctly
- Error: invalid config -> graceful degradation
- Boundary: empty config -> function hooks still work
"""

import unittest
from unittest.mock import patch, MagicMock

from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.hooks.hooks_config import HooksConfigManager
from src.lib.smolagents.hooks.types import (
    CommandHook,
    HookCommand,
    HookContext,
    HookEvent,
    HookMatcher,
    HookResult,
)


def _allow_hook(context: HookContext) -> HookResult:
    return HookResult(success=True, decision="allow", reason="func_hook_ran")


class TestConfigBridge(unittest.TestCase):
    """Config hook bridge: YAML -> Callable -> execution."""

    def setUp(self):
        self.manager = HookManager()

    def test_config_command_hook_executed(self):
        """A CommandHook from config should be executed via trigger_hooks."""
        cm = HooksConfigManager()
        cm.update({
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "echo ok"}
                    ],
                }
            ],
        })
        self.manager.set_config_manager(cm)

        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "test_tool", {},
        )
        # Command hook 'echo ok' exits 0 -> success
        self.assertTrue(result.success)
        self.assertEqual(result.decision, "allow")

    def test_config_blocking_command_hook(self):
        """A CommandHook that exits 2 should block."""
        cm = HooksConfigManager()
        cm.update({
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "exit 2"}
                    ],
                }
            ],
        })
        self.manager.set_config_manager(cm)

        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "test_tool", {},
        )
        self.assertFalse(result.success)
        self.assertEqual(result.decision, "block")

    def test_config_and_function_hooks_both_run(self):
        """Both config hooks and function hooks should execute."""
        cm = HooksConfigManager()
        cm.update({
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": "echo ok"}
                    ],
                }
            ],
        })
        self.manager.set_config_manager(cm)
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", _allow_hook,
        )

        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "test_tool", {},
        )
        # Both should succeed
        self.assertTrue(result.success)
        # The function hook's reason should be present
        self.assertIn("func_hook_ran", result.reason or "")

    def test_empty_config_function_hooks_still_work(self):
        """With empty config, function hooks should still execute."""
        cm = HooksConfigManager()
        # Empty config
        self.manager.set_config_manager(cm)
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", _allow_hook,
        )

        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "test_tool", {},
        )
        self.assertTrue(result.success)
        self.assertIn("func_hook_ran", result.reason or "")

    def test_no_config_manager_function_hooks_still_work(self):
        """Without config manager, function hooks should still execute."""
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*", _allow_hook,
        )

        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "test_tool", {},
        )
        self.assertTrue(result.success)

    def test_config_matcher_pattern_filters(self):
        """Config hooks should respect matcher pattern filtering."""
        cm = HooksConfigManager()
        cm.update({
            "PreToolUse": [
                {
                    "matcher": "Write",
                    "hooks": [
                        {"type": "command", "command": 'echo "matched" >&2; exit 2'}
                    ],
                }
            ],
        })
        self.manager.set_config_manager(cm)

        # Should NOT match 'Read' tool
        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "Read", {},
        )
        self.assertTrue(result.success)

        # Should match 'Write' tool
        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "Write", {},
        )
        self.assertFalse(result.success)
        self.assertEqual(result.decision, "block")


class TestConfigBridgeBuildExecutor(unittest.TestCase):
    """Test _build_executor factory method."""

    def test_command_hook_builds_callable(self):
        cmd = CommandHook(command="echo test")
        executor = HookManager._build_executor(cmd)
        self.assertIsNotNone(executor)
        self.assertTrue(callable(executor))

    def test_unknown_type_returns_none(self):
        """Unknown hook type should return None."""
        result = HookManager._build_executor("not a hook command")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
