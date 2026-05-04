"""Tests for security policy summary generation.

Validates that policy_summary generates correct structured text
for tool descriptions and environment prompts, reading from the
same config sources as enforcement.
"""

import pytest
from unittest.mock import patch, MagicMock

from src.lib.permissions.policy_summary import (
    SECURITY_CHECK_DESCRIPTIONS,
    DENIAL_BEHAVIOR_TEXT,
    SECURITY_BEHAVIOR_TEXT,
    build_shell_security_section,
    build_security_behavior_section,
    _get_active_check_ids,
    _load_enabled_checks,
)


# ---------------------------------------------------------------------------
# build_shell_security_section
# ---------------------------------------------------------------------------


class TestBuildShellSecuritySection:
    """Tests for the shell_tool description security section builder."""

    @patch("src.lib.permissions.policy_summary.get_allowed_directories")
    @patch("src.lib.permissions.policy_summary._load_enabled_checks")
    def test_default_config_returns_non_empty(self, mock_checks, mock_dirs):
        """With default config (all checks enabled, some dirs), output is non-empty."""
        mock_dirs.return_value = ["/workspace/project"]
        mock_checks.return_value = {}
        result = build_shell_security_section()
        assert result
        assert "Security sandbox" in result

    @patch("src.lib.permissions.policy_summary.get_allowed_directories")
    @patch("src.lib.permissions.policy_summary._load_enabled_checks")
    def test_all_checks_enabled_all_descriptions_present(self, mock_checks, mock_dirs):
        """When all checks are enabled, all descriptions appear in output."""
        mock_dirs.return_value = ["/workspace"]
        mock_checks.return_value = {}  # empty overrides = all default to True
        result = build_shell_security_section()
        for check_id, desc in SECURITY_CHECK_DESCRIPTIONS.items():
            assert desc in result, f"Missing description for '{check_id}'"

    @patch("src.lib.permissions.policy_summary.get_allowed_directories")
    @patch("src.lib.permissions.policy_summary._load_enabled_checks")
    def test_all_checks_disabled_no_restrictions_section(self, mock_checks, mock_dirs):
        """When all checks are disabled, no active restrictions section."""
        mock_dirs.return_value = []
        # Explicitly disable all checks
        mock_checks.return_value = {k: False for k in SECURITY_CHECK_DESCRIPTIONS}
        result = build_shell_security_section()
        assert result == ""

    @patch("src.lib.permissions.policy_summary.get_allowed_directories")
    @patch("src.lib.permissions.policy_summary._load_enabled_checks")
    def test_custom_allowed_directories_all_listed(self, mock_checks, mock_dirs):
        """All custom allowed directories appear in the output."""
        dirs = ["/workspace/project", "/data/shared", "/tmp/sandbox"]
        mock_dirs.return_value = dirs
        mock_checks.return_value = {}
        result = build_shell_security_section()
        for d in dirs:
            assert d in result, f"Directory '{d}' not listed in output"

    @patch("src.lib.permissions.policy_summary.get_allowed_directories")
    @patch("src.lib.permissions.policy_summary._load_enabled_checks")
    def test_workspace_root_only(self, mock_checks, mock_dirs):
        """Single workspace root directory is listed."""
        mock_dirs.return_value = ["/home/user/project"]
        mock_checks.return_value = {}
        result = build_shell_security_section()
        assert "/home/user/project" in result
        assert "Allowed directories" in result

    @patch("src.lib.permissions.policy_summary.get_allowed_directories")
    @patch("src.lib.permissions.policy_summary._load_enabled_checks")
    def test_denial_behavior_included_when_restrictions_exist(self, mock_checks, mock_dirs):
        """Denial behavior text is included when there are any restrictions."""
        mock_dirs.return_value = ["/workspace"]
        mock_checks.return_value = {}
        result = build_shell_security_section()
        assert "Do NOT retry the same command" in result

    @patch("src.lib.permissions.policy_summary.get_allowed_directories")
    @patch("src.lib.permissions.policy_summary._load_enabled_checks")
    def test_single_source_of_truth_directories(self, mock_checks, mock_dirs):
        """Output directories match exactly what get_allowed_directories returns."""
        expected_dirs = ["/a/b/c", "/d/e/f"]
        mock_dirs.return_value = expected_dirs
        mock_checks.return_value = {}
        result = build_shell_security_section()
        for d in expected_dirs:
            assert d in result
        # Verify no extra phantom directories
        assert "/phantom" not in result

    @patch("src.lib.permissions.policy_summary.get_allowed_directories")
    @patch("src.lib.permissions.policy_summary._load_enabled_checks")
    def test_partial_checks_only_enabled_shown(self, mock_checks, mock_dirs):
        """Only enabled checks appear in output."""
        mock_dirs.return_value = ["/workspace"]
        mock_checks.return_value = {
            "command_substitution": True,
            "env_injection": False,
            "control_characters": True,
            "dangerous_shell_prefix": False,
            "zsh_dangerous_commands": False,
            "incomplete_commands": False,
            "process_substitution": False,
            "ifs_injection": False,
            "parameter_expansion": False,
            "destructive_patterns": False,
        }
        result = build_shell_security_section()
        assert SECURITY_CHECK_DESCRIPTIONS["command_substitution"] in result
        assert SECURITY_CHECK_DESCRIPTIONS["control_characters"] in result
        assert SECURITY_CHECK_DESCRIPTIONS["env_injection"] not in result
        assert SECURITY_CHECK_DESCRIPTIONS["dangerous_shell_prefix"] not in result

    @patch("src.lib.permissions.policy_summary.get_allowed_directories")
    @patch("src.lib.permissions.policy_summary._load_enabled_checks")
    def test_get_allowed_directories_exception_handled(self, mock_checks, mock_dirs):
        """If get_allowed_directories raises, function handles gracefully."""
        mock_dirs.side_effect = RuntimeError("config unavailable")
        mock_checks.return_value = {}
        # Should not raise — returns "" or partial output
        result = build_shell_security_section()
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# build_security_behavior_section
# ---------------------------------------------------------------------------


class TestBuildSecurityBehaviorSection:
    """Tests for the environment prompt security behavior section builder."""

    def test_returns_static_denial_behavior(self):
        """Returns static text teaching denial response."""
        result = build_security_behavior_section()
        assert result == SECURITY_BEHAVIOR_TEXT

    def test_contains_key_rules(self):
        """Key behavioral rules are present in the output."""
        result = build_security_behavior_section()
        assert "Do NOT retry" in result
        assert "alternative tools" in result
        assert "report the limitation" in result

    def test_return_type_is_string(self):
        """Always returns a string."""
        result = build_security_behavior_section()
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# _get_active_check_ids
# ---------------------------------------------------------------------------


class TestGetActiveCheckIds:

    @patch("src.lib.permissions.policy_summary._load_enabled_checks")
    def test_all_default_enabled(self, mock_load):
        """With empty overrides, all checks default to enabled."""
        mock_load.return_value = {}
        active = _get_active_check_ids()
        assert set(active) == set(SECURITY_CHECK_DESCRIPTIONS.keys())

    @patch("src.lib.permissions.policy_summary._load_enabled_checks")
    def test_all_explicitly_disabled(self, mock_load):
        """With all checks explicitly disabled, returns empty list."""
        mock_load.return_value = {k: False for k in SECURITY_CHECK_DESCRIPTIONS}
        active = _get_active_check_ids()
        assert active == []

    @patch("src.lib.permissions.policy_summary._load_enabled_checks")
    def test_partial_disable(self, mock_load):
        """Partially disabling checks returns only enabled ones."""
        mock_load.return_value = {
            "command_substitution": False,
            "env_injection": False,
        }
        active = _get_active_check_ids()
        assert "command_substitution" not in active
        assert "env_injection" not in active
        assert "control_characters" in active
        assert "destructive_patterns" in active


# ---------------------------------------------------------------------------
# Constants validation
# ---------------------------------------------------------------------------


class TestConstants:

    def test_security_check_descriptions_has_all_expected_keys(self):
        """All 10 security check IDs are present."""
        expected = {
            "command_substitution", "env_injection", "control_characters",
            "dangerous_shell_prefix", "zsh_dangerous_commands",
            "incomplete_commands", "process_substitution", "ifs_injection",
            "parameter_expansion", "destructive_patterns",
        }
        assert set(SECURITY_CHECK_DESCRIPTIONS.keys()) == expected

    def test_denial_behavior_text_is_non_empty(self):
        assert len(DENIAL_BEHAVIOR_TEXT) > 50

    def test_security_behavior_text_is_non_empty(self):
        assert len(SECURITY_BEHAVIOR_TEXT) > 50

    def test_descriptions_are_strings(self):
        """All descriptions are non-empty strings."""
        for check_id, desc in SECURITY_CHECK_DESCRIPTIONS.items():
            assert isinstance(desc, str), f"{check_id} description is not a string"
            assert len(desc) > 10, f"{check_id} description is too short"
