"""Tests for AgentLoomLogLevel filtering and file output correctness.

Covers:
- Each log method writes the correct level tag ([DEBUG]/[INFO]/[WARNING]/[ERROR]).
- Messages below the configured level are NOT written to the file.
- Messages at or above the configured level ARE written.
- Log file contains no ANSI escape codes.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import src.lib.config.config as config_module
import pytest


# ---------------------------------------------------------------------------
# Helpers (mirrors conventions in test_logging_v4.py)
# ---------------------------------------------------------------------------

def _patch_config(monkeypatch, raw: dict, root: Path) -> None:
    monkeypatch.setattr(
        config_module,
        "_ACTIVE_CONFIG",
        config_module.UnifiedConfig(
            raw,
            agent_root=root,
            llm_config=config_module.LLMConfig(),
        ),
        raising=True,
    )


def _make_script(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    return path


def _patch_main_script(monkeypatch, script: Path | None) -> None:
    main_module = sys.modules.get("__main__")
    if main_module is not None:
        monkeypatch.setattr(
            main_module,
            "__file__",
            str(script) if script is not None else None,
            raising=False,
        )


def _build_backend(monkeypatch, tmp_path: Path, level: str):
    """Build an EnhancedAgentLogger backend with the given level string."""
    from src.lib.logging import LoggingConfigBuilder, build_logger_backend_from_config
    import src.lib.logging.logger_manager as logger_manager

    _patch_config(
        monkeypatch,
        {"logging": {"enabled": True, "level": level, "dir": ".logs"}},
        tmp_path,
    )
    monkeypatch.setattr(logger_manager, "_PROCESS_LOG_FILE_PATH", None, raising=True)

    return build_logger_backend_from_config(
        "test_log_levels",
        logging_builder=LoggingConfigBuilder().apply_mapping({"level": level}, source="test"),
    )


def _log_file_path(backend) -> Path:
    return Path(getattr(getattr(backend, "console", None), "log_file_path", ""))


def _write_and_read(backend, method: str, msg: str) -> str:
    """Call backend.<method>(msg) then return log file content."""
    getattr(backend, method)(msg)
    log_path = _log_file_path(backend)
    if not log_path.exists():
        return ""
    return log_path.read_text(encoding="utf-8")


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mABCDEFGHJKSTflnprsu]")


# ---------------------------------------------------------------------------
# 1. Level tag correctness
# ---------------------------------------------------------------------------

def test_debug_writes_debug_tag(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "DEBUG")
    content = _write_and_read(backend, "debug", "debug msg check")
    assert "[DEBUG]" in content
    assert "debug msg check" in content


def test_info_writes_info_tag(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "DEBUG")
    content = _write_and_read(backend, "info", "info msg check")
    assert "[INFO]" in content
    assert "info msg check" in content


def test_warning_writes_warning_tag(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "DEBUG")
    content = _write_and_read(backend, "warning", "warning msg check")
    assert "[WARNING]" in content
    assert "warning msg check" in content


def test_error_writes_error_tag(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "DEBUG")
    content = _write_and_read(backend, "error", "error msg check")
    assert "[ERROR]" in content
    assert "error msg check" in content


# ---------------------------------------------------------------------------
# 2. Level filtering — configured level = INFO
# ---------------------------------------------------------------------------

def test_info_level_allows_info(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "INFO")
    content = _write_and_read(backend, "info", "info allowed")
    assert "info allowed" in content


def test_info_level_allows_warning(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "INFO")
    content = _write_and_read(backend, "warning", "warning allowed under info cfg")
    assert "warning allowed under info cfg" in content


def test_info_level_allows_error(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "INFO")
    content = _write_and_read(backend, "error", "error allowed under info cfg")
    assert "error allowed under info cfg" in content


def test_info_level_filters_debug(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "INFO")
    content = _write_and_read(backend, "debug", "should not appear in file")
    assert "should not appear in file" not in content


# ---------------------------------------------------------------------------
# 3. Level filtering — configured level = WARNING
# ---------------------------------------------------------------------------

def test_warning_level_allows_warning(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "WARNING")
    content = _write_and_read(backend, "warning", "warning visible")
    assert "warning visible" in content


def test_warning_level_allows_error(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "WARNING")
    content = _write_and_read(backend, "error", "error visible")
    assert "error visible" in content


def test_warning_level_filters_info(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "WARNING")
    content = _write_and_read(backend, "info", "info should be hidden")
    assert "info should be hidden" not in content


def test_warning_level_filters_debug(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "WARNING")
    content = _write_and_read(backend, "debug", "debug should be hidden")
    assert "debug should be hidden" not in content


# ---------------------------------------------------------------------------
# 4. Level filtering — configured level = ERROR
# ---------------------------------------------------------------------------

def test_error_level_allows_error(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "ERROR")
    content = _write_and_read(backend, "error", "error only visible")
    assert "error only visible" in content


def test_error_level_filters_warning(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "ERROR")
    content = _write_and_read(backend, "warning", "warning hidden under error cfg")
    assert "warning hidden under error cfg" not in content


def test_error_level_filters_info(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "ERROR")
    content = _write_and_read(backend, "info", "info hidden under error cfg")
    assert "info hidden under error cfg" not in content


# ---------------------------------------------------------------------------
# 5. No ANSI escape codes in log file
# ---------------------------------------------------------------------------

def test_file_content_has_no_ansi_codes(monkeypatch, tmp_path: Path):
    backend = _build_backend(monkeypatch, tmp_path, "DEBUG")
    # Write one message at every level.
    backend.debug("ansi check debug")
    backend.info("ansi check info")
    backend.warning("ansi check warning")
    backend.error("ansi check error")

    log_path = _log_file_path(backend)
    assert log_path.exists(), "Log file was not created"
    content = log_path.read_text(encoding="utf-8")
    assert not ANSI_RE.search(content), (
        f"ANSI escape codes found in log file:\n{content[:500]}"
    )


# ---------------------------------------------------------------------------
# 6. AgentLoomLogLevel.from_str / from_int round-trip
# ---------------------------------------------------------------------------

def test_agent_loom_log_level_from_str():
    from src.lib.logging.agent_logger import AgentLoomLogLevel

    assert AgentLoomLogLevel.from_str("debug")   == AgentLoomLogLevel.DEBUG
    assert AgentLoomLogLevel.from_str("INFO")    == AgentLoomLogLevel.INFO
    assert AgentLoomLogLevel.from_str("Warning") == AgentLoomLogLevel.WARNING
    assert AgentLoomLogLevel.from_str("WARN")    == AgentLoomLogLevel.WARNING
    assert AgentLoomLogLevel.from_str("error")   == AgentLoomLogLevel.ERROR
    assert AgentLoomLogLevel.from_str("CRITICAL")== AgentLoomLogLevel.ERROR
    assert AgentLoomLogLevel.from_str("off")     == AgentLoomLogLevel.OFF


def test_agent_loom_log_level_from_str_invalid():
    from src.lib.logging.agent_logger import AgentLoomLogLevel

    with pytest.raises(ValueError):
        AgentLoomLogLevel.from_str("NONSENSE")


def test_agent_loom_log_level_from_int():
    import logging as _logging
    from src.lib.logging.agent_logger import AgentLoomLogLevel

    assert AgentLoomLogLevel.from_int(_logging.DEBUG)   == AgentLoomLogLevel.DEBUG
    assert AgentLoomLogLevel.from_int(_logging.INFO)    == AgentLoomLogLevel.INFO
    assert AgentLoomLogLevel.from_int(_logging.WARNING) == AgentLoomLogLevel.WARNING
    assert AgentLoomLogLevel.from_int(_logging.ERROR)   == AgentLoomLogLevel.ERROR
    assert AgentLoomLogLevel.from_int(_logging.CRITICAL)== AgentLoomLogLevel.OFF
