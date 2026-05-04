"""Tests for enriched security denial messages.

Validates that security check failures and path violations include
alternative guidance in their error messages, helping the AI choose
the right alternative tool or approach.
"""

import os
import pytest
from unittest.mock import patch

from src.tools.shell.security import (
    SecurityCheckResult,
    check_command_security,
    validate_command_security,
)


# ---------------------------------------------------------------------------
# SecurityCheckResult.alternative field
# ---------------------------------------------------------------------------


class TestSecurityCheckResultAlternative:
    """Verify the alternative field on SecurityCheckResult."""

    def test_alternative_default_empty(self):
        """alternative defaults to empty string."""
        result = SecurityCheckResult(is_safe=True, check_id="test", message="ok")
        assert result.alternative == ""

    def test_alternative_set_explicitly(self):
        """alternative can be set explicitly."""
        result = SecurityCheckResult(
            is_safe=False,
            check_id="test",
            message="blocked",
            alternative="Use edit_file instead",
        )
        assert result.alternative == "Use edit_file instead"


# ---------------------------------------------------------------------------
# Command substitution -> error includes alternative
# ---------------------------------------------------------------------------


class TestCommandSubstitutionAlternative:

    @patch("src.tools.shell.security._load_enabled_checks", return_value={})
    def test_dollar_paren_has_alternative(self, _mock):
        """$() substitution error includes write_markdown_file suggestion."""
        failures = check_command_security("echo $(whoami)")
        assert len(failures) >= 1
        cmd_sub = [f for f in failures if f.check_id == "command_substitution"]
        assert cmd_sub
        assert cmd_sub[0].alternative
        assert "write_markdown_file" in cmd_sub[0].alternative or "edit_file" in cmd_sub[0].alternative

    @patch("src.tools.shell.security._load_enabled_checks", return_value={})
    def test_backtick_has_alternative(self, _mock):
        """Backtick substitution error includes alternative."""
        failures = check_command_security("echo `whoami`")
        cmd_sub = [f for f in failures if f.check_id == "command_substitution"]
        assert cmd_sub
        assert cmd_sub[0].alternative != ""

    @patch("src.tools.shell.security._load_enabled_checks", return_value={})
    def test_validate_raises_with_alternative(self, _mock):
        """validate_command_security() includes 'Suggested alternative:' in error."""
        with pytest.raises(ValueError, match="Suggested alternative:"):
            validate_command_security("echo $(whoami)")


# ---------------------------------------------------------------------------
# Environment injection -> error includes alternative
# ---------------------------------------------------------------------------


class TestEnvInjectionAlternative:

    @patch("src.tools.shell.security._load_enabled_checks", return_value={})
    def test_env_injection_has_alternative(self, _mock):
        """Dangerous env var error includes YAML config suggestion."""
        failures = check_command_security("LD_PRELOAD=/lib/evil.so ls")
        env_inj = [f for f in failures if f.check_id == "env_injection"]
        assert env_inj
        assert "YAML" in env_inj[0].alternative or "config" in env_inj[0].alternative


# ---------------------------------------------------------------------------
# Dangerous shell prefix -> error includes alternative
# ---------------------------------------------------------------------------


class TestDangerousShellPrefixAlternative:

    @patch("src.tools.shell.security._load_enabled_checks", return_value={})
    def test_sudo_has_alternative(self, _mock):
        """sudo error includes 'directly without sudo' suggestion."""
        failures = check_command_security("sudo ls -la")
        prefix = [f for f in failures if f.check_id == "dangerous_shell_prefix"]
        assert prefix
        assert "directly" in prefix[0].alternative.lower() or "without" in prefix[0].alternative.lower()


# ---------------------------------------------------------------------------
# Destructive patterns -> error includes alternative
# ---------------------------------------------------------------------------


class TestDestructivePatternAlternative:

    @patch("src.tools.shell.security._load_enabled_checks", return_value={})
    def test_rm_rf_root_has_alternative(self, _mock):
        """rm -rf / error includes targeted operations suggestion."""
        failures = check_command_security("rm -rf /")
        destr = [f for f in failures if f.check_id == "destructive_patterns"]
        assert destr
        assert "targeted" in destr[0].alternative.lower() or "specific" in destr[0].alternative.lower()

    @patch("src.tools.shell.security._load_enabled_checks", return_value={})
    def test_git_reset_hard_has_alternative(self, _mock):
        """git reset --hard error includes alternative."""
        failures = check_command_security("git reset --hard HEAD~5")
        destr = [f for f in failures if f.check_id == "destructive_patterns"]
        assert destr
        assert destr[0].alternative != ""


# ---------------------------------------------------------------------------
# Process substitution -> error includes alternative
# ---------------------------------------------------------------------------


class TestProcessSubstitutionAlternative:

    @patch("src.tools.shell.security._load_enabled_checks", return_value={})
    def test_process_sub_has_alternative(self, _mock):
        """<() process substitution error includes file-based alternative."""
        failures = check_command_security("diff <(ls dir1) <(ls dir2)")
        proc_sub = [f for f in failures if f.check_id == "process_substitution"]
        assert proc_sub
        assert "file" in proc_sub[0].alternative.lower() or "write" in proc_sub[0].alternative.lower()


# ---------------------------------------------------------------------------
# Checks without alternative -> no extra line
# ---------------------------------------------------------------------------


class TestNoAlternativeChecks:

    @patch("src.tools.shell.security._load_enabled_checks", return_value={})
    def test_control_characters_no_alternative(self, _mock):
        """Control characters check has empty alternative."""
        failures = check_command_security("echo \x07hello")
        ctrl = [f for f in failures if f.check_id == "control_characters"]
        assert ctrl
        assert ctrl[0].alternative == ""

    @patch("src.tools.shell.security._load_enabled_checks", return_value={})
    def test_validate_no_alternative_no_suggested_line(self, _mock):
        """When alternative is empty, error message has no 'Suggested alternative:'."""
        with pytest.raises(ValueError) as exc_info:
            validate_command_security("echo \x07hello")
        assert "Suggested alternative:" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Error message format
# ---------------------------------------------------------------------------


class TestErrorMessageFormat:

    @patch("src.tools.shell.security._load_enabled_checks", return_value={})
    def test_no_duplicate_punctuation(self, _mock):
        """Error message doesn't have double periods or trailing issues."""
        with pytest.raises(ValueError) as exc_info:
            validate_command_security("echo $(whoami)")
        msg = str(exc_info.value)
        assert ".." not in msg
        assert msg.strip() == msg  # no leading/trailing whitespace

    @patch("src.tools.shell.security._load_enabled_checks", return_value={})
    def test_newline_separates_alternative(self, _mock):
        """Alternative text is on a separate line."""
        with pytest.raises(ValueError) as exc_info:
            validate_command_security("echo $(whoami)")
        msg = str(exc_info.value)
        lines = msg.split("\n")
        assert len(lines) >= 2  # at least message + alternative


# ---------------------------------------------------------------------------
# Path validation guidance (integration-level, requires allowed_roots setup)
# ---------------------------------------------------------------------------


class TestPathValidationGuidance:

    def test_path_error_includes_guidance_suffix(self):
        """Path violation error messages include tool alternative guidance."""
        from src.tools.shell.path_validation import _PATH_GUIDANCE
        assert "read_file" in _PATH_GUIDANCE
        assert "grep_search" in _PATH_GUIDANCE
