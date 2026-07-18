from __future__ import annotations

import logging
import threading
from pathlib import Path

from src.lib.runtime import RuntimeHome, bind_run_context, copy_runtime_context


def test_null_backend_suppresses_only_the_bound_context(caplog) -> None:
    from src.lib.logging import NullLoggerBackend, bind_logger_backend, get_logger

    muted = get_logger("tests.context_muted")
    visible = logging.getLogger("tests.context_visible")
    with caplog.at_level(logging.WARNING):
        with bind_logger_backend(NullLoggerBackend()):
            muted.warning("checkpoint compatibility warning")
            visible.warning("builder audit remains visible")

    assert "checkpoint compatibility warning" not in caplog.messages
    assert "builder audit remains visible" in caplog.messages


def test_lazy_logger_is_bound_to_each_run_without_cross_writes(tmp_path: Path) -> None:
    from src.lib.logging import (
        LoggingConfigBuilder,
        bind_logger_backend,
        get_logger,
        initialize_run_logger,
    )

    home = RuntimeHome(tmp_path / ".agentloom")
    first = home.context(application_id="alpha", task_id="task-a", run_id="run-a")
    second = home.context(application_id="beta", task_id="task-b", run_id="run-b")
    logging_config = LoggingConfigBuilder().apply_mapping(
        {
            "level": "INFO",
            "console_enabled": False,
            "file_enabled": True,
            "max_file_bytes": 1024,
            "backup_count": 3,
        },
        source="test",
    )
    logger = get_logger("tests.run_scope")

    with bind_run_context(first):
        backend = initialize_run_logger(first, logging_builder=logging_config)
        with bind_logger_backend(backend, context=first):
            logger.info("only-first-run")

    # With no RuntimeContext, the adapter must not borrow the previous file sink.
    logger.info("outside-any-run")

    with bind_run_context(second):
        backend = initialize_run_logger(second, logging_builder=logging_config)
        with bind_logger_backend(backend, context=second):
            logger.info("only-second-run")

    first_text = first.log_path.read_text(encoding="utf-8")
    second_text = second.log_path.read_text(encoding="utf-8")
    assert "only-first-run" in first_text
    assert "only-second-run" not in first_text
    assert "outside-any-run" not in first_text
    assert "only-second-run" in second_text
    assert "only-first-run" not in second_text
    assert "outside-any-run" not in second_text


def test_thread_logging_requires_explicit_context_propagation(tmp_path: Path) -> None:
    from src.lib.logging import (
        LoggingConfigBuilder,
        bind_logger_backend,
        get_logger,
        initialize_run_logger,
    )

    context = RuntimeHome(tmp_path / ".agentloom").context(application_id="threaded", task_id="task", run_id="run")
    config = LoggingConfigBuilder().apply_mapping({"console_enabled": False, "file_enabled": True}, source="test")
    logger = get_logger("tests.thread_scope")

    with bind_run_context(context):
        backend = initialize_run_logger(context, logging_builder=config)
        with bind_logger_backend(backend, context=context):
            unpropagated = threading.Thread(target=lambda: logger.info("must-not-borrow-run"))
            unpropagated.start()
            unpropagated.join()

            propagated_context = copy_runtime_context()
            propagated = threading.Thread(
                target=propagated_context.run,
                args=(lambda: logger.info("explicitly-propagated"),),
            )
            propagated.start()
            propagated.join()

    text = context.log_path.read_text(encoding="utf-8")
    assert "explicitly-propagated" in text
    assert "must-not-borrow-run" not in text


def test_bound_run_logger_rejects_missing_or_different_runtime_context(
    tmp_path: Path,
) -> None:
    from src.lib.logging import (
        LoggingConfigBuilder,
        bind_logger_backend,
        get_logger,
        initialize_run_logger,
    )

    home = RuntimeHome(tmp_path / ".agentloom")
    first = home.context(application_id="alpha", task_id="task-a", run_id="run-a")
    second = home.context(application_id="beta", task_id="task-b", run_id="run-b")
    config = LoggingConfigBuilder().apply_mapping(
        {"console_enabled": False, "file_enabled": True},
        source="test",
    )

    with bind_run_context(first):
        first_backend = initialize_run_logger(first, logging_builder=config)
        with bind_logger_backend(first_backend, context=first):
            bound_to_first = get_logger(first_backend, "tests.bound_scope")
            bound_to_first.info("valid-first")

            missing_context = threading.Thread(
                target=lambda: bound_to_first.info("missing-context")
            )
            missing_context.start()
            missing_context.join()

            with bind_run_context(second):
                second_backend = initialize_run_logger(second, logging_builder=config)
                with bind_logger_backend(second_backend, context=second):
                    bound_to_first.info("wrong-run")
                    get_logger(second_backend, "tests.bound_scope").info("valid-second")

    first_text = first.log_path.read_text(encoding="utf-8")
    second_text = second.log_path.read_text(encoding="utf-8")
    assert "valid-first" in first_text
    assert "valid-second" in second_text
    assert "missing-context" not in first_text
    assert "wrong-run" not in first_text
    assert "wrong-run" not in second_text


def test_runtime_log_rotation_is_bounded_per_run(tmp_path: Path) -> None:
    from src.lib.logging import (
        LoggingConfigBuilder,
        bind_logger_backend,
        get_logger,
        initialize_run_logger,
    )

    context = RuntimeHome(tmp_path / ".agentloom").context(application_id="rotating", task_id="task", run_id="run")
    config = LoggingConfigBuilder().apply_mapping(
        {
            "console_enabled": False,
            "file_enabled": True,
            "max_file_bytes": 180,
            "backup_count": 3,
        },
        source="test",
    )
    logger = get_logger("tests.rotation")
    with bind_run_context(context):
        backend = initialize_run_logger(context, logging_builder=config)
        with bind_logger_backend(backend, context=context):
            for index in range(30):
                logger.info("rotation-record-%02d", index)

    assert context.log_path.exists()
    assert Path(f"{context.log_path}.1").exists()
    assert Path(f"{context.log_path}.3").exists()
    assert not Path(f"{context.log_path}.4").exists()


def test_runtime_log_rotation_keeps_opened_directory_when_path_is_replaced(
    tmp_path: Path,
) -> None:
    from src.lib.logging import (
        LoggingConfigBuilder,
        bind_logger_backend,
        get_logger,
        initialize_run_logger,
    )

    home = RuntimeHome(tmp_path / ".agentloom")
    first = home.context(application_id="first", task_id="task", run_id="run")
    second = home.context(application_id="second", task_id="task", run_id="run")
    first.prepare_run()
    second.prepare_run()
    second.log_path.write_text("SECOND-ONLY\n", encoding="utf-8")
    detached_logs = first.run_dir / "logs-detached"
    config = LoggingConfigBuilder().apply_mapping(
        {
            "console_enabled": False,
            "file_enabled": True,
            "max_file_bytes": 120,
            "backup_count": 2,
        },
        source="test",
    )
    logger = get_logger("tests.secure_rotation")

    with bind_run_context(first):
        backend = initialize_run_logger(first, logging_builder=config)
        with bind_logger_backend(backend, context=first):
            logger.info("FIRST-BEFORE")
            first.logs_dir.rename(detached_logs)
            first.logs_dir.symlink_to(second.logs_dir, target_is_directory=True)
            for _ in range(8):
                logger.info("FIRST-AFTER-ROTATION-BOUNDARY")

    second_text = second.log_path.read_text(encoding="utf-8")
    assert second_text == "SECOND-ONLY\n"
    assert any("FIRST" in path.read_text(encoding="utf-8") for path in detached_logs.iterdir())


def test_agent_log_prefix_never_reads_process_global_trace_fallbacks(monkeypatch) -> None:
    import importlib

    from src.lib.logging.agent_logger import AgentLoomLogLevel, EnhancedAgentLogger

    task_context_module = importlib.import_module("src.trace.task_context")

    monkeypatch.setattr(task_context_module, "_global_task_id_fallback", "wrong-task")
    monkeypatch.setattr(task_context_module, "_global_sub_task_id_fallback", "wrong-subtask")
    monkeypatch.setattr(task_context_module, "_global_agent_name_fallback", "wrong-agent")

    logger = EnhancedAgentLogger(show_timestamp=False, show_trace_info=True)
    prefix = logger._build_prefix(AgentLoomLogLevel.INFO).plain

    assert "wrong-task" not in prefix
    assert "wrong-subtask" not in prefix
    assert "wrong-agent" not in prefix
