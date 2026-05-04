"""Tests for the command hook executor.

Validates:
- Normal: echo script -> exit 0 -> success with valid JSON
- Normal: exit 2 -> blocking error with stderr as reason
- Error: dead-loop script + timeout -> process killed, no orphans
- Error: invalid command -> error result, no crash
- Boundary: empty stdout with exit 0 -> success
- Boundary: first-line async detection -> immediate return
"""

import json
import os
import subprocess
import time
import unittest
from unittest.mock import MagicMock

from src.lib.smolagents.hooks.exec_command_hook import (
    DEFAULT_COMMAND_TIMEOUT,
    exec_command_hook,
    _build_hook_env,
    _process_command_result,
)
from src.lib.smolagents.hooks.types import CommandHook, HookResult


class TestExecCommandHookSuccess(unittest.TestCase):
    """Normal path: successful command execution."""

    def test_echo_exit_0_returns_success(self):
        """Simple echo command with exit 0 should return success."""
        hook = CommandHook(command='echo "hello"')
        result = exec_command_hook(hook, {"tool_name": "test"})
        self.assertTrue(result.success)
        self.assertEqual(result.decision, "allow")
        self.assertEqual(result.outcome, "success")

    def test_json_output_parsed(self):
        """Command outputting valid JSON should have it parsed."""
        hook = CommandHook(command='echo \'{"decision": "approve"}\'')
        result = exec_command_hook(hook, {"tool_name": "test"})
        self.assertTrue(result.success)

    def test_exit_0_empty_stdout_returns_success(self):
        """Exit 0 with no stdout should return success."""
        hook = CommandHook(command="true")
        result = exec_command_hook(hook, {"tool_name": "test"})
        self.assertTrue(result.success)
        self.assertEqual(result.decision, "allow")


class TestExecCommandHookBlocking(unittest.TestCase):
    """Normal path: blocking (exit 2) command execution."""

    def test_exit_2_returns_blocking(self):
        """Exit code 2 should return blocking outcome."""
        hook = CommandHook(command="exit 2")
        result = exec_command_hook(hook, {"tool_name": "test"})
        self.assertFalse(result.success)
        self.assertEqual(result.decision, "block")
        self.assertEqual(result.outcome, "blocking")

    def test_exit_2_stderr_as_reason(self):
        """Exit 2 with stderr should use stderr as reason."""
        hook = CommandHook(command='echo "blocked reason" >&2; exit 2')
        result = exec_command_hook(hook, {"tool_name": "test"})
        self.assertIn("blocked reason", result.reason or "")

    def test_exit_1_non_blocking_error(self):
        """Exit code 1 should be non-blocking error."""
        hook = CommandHook(command="exit 1")
        result = exec_command_hook(hook, {"tool_name": "test"})
        self.assertFalse(result.success)
        self.assertEqual(result.decision, "allow")
        self.assertEqual(result.outcome, "non_blocking_error")


class TestExecCommandHookTimeout(unittest.TestCase):
    """Error path: timeout handling."""

    def test_timeout_kills_process(self):
        """A slow command should be killed after timeout."""
        hook = CommandHook(command="sleep 60", timeout=1.0)
        start = time.monotonic()
        result = exec_command_hook(hook, {"tool_name": "test"}, timeout=1.0)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 5.0, "Timeout did not kill process promptly")
        self.assertIn("timed out", (result.reason or "").lower())
        self.assertEqual(result.outcome, "cancelled")

    def test_invalid_command_returns_error(self):
        """A nonexistent command should return error, not crash."""
        hook = CommandHook(command="/nonexistent/binary/xyz123")
        result = exec_command_hook(hook, {"tool_name": "test"})
        # Should get a result (not an exception)
        self.assertIsInstance(result, HookResult)


class TestExecCommandHookAsync(unittest.TestCase):
    """Async hook detection via first-line streaming."""

    def test_async_marker_immediate_return(self):
        """Script outputting async marker on first line should return
        immediately without waiting for the full script to finish."""
        # The script echoes async marker then sleeps; we should return fast.
        hook = CommandHook(
            command='echo \'{"async": true}\'; sleep 60',
        )
        start = time.monotonic()
        result = exec_command_hook(hook, {"tool_name": "test"})
        elapsed = time.monotonic() - start

        self.assertTrue(result.success)
        self.assertEqual(result.decision, "allow")
        self.assertTrue(result.telemetry.get("async"))
        # Should return in well under 60 seconds
        self.assertLess(elapsed, 5.0, "Async hook did not return immediately")

    def test_non_json_first_line_waits_for_completion(self):
        """Non-JSON first line should wait for process completion."""
        hook = CommandHook(command='echo "not json"')
        result = exec_command_hook(hook, {"tool_name": "test"})
        # Should still succeed (exit 0)
        self.assertTrue(result.success)


class TestBuildHookEnv(unittest.TestCase):
    """Environment building tests."""

    def test_env_contains_agentloom_project_dir(self):
        env = _build_hook_env({"tool_name": "test"})
        self.assertIn("AGENTLOOM_PROJECT_DIR", env)
        self.assertEqual(env["AGENTLOOM_PROJECT_DIR"], os.getcwd())

    def test_env_contains_tool_name(self):
        env = _build_hook_env({"tool_name": "my_tool"})
        self.assertEqual(env["TOOL_NAME"], "my_tool")


class TestProcessCommandResult(unittest.TestCase):
    """Unit tests for _process_command_result helper."""

    def test_exit_0_no_stdout(self):
        hook = CommandHook(command="test")
        result = _process_command_result(hook, {}, "", "", 0)
        self.assertTrue(result.success)

    def test_exit_2_no_stdout(self):
        hook = CommandHook(command="test")
        result = _process_command_result(hook, {}, "", "error msg", 2)
        self.assertEqual(result.outcome, "blocking")
        self.assertIn("error msg", result.reason or "")

    def test_exit_3_non_blocking(self):
        hook = CommandHook(command="test")
        result = _process_command_result(hook, {}, "", "", 3)
        self.assertEqual(result.outcome, "non_blocking_error")
        self.assertEqual(result.decision, "allow")


if __name__ == "__main__":
    unittest.main()
