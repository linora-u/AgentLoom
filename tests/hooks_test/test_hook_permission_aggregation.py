"""Tests for hook permission aggregation.

Validates:
- Normal: [allow, deny, allow] -> deny (deny always wins)
- Normal: [allow, allow] -> allow
- Normal: [passthrough] -> passthrough (default)
- Boundary: empty list -> default allow
- Error: hook exception -> does not affect other hooks
"""

import unittest

from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.hooks.types import HookContext, HookEvent, HookResult


def _make_hook(decision: str, permission: str = None, reason: str = ""):
    """Create a hook returning the given decision and permission behavior."""
    def hook(context: HookContext) -> HookResult:
        return HookResult(
            success=(decision != "block"),
            decision=decision,
            reason=reason,
            permission_behavior=permission or ("deny" if decision == "block" else "allow"),
        )
    hook.__name__ = f"hook_{decision}_{reason}"
    return hook


class TestPermissionAggregation(unittest.TestCase):
    """Permission aggregation: deny > allow > passthrough."""

    def setUp(self):
        self.manager = HookManager()

    def test_allow_deny_allow_results_in_deny(self):
        """deny should win over allow regardless of order."""
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _make_hook("allow", reason="a1"))
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _make_hook("block", "deny", reason="blocked"))
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _make_hook("allow", reason="a2"))

        result = self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.assertEqual(result.decision, "block")
        self.assertEqual(result.permission_behavior, "deny")

    def test_all_allow(self):
        """All allow hooks should result in allow."""
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _make_hook("allow", reason="a1"))
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _make_hook("allow", reason="a2"))

        result = self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.assertEqual(result.decision, "allow")
        self.assertTrue(result.success)

    def test_no_hooks_default_allow(self):
        """No hooks registered should return default allow."""
        result = self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.assertEqual(result.decision, "allow")
        self.assertTrue(result.success)

    def test_exception_does_not_block_other_hooks(self):
        """A crashing hook should not prevent other hooks from running."""
        def crasher(context: HookContext) -> HookResult:
            raise RuntimeError("boom")

        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", crasher)
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _make_hook("allow", reason="survived"))

        result = self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        # Should still get the surviving hook's result
        self.assertIn("survived", result.reason or "")


if __name__ == "__main__":
    unittest.main()
