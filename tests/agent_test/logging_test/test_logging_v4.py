from __future__ import annotations

from pathlib import Path

import pytest


def test_logging_overlay_exposes_only_bounded_run_settings() -> None:
    from src.lib.logging import LoggingConfigOverlay

    overlay = LoggingConfigOverlay(
        level="DEBUG",
        console_enabled=False,
        file_enabled=True,
        max_file_bytes=1024,
        backup_count=2,
    )
    assert overlay.to_mapping() == {
        "level": "DEBUG",
        "console_enabled": False,
        "file_enabled": True,
        "max_file_bytes": 1024,
        "backup_count": 2,
    }


@pytest.mark.parametrize("removed_key", ["enabled", "dir", "file_path"])
def test_removed_path_settings_are_rejected(removed_key: str) -> None:
    from src.lib.logging import validate_logging_config

    with pytest.raises(ValueError, match=removed_key):
        validate_logging_config(
            {"level": "INFO", removed_key: "legacy"},
            source="test",
        )


def test_arbitrary_unknown_logging_setting_is_rejected() -> None:
    from src.lib.logging import LoggingConfigBuilder

    with pytest.raises(ValueError, match="surprise"):
        LoggingConfigBuilder().apply_mapping(
            {"surprise": True},
            source="python overlay",
        )


def test_backend_without_runtime_context_has_no_file_sink(tmp_path: Path) -> None:
    from src.lib.logging import LoggingConfigBuilder, build_logger_backend_from_config

    backend = build_logger_backend_from_config(
        "standalone",
        logging_builder=LoggingConfigBuilder().apply_mapping(
            {"console_enabled": True, "file_enabled": True}, source="test"
        ),
    )
    assert getattr(getattr(backend, "console", None), "log_file_path", None) is None
    assert not (tmp_path / ".logs").exists()


def test_file_logging_override_can_disable_only_the_file_sink(tmp_path: Path) -> None:
    from src.lib.logging import LoggingConfigBuilder, initialize_run_logger
    from src.lib.runtime import RuntimeHome

    context = RuntimeHome(tmp_path / ".agentloom").context(
        application_id="app", task_id="task", run_id="run"
    )
    backend = initialize_run_logger(
        context,
        logging_builder=LoggingConfigBuilder().apply_mapping(
            {"console_enabled": True, "file_enabled": True}, source="test"
        ),
        file_logging=False,
    )
    backend.info("console-only")
    assert not context.log_path.exists()
