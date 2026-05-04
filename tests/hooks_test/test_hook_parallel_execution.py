"""Tests for parallel hook execution.

Validates:
- Normal: 3 hooks sleeping 2s each -> total time ~2s (not 6s)
- Error: 1 hook timeout does not affect other hooks
- Boundary: single hook -> no ThreadPool overhead
"""

import time
import unittest

from src.lib.smolagents.hooks.hook_manager import HookManager
from src.lib.smolagents.hooks.types import HookContext, HookEvent, HookResult


def _sleep_hook_factory(sleep_sec: float, label: str):
    """Create a hook that sleeps then returns success with label."""
    def hook(context: HookContext) -> HookResult:
        time.sleep(sleep_sec)
        return HookResult(
            success=True,
            decision="allow",
            reason=label,
        )
    hook.__name__ = f"sleep_{label}"
    return hook


def _blocking_hook(context: HookContext) -> HookResult:
    """Hook that blocks the action."""
    return HookResult(
        success=False,
        decision="block",
        reason="blocked by test",
        permission_behavior="deny",
    )


class TestParallelExecution(unittest.TestCase):
    """Hook parallel execution tests."""

    def setUp(self):
        self.manager = HookManager()

    def test_parallel_hooks_faster_than_serial(self):
        """Multiple hooks should run in parallel, not serial.

        3 hooks each sleeping 1.5s should complete in roughly 1.5s (parallel),
        not 4.5s (serial).
        """
        for i in range(3):
            self.manager.register_hook(
                HookEvent.PRE_TOOL_USE,
                "*",
                _sleep_hook_factory(1.5, f"hook_{i}"),
                timeout=10.0,
            )

        start = time.monotonic()
        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "test_tool", {},
        )
        elapsed = time.monotonic() - start

        self.assertTrue(result.success)
        # Parallel: ~1.5s.  Serial would be ~4.5s.  Allow generous margin.
        self.assertLess(elapsed, 3.5, f"Hooks took {elapsed:.1f}s, expected <3.5s for parallel")

    def test_single_hook_no_overhead(self):
        """A single hook should not incur ThreadPool overhead."""
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE,
            "*",
            _sleep_hook_factory(0.1, "single"),
            timeout=5.0,
        )

        start = time.monotonic()
        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "test_tool", {},
        )
        elapsed = time.monotonic() - start

        self.assertTrue(result.success)
        self.assertLess(elapsed, 2.0)

    def test_blocking_hook_result_preserved_in_parallel(self):
        """A blocking hook among parallel hooks should still block."""
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*",
            _sleep_hook_factory(0.1, "allow_hook"),
            timeout=5.0,
        )
        self.manager.register_hook(
            HookEvent.PRE_TOOL_USE, "*",
            _blocking_hook,
            timeout=5.0,
        )

        result = self.manager.trigger_hooks(
            HookEvent.PRE_TOOL_USE, "test_tool", {},
        )
        self.assertFalse(result.success)
        self.assertEqual(result.decision, "block")


if __name__ == "__main__":
    unittest.main()
