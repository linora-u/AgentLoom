"""Retain one run-level shell audit contract across concurrent writers."""

import json
import threading

import pytest
from agentloom.execution import RuntimeHome, bind_run_context, copy_runtime_context
from agentloom.execution.tool_governance.shell import validator
from agentloom.execution.tool_governance.shell.audit import policy_audit
from agentloom.runtimes.smolagents.tools.shell.shell_audit_log import (
    ShellAuditLogger,
    reset_audit_loggers,
)


def test_canonical_audit_path_policy_snapshot_and_concurrent_writes(tmp_path, monkeypatch):
    reset_audit_loggers()
    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="shell-audit", task_id="task", run_id="run"
    )
    with bind_run_context(context):
        audit = ShellAuditLogger("worker")
        audit._enabled = True
        audit._log_policy_snapshot = True
        audit.log_effective_policy()
        audit.log_effective_policy()

        def write(index: int) -> None:
            audit.log_security_block(f"cmd_{index}", "guard", "blocked")

        threads = [
            threading.Thread(target=copy_runtime_context().run, args=(write, index))
            for index in range(12)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        monkeypatch.setattr(
            validator, "_get_shell_config",
            lambda key, *, default=None: ["echo"] if key == "allowed_commands" else [],
        )
        with policy_audit(audit), pytest.raises(ValueError, match="Command not allowed: cat"):
            validator.validate_command("cat", cwd=str(tmp_path))

        assert audit.file_path == context.shell_audit_path
        events = [json.loads(line) for line in audit.file_path.read_text().splitlines()]
        assert sum(event["event_type"] == "POLICY_SNAPSHOT" for event in events) == 1
        assert {event["command"] for event in events if event["event_type"] == "SECURITY_BLOCK"} == {
            f"cmd_{index}" for index in range(12)
        }
        assert [(event["command"], event["event_type"]) for event in events
                if event["event_type"] == "WHITELIST_REJECT"] == [("cat", "WHITELIST_REJECT")]
    reset_audit_loggers()
