import unittest
from unittest.mock import MagicMock, patch



from src.tools.shell import shell_tool
from src.tools.shell import validator as validator_module


class TestShellTool(unittest.TestCase):
    def setUp(self):
        # Ensure no agent context bleeds in from prior tests
        from src.trace.task_context import clear_current_agent_id, clear_current_agent_config
        try:
            clear_current_agent_id()
        except Exception:
            pass
        try:
            clear_current_agent_config()
        except Exception:
            pass

        self._commands_patcher = patch.object(
            validator_module,
            "load_allowed_commands",
            return_value=["echo", "ls", "pwd", "cat", "python", "python3", "base64", "command"],
        )
        self._operators_patcher = patch.object(
            validator_module,
            "load_allowed_operators",
            return_value=["|", "||", "&&", ">", ">>", "<", ";"],
        )
        self._commands_patcher.start()
        self._operators_patcher.start()

    def tearDown(self):
        self._commands_patcher.stop()
        self._operators_patcher.stop()

    def test_shell_tool_runs_commands(self):
        result = shell_tool("echo 'Hello World!' && ls -l")
        print(result)
        self.assertIn("Hello World!", result)

    def test_shell_tool_single_command(self):
        result = shell_tool("pwd")
        print(result)
        self.assertTrue(result.strip())

    def test_shell_tool_blocks_disallowed_command(self):
        with self.assertRaises(ValueError):
            shell_tool("whoami")

    def test_shell_tool_pipeline_allowed(self):
        result = shell_tool("echo 'Hello' | cat")
        print(result)
        self.assertIn("Hello", result)

    def test_shell_tool_blocks_disallowed_command_in_pipeline(self):
        with self.assertRaises(ValueError):
            shell_tool("echo 'Hello' | whoami")

    def test_shell_tool_blocks_disallowed_command_not_executed(self):
        with self.assertRaises(ValueError):
            shell_tool("echo 'Hello' || whoami")

    def test_shell_tool_blocks_command_substitution_in_subshell(self):
        # $() command substitution is blocked by security checks
        with self.assertRaises(ValueError, msg="$() substitution should be blocked"):
            shell_tool("$(command -v echo || command -v cat) 'Hello'")

    def test_shell_tool_blocks_unsafe_command_builtin_usage(self):
        with self.assertRaises(ValueError):
            shell_tool("command echo 'Hello'")

    def test_shell_tool_allows_command_v_direct_usage(self):
        result = shell_tool("command -v echo")
        print(result)
        self.assertIn("echo", result)

    def test_shell_tool_allows_command_V_direct_usage(self):
        result = shell_tool("command -V echo")
        print(result)
        self.assertIn("echo", result)

    def test_shell_tool_blocks_python_resolution_via_substitution(self):
        # $() command substitution is blocked by security checks
        with self.assertRaises(ValueError, msg="$() substitution should be blocked"):
            shell_tool('$(command -v python3 || command -v python) -c "print(\'ok\')"')

    def test_shell_tool_blocks_disallowed_lookup_in_command_v_substitution(self):
        with self.assertRaises(ValueError):
            shell_tool("$(command -v whoami || command -v echo) 'Hello'")

    def test_shell_tool_blocks_disallowed_lookup_even_if_not_executed(self):
        with self.assertRaises(ValueError):
            shell_tool("$(command -v echo || command -v whoami) 'Hello'")

    def test_shell_tool_blocks_unsupported_command_v_form(self):
        with self.assertRaises(ValueError):
            shell_tool("command -v -p echo")

    def test_shell_tool_allows_base64_command(self):
        result = shell_tool("echo 'hello' | base64")
        self.assertIn("aGVsbG8", result)

    def test_shell_tool_empty_commands_returns_helpful_message(self):
        result = shell_tool("")
        self.assertIn("No shell command was provided", result)
        self.assertIn("nothing was executed", result)

    def test_shell_tool_rejects_list_command(self):
        with self.assertRaisesRegex(ValueError, "command must be a non-empty string"):
            shell_tool(["echo hi"])

    def test_shell_tool_no_output_returns_helpful_message(self):
        result = shell_tool("python3 -c 'pass'")
        self.assertIn("produced no output", result)
        self.assertIn("python3 -c 'pass'", result)

    def test_shell_tool_logs_policy_snapshot_for_successful_command(self):
        mock_audit = MagicMock()
        with patch(
            "src.tools.shell.shell_audit_log.get_shell_audit_logger",
            return_value=mock_audit,
        ):
            result = shell_tool("echo audit_policy_ok")

        self.assertIn("audit_policy_ok", result)
        mock_audit.log_effective_policy.assert_called()


if __name__ == "__main__":
    unittest.main()
