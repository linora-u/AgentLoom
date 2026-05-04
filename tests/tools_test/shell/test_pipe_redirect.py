"""Tests for pipe redirect normalization."""

import pytest

from src.tools.shell.pipe_redirect import (
    rearrange_pipe_command,
    _extract_unquoted,
    _split_on_unquoted_pipe,
)


class TestRearrangePipeCommand:
    """Normal path: basic pipe rearrangement."""

    def test_simple_pipe(self):
        result = rearrange_pipe_command("rg foo | wc -l")
        assert result == "rg foo < /dev/null | wc -l"

    def test_pipe_with_args(self):
        result = rearrange_pipe_command("grep -r pattern src/ | head -20")
        assert result == "grep -r pattern src/ < /dev/null | head -20"

    def test_multi_pipe(self):
        result = rearrange_pipe_command("cat file | sort | uniq -c")
        assert result == "cat file < /dev/null | sort | uniq -c"

    def test_pipe_with_env_prefix(self):
        result = rearrange_pipe_command("LANG=C sort data.txt | head")
        assert result == "LANG=C sort data.txt < /dev/null | head"

    def test_preserves_quoted_pipe(self):
        # Pipe inside quotes should not trigger split.
        result = rearrange_pipe_command('echo "a|b" | wc')
        assert "< /dev/null" in result


class TestRearrangeSkipCases:
    """Commands that should be returned unchanged."""

    def test_no_pipe(self):
        cmd = "echo hello"
        assert rearrange_pipe_command(cmd) == cmd

    def test_empty_command(self):
        assert rearrange_pipe_command("") == ""
        assert rearrange_pipe_command("  ") == "  "

    def test_skip_command_substitution(self):
        cmd = "echo $(ls) | wc -l"
        assert rearrange_pipe_command(cmd) == cmd

    def test_skip_backtick(self):
        cmd = "echo `ls` | wc -l"
        assert rearrange_pipe_command(cmd) == cmd

    def test_skip_parameter_expansion(self):
        cmd = "echo ${HOME} | cat"
        assert rearrange_pipe_command(cmd) == cmd

    def test_skip_arithmetic_expansion(self):
        cmd = "echo $[1+1] | cat"
        assert rearrange_pipe_command(cmd) == cmd

    def test_skip_process_substitution_input(self):
        cmd = "diff <(ls dir1) <(ls dir2) | head"
        assert rearrange_pipe_command(cmd) == cmd

    def test_skip_process_substitution_output(self):
        cmd = "tee >(wc -l) | head"
        assert rearrange_pipe_command(cmd) == cmd

    def test_skip_for_loop(self):
        cmd = "for x in 1 2 3; do echo $x | cat; done"
        assert rearrange_pipe_command(cmd) == cmd

    def test_skip_while_loop(self):
        cmd = "while read line; do echo $line | wc; done"
        assert rearrange_pipe_command(cmd) == cmd

    def test_skip_if_statement(self):
        cmd = "if true; then echo yes | cat; fi"
        assert rearrange_pipe_command(cmd) == cmd

    def test_skip_case_statement(self):
        cmd = "case x in a) echo y | cat ;; esac"
        assert rearrange_pipe_command(cmd) == cmd

    def test_skip_newline(self):
        cmd = "echo hello\necho world | cat"
        assert rearrange_pipe_command(cmd) == cmd

    def test_skip_existing_stdin_redirect(self):
        cmd = "cmd < input.txt | other"
        assert rearrange_pipe_command(cmd) == cmd

    def test_skip_dollar_brace(self):
        cmd = "echo ${VAR:-default} | head"
        assert rearrange_pipe_command(cmd) == cmd


class TestRearrangeBoundary:
    """Boundary and edge cases."""

    def test_or_operator_not_split(self):
        # || should not be treated as a pipe.
        cmd = "cmd1 || cmd2"
        assert rearrange_pipe_command(cmd) == cmd

    def test_or_followed_by_pipe(self):
        cmd = "cmd1 || cmd2 | head"
        result = rearrange_pipe_command(cmd)
        assert "< /dev/null" in result
        # The first segment should include cmd1 || cmd2.
        assert result.startswith("cmd1 || cmd2")

    def test_single_pipe_only(self):
        result = rearrange_pipe_command("|")
        # Degenerate — first segment is empty, second is empty.
        assert "< /dev/null" in result

    def test_pipe_with_single_quoted_dollar(self):
        # $ inside single quotes should NOT trigger skip.
        cmd = "echo '$HOME' | wc"
        result = rearrange_pipe_command(cmd)
        assert "< /dev/null" in result

    def test_none_command(self):
        # Should not crash on None.
        assert rearrange_pipe_command(None) is None


class TestExtractUnquoted:
    """Tests for the _extract_unquoted helper."""

    def test_all_unquoted(self):
        assert _extract_unquoted("echo hello") == "echo hello"

    def test_single_quoted_removed(self):
        result = _extract_unquoted("echo '$HOME'")
        assert "$HOME" not in result

    def test_double_quoted_removed(self):
        result = _extract_unquoted('echo "$HOME"')
        assert "$HOME" not in result

    def test_escaped_char(self):
        result = _extract_unquoted("echo \\$HOME")
        assert "$" not in result


class TestSplitOnUnquotedPipe:
    """Tests for the _split_on_unquoted_pipe helper."""

    def test_simple_split(self):
        segments = _split_on_unquoted_pipe("a | b | c")
        assert len(segments) == 3
        assert segments[0].strip() == "a"
        assert segments[1].strip() == "b"
        assert segments[2].strip() == "c"

    def test_no_pipe(self):
        segments = _split_on_unquoted_pipe("echo hello")
        assert segments == ["echo hello"]

    def test_quoted_pipe_not_split(self):
        segments = _split_on_unquoted_pipe('echo "a|b" | wc')
        assert len(segments) == 2

    def test_or_operator_not_split(self):
        segments = _split_on_unquoted_pipe("cmd1 || cmd2")
        assert len(segments) == 1
        assert "||" in segments[0]

    def test_unbalanced_quotes_returns_none(self):
        result = _split_on_unquoted_pipe("echo 'unclosed | wc")
        assert result is None
