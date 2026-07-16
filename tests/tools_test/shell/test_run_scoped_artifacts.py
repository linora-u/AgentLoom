from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from src.lib.runtime import RuntimeHome, bind_run_context, copy_runtime_context


def test_shell_audit_is_jsonl_in_the_current_run(tmp_path: Path) -> None:
    from src.tools.shell.shell_audit_log import (
        get_shell_audit_logger,
        reset_audit_loggers,
    )

    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="search",
        task_id="task-1",
        run_id="run-1",
    )
    reset_audit_loggers()
    with bind_run_context(context):
        audit = get_shell_audit_logger("worker")
        audit._enabled = True
        audit._log_policy_snapshot = False
        audit.log_security_block("rm -rf /", "destructive", "blocked")
        assert Path(audit.get_log_path() or "") == context.shell_audit_path

    lines = context.shell_audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event_type"] == "SECURITY_BLOCK"
    assert event["agent"] == "worker"
    assert event["command"] == "rm -rf /"
    assert event["check_id"] == "destructive"


def test_large_shell_output_spills_to_the_current_run(tmp_path: Path) -> None:
    from src.tools.shell.output_interceptor import OutputInterceptor

    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="search",
        task_id="task-1",
        run_id="run-1",
    )
    with bind_run_context(context):
        output = OutputInterceptor(preview_bytes=8)
        output.write("0123456789abcdef")
        output.finalize()

    assert output.artifact_path is not None
    artifact = Path(output.artifact_path)
    assert artifact.parent == context.shell_artifacts_dir
    assert artifact.read_text(encoding="utf-8") == "0123456789abcdef"


def test_background_shell_output_belongs_to_the_current_run(tmp_path: Path) -> None:
    from src.tools.shell.background_task import BackgroundTaskRegistry
    from src.tools.shell.shell_tool import shell_tool

    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="search",
        task_id="task-1",
        run_id="run-1",
    )
    registry = BackgroundTaskRegistry.get_instance()
    try:
        with bind_run_context(context):
            result = shell_tool("printf background-output", run_in_background=True)
            assert "[Background Task:" in result
            task = registry.list_all()[-1]
            assert Path(task.output_path).parent == context.background_artifacts_dir
    finally:
        BackgroundTaskRegistry._reset_instance()


def test_background_check_keeps_the_original_output_inode_when_directory_is_replaced(
    tmp_path: Path,
) -> None:
    import os
    import subprocess

    from src.tools.shell.background_task import BackgroundTaskRegistry
    from src.tools.shell.background_task_tools import check_background_task

    home = RuntimeHome(tmp_path / ".agentloom")
    first = home.context(application_id="first", task_id="task", run_id="run")
    second = home.context(application_id="second", task_id="task", run_id="run")
    first.prepare_run()
    second.prepare_run()
    registry = BackgroundTaskRegistry.get_instance()
    process = None
    output_fd = -1

    try:
        with bind_run_context(first):
            output_fd, output_path = first.allocate_artifact(
                "background",
                prefix="background-",
                suffix=".txt",
            )
            os.write(output_fd, b"FIRST-RUN-OUTPUT\n")
            process = subprocess.Popen(
                ["sleep", "60"],
                stdout=output_fd,
                stderr=output_fd,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            task_id = registry.register(
                process,
                "sleep 60",
                str(output_path),
                output_fd=output_fd,
            )
            os.close(output_fd)
            output_fd = -1

            detached = first.run_dir / "background-detached"
            first.background_artifacts_dir.rename(detached)
            first.background_artifacts_dir.symlink_to(
                second.background_artifacts_dir,
                target_is_directory=True,
            )
            (second.background_artifacts_dir / output_path.name).write_text(
                "SECOND-RUN-SECRET\n",
                encoding="utf-8",
            )

            rendered = check_background_task(task_id)

        assert "FIRST-RUN-OUTPUT" in rendered
        assert "SECOND-RUN-SECRET" not in rendered
    finally:
        if output_fd >= 0:
            os.close(output_fd)
        BackgroundTaskRegistry._reset_instance()
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def test_explicit_background_shell_requires_a_runtime_context() -> None:
    from src.tools.shell.shell_tool import shell_tool

    with pytest.raises(RuntimeError, match="RuntimeContext"):
        shell_tool("printf no-run-context", run_in_background=True)


def test_background_spawn_failure_removes_partial_run_artifact(tmp_path: Path) -> None:
    from src.tools.shell.shell_tool import shell_tool

    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="search",
        task_id="task",
        run_id="run",
    )
    context.prepare_run()
    with (
        bind_run_context(context),
        patch(
            "subprocess.Popen",
            side_effect=OSError("spawn failed"),
        ),
    ):
        with pytest.raises(OSError, match="spawn failed"):
            shell_tool("printf never-started", run_in_background=True)

    assert list(context.background_artifacts_dir.iterdir()) == []


def test_shell_audit_rotation_keeps_two_backups(tmp_path: Path) -> None:
    from src.tools.shell.shell_audit_log import ShellAuditLogger

    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="search",
        task_id="task-1",
        run_id="run-1",
    )
    with bind_run_context(context):
        audit = ShellAuditLogger(
            "worker",
            runtime_context=context,
            max_file_bytes=220,
            backup_count=2,
        )
        audit._enabled = True
        audit._log_policy_snapshot = False
        for index in range(20):
            audit.log_security_block(
                f"command-{index}",
                "check",
                "x" * 80,
            )
        audit.close()

    assert context.shell_audit_path.exists()
    assert Path(f"{context.shell_audit_path}.1").exists()
    assert Path(f"{context.shell_audit_path}.2").exists()
    assert not Path(f"{context.shell_audit_path}.3").exists()


def test_shell_audit_keeps_opened_directory_when_path_is_replaced(tmp_path: Path) -> None:
    from src.tools.shell.shell_audit_log import ShellAuditLogger

    home = RuntimeHome(tmp_path / ".agentloom")
    first = home.context(application_id="first", task_id="task", run_id="run")
    second = home.context(application_id="second", task_id="task", run_id="run")
    first.prepare_run()
    second.prepare_run()
    second.shell_audit_path.write_text("SECOND-ONLY\n", encoding="utf-8")
    detached_audit = first.run_dir / "audit-detached"

    with bind_run_context(first):
        audit = ShellAuditLogger("worker", runtime_context=first)
        audit._enabled = True
        audit._log_policy_snapshot = False
        first.audit_dir.rename(detached_audit)
        first.audit_dir.symlink_to(second.audit_dir, target_is_directory=True)
        audit.log_security_block("FIRST-ONLY", "check", "message")
        audit.close()

    assert second.shell_audit_path.read_text(encoding="utf-8") == "SECOND-ONLY\n"
    assert "FIRST-ONLY" in (detached_audit / "shell.jsonl").read_text(encoding="utf-8")


def test_large_output_does_not_follow_replaced_shell_artifact_directory(
    tmp_path: Path,
) -> None:
    from src.tools.shell.output_interceptor import OutputInterceptor

    home = RuntimeHome(tmp_path / ".agentloom")
    first = home.context(application_id="first", task_id="task", run_id="run")
    second = home.context(application_id="second", task_id="task", run_id="run")
    first.prepare_run()
    second.prepare_run()
    first.shell_artifacts_dir.rmdir()
    first.shell_artifacts_dir.symlink_to(
        second.shell_artifacts_dir,
        target_is_directory=True,
    )

    with bind_run_context(first):
        interceptor = OutputInterceptor(preview_bytes=8)
        interceptor.write("FIRST-SECRET-LARGE-OUTPUT")
        rendered = interceptor.finalize()

    assert "saved to" not in rendered
    assert list(second.shell_artifacts_dir.iterdir()) == []


def test_sequential_runs_do_not_reuse_shell_audit_sinks(tmp_path: Path) -> None:
    from src.tools.shell.shell_audit_log import (
        get_shell_audit_logger,
        reset_audit_loggers,
    )

    home = RuntimeHome(tmp_path / ".agentloom")
    first = home.context(application_id="alpha", task_id="task-a", run_id="run-a")
    second = home.context(application_id="beta", task_id="task-b", run_id="run-b")
    reset_audit_loggers()

    with bind_run_context(first):
        audit = get_shell_audit_logger("worker")
        audit._enabled = True
        audit._log_policy_snapshot = False
        audit.log_security_block("first-only", "check", "message")
    with bind_run_context(second):
        audit = get_shell_audit_logger("worker")
        audit._enabled = True
        audit._log_policy_snapshot = False
        audit.log_security_block("second-only", "check", "message")

    first_text = first.shell_audit_path.read_text(encoding="utf-8")
    second_text = second.shell_audit_path.read_text(encoding="utf-8")
    assert "first-only" in first_text and "second-only" not in first_text
    assert "second-only" in second_text and "first-only" not in second_text


def test_concurrent_runs_keep_shell_audit_contexts_isolated(tmp_path: Path) -> None:
    from src.tools.shell.shell_audit_log import (
        get_shell_audit_logger,
        reset_audit_loggers,
    )

    home = RuntimeHome(tmp_path / ".agentloom")
    contexts = [
        home.context(application_id="alpha", task_id="task-a", run_id="run-a"),
        home.context(application_id="beta", task_id="task-b", run_id="run-b"),
    ]
    reset_audit_loggers()

    def write(context, message: str) -> None:
        with bind_run_context(context):
            audit = get_shell_audit_logger("worker")
            audit._enabled = True
            audit._log_policy_snapshot = False
            audit.log_security_block(message, "check", "message")

    threads = [
        threading.Thread(target=write, args=(contexts[0], "alpha-only")),
        threading.Thread(target=write, args=(contexts[1], "beta-only")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    alpha = contexts[0].shell_audit_path.read_text(encoding="utf-8")
    beta = contexts[1].shell_audit_path.read_text(encoding="utf-8")
    assert "alpha-only" in alpha and "beta-only" not in alpha
    assert "beta-only" in beta and "alpha-only" not in beta


def test_run_logger_scope_closes_shell_audit_handler(tmp_path: Path) -> None:
    from src.lib.logging import (
        LoggingConfigBuilder,
        bind_logger_backend,
        initialize_run_logger,
    )
    from src.tools.shell.shell_audit_log import get_shell_audit_logger

    context = RuntimeHome(tmp_path / ".agentloom").context(application_id="search", task_id="task", run_id="run")
    config = LoggingConfigBuilder().apply_mapping({"console_enabled": False, "file_enabled": False}, source="test")
    with bind_run_context(context):
        backend = initialize_run_logger(context, logging_builder=config)
        with bind_logger_backend(backend, context=context):
            audit = get_shell_audit_logger("worker")
            audit._enabled = True
            audit._log_policy_snapshot = False
            audit.log_security_block("command", "check", "message")
            handler = audit._sink._handler
            assert handler is not None and handler.stream is not None
            stale_thread_context = copy_runtime_context()

        assert handler.stream is None

    stale_thread_context.run(lambda: audit.log_security_block("after-run", "check", "message"))
    assert "after-run" not in context.shell_audit_path.read_text(encoding="utf-8")


def test_shell_audit_agent_never_reads_process_global_trace_fallback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import importlib
    from dataclasses import replace

    from src.tools.shell.shell_audit_log import get_shell_audit_logger, reset_audit_loggers
    from src.trace import (
        bind_explicit_execution_context,
        capture_explicit_execution_context,
    )

    task_context_module = importlib.import_module("src.trace.task_context")

    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="search",
        task_id="task",
        run_id="run",
    )
    monkeypatch.setattr(task_context_module, "_global_agent_name_fallback", "wrong-agent")
    reset_audit_loggers()

    explicit = replace(
        capture_explicit_execution_context(),
        task_id=None,
        sub_task_id=None,
        agent_id=None,
        agent_name=None,
    )
    with bind_run_context(context), bind_explicit_execution_context(explicit):
        audit = get_shell_audit_logger()
        audit._enabled = True
        audit._log_policy_snapshot = False
        audit.log_security_block("command", "check", "message")

    event = json.loads(context.shell_audit_path.read_text(encoding="utf-8"))
    assert event["agent"] == "_global"
