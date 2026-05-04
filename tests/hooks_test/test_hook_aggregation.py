"""Tests for hook result aggregation and permission precedence.

Validates:
- Permission precedence: deny > allow
- Additional contexts collected as list
- Exception in one hook does not affect others
- Zero matching hooks returns default allow
- Queue overflow behavior
"""

import unittest

from src.lib.smolagents.hooks import HookEvent, HookManager, HookResult
from src.lib.smolagents.hooks.types import HookContext


def _allow_hook(context: HookContext) -> HookResult:
    return HookResult(success=True, decision="allow", agent_context="ctx_allow")


def _deny_hook(context: HookContext) -> HookResult:
    return HookResult(
        success=False,
        decision="block",
        reason="denied by policy",
        agent_context="ctx_deny",
    )


def _modify_hook(context: HookContext) -> HookResult:
    return HookResult(
        success=True,
        decision="modify",
        modified_input={"extra_key": "injected"},
        agent_context="ctx_modify",
    )


def _crashing_hook(context: HookContext) -> HookResult:
    raise RuntimeError("intentional crash")


class TestPermissionPrecedence(unittest.TestCase):
    """Permission aggregation: deny > allow."""

    def setUp(self):
        self.manager = HookManager()

    def test_single_allow(self):
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _allow_hook)
        result = self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.assertTrue(result.success)
        self.assertEqual(result.decision, "allow")

    def test_single_deny(self):
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _deny_hook)
        result = self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.assertTrue(result.should_block())
        self.assertIn("denied", result.reason)

    def test_deny_wins_over_allow(self):
        """If any hook denies, the aggregated result blocks."""
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _allow_hook)
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _deny_hook)
        result = self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.assertTrue(result.should_block())

    def test_allow_allow_allow(self):
        """All allow hooks -> final allow."""
        for _ in range(3):
            self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _allow_hook)
        result = self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.assertTrue(result.success)
        self.assertEqual(result.decision, "allow")

    def test_modify_accumulates_input(self):
        """Modify hooks should accumulate input changes."""
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _modify_hook)
        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "tool", {"original": "value"},
        )
        self.assertEqual(result.decision, "modify")
        self.assertIsNotNone(result.modified_input)
        self.assertEqual(result.modified_input.get("extra_key"), "injected")
        self.assertEqual(result.modified_input.get("original"), "value")


class TestContextCollection(unittest.TestCase):
    """Additional context collection as list."""

    def setUp(self):
        self.manager = HookManager()

    def test_single_context_queued(self):
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _allow_hook)
        self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        contexts = self.manager.consume_pending_agent_context()
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0], "ctx_allow")

    def test_multiple_contexts_collected(self):
        """Multiple hooks each adding context -> all collected."""
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _allow_hook)
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _modify_hook)
        self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        contexts = self.manager.consume_pending_agent_context()
        self.assertEqual(len(contexts), 2)
        self.assertIn("ctx_allow", contexts)
        self.assertIn("ctx_modify", contexts)

    def test_context_consumed_once(self):
        """After consume, queue is empty."""
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _allow_hook)
        self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.manager.consume_pending_agent_context()
        second = self.manager.consume_pending_agent_context()
        self.assertEqual(len(second), 0)


class TestExceptionIsolation(unittest.TestCase):
    """Exception in one hook does not affect others."""

    def setUp(self):
        self.manager = HookManager()

    def test_crash_does_not_block_other_hooks(self):
        """A crashing hook is skipped; subsequent hooks still run."""
        counters = {"calls": 0}

        def counting_hook(context: HookContext) -> HookResult:
            counters["calls"] += 1
            return HookResult(success=True, decision="allow")

        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", _crashing_hook)
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", counting_hook)
        result = self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        # The counting hook should have executed despite the crash
        self.assertEqual(counters["calls"], 1)
        # Final result should still be usable
        self.assertEqual(result.decision, "allow")


class TestZeroMatchingHooks(unittest.TestCase):
    """No matching hooks -> default allow."""

    def setUp(self):
        self.manager = HookManager()

    def test_no_hooks_registered(self):
        result = self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "tool", {})
        self.assertTrue(result.success)
        self.assertEqual(result.decision, "allow")

    def test_hooks_registered_but_no_pattern_match(self):
        self.manager.register_hook(HookEvent.PRE_TOOL_USE, "Write", _allow_hook)
        result = self.manager.trigger_hooks(HookEvent.PRE_TOOL_USE, "Read", {})
        self.assertTrue(result.success)
        self.assertEqual(result.decision, "allow")


class TestQueueOverflow(unittest.TestCase):
    """Message queue bounded size."""

    def setUp(self):
        self.manager = HookManager()

    def test_agent_context_overflow_drops_oldest(self):
        """Exceeding the queue limit drops the oldest entry."""
        from src.lib.smolagents.hooks.hook_manager import _MAX_PENDING_ITEMS

        for i in range(_MAX_PENDING_ITEMS + 5):
            self.manager.queue_agent_context(f"msg_{i}")

        contexts = self.manager.consume_pending_agent_context()
        self.assertEqual(len(contexts), _MAX_PENDING_ITEMS)
        # The first 5 should have been dropped
        self.assertEqual(contexts[0], "msg_5")
        self.assertEqual(contexts[-1], f"msg_{_MAX_PENDING_ITEMS + 4}")

    def test_user_message_overflow_drops_oldest(self):
        from src.lib.smolagents.hooks.hook_manager import _MAX_PENDING_ITEMS

        for i in range(_MAX_PENDING_ITEMS + 3):
            self.manager.queue_user_message(f"user_{i}")

        messages = self.manager.consume_pending_user_messages()
        self.assertEqual(len(messages), _MAX_PENDING_ITEMS)
        self.assertEqual(messages[0], "user_3")


class TestRegisterHookValidation(unittest.TestCase):
    """Registration validation."""

    def setUp(self):
        self.manager = HookManager()

    def test_non_callable_raises_type_error(self):
        """Registering a non-callable raises TypeError."""
        with self.assertRaises(TypeError):
            self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", "not_a_function")

    def test_none_func_raises_type_error(self):
        with self.assertRaises(TypeError):
            self.manager.register_hook(HookEvent.PRE_TOOL_USE, "*", None)


if __name__ == "__main__":
    unittest.main()
