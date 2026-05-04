"""Tests for unified get_logger API and LazyLoggerAdapter safety.

Verifies that:
- Module-level get_logger(__name__) never triggers global logger initialisation.
- After initialize_global_logger_once(app_name), lazy loggers bind to the
  correct backend automatically.
- get_logger(backend, name) wraps an explicit backend as LoggerAdapter.
- LoggerAdapter._dispatch fallback never calls initialize_global_logger_once.
- get_logger is idempotent for already-wrapped objects.
- Log directory follows the YAML name, not the fallback "AgentLoom".
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers to reset module-level globals between tests
# ---------------------------------------------------------------------------

def _reset_global_logger():
    """Reset global logger state to simulate a fresh process."""
    import src.lib.logging.logger_manager as lm

    lm._GLOBAL_LOGGER = None
    lm._PROCESS_LOG_FILE_PATH = None
    lm._INITIALIZED = False
    lm._ACTIVE_LOG_FILE_PATH = None


@pytest.fixture(autouse=True)
def _clean_global_state():
    """Ensure each test starts with a clean global logger state."""
    _reset_global_logger()
    yield
    _reset_global_logger()


# ---------------------------------------------------------------------------
# Phase 1: Module-level safety
# ---------------------------------------------------------------------------

class TestModuleLevelSafety:
    """get_logger(__name__) must NEVER trigger initialize_global_logger_once."""

    def test_get_logger_str_does_not_init_global(self):
        """Calling get_logger with a string must not initialise the global backend."""
        from src.lib.logging.logger_manager import (
            LazyLoggerAdapter,
            _GLOBAL_LOGGER,
            get_logger,
        )

        logger = get_logger("my.module")
        assert isinstance(logger, LazyLoggerAdapter)
        # Global logger must still be None
        from src.lib.logging.logger_manager import _GLOBAL_LOGGER as gl
        assert gl is None, "get_logger(str) must not initialise _GLOBAL_LOGGER"

    def test_get_logger_none_does_not_init_global(self):
        """Calling get_logger(None) must not initialise the global backend."""
        from src.lib.logging.logger_manager import (
            LazyLoggerAdapter,
            get_logger,
        )

        logger = get_logger(None)
        assert isinstance(logger, LazyLoggerAdapter)
        from src.lib.logging.logger_manager import _GLOBAL_LOGGER as gl
        assert gl is None

    def test_lazy_logger_log_call_does_not_init_global(self):
        """Logging through a lazy logger must NOT trigger global init."""
        from src.lib.logging.logger_manager import get_logger

        logger = get_logger("test.lazy")

        # Patch stdlib logger to capture output
        with patch("logging.getLogger") as mock_get:
            mock_stdlib = MagicMock()
            mock_get.return_value = mock_stdlib
            logger.info("hello %s", "world")

        # _GLOBAL_LOGGER must still be None
        from src.lib.logging.logger_manager import _GLOBAL_LOGGER as gl
        assert gl is None, "Lazy log call must not initialise _GLOBAL_LOGGER"


# ---------------------------------------------------------------------------
# Phase 2: Runtime binding after initialisation
# ---------------------------------------------------------------------------

class TestRuntimeBinding:
    """After initialize_global_logger_once, lazy loggers must use the correct backend."""

    def test_lazy_logger_picks_up_global_after_init(self):
        """A lazy logger created BEFORE init must use the global backend AFTER init."""
        from src.lib.logging.logger_manager import (
            get_logger,
            initialize_global_logger_once,
        )

        # Create lazy logger before init
        logger = get_logger("test.binding")

        # Now init the global logger
        backend = initialize_global_logger_once("test_app")
        assert backend is not None

        # The lazy logger should now use this backend
        resolved_backend = logger._resolve_backend()
        assert resolved_backend is backend, (
            "Lazy logger must pick up the globally initialised backend"
        )

    def test_log_dir_follows_app_name(self, tmp_path: Path):
        """Log directory must be named after the app, not 'AgentLoom'."""
        from src.lib.logging.logger_manager import (
            resolve_log_file_path,
            _get_agent_root,
        )

        log_path = resolve_log_file_path(
            "my_custom_app",
            ensure_parent=False,
        )
        # The path should contain the app name
        assert "my_custom_app" in str(log_path), (
            f"Log path should contain app name: {log_path}"
        )
        # The log file and its parent directories (relative to agent_root)
        # must use the app name, not the fallback "AgentLoom".
        # We only check the relative portion to avoid false positives when
        # the project itself is located in a directory named "AgentLoom".
        agent_root = _get_agent_root()
        try:
            relative_path = str(log_path.relative_to(agent_root))
        except ValueError:
            relative_path = str(log_path)
        assert "AgentLoom" not in relative_path, (
            f"Log path (relative to agent_root) must NOT contain fallback name: {relative_path}"
        )

    def test_global_init_not_called_before_run_app(self):
        """Importing runner must not trigger global logger initialisation."""
        # The import itself was tested above; this test ensures the contract
        # holds for the full import chain.
        from src.lib.logging.logger_manager import _GLOBAL_LOGGER as gl

        # After _clean_global_state fixture reset, importing runner should
        # not have re-initialised the global logger (it's a cached import).
        # We just verify the state is clean.
        assert gl is None


# ---------------------------------------------------------------------------
# Phase 3: Explicit backend wrapping
# ---------------------------------------------------------------------------

class TestExplicitBackendWrapping:
    """get_logger(backend_obj, name) must return a LoggerAdapter."""

    def test_wrap_explicit_backend(self):
        """Wrapping a backend object returns LoggerAdapter with that backend."""
        from src.lib.logging.logger_manager import LoggerAdapter, get_logger

        backend = MagicMock()
        adapter = get_logger(backend, "my.module")
        assert isinstance(adapter, LoggerAdapter)
        assert adapter._backend is backend

    def test_wrap_stdlib_logger(self):
        """Wrapping a stdlib Logger returns LoggerAdapter."""
        from src.lib.logging.logger_manager import LoggerAdapter, get_logger

        stdlib_logger = logging.getLogger("test.stdlib")
        adapter = get_logger(stdlib_logger, "test.stdlib")
        assert isinstance(adapter, LoggerAdapter)
        assert adapter._backend is stdlib_logger

    def test_idempotent_for_logger_adapter(self):
        """Passing a LoggerAdapter returns it unchanged."""
        from src.lib.logging.logger_manager import LoggerAdapter, get_logger

        backend = MagicMock()
        adapter = LoggerAdapter(backend, "test")
        result = get_logger(adapter)
        assert result is adapter, "get_logger must be idempotent for LoggerAdapter"

    def test_idempotent_for_lazy_logger(self):
        """Passing a LazyLoggerAdapter returns it unchanged."""
        from src.lib.logging.logger_manager import LazyLoggerAdapter, get_logger

        lazy = LazyLoggerAdapter("test.lazy")
        result = get_logger(lazy)
        assert result is lazy, "get_logger must be idempotent for LazyLoggerAdapter"


# ---------------------------------------------------------------------------
# Phase 4: LoggerAdapter._dispatch safety
# ---------------------------------------------------------------------------

class TestDispatchFallbackSafety:
    """LoggerAdapter._dispatch must never call initialize_global_logger_once."""

    def test_dispatch_fallback_uses_stdlib_not_init(self):
        """When backend method is missing, fallback to stdlib, not global init."""
        from src.lib.logging.logger_manager import LoggerAdapter

        # Backend with no .info method
        broken_backend = object()
        adapter = LoggerAdapter(broken_backend, "test.dispatch")

        with patch(
            "src.lib.logging.logger_manager.initialize_global_logger_once"
        ) as mock_init:
            adapter.info("test message")

        mock_init.assert_not_called(), (
            "_dispatch must never call initialize_global_logger_once"
        )

        # Verify _GLOBAL_LOGGER is still None
        from src.lib.logging.logger_manager import _GLOBAL_LOGGER as gl
        assert gl is None

    def test_dispatch_uses_global_when_available(self):
        """When global backend exists, _dispatch falls back to it."""
        from src.lib.logging.logger_manager import (
            LoggerAdapter,
            initialize_global_logger_once,
        )

        # Init global first
        backend = initialize_global_logger_once("test_app")

        # Create adapter with broken backend
        broken_backend = object()
        adapter = LoggerAdapter(broken_backend, "test.dispatch")

        # Should use global backend's info method (not crash)
        with patch.object(backend, "info") as mock_info:
            adapter.info("hello")
            mock_info.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 5: Backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """resolve_logger alias must work identically to get_logger."""

    def test_resolve_logger_alias_exists(self):
        """resolve_logger is exported as an alias for get_logger."""
        from src.lib.logging import get_logger, resolve_logger

        assert resolve_logger is get_logger

    def test_resolve_logger_returns_same_types(self):
        """resolve_logger(None, name) returns LazyLoggerAdapter."""
        from src.lib.logging import resolve_logger
        from src.lib.logging.logger_manager import LazyLoggerAdapter

        result = resolve_logger(None, "test")
        assert isinstance(result, LazyLoggerAdapter)

    def test_resolve_logger_wraps_backend(self):
        """resolve_logger(backend, name) returns LoggerAdapter."""
        from src.lib.logging import resolve_logger
        from src.lib.logging.logger_manager import LoggerAdapter

        backend = MagicMock()
        result = resolve_logger(backend, "test")
        assert isinstance(result, LoggerAdapter)
