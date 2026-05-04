from __future__ import annotations

from datetime import datetime
from pathlib import Path

import src.lib.config.config as config_module
import pytest


def _patch_config(monkeypatch, raw: dict, root: Path) -> None:
    monkeypatch.setattr(
        config_module,
        "_ACTIVE_CONFIG",
        config_module.UnifiedConfig(raw, agent_root=root, llm_config=config_module.LLMConfig()),
        raising=True,
    )


def _reset_logger_manager_state(monkeypatch) -> None:
    import src.lib.logging.logger_manager as logger_manager

    monkeypatch.setattr(logger_manager, "_INITIALIZED", False, raising=True)
    monkeypatch.setattr(logger_manager, "_ACTIVE_LOG_FILE_PATH", None, raising=True)
    monkeypatch.setattr(logger_manager, "_PROCESS_LOG_FILE_PATH", None, raising=True)


def test_build_default_log_filename_uses_app_name_without_timestamp():
    from src.lib.logging import build_default_log_filename

    fixed_now = datetime(2026, 1, 14, 15, 1, 11)
    # Timestamp is now in the parent directory, not the filename.
    assert build_default_log_filename("my_agent", now=fixed_now) == "my_agent.log"


def test_build_default_log_filename_sanitizes_app_name():
    from src.lib.logging import build_default_log_filename

    fixed_now = datetime(2026, 1, 14, 15, 1, 11)
    assert build_default_log_filename("my agent/v2", now=fixed_now) == "my_agent_v2.log"


def test_build_run_timestamp_dirname():
    from src.lib.logging import build_run_timestamp_dirname

    fixed_now = datetime(2026, 1, 14, 15, 1, 11)
    assert build_run_timestamp_dirname(now=fixed_now) == "20260114_150111"


def test_resolve_log_file_path_uses_timestamp_subdir(monkeypatch, tmp_path: Path):
    from src.lib.logging import resolve_log_file_path

    _patch_config(monkeypatch, {"logging": {"enabled": True, "level": "INFO", "dir": ".logs"}}, tmp_path)
    _reset_logger_manager_state(monkeypatch)
    fixed_now = datetime(2026, 1, 14, 15, 1, 11)

    resolved = resolve_log_file_path("code_review_agent", now=fixed_now)

    # New layout: .logs/{agent}/{timestamp}/{agent}.log
    assert resolved.parent == (tmp_path / ".logs" / "code_review_agent" / "20260114_150111")
    assert resolved.name == "code_review_agent.log"


def test_resolve_log_file_path_collision_adds_suffix_to_dir(monkeypatch, tmp_path: Path):
    from src.lib.logging import resolve_log_file_path

    _patch_config(monkeypatch, {"logging": {"enabled": True, "level": "INFO", "dir": ".logs"}}, tmp_path)
    _reset_logger_manager_state(monkeypatch)
    fixed_now = datetime(2026, 1, 14, 15, 1, 11)

    first = resolve_log_file_path("test_agent", now=fixed_now)
    # first creates .logs/test_agent/20260114_150111/
    assert first.parent.name == "20260114_150111"
    assert first.name == "test_agent.log"

    # Same timestamp → directory already exists → suffix on dir name
    second = resolve_log_file_path("test_agent", now=fixed_now)
    assert second.parent.name == "20260114_150111_1"
    assert second.name == "test_agent.log"
    assert first.parent != second.parent


def test_resolve_log_file_path_explicit_path_has_highest_priority(monkeypatch, tmp_path: Path):
    from src.lib.logging import resolve_log_file_path

    _patch_config(
        monkeypatch,
        {
            "logging": {
                "enabled": True,
                "level": "INFO",
                "dir": ".logs",
                "file_path": ".logs/from_config.log",
            }
        },
        tmp_path,
    )
    explicit = tmp_path / "my_logs" / "custom.log"

    resolved = resolve_log_file_path("ignored_name", log_file_path=str(explicit), now=datetime(2026, 1, 14, 15, 1, 11))

    assert resolved == explicit.resolve()
    assert resolved.parent.exists()
    explicit.touch()
    resolved_again = resolve_log_file_path("ignored_name", log_file_path=str(explicit), now=datetime(2026, 1, 14, 15, 1, 11))
    assert resolved_again == explicit.resolve()


def test_build_logger_backend_from_config_inherits_global_dir(monkeypatch, tmp_path: Path):
    from src.lib.logging import LoggingConfigBuilder, build_logger_backend_from_config

    _patch_config(monkeypatch, {"logging": {"enabled": True, "level": "INFO", "dir": ".logs"}}, tmp_path)
    _reset_logger_manager_state(monkeypatch)

    backend = build_logger_backend_from_config(
        "my_app",
        logging_builder=LoggingConfigBuilder().apply_mapping({"level": "DEBUG"}, source="test"),
    )

    file_path = Path(getattr(getattr(backend, "console", None), "log_file_path", ""))
    # New layout: .logs/my_app/{timestamp}/my_app.log
    assert file_path.parent.parent == (tmp_path / ".logs" / "my_app")
    assert file_path.name == "my_app.log"


def test_build_logger_backend_from_config_enabled_false_returns_null(monkeypatch, tmp_path: Path):
    from src.lib.logging import LoggingConfigBuilder, NullLoggerBackend, build_logger_backend_from_config

    _patch_config(monkeypatch, {"logging": {"enabled": True, "level": "INFO", "dir": ".logs"}}, tmp_path)
    backend = build_logger_backend_from_config(
        "my_app",
        logging_builder=LoggingConfigBuilder().apply_mapping({"enabled": False}, source="test"),
    )

    assert isinstance(backend, NullLoggerBackend)
    assert getattr(backend, "console", None) is None


def test_role_driven_agent_logger_resolution_uses_global_logger(monkeypatch, tmp_path: Path):
    from src.lib.logging import get_global_logger, initialize_global_logger_once, set_global_logger
    from src.lib.smolagents.agent.base_agent import RoleDrivenAgent

    _patch_config(monkeypatch, {"logging": {"enabled": True, "level": "INFO", "dir": ".logs"}}, tmp_path)
    _reset_logger_manager_state(monkeypatch)

    previous_global = get_global_logger(create_if_missing=False)
    set_global_logger(None)
    try:
        initialize_global_logger_once("test_app")
        backend = RoleDrivenAgent.resolve_agent_logger_from_config(
            {
                "name": "sample_worker",
                "logging": {"level": "DEBUG"},
            }
        )
        file_path = Path(getattr(getattr(backend, "console", None), "log_file_path", ""))
        assert backend is get_global_logger(create_if_missing=False)
        # New layout: .logs/test_app/{timestamp}/test_app.log
        assert file_path.parent.parent == (tmp_path / ".logs" / "test_app")
        assert file_path.name == "test_app.log"
    finally:
        set_global_logger(previous_global)


