"""Unified logging helpers for AgentLoom."""

from __future__ import annotations

from typing import Any

from .logger_manager import (
    LazyLoggerAdapter,
    LoggerAdapter,
    LoggingConfigBuilder,
    LoggingConfigOverlay,
    NullLoggerBackend,
    UnifiedLogger,
    bind_logger_backend,
    build_logger_backend_from_config,
    close_run_logger,
    get_active_log_file_path,
    get_global_logger,
    get_logger,
    initialize_global_logger_once,
    initialize_run_logger,
    merge_logging_config,
    resolve_logger,
    set_global_logger,
    validate_logging_config,
)

__all__ = [
    "AgentLoomLogLevel",
    "EnhancedAgentLogger",
    "UnifiedLogger",
    "LazyLoggerAdapter",
    "LoggerAdapter",
    "LoggingConfigBuilder",
    "LoggingConfigOverlay",
    "NullLoggerBackend",
    "bind_logger_backend",
    "close_run_logger",
    "initialize_global_logger_once",
    "initialize_run_logger",
    "get_logger",
    "resolve_logger",  # backward-compatible alias for get_logger
    "set_global_logger",
    "get_global_logger",
    "build_logger_backend_from_config",
    "merge_logging_config",
    "validate_logging_config",
    "get_active_log_file_path",
]


def __getattr__(name: str) -> Any:
    # AgentLoomLogLevel/EnhancedAgentLogger inherit upstream smolagents types.
    # Keep them lazy so lightweight config/YAML validation (including the TUI
    # chat sidecar) does not initialize the complete execution framework.
    if name in {"AgentLoomLogLevel", "EnhancedAgentLogger"}:
        from . import agent_logger

        value = getattr(agent_logger, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
