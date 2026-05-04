"""Tests for shell command exit code semantic interpretation."""

import pytest

from src.tools.shell.command_semantics import (
    interpret_exit_code,
    is_silent_command,
    is_search_or_read_command,
    _extract_last_command_name,
    ExitCodeInterpretation,
)


# =========================================================================
# Normal path — correct interpretation of known exit codes
# =========================================================================

class TestExitCodeInterpretation:
    """Verify exit codes are interpreted with command-specific semantics."""

    def test_grep_match_found(self):
        result = interpret_exit_code("grep pattern file.txt", 0)
        assert result.is_error is False
        assert result.message is None

    def test_grep_no_match(self):
        result = interpret_exit_code("grep nonexistent file.txt", 1)
        assert result.is_error is False
        assert result.message == "No matches found"

    def test_grep_error(self):
        result = interpret_exit_code("grep pattern", 2)
        assert result.is_error is True

    def test_rg_no_match(self):
        """ripgrep has same semantics as grep."""
        result = interpret_exit_code("rg pattern src/", 1)
        assert result.is_error is False
        assert "No matches" in result.message

    def test_diff_no_differences(self):
        result = interpret_exit_code("diff file1 file2", 0)
        assert result.is_error is False

    def test_diff_has_differences(self):
        result = interpret_exit_code("diff file1 file2", 1)
        assert result.is_error is False
        assert "differ" in result.message.lower()

    def test_diff_error(self):
        result = interpret_exit_code("diff file1 nonexistent", 2)
        assert result.is_error is True

    def test_find_success(self):
        result = interpret_exit_code("find . -name '*.py'", 0)
        assert result.is_error is False

    def test_find_partial(self):
        result = interpret_exit_code("find /root -name '*'", 1)
        assert result.is_error is False
        assert "inaccessible" in result.message.lower()

    def test_test_true(self):
        result = interpret_exit_code("test -f file.txt", 0)
        assert result.is_error is False

    def test_test_false(self):
        result = interpret_exit_code("test -f nonexistent", 1)
        assert result.is_error is False
        assert "false" in result.message.lower()


# =========================================================================
# Abnormal path — default behavior for unknown commands
# =========================================================================

class TestDefaultSemantics:
    """Verify unknown commands use default (non-zero = error) semantics."""

    def test_unknown_command_zero_ok(self):
        result = interpret_exit_code("mycommand", 0)
        assert result.is_error is False

    def test_unknown_command_nonzero_error(self):
        result = interpret_exit_code("python script.py", 1)
        assert result.is_error is True
        assert "exit code 1" in result.message

    def test_unknown_command_high_exit_code(self):
        result = interpret_exit_code("make build", 137)
        assert result.is_error is True
        assert "137" in result.message


# =========================================================================
# Boundary / edge cases
# =========================================================================

class TestEdgeCases:
    """Edge cases: pipelines, env vars, absolute paths."""

    def test_pipeline_uses_last_command(self):
        """Last command in pipeline determines exit code semantics."""
        result = interpret_exit_code("cat file.txt | grep pattern", 1)
        assert result.is_error is False
        assert "No matches" in result.message

    def test_command_with_env_var(self):
        result = interpret_exit_code("NODE_ENV=test grep pattern file", 1)
        assert result.is_error is False

    def test_absolute_path_command(self):
        result = interpret_exit_code("/usr/bin/grep pattern file", 1)
        assert result.is_error is False

    def test_chained_command_last_wins(self):
        result = interpret_exit_code("echo hello && diff a b", 1)
        assert result.is_error is False

    def test_extract_last_command_simple(self):
        assert _extract_last_command_name("ls -la") == "ls"

    def test_extract_last_command_pipeline(self):
        assert _extract_last_command_name("cat f | grep p") == "grep"

    def test_extract_last_command_chain(self):
        assert _extract_last_command_name("cd src && make") == "make"


class TestSilentCommand:
    """Verify silent command detection."""

    def test_mv_is_silent(self):
        assert is_silent_command("mv a b") is True

    def test_rm_is_silent(self):
        assert is_silent_command("rm file") is True

    def test_echo_is_not_silent(self):
        assert is_silent_command("echo hello") is False

    def test_ls_is_not_silent(self):
        assert is_silent_command("ls") is False

    def test_empty_is_not_silent(self):
        assert is_silent_command("") is False


class TestSearchOrReadCommand:
    """Verify search/read classification."""

    def test_grep_is_search(self):
        assert is_search_or_read_command("grep pattern file") is True

    def test_cat_is_read(self):
        assert is_search_or_read_command("cat file.txt") is True

    def test_ls_is_read(self):
        assert is_search_or_read_command("ls -la") is True

    def test_rm_is_not_read(self):
        assert is_search_or_read_command("rm file") is False

    def test_pipeline_all_read(self):
        assert is_search_or_read_command("cat file | grep pattern") is True

    def test_pipeline_with_write(self):
        assert is_search_or_read_command("cat file | rm something") is False
