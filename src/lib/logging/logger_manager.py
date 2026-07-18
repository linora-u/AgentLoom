from __future__ import annotations

import contextvars
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from src.lib.config.config_validation import BoolParser
from src.lib.runtime import RuntimeContext, get_current_run_context

DEFAULT_LEVEL = "INFO"
DEFAULT_CONSOLE_ENABLED = True
DEFAULT_FILE_ENABLED = True
DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 3
_ALLOWED_LOGGING_KEYS = {
    "level",
    "console_enabled",
    "file_enabled",
    "max_file_bytes",
    "backup_count",
}
_MEMORY_CAMPAIGN_SAFE_ARTIFACTS_ENV = "AGENTLOOM_MEMORY_CAMPAIGN_SAFE_ARTIFACTS"


@dataclass(frozen=True)
class _LoggerBinding:
    backend: Any
    runtime_key: tuple[str, str, str, str] | None


_CURRENT_LOGGER_BINDING: contextvars.ContextVar[_LoggerBinding | None] = (
    contextvars.ContextVar("agentloom_logger_backend", default=None)
)


def _memory_campaign_safe_artifacts_enabled() -> bool:
    """Return whether the internal validation campaign forbids file sinks."""
    return os.environ.get(_MEMORY_CAMPAIGN_SAFE_ARTIFACTS_ENV, "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _get_config_value(*keys: str, default: Any = None) -> Any:
    try:
        from src.lib.config import C

        return C.get_nested(*keys, default=default)
    except Exception:
        return default


def _resolve_config_logging_section() -> dict[str, Any]:
    cfg = _get_config_value("logging", default={})
    if isinstance(cfg, dict):
        return dict(cfg)
    return {}


def validate_logging_config(
    logging_config: dict[str, Any] | None,
    *,
    source: str = "logging",
) -> dict[str, Any]:
    if logging_config is None:
        return {}
    if not isinstance(logging_config, dict):
        raise ValueError(f"{source} must be a mapping")
    unknown = sorted(set(logging_config.keys()) - _ALLOWED_LOGGING_KEYS)
    if unknown:
        raise ValueError(
            f"{source} contains unsupported logging key(s): "
            f"{', '.join(unknown)}"
        )
    return dict(logging_config)


@dataclass(frozen=True)
class LoggingConfigOverlay:
    level: Any = None
    console_enabled: Any = None
    file_enabled: Any = None
    max_file_bytes: Any = None
    backup_count: Any = None

    def to_mapping(self) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        if self.level is not None:
            mapping["level"] = self.level
        if self.console_enabled is not None:
            mapping["console_enabled"] = self.console_enabled
        if self.file_enabled is not None:
            mapping["file_enabled"] = self.file_enabled
        if self.max_file_bytes is not None:
            mapping["max_file_bytes"] = self.max_file_bytes
        if self.backup_count is not None:
            mapping["backup_count"] = self.backup_count
        return mapping


class LoggingConfigBuilder:
    """Build effective logging config through ordered overlays."""

    def __init__(self) -> None:
        self._layers: list[tuple[str, dict[str, Any]]] = []

    def apply_overlay(
        self,
        overlay: LoggingConfigOverlay,
        *,
        source: str = "overlay",
    ) -> LoggingConfigBuilder:
        self._layers.append((source, overlay.to_mapping()))
        return self

    def apply_mapping(
        self,
        mapping: dict[str, Any] | None,
        *,
        source: str = "overlay",
    ) -> LoggingConfigBuilder:
        normalized = validate_logging_config(mapping, source=source)
        if normalized:
            self._layers.append((source, normalized))
        return self

    def extend(self, other: LoggingConfigBuilder) -> LoggingConfigBuilder:
        self._layers.extend(other._layers)
        return self

    def build(self, *, include_system_defaults: bool = True) -> dict[str, Any]:
        merged = _resolve_config_logging_section() if include_system_defaults else {}
        if not isinstance(merged, dict):
            merged = {}
        merged = dict(merged)
        for _source, layer in self._layers:
            for key, value in layer.items():
                merged[key] = value
        return validate_logging_config(merged, source="effective_logging")


def merge_logging_config(logging_builder: LoggingConfigBuilder | None = None) -> dict[str, Any]:
    if logging_builder is None:
        logging_builder = LoggingConfigBuilder()
    return logging_builder.build(include_system_defaults=True)


def _extract_backend_log_file_path(logger_backend: Any | None) -> Path | None:
    if logger_backend is None:
        return None
    try:
        console = getattr(logger_backend, "console", None)
        log_file_path = getattr(console, "log_file_path", None)
        if not isinstance(log_file_path, str) or not log_file_path.strip():
            return None
        return Path(log_file_path).expanduser().resolve()
    except Exception:
        return None


class NullLoggerBackend:
    """No-op logger backend used when logging is explicitly disabled."""

    level = "OFF"
    console = None
    mirror_to_stdlib = False

    def log(self, *args: Any, **kwargs: Any) -> None:
        return None

    def debug(self, *args: Any, **kwargs: Any) -> None:
        return None

    def info(self, *args: Any, **kwargs: Any) -> None:
        return None

    def warning(self, *args: Any, **kwargs: Any) -> None:
        return None

    def error(self, *args: Any, **kwargs: Any) -> None:
        return None


def _resolve_agent_log_level(level_value: Any):
    """Convert any level representation to AgentLoomLogLevel."""
    from src.lib.logging.agent_logger import AgentLoomLogLevel

    if isinstance(level_value, AgentLoomLogLevel):
        return level_value
    if isinstance(level_value, int):
        return AgentLoomLogLevel.from_int(level_value)
    if isinstance(level_value, str):
        try:
            return AgentLoomLogLevel.from_str(level_value)
        except ValueError:
            return AgentLoomLogLevel.INFO
    return AgentLoomLogLevel.INFO


def _positive_int(value: Any, *, default: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _runtime_key(context: RuntimeContext | None) -> tuple[str, str, str, str] | None:
    if context is None:
        return None
    return (
        str(context.root_dir),
        context.application_id,
        context.task_id,
        context.run_id,
    )


def _backend_runtime_tag(backend: Any) -> tuple[str, str, str, str] | None:
    try:
        attributes = vars(backend)
    except TypeError:
        attributes = {}
    value = attributes.get("_agentloom_runtime_key")
    return value if isinstance(value, tuple) and len(value) == 4 else None


def _tag_backend_runtime(backend: Any, context: RuntimeContext | None) -> Any:
    """Attach immutable run ownership to a backend when the object permits it."""
    runtime_key = _runtime_key(context)
    if runtime_key is None:
        return backend
    existing = _backend_runtime_tag(backend)
    if existing is not None and existing != runtime_key:
        raise ValueError("logger backend is already owned by a different RuntimeContext")
    try:
        backend._agentloom_runtime_key = runtime_key
    except Exception:
        pass
    return backend


def build_logger_backend_from_config(
    app_name: str = "AgentLoom",
    *,
    logging_builder: LoggingConfigBuilder | None = None,
    runtime_context: RuntimeContext | None = None,
    file_logging: bool | None = None,
) -> Any:
    """Build a logger without deriving any filesystem path.

    File output is only possible when a canonical ``RuntimeContext`` is
    supplied.  Standalone callers receive a console-only backend.
    """
    # Lazy import to keep logging core lightweight and avoid import cycles.
    from rich.console import Console

    from src.lib.logging.agent_logger import AgentLoomLogLevel, EnhancedAgentLogger
    from src.lib.logging.rich_console import DualConsole

    effective_logging = merge_logging_config(logging_builder)
    level = _resolve_agent_log_level(effective_logging.get("level", DEFAULT_LEVEL))
    console_enabled = BoolParser.parse(
        effective_logging.get("console_enabled", DEFAULT_CONSOLE_ENABLED),
        default=DEFAULT_CONSOLE_ENABLED,
    )
    file_enabled = BoolParser.parse(
        effective_logging.get("file_enabled", DEFAULT_FILE_ENABLED),
        default=DEFAULT_FILE_ENABLED,
    )
    if file_logging is not None:
        file_enabled = bool(file_logging)
    if _memory_campaign_safe_artifacts_enabled():
        file_enabled = False
    if runtime_context is None:
        file_enabled = False

    if level == AgentLoomLogLevel.OFF or not (console_enabled or file_enabled):
        return _tag_backend_runtime(NullLoggerBackend(), runtime_context)

    if file_enabled and runtime_context is not None:
        max_file_bytes = _positive_int(
            effective_logging.get("max_file_bytes"),
            default=DEFAULT_MAX_FILE_BYTES,
            minimum=1,
        )
        backup_count = _positive_int(
            effective_logging.get("backup_count"),
            default=DEFAULT_BACKUP_COUNT,
            minimum=0,
        )
        console = DualConsole(
            log_file_path=str(runtime_context.log_path),
            max_file_bytes=max_file_bytes,
            backup_count=backup_count,
            console_enabled=console_enabled,
            runtime_context=runtime_context,
            highlight=False,
        )
    elif console_enabled:
        console = Console(highlight=False)
    else:
        return _tag_backend_runtime(NullLoggerBackend(), runtime_context)
    backend = EnhancedAgentLogger(
        level=level,
        console=console,
        show_timestamp=True,
        timestamp_format="%Y-%m-%d %H:%M:%S",
        show_trace_info=True,
        truncate_id_length=8,
    )
    return _tag_backend_runtime(backend, runtime_context)


def initialize_run_logger(
    context: RuntimeContext,
    *,
    logging_builder: LoggingConfigBuilder | None = None,
    file_logging: bool | None = None,
) -> Any:
    """Create the backend for one execution attempt.

    Binding is deliberately separate so callers can establish the complete
    run context before constructing agents and can deterministically close it.
    """
    return build_logger_backend_from_config(
        context.application_id,
        logging_builder=logging_builder,
        runtime_context=context,
        file_logging=file_logging,
    )


def close_run_logger(logger_backend: Any | None) -> None:
    """Close a run logger's file sink without changing any context binding."""
    if logger_backend is None:
        return
    try:
        console = getattr(logger_backend, "console", None)
        close_log_file = getattr(console, "close_log_file", None)
        if callable(close_log_file):
            close_log_file()
    except Exception:
        # Logging cleanup must never replace the application's real outcome.
        return


@contextmanager
def bind_logger_backend(
    logger_backend: Any,
    *,
    context: RuntimeContext | None = None,
) -> Iterator[Any]:
    """Bind a backend to exactly one RuntimeContext and close it on exit."""
    effective_context = context or get_current_run_context()
    _tag_backend_runtime(logger_backend, effective_context)
    token = _CURRENT_LOGGER_BINDING.set(
        _LoggerBinding(logger_backend, _runtime_key(effective_context))
    )
    try:
        if effective_context is not None:
            from src.tools.shell.shell_audit_log import (
                initialize_shell_audit_scope,
            )

            initialize_shell_audit_scope(effective_context)
        yield logger_backend
    finally:
        try:
            from src.tools.shell.shell_audit_log import (
                close_current_shell_audit_loggers,
            )

            close_current_shell_audit_loggers()
        except Exception:
            # Audit cleanup is best-effort and must not mask run failures.
            pass
        _CURRENT_LOGGER_BINDING.reset(token)
        close_run_logger(logger_backend)


def initialize_global_logger_once(app_name: str) -> Any | None:
    """Legacy helper for tests and standalone construction.

    It never invents a file path.  When called under a RuntimeContext the
    canonical run path is used; otherwise the backend is console-only.
    """
    existing = get_global_logger(create_if_missing=False)
    if existing is not None:
        return existing
    runtime_context = get_current_run_context()
    backend = build_logger_backend_from_config(
        app_name,
        runtime_context=runtime_context,
    )
    set_global_logger(backend)
    return backend


def set_global_logger(logger_backend: Any | None) -> None:
    """Set or clear the backend in the current execution context only."""
    previous = _CURRENT_LOGGER_BINDING.get()
    if logger_backend is None:
        _CURRENT_LOGGER_BINDING.set(None)
    else:
        context = get_current_run_context()
        _tag_backend_runtime(logger_backend, context)
        _CURRENT_LOGGER_BINDING.set(
            _LoggerBinding(logger_backend, _runtime_key(context))
        )
    if previous is not None and previous.backend is not logger_backend:
        close_run_logger(previous.backend)


def get_global_logger(*, create_if_missing: bool = False) -> Any | None:
    """Get the backend bound to the current execution context.

    A backend tied to another (or already-ended) run is deliberately ignored.
    ``create_if_missing`` remains accepted for call-site stability but never
    creates a backend implicitly.
    """
    binding = _CURRENT_LOGGER_BINDING.get()
    if binding is None:
        return None
    if binding.runtime_key != _runtime_key(get_current_run_context()):
        return None
    return binding.backend


def get_active_log_file_path() -> Path | None:
    """Return the current run backend's canonical file path, if enabled."""
    return _extract_backend_log_file_path(get_global_logger())


@runtime_checkable
class UnifiedLogger(Protocol):
    def debug(self, msg: Any, *args: Any, **kwargs: Any) -> None: ...

    def info(self, msg: Any, *args: Any, **kwargs: Any) -> None: ...

    def warning(self, msg: Any, *args: Any, **kwargs: Any) -> None: ...

    def error(self, msg: Any, *args: Any, **kwargs: Any) -> None: ...


# ---------------------------------------------------------------------------
# LoggerAdapter – the single logger wrapper for the entire project
# ---------------------------------------------------------------------------

def _format_message(msg: Any, args: tuple[Any, ...]) -> str:
    """Printf-style message formatting with safe fallback."""
    template = msg if isinstance(msg, str) else str(msg)
    if not args:
        return template
    try:
        return template % args
    except Exception:
        joined = " ".join(str(item) for item in args)
        return f"{template} {joined}".strip()


def _call_log_method(backend: Any, method_name: str, rendered: str, **kwargs: Any) -> bool:
    """Try to call *method_name* on *backend*.  Return True on success."""
    method = getattr(backend, method_name, None)
    if not callable(method):
        return False
    try:
        method(rendered, **kwargs)
    except TypeError:
        method(rendered)
    return True


def _stdlib_emit(logger_name: str, method_name: str, rendered: str) -> None:
    """Emit a log record through the stdlib logging hierarchy.

    This ensures that pytest ``caplog``, third-party log aggregators, and any
    handlers attached to the stdlib root logger always see our log output,
    even when the primary backend is ``EnhancedAgentLogger`` (which bypasses
    stdlib entirely).
    """
    stdlib_logger = logging.getLogger(logger_name)
    method = getattr(stdlib_logger, method_name, None)
    if callable(method):
        method(rendered)


class LoggerAdapter:
    """Unified logger adapter — safe at module level AND as a backend wrapper.

    **Two modes**, determined by the constructor arguments:

    *Lazy mode* (``backend=None``): created by ``get_logger(__name__)``.
    Safe at **import time** — never triggers ``initialize_global_logger_once``
    or any other side-effecting initialisation.  On each log call it
    dynamically looks up the run-scoped backend via
    :func:`get_global_logger`; if none exists yet, it falls back to the
    stdlib ``logging`` hierarchy.

    *Bound mode* (``backend=<object>``): created by ``get_logger(backend_obj, __name__)``.
    Wraps a concrete backend (``EnhancedAgentLogger``, stdlib ``Logger``, etc.)
    and dispatches directly to it.  If the backend method is missing or fails,
    applies the same global → stdlib fallback chain as lazy mode.

    After a backend is bound with :func:`bind_logger_backend`, **all** lazy-mode
    instances in that context automatically pick it up on subsequent calls.

    Usage::

        # Module level — safe, lazy mode
        logger = get_logger(__name__)

        # Inside a function — bound mode
        def build_agent(logger_backend):
            log = get_logger(logger_backend, __name__)
            log.info("building...")
    """

    __slots__ = ("_backend", "_name", "_runtime_key")

    def __init__(self, backend: Any = None, name: str | None = None):
        self._backend = backend
        self._name = name or __name__
        self._runtime_key = _backend_runtime_tag(backend)
        if backend is not None and self._runtime_key is None:
            binding = _CURRENT_LOGGER_BINDING.get()
            if binding is not None and binding.backend is backend:
                self._runtime_key = binding.runtime_key

    # -- internal helpers ---------------------------------------------------

    @property
    def is_lazy(self) -> bool:
        """True when this adapter defers backend resolution (lazy mode)."""
        return self._backend is None

    def _resolve_backend(self) -> Any:
        """Return the best available backend *without* initialisation.

        Used by both lazy mode (always) and bound mode (fallback path).
        """
        backend = get_global_logger(create_if_missing=False)
        if backend is not None:
            return backend
        return logging.getLogger(self._name)

    def _dispatch(self, method_name: str, msg: Any, *args: Any, **kwargs: Any) -> None:
        rendered = _format_message(msg, args)
        used_stdlib = False

        # Bound mode: try the explicit backend first.
        if self._backend is not None:
            if (
                self._runtime_key is not None
                and self._runtime_key != _runtime_key(get_current_run_context())
            ):
                _stdlib_emit(self._name, method_name, rendered)
                return
            if isinstance(self._backend, logging.Logger):
                used_stdlib = True
            if _call_log_method(self._backend, method_name, rendered, **kwargs):
                # Also mirror to stdlib so caplog / third-party handlers work.
                if not used_stdlib and getattr(self._backend, "mirror_to_stdlib", True):
                    _stdlib_emit(self._name, method_name, rendered)
                return
            # Backend method missing/failed — fall through to global/stdlib.

        # Lazy mode or bound-mode fallback: global backend → stdlib.
        backend = self._resolve_backend()
        if isinstance(backend, logging.Logger):
            used_stdlib = True
        _call_log_method(backend, method_name, rendered, **kwargs)

        # Mirror to stdlib if the backend was not stdlib itself.
        if not used_stdlib and getattr(backend, "mirror_to_stdlib", True):
            _stdlib_emit(self._name, method_name, rendered)

    # -- public API (only four methods, nothing else) -----------------------

    def debug(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._dispatch("debug", msg, *args, **kwargs)

    def info(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._dispatch("info", msg, *args, **kwargs)

    def warning(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._dispatch("warning", msg, *args, **kwargs)

    def error(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        self._dispatch("error", msg, *args, **kwargs)


# Backward-compatible alias — existing code that references the old class
# name will continue to work.
LazyLoggerAdapter = LoggerAdapter


# ---------------------------------------------------------------------------
# get_logger – the ONE unified logger API
# ---------------------------------------------------------------------------

def get_logger(
    name_or_backend: str | Any | None = None,
    name: str | None = None,
) -> LoggerAdapter:
    """Obtain a logger — the **single unified API** for all logging needs.

    Two usage patterns:

    **1. Module-level logger** (safe at import time)::

        from src.lib.logging import get_logger
        logger = get_logger(__name__)

    Returns a :class:`LoggerAdapter` in *lazy mode* (``backend=None``) that
    defers backend resolution.  No side effects, no file creation, no global
    state mutation.

    **2. Wrap an existing backend** (inside functions/methods)::

        def build_agent(logger_backend):
            log = get_logger(logger_backend, __name__)
            log.info("building...")

    Returns a :class:`LoggerAdapter` in *bound mode* wrapping the backend.

    Args:
        name_or_backend: Either a ``str`` module name (pattern 1) or a
            logger backend object (pattern 2).  ``None`` is treated as
            pattern 1 with a default name.
        name: Optional module name used when *name_or_backend* is a
            backend object (pattern 2).  Ignored when *name_or_backend*
            is a ``str``.

    Returns:
        :class:`LoggerAdapter` (lazy-mode or bound-mode).
    """
    # Already wrapped — return as-is (idempotent).
    if isinstance(name_or_backend, LoggerAdapter):
        return name_or_backend

    # Pattern 1: str or None → lazy-mode adapter (module-level safe).
    if name_or_backend is None or isinstance(name_or_backend, str):
        effective_name = name_or_backend or name or __name__
        return LoggerAdapter(backend=None, name=effective_name)

    # Pattern 2: backend object → bound-mode adapter.
    return LoggerAdapter(backend=name_or_backend, name=name)


# Backward-compatible alias – will be removed in a future release.
resolve_logger = get_logger
