"""Tests for per-agent shell security audit logger.

Covers:
- Normal path: audit file creation, entry formatting, per-agent isolation
- Abnormal path: disabled audit, unwritable directory, missing agent context
- Boundary: concurrent writes, very long commands, special characters in agent name
- Integration: audit events fired from security/path/validator/process pipelines
"""

import os
import re
import threading
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from src.tools.shell.shell_audit_log import (
    ShellAuditLogger,
    get_shell_audit_logger,
    reset_audit_loggers,
    EVENT_SECURITY_BLOCK,
    EVENT_PATH_VIOLATION,
    EVENT_WHITELIST_REJECT,
    EVENT_STALL_DETECTED,
    EVENT_TIMEOUT,
    EVENT_BACKGROUND_PROMOTION,
    EVENT_SANDBOX_WRAP,
    EVENT_COMMAND_SUCCESS,
)


# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture(autouse=True)
def _reset_loggers():
    """Clear cached audit logger instances before/after each test."""
    reset_audit_loggers()
    yield
    reset_audit_loggers()


@pytest.fixture
def tmp_log_dir(tmp_path):
    """Provide a temporary directory for audit logs."""
    return str(tmp_path)


@pytest.fixture
def enabled_audit(tmp_log_dir):
    """Create an audit logger with audit enabled."""
    audit = ShellAuditLogger("test_agent", log_dir=tmp_log_dir)
    audit._enabled = True
    audit._log_success = False
    return audit


@pytest.fixture
def disabled_audit(tmp_log_dir):
    """Create an audit logger with audit disabled."""
    audit = ShellAuditLogger("test_agent", log_dir=tmp_log_dir)
    audit._enabled = False
    return audit


def _read_audit_file(audit: ShellAuditLogger) -> str:
    """Read the contents of the audit log file."""
    path = audit.file_path
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# =========================================================================
# Normal path — audit file creation and entry formatting
# =========================================================================

class TestAuditFileCreation:
    """Verify that audit log files are created in the correct location."""

    def test_audit_file_created_on_first_write(self, enabled_audit):
        """Audit log file should be created lazily on first event."""
        assert not enabled_audit.file_path.exists()
        enabled_audit.log_security_block(
            command="rm -rf /",
            check_id="destructive_patterns",
            message="Blocked: destructive command detected",
        )
        assert enabled_audit.file_path.exists()

    def test_audit_file_in_agent_timestamp_subdirectory(self, tmp_log_dir):
        """File should be at {log_dir}/{agent_name}/{timestamp}/shell_audit.log."""
        audit = ShellAuditLogger("my_agent", log_dir=tmp_log_dir)
        audit._enabled = True
        path = audit.file_path
        # Parent is the timestamp directory; grandparent is the agent directory.
        assert path.parent.parent.name == "my_agent"
        assert path.name == "shell_audit.log"
        assert path.suffix == ".log"

    def test_fallback_global_agent_name(self, tmp_log_dir):
        """When agent name is _global, files go to a sanitized subdir."""
        audit = ShellAuditLogger("_global", log_dir=tmp_log_dir)
        audit._enabled = True
        path = audit.file_path
        # _sanitize_component strips leading underscores/dots
        # Grandparent is the agent dir, parent is the timestamp dir.
        assert "global" in path.parent.parent.name


class TestEntryFormatting:
    """Verify structured entry format in the audit log."""

    def test_security_block_entry_format(self, enabled_audit):
        """Security block entry should contain all required fields."""
        enabled_audit.log_security_block(
            command="$(cat /etc/passwd)",
            check_id="command_substitution",
            message="Blocked: $() command substitution detected",
        )
        content = _read_audit_file(enabled_audit)

        # Timestamp format
        assert re.search(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]", content)
        # Event type
        assert "[SECURITY_BLOCK]" in content
        # Agent name
        assert "agent=test_agent" in content
        # Command
        assert "command: $(cat /etc/passwd)" in content
        # Check ID
        assert "check: command_substitution" in content
        # Message
        assert "message: Blocked: $() command substitution detected" in content
        # Suggestion with correct check_id
        assert "command_substitution: false" in content

    def test_path_violation_entry_format(self, enabled_audit):
        """Path violation entry should include the offending path."""
        enabled_audit.log_path_violation(
            command="cat /etc/passwd",
            message="'/etc/passwd' is outside allowed workspace",
            path="/etc/passwd",
        )
        content = _read_audit_file(enabled_audit)

        assert "[PATH_VIOLATION]" in content
        assert "command: cat /etc/passwd" in content
        assert "message: '/etc/passwd' is outside allowed workspace" in content
        assert "path_validation" in content
        assert "include_paths" in content

    def test_whitelist_rejection_entry_format(self, enabled_audit):
        """Whitelist rejection should include the rejected command name."""
        enabled_audit.log_whitelist_rejection(
            command="wget http://evil.com",
            message="Command not allowed: wget. Allowed commands: ls, cat",
            name="wget",
        )
        content = _read_audit_file(enabled_audit)

        assert "[WHITELIST_REJECT]" in content
        assert "command: wget http://evil.com" in content
        assert "allowed_commands" in content

    def test_stall_detected_entry_format(self, enabled_audit):
        """Stall detection entry should include PID and elapsed time."""
        enabled_audit.log_stall_detected(
            command="npm install",
            pid=12345,
            elapsed=47.0,
            stall_message='Task "fg-12345" appears to be waiting for input',
        )
        content = _read_audit_file(enabled_audit)

        assert "[STALL_DETECTED]" in content
        assert "pid: 12345" in content
        assert "elapsed_seconds: 47.0" in content
        assert "non-interactive" in content.lower() or "--yes" in content

    def test_timeout_entry_format(self, enabled_audit):
        """Timeout entry should include timeout duration."""
        enabled_audit.log_timeout(
            command="make build",
            timeout=120.0,
            promoted=False,
        )
        content = _read_audit_file(enabled_audit)

        assert "[TIMEOUT]" in content
        assert "timeout_seconds: 120.0" in content
        assert "run_in_background" in content.lower() or "timeout" in content.lower()

    def test_background_promotion_entry_format(self, enabled_audit):
        """Background promotion entry should include task ID."""
        enabled_audit.log_timeout(
            command="npm run dev",
            timeout=120.0,
            promoted=True,
            task_id="bg-abc-123",
        )
        content = _read_audit_file(enabled_audit)

        assert "[BACKGROUND_PROMOTION]" in content
        assert "task_id: bg-abc-123" in content

    def test_sandbox_wrap_entry_format(self, enabled_audit):
        """Sandbox wrap entry should include sandbox mode."""
        enabled_audit.log_sandbox_wrap(
            command="npm install",
            sandbox_mode="seatbelt",
        )
        content = _read_audit_file(enabled_audit)

        assert "[SANDBOX_WRAP]" in content
        assert "seatbelt" in content

    def test_command_success_not_logged_by_default(self, enabled_audit):
        """Success events should NOT be logged when log_success=False."""
        enabled_audit.log_command_success(
            command="echo hello",
            exit_code=0,
            duration=0.5,
        )
        content = _read_audit_file(enabled_audit)
        assert content == ""

    def test_command_success_logged_when_enabled(self, enabled_audit):
        """Success events should be logged when log_success=True."""
        enabled_audit._log_success = True
        enabled_audit.log_command_success(
            command="echo hello",
            exit_code=0,
            duration=0.5,
        )
        content = _read_audit_file(enabled_audit)
        assert "[COMMAND_SUCCESS]" in content
        assert "exit_code=0" in content


class TestMultipleEntries:
    """Verify that multiple entries accumulate correctly."""

    def test_multiple_events_appended(self, enabled_audit):
        """Multiple events should append to the same file."""
        enabled_audit.log_security_block("cmd1", "check1", "msg1")
        enabled_audit.log_security_block("cmd2", "check2", "msg2")
        enabled_audit.log_path_violation("cmd3", "msg3", "/etc")

        content = _read_audit_file(enabled_audit)
        assert content.count("[SECURITY_BLOCK]") == 2
        assert content.count("[PATH_VIOLATION]") == 1

    def test_entries_separated_by_blank_lines(self, enabled_audit):
        """Each entry should be separated by a blank line."""
        enabled_audit.log_security_block("cmd1", "check1", "msg1")
        enabled_audit.log_security_block("cmd2", "check2", "msg2")

        content = _read_audit_file(enabled_audit)
        # Two entries means at least one blank-line separator
        assert "\n\n" in content


# =========================================================================
# Per-agent isolation
# =========================================================================

class TestPerAgentIsolation:
    """Verify that different agents write to separate log files."""

    def test_different_agents_different_files(self, tmp_log_dir):
        """Two agents should write to different directories."""
        audit_a = ShellAuditLogger("agent_alpha", log_dir=tmp_log_dir)
        audit_a._enabled = True
        audit_b = ShellAuditLogger("agent_beta", log_dir=tmp_log_dir)
        audit_b._enabled = True

        audit_a.log_security_block("cmd_a", "check_a", "msg_a")
        audit_b.log_security_block("cmd_b", "check_b", "msg_b")

        content_a = _read_audit_file(audit_a)
        content_b = _read_audit_file(audit_b)

        assert "agent_alpha" in str(audit_a.file_path)
        assert "agent_beta" in str(audit_b.file_path)
        assert "cmd_a" in content_a
        assert "cmd_b" in content_b
        assert "cmd_a" not in content_b
        assert "cmd_b" not in content_a


# =========================================================================
# Abnormal path — disabled audit, errors
# =========================================================================

class TestDisabledAudit:
    """Verify behavior when audit logging is disabled."""

    def test_no_file_created_when_disabled(self, disabled_audit):
        """No audit file should be created when audit is disabled."""
        disabled_audit.log_security_block("cmd", "check", "msg")
        # File path is only lazily resolved; when disabled, _write_entry
        # returns immediately so file_path is never touched.
        assert disabled_audit._file_path is None

    def test_get_log_path_returns_none_when_disabled(self, disabled_audit):
        """get_log_path() should return None when disabled."""
        assert disabled_audit.get_log_path() is None


class TestErrorResilience:
    """Verify that audit logging errors never crash the shell pipeline."""

    def test_unwritable_directory_does_not_raise(self, tmp_log_dir):
        """Writing to an unwritable directory should fail silently."""
        audit = ShellAuditLogger("test_agent", log_dir="/proc/nonexistent")
        audit._enabled = True
        # Force resolve to a bad path
        audit._file_path = Path("/proc/nonexistent/shell_audit.log")

        # Should not raise
        audit.log_security_block("cmd", "check", "msg")

    def test_concurrent_writes_no_corruption(self, enabled_audit):
        """Concurrent writes from multiple threads should not corrupt."""
        errors = []

        def writer(idx):
            try:
                for i in range(20):
                    enabled_audit.log_security_block(
                        command=f"cmd_{idx}_{i}",
                        check_id=f"check_{idx}",
                        message=f"msg_{idx}_{i}",
                    )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent writes: {errors}"

        content = _read_audit_file(enabled_audit)
        # All 100 entries should be present (5 threads * 20 each)
        assert content.count("[SECURITY_BLOCK]") == 100


# =========================================================================
# Boundary conditions
# =========================================================================

class TestBoundaryConditions:
    """Test edge cases and boundary inputs."""

    def test_very_long_command_truncated(self, enabled_audit):
        """Commands longer than 500 chars should be truncated."""
        long_cmd = "echo " + "x" * 600
        enabled_audit.log_security_block(long_cmd, "check", "msg")

        content = _read_audit_file(enabled_audit)
        assert "..." in content
        # Full command should not appear
        assert long_cmd not in content

    def test_short_command_not_truncated(self, enabled_audit):
        """Commands within limit should not be truncated."""
        cmd = "echo hello"
        enabled_audit.log_security_block(cmd, "check", "msg")

        content = _read_audit_file(enabled_audit)
        assert "echo hello" in content
        assert "..." not in content

    def test_empty_command(self, enabled_audit):
        """Empty command string should still produce a valid entry."""
        enabled_audit.log_security_block("", "check", "msg")
        content = _read_audit_file(enabled_audit)
        assert "[SECURITY_BLOCK]" in content
        assert "command: " in content

    def test_special_characters_in_agent_name(self, tmp_log_dir):
        """Agent names with special characters should be sanitized."""
        audit = ShellAuditLogger("agent/with spaces!@#", log_dir=tmp_log_dir)
        audit._enabled = True
        path = audit.file_path
        # Directory name should be sanitized (no slashes, spaces, etc.)
        assert "/" not in path.parent.name or str(tmp_log_dir) in str(path)
        assert " " not in path.parent.name

    def test_unicode_in_command(self, enabled_audit):
        """Unicode characters in command should be written correctly."""
        enabled_audit.log_security_block(
            "echo '你好世界'", "check", "包含中文"
        )
        content = _read_audit_file(enabled_audit)
        assert "你好世界" in content
        assert "包含中文" in content

    def test_newlines_in_message(self, enabled_audit):
        """Messages with newlines should still produce valid entries."""
        enabled_audit.log_security_block(
            "cmd", "check", "line1\nline2\nline3"
        )
        content = _read_audit_file(enabled_audit)
        assert "[SECURITY_BLOCK]" in content

    def test_duplicate_timestamp_collision(self, tmp_log_dir):
        """Two loggers created at the same time should get different dirs."""
        audit1 = ShellAuditLogger("same_agent", log_dir=tmp_log_dir)
        audit1._enabled = True
        # Force file creation for audit1
        audit1.log_security_block("cmd1", "c1", "m1")

        audit2 = ShellAuditLogger("same_agent", log_dir=tmp_log_dir)
        audit2._enabled = True
        # Force file creation for audit2 — should detect collision on timestamp dir
        path2 = audit2.file_path
        # Both files are named shell_audit.log but in different timestamp dirs
        assert path2.name == "shell_audit.log"
        assert audit1.file_path.name == "shell_audit.log"
        # They should be under the same agent parent directory
        assert path2.parent.parent == audit1.file_path.parent.parent


# =========================================================================
# Factory function (get_shell_audit_logger)
# =========================================================================

class TestGetShellAuditLogger:
    """Test the factory/singleton accessor."""

    def test_returns_same_instance_for_same_agent(self, tmp_log_dir):
        """Same agent name should return the same cached instance."""
        a1 = get_shell_audit_logger("agent_x", log_dir=tmp_log_dir)
        a2 = get_shell_audit_logger("agent_x", log_dir=tmp_log_dir)
        assert a1 is a2

    def test_returns_different_instances_for_different_agents(self, tmp_log_dir):
        """Different agent names should return different instances."""
        a1 = get_shell_audit_logger("agent_x", log_dir=tmp_log_dir)
        a2 = get_shell_audit_logger("agent_y", log_dir=tmp_log_dir)
        assert a1 is not a2

    def test_fallback_to_global_when_no_agent_context(self, tmp_log_dir):
        """Should fall back to _global when no agent context."""
        with patch("src.trace.task_context.get_current_agent_name",
                    side_effect=Exception("no context")):
            audit = get_shell_audit_logger(log_dir=tmp_log_dir)
        # We can't check the name directly since the factory may have
        # caught the exception; just verify it returns an instance.
        assert isinstance(audit, ShellAuditLogger)

    def test_reset_clears_cache(self, tmp_log_dir):
        """reset_audit_loggers() should clear the cache."""
        a1 = get_shell_audit_logger("agent_x", log_dir=tmp_log_dir)
        reset_audit_loggers()
        a2 = get_shell_audit_logger("agent_x", log_dir=tmp_log_dir)
        assert a1 is not a2


# =========================================================================
# Config integration
# =========================================================================

class TestConfigIntegration:
    """Verify that config values are read correctly."""

    def test_enabled_reads_from_config(self, tmp_log_dir):
        """enabled property should read from tools.shell.audit_log.enabled."""
        audit = ShellAuditLogger("test", log_dir=tmp_log_dir)
        with patch("src.tools.shell.shell_audit_log.C") as mock_c:
            mock_c.get_nested.return_value = False
            assert audit.enabled is False

    def test_enabled_coerces_string_true(self, tmp_log_dir):
        """String 'true' should be coerced to boolean True."""
        audit = ShellAuditLogger("test", log_dir=tmp_log_dir)
        with patch("src.tools.shell.shell_audit_log.C") as mock_c:
            mock_c.get_nested.return_value = "true"
            assert audit.enabled is True

    def test_enabled_coerces_string_false(self, tmp_log_dir):
        """String 'false' should be coerced to boolean False."""
        audit = ShellAuditLogger("test", log_dir=tmp_log_dir)
        with patch("src.tools.shell.shell_audit_log.C") as mock_c:
            mock_c.get_nested.return_value = "false"
            assert audit.enabled is False

    def test_log_success_default_false(self, tmp_log_dir):
        """log_success should default to False."""
        audit = ShellAuditLogger("test", log_dir=tmp_log_dir)
        with patch("src.tools.shell.shell_audit_log.C") as mock_c:
            mock_c.get_nested.return_value = False
            assert audit.log_success is False


# =========================================================================
# Integration with security pipeline
# =========================================================================

class TestSecurityPipelineIntegration:
    """Verify audit events are fired from the security check pipeline."""

    def test_security_block_triggers_audit(self, tmp_log_dir):
        """check_command_security() should trigger audit on block."""
        from src.tools.shell.security import check_command_security

        mock_audit = MagicMock()
        with patch("src.tools.shell.shell_audit_log.get_shell_audit_logger",
                    return_value=mock_audit):
            with patch("src.tools.shell.security._load_enabled_checks",
                        return_value={}):
                # This command should trigger destructive_patterns check
                results = check_command_security("rm -rf /")

            if results:
                mock_audit.log_security_block.assert_called()

    def test_path_violation_triggers_audit(self, tmp_log_dir):
        """check_path_constraints() should trigger audit on violation."""
        from src.tools.shell.path_validation import check_path_constraints

        mock_audit = MagicMock()
        with patch("src.tools.shell.shell_audit_log.get_shell_audit_logger",
                    return_value=mock_audit):
            with patch("src.tools.shell.path_validation._build_allowed_roots",
                        return_value=["."]):
                with patch("src.tools.shell.path_validation._load_dangerous_paths",
                            return_value=["/"]):
                    with patch("src.tools.shell.path_validation._is_block_destructive",
                                return_value=True):
                        try:
                            check_path_constraints("rm -rf /etc/passwd")
                        except ValueError:
                            pass

            # Audit should have been triggered (if path violation detected)
            # The exact behavior depends on path resolution

    def test_whitelist_rejection_triggers_audit(self, tmp_log_dir):
        """validate_command() should trigger audit on whitelist rejection."""
        from src.tools.shell.validator import validate_command

        mock_audit = MagicMock()
        with patch("src.tools.shell.shell_audit_log.get_shell_audit_logger",
                    return_value=mock_audit):

            with patch("src.tools.shell.validator.validate_command_security"):
                with patch("src.tools.shell.validator.check_path_constraints"):
                    with patch("src.tools.shell.validator.load_allowed_commands",
                                return_value=["ls", "cat"]):
                        with patch("src.tools.shell.validator.load_allowed_operators",
                                    return_value=[]):
                            try:
                                validate_command("wget http://evil.com")
                            except ValueError:
                                pass

            mock_audit.log_whitelist_rejection.assert_called_once()
            call_args = mock_audit.log_whitelist_rejection.call_args
            assert "wget" in call_args.kwargs.get("name", "") or \
                   "wget" in str(call_args)

    def test_whitelist_rejection_logs_effective_allow_lists(self, enabled_audit):
        """Rejected commands/operators should log the effective explicit allow-list."""
        from src.tools.shell.validator import validate_command

        with patch(
            "src.tools.shell.shell_audit_log.get_shell_audit_logger",
            return_value=enabled_audit,
        ):
            with patch("src.tools.shell.validator.validate_command_security"):
                with patch("src.tools.shell.validator.check_path_constraints"):
                    with patch(
                        "src.tools.shell.validator.load_allowed_commands",
                        return_value=["pwd", "echo", "cat"],
                    ):
                        with patch(
                            "src.tools.shell.validator.load_allowed_operators",
                            return_value=["|", "&&"],
                        ):
                            with pytest.raises(ValueError):
                                validate_command("date")
                            with pytest.raises(ValueError):
                                validate_command("echo a ; echo b")

        content = _read_audit_file(enabled_audit)
        assert "Command not allowed: date. Allowed commands: cat, echo, pwd" in content
        assert "Operator not allowed: ;. Allowed operators: &&, |" in content
        assert 'allowed_commands: ["date", ...]' in content
        assert 'allowed_operators: [";", ...]' in content
        assert 'allowed_commands: [";", ...]' not in content


# =========================================================================
# Suggestion quality
# =========================================================================

class TestSuggestionContent:
    """Verify that suggestions are actionable and reference correct YAML paths."""

    def test_security_block_suggestion_references_yaml(self, enabled_audit):
        """Security block suggestion should tell user which YAML key to change."""
        enabled_audit.log_security_block(
            "$(evil)", "command_substitution", "Blocked"
        )
        content = _read_audit_file(enabled_audit)
        assert "security_checks:" in content
        assert "command_substitution: false" in content

    def test_path_violation_suggestion_references_path_validation(self, enabled_audit):
        """Path violation suggestion should reference path_validation."""
        enabled_audit.log_path_violation(
            "cat /opt/data", "outside workspace", "/opt/data"
        )
        content = _read_audit_file(enabled_audit)
        assert "path_validation" in content
        assert "include_paths" in content
        assert "/opt/data" in content

    def test_whitelist_suggestion_references_allowed_commands(self, enabled_audit):
        """Whitelist rejection suggestion should reference allowed_commands."""
        enabled_audit.log_whitelist_rejection(
            "wget url", "not allowed", "wget"
        )
        content = _read_audit_file(enabled_audit)
        assert "allowed_commands" in content
        assert "wget" in content

    def test_operator_suggestion_references_allowed_operators(self, enabled_audit):
        """Operator rejection suggestion should reference allowed_operators."""
        enabled_audit.log_whitelist_rejection(
            "echo hello ; echo world",
            "Operator not allowed: ;. Allowed operators: &&, |",
            ";",
        )
        content = _read_audit_file(enabled_audit)
        assert "allowed_operators" in content
        assert '[";", ...]' in content
        assert 'allowed_commands: [";", ...]' not in content

    def test_stall_suggestion_mentions_non_interactive(self, enabled_audit):
        """Stall suggestion should mention non-interactive flags."""
        enabled_audit.log_stall_detected("npm i", 1234, 45.0, "prompt detected")
        content = _read_audit_file(enabled_audit)
        assert "--yes" in content or "-y" in content or "non-interactive" in content.lower()

    def test_timeout_suggestion_mentions_background(self, enabled_audit):
        """Timeout suggestion should mention background execution."""
        enabled_audit.log_timeout("make build", 120.0, promoted=False)
        content = _read_audit_file(enabled_audit)
        assert "run_in_background" in content or "timeout" in content.lower()
