"""Tests for read-only command validation module."""

import pytest

from src.tools.shell.readonly_validation import is_read_only_command


# =========================================================================
# Normal path — read-only commands correctly identified
# =========================================================================

class TestReadOnlyCommands:
    """Verify common read-only commands are correctly classified."""

    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "cat README.md",
        "head -n 10 file.txt",
        "tail -f log.txt",
        "grep -r 'pattern' src/",
        "find . -name '*.py'",
        "wc -l file.txt",
        "stat file.txt",
        "diff file1 file2",
        "tree src/",
        "du -sh .",
        "pwd",
        "echo hello",
        "git status",
        "git log --oneline -5",
        "git diff HEAD",
        "jq '.key' data.json",
    ])
    def test_read_only_commands(self, cmd):
        assert is_read_only_command(cmd) is True, f"Expected '{cmd}' to be read-only"

    def test_pipeline_all_readonly(self):
        assert is_read_only_command("cat file.txt | grep pattern | wc -l") is True

    def test_chain_all_readonly(self):
        assert is_read_only_command("ls -la && cat README.md") is True

    def test_empty_is_readonly(self):
        assert is_read_only_command("") is True
        assert is_read_only_command("   ") is True


# =========================================================================
# Abnormal path — write commands correctly classified
# =========================================================================

class TestWriteCommands:
    """Verify write commands are NOT classified as read-only."""

    @pytest.mark.parametrize("cmd", [
        "rm file.txt",
        "mv old.txt new.txt",
        "cp src dst",
        "mkdir new_dir",
        "chmod 755 script.sh",
        "touch new_file.txt",
        "git push origin main",
        "git commit -m 'msg'",
        "git add .",
        "git reset --hard HEAD",
    ])
    def test_write_commands_not_readonly(self, cmd):
        assert is_read_only_command(cmd) is False, f"Expected '{cmd}' to be NOT read-only"

    def test_sed_inplace_not_readonly(self):
        assert is_read_only_command("sed -i 's/old/new/' file.txt") is False

    def test_output_redirect_not_readonly(self):
        assert is_read_only_command("echo hello > file.txt") is False
        assert is_read_only_command("cat a >> b") is False


# =========================================================================
# Boundary / edge cases
# =========================================================================

class TestEdgeCases:
    """Boundary conditions and special patterns."""

    def test_sed_print_is_readonly(self):
        """sed -n 'Np' (line printing) is read-only."""
        assert is_read_only_command("sed -n '1,10p' file.txt") is True

    def test_pipeline_with_one_write(self):
        """One write segment makes whole pipeline non-read-only."""
        assert is_read_only_command("cat file | rm something") is False

    def test_unknown_command_conservative(self):
        """Unknown commands default to NOT read-only (conservative)."""
        assert is_read_only_command("my_custom_command args") is False

    def test_git_unknown_subcommand_conservative(self):
        """Unknown git subcommands default to NOT read-only."""
        assert is_read_only_command("git stash") is False

    def test_absolute_path_command(self):
        """Commands with absolute paths should be normalized."""
        assert is_read_only_command("/usr/bin/cat file.txt") is True

    def test_env_prefix_stripped(self):
        """Env var assignments before command should be ignored."""
        assert is_read_only_command("NODE_ENV=test cat file.txt") is True

    def test_awk_is_readonly(self):
        """awk without output redirect is read-only."""
        assert is_read_only_command("awk '{print $1}' file") is True

    def test_stderr_redirect_is_readonly(self):
        """2> redirect (stderr only) should not be confused with stdout redirect."""
        assert is_read_only_command("ls 2>/dev/null") is True
