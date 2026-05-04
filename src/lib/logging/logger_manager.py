from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path
import re
from threading import Lock, RLock
from typing import Any, Protocol, runtime_checkable

from src.lib.config.config_validation import BoolParser, LogLevelParser

DEFAULT_LEVEL = "INFO"
DEFAULT_LOG_DIR = ".logs"
_ALLOWED_LOGGING_KEYS = {"enabled", "level", "file_path", "dir"}


_INIT_LOCK = Lock()
_INITIALIZED = False
_ACTIVE_LOG_FILE_PATH: Path | None = None
_PROCESS_LOG_FILE_PATH: Path | None = None
_PROCESS_LOG_PATH_LOCK = RLock()
_GLOBAL_LOGGER_LOCK = RLock()
_GLOBAL_LOGGER: Any | None = None
_SAFE_COMPONENT_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


def _get_config_value(*keys: str, default: Any = None) -> Any:
    try:
        from src.lib.config import C

        return C.get_nested(*keys, default=default)
    except Exception:
        return default


def _get_agent_root() -> Path:
    try:
        from src.lib.config import C

        return Path(C.agent_root).resolve()
    except Exception:
        return Path.cwd().resolve()


def _sanitize_path_component(value: str | None, *, fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    normalized = _SAFE_COMPONENT_PATTERN.sub("_", value.strip())
    normalized = normalized.strip("._")
    return normalized or fallback


def build_default_log_filename(app_name: str, now: datetime | None = None) -> str:
    """Build ``<app_name>.log`` — the per-run log filename.

    The timestamp is now encoded in the **parent directory** name
    (``{base}/{agent}/{timestamp}/``) so the filename itself is stable.

    Args:
        app_name: Application name from the YAML ``name`` field.
        now: Accepted for backward compatibility but no longer used.
    """
    stem = _sanitize_path_component(app_name, fallback="session")
    return f"{stem}.log"


def build_run_timestamp_dirname(now: datetime | None = None) -> str:
    """Build the per-run timestamp directory name ``YYYYMMDD_HHMMSS``.

    Args:
        now: Optional fixed datetime for deterministic names in tests.
    """
    return (now or datetime.now()).strftime("%Y%m%d_%H%M%S")


def _candidate_with_suffix(path: Path, suffix_index: int) -> Path:
    return path.with_name(f"{path.stem}_{suffix_index}{path.suffix}")


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
        logging.getLogger(__name__).warning(
            "%s contains unsupported keys and they will be ignored: %s",
            source,
            ", ".join(unknown),
        )
    return {key: value for key, value in logging_config.items() if key in _ALLOWED_LOGGING_KEYS}


@dataclass(frozen=True)
class LoggingConfigOverlay:
    enabled: Any = None
    level: Any = None
    file_path: Any = None
    dir: Any = None

    def to_mapping(self) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        if self.enabled is not None:
            mapping["enabled"] = self.enabled
        if self.level is not None:
            mapping["level"] = self.level
        if self.file_path is not None:
            mapping["file_path"] = self.file_path
        if self.dir is not None:
            mapping["dir"] = self.dir
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
    ) -> "LoggingConfigBuilder":
        self._layers.append((source, overlay.to_mapping()))
        return self

    def apply_mapping(
        self,
        mapping: dict[str, Any] | None,
        *,
        source: str = "overlay",
    ) -> "LoggingConfigBuilder":
        normalized = validate_logging_config(mapping, source=source)
        if normalized:
            self._layers.append((source, normalized))
        return self

    def extend(self, other: "LoggingConfigBuilder") -> "LoggingConfigBuilder":
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


def _resolve_explicit_path(path_value: str | Path, ensure_parent: bool = True) -> Path:
    path_obj = Path(path_value).expanduser()
    if not path_obj.is_absolute():
        path_obj = _get_agent_root() / path_obj
    resolved = path_obj.resolve()
    if ensure_parent:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def resolve_log_file_path(
    app_name: str,
    *,
    log_file_path: str | Path | None = None,
    now: datetime | None = None,
    logging_builder: LoggingConfigBuilder | None = None,
    ensure_parent: bool = True,
) -> Path:
    """Resolve final log file path.

    Priority:
      1) explicit *log_file_path* argument
      2) merged ``logging.file_path`` from config + override
      3) default layered path: ``{logging.dir}/{app_name}/{app_name}_{ts}.log``

    Args:
        app_name: Application name from the YAML ``name`` field.  Used both as
            the sub-directory under the log root **and** the filename stem.
    """
    if log_file_path:
        return _resolve_explicit_path(log_file_path, ensure_parent=ensure_parent)

    effective_logging = merge_logging_config(logging_builder)
    configured_path = effective_logging.get("file_path")
    if isinstance(configured_path, str) and configured_path.strip():
        return _resolve_explicit_path(configured_path.strip(), ensure_parent=ensure_parent)

    configured_dir = effective_logging.get("dir", DEFAULT_LOG_DIR)
    if isinstance(configured_dir, str) and configured_dir.strip():
        base_dir = Path(configured_dir.strip()).expanduser()
    else:
        base_dir = Path(DEFAULT_LOG_DIR)
    if not base_dir.is_absolute():
        base_dir = _get_agent_root() / base_dir
    base_dir = base_dir.resolve()

    sanitized_name = _sanitize_path_component(app_name, fallback="session")
    ts_dir_name = build_run_timestamp_dirname(now=now)
    # Layout: {base_dir}/{agent_name}/{timestamp}/{agent_name}.log
    target_dir = (base_dir / sanitized_name / ts_dir_name).resolve()
    filename = build_default_log_filename(app_name, now=now)

    # Handle collision: if the timestamp dir already exists (same-second run),
    # append a numeric suffix to the directory name.
    suffix_index = 1
    while target_dir.exists():
        target_dir = (base_dir / sanitized_name / f"{ts_dir_name}_{suffix_index}").resolve()
        suffix_index += 1

    if ensure_parent:
        target_dir.mkdir(parents=True, exist_ok=True)

    return (target_dir / filename).resolve()


def get_or_create_process_log_path(
    app_name: str,
    *,
    log_file_path: str | Path | None = None,
    now: datetime | None = None,
    logging_builder: LoggingConfigBuilder | None = None,
    ensure_parent: bool = True,
) -> Path:
    """Return process-scoped log file path, creating and caching it on first access.

    Args:
        app_name: Application name from the YAML ``name`` field.
    """
    global _PROCESS_LOG_FILE_PATH

    with _PROCESS_LOG_PATH_LOCK:
        if log_file_path:
            resolved = resolve_log_file_path(
                app_name,
                log_file_path=log_file_path,
                now=now,
                logging_builder=logging_builder,
                ensure_parent=ensure_parent,
            )
            _PROCESS_LOG_FILE_PATH = resolved
            return resolved

        if _PROCESS_LOG_FILE_PATH is not None:
            if ensure_parent:
                _PROCESS_LOG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            return _PROCESS_LOG_FILE_PATH

        resolved = resolve_log_file_path(
            app_name,
            log_file_path=None,
            now=now,
            logging_builder=logging_builder,
            ensure_parent=ensure_parent,
        )
        _PROCESS_LOG_FILE_PATH = resolved
        return resolved


def _configure_noisy_loggers() -> None:
    for lib in ("httpx", "litellm", "openai"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def initialize_logging(
    app_name: str,
    *,
    force_reconfigure: bool = False,
    log_file_path: str | Path | None = None,
) -> Path | None:
    """Initialize root logging exactly once (unless forced).

    Args:
        app_name: Application name from the YAML ``name`` field.
    """
    global _INITIALIZED, _ACTIVE_LOG_FILE_PATH

    with _INIT_LOCK:
        if _INITIALIZED and not force_reconfigure and log_file_path is None:
            return _ACTIVE_LOG_FILE_PATH

        root_logger = logging.getLogger()
        if force_reconfigure or not _INITIALIZED:
            for handler in list(root_logger.handlers):
                root_logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass

        enabled = BoolParser.parse(_get_config_value("logging", "enabled", default=True), default=True)
        level = LogLevelParser.parse(_get_config_value("logging", "level", default=DEFAULT_LEVEL))

        if not enabled or level >= LogLevelParser.OFF_LEVEL:
            logging.disable(LogLevelParser.OFF_LEVEL)
            root_logger.setLevel(LogLevelParser.OFF_LEVEL)
            _ACTIVE_LOG_FILE_PATH = None
            _INITIALIZED = True
            return None

        logging.disable(logging.NOTSET)
        resolved_log_file = get_or_create_process_log_path(app_name, log_file_path=log_file_path)
        formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(level)
        stream_handler.setFormatter(formatter)

        file_handler = logging.FileHandler(resolved_log_file, mode="a", encoding="utf-8", delay=True)
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)

        root_logger.addHandler(stream_handler)
        root_logger.addHandler(file_handler)
        root_logger.setLevel(level)

        _configure_noisy_loggers()

        _ACTIVE_LOG_FILE_PATH = resolved_log_file
        _INITIALIZED = True
        return resolved_log_file


def get_active_log_file_path() -> Path | None:
    return _ACTIVE_LOG_FILE_PATH


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


def build_logger_backend_from_config(
    app_name: str,
    *,
    logging_builder: LoggingConfigBuilder | None = None,
    log_file_path: str | Path | None = None,
    now: datetime | None = None,
) -> Any:
    """Build an :class:`EnhancedAgentLogger` from merged logging config.

    Args:
        app_name: Application name from the YAML ``name`` field.
    """
    # Lazy import to keep logging core lightweight and avoid import cycles.
    from src.lib.logging.agent_logger import EnhancedAgentLogger, AgentLoomLogLevel
    from src.lib.logging.rich_console import DualConsole

    effective_logging = merge_logging_config(logging_builder)
    enabled = BoolParser.parse(effective_logging.get("enabled", True), default=True)
    level = _resolve_agent_log_level(effective_logging.get("level", DEFAULT_LEVEL))
    if not enabled or level == AgentLoomLogLevel.OFF:
        return NullLoggerBackend()

    resolved_path = get_or_create_process_log_path(
        app_name,
        log_file_path=log_file_path,
        now=now,
        logging_builder=LoggingConfigBuilder().apply_mapping(
            effective_logging,
            source="effective_logging",
        ),
    )
    console = DualConsole(log_file_path=str(resolved_path))
    return EnhancedAgentLogger(
        level=level,
        console=console,
        show_timestamp=True,
        timestamp_format="%Y-%m-%d %H:%M:%S",
        show_trace_info=True,
        truncate_id_length=8,
    )


def get_current_run_log_dir() -> Path | None:
    """Return the per-run timestamp log directory for the current process.

    Shell audit loggers and other subsystems call this to share the same
    run directory as the main agent log, producing a layout like::

        .logs/{agent_name}/{timestamp}/
            {agent_name}.log
            shell_audit.log

    Returns ``None`` if no process log path has been established yet.
    """
    if _PROCESS_LOG_FILE_PATH is not None:
        return _PROCESS_LOG_FILE_PATH.parent
    return None


def initialize_global_logger_once(app_name: str) -> Any | None:
    """Initialize process-global logger backend exactly once.

    Must be called with the YAML ``name`` field **before** any agent
    construction.  Subsequent calls return the cached backend.

    Args:
        app_name: Application name from the YAML ``name`` field.  Determines
            the log directory: ``{logging.dir}/{app_name}/{timestamp}/{app_name}.log``.
    """
    if _GLOBAL_LOGGER is not None:
        return _GLOBAL_LOGGER

    backend = build_logger_backend_from_config(app_name)
    set_global_logger(backend)
    return backend


def set_global_logger(logger_backend: Any | None) -> None:
    """Set or clear process-wide default logger backend."""
    global _GLOBAL_LOGGER
    previous_backend: Any | None = None
    with _GLOBAL_LOGGER_LOCK:
        previous_backend = _GLOBAL_LOGGER
        _GLOBAL_LOGGER = logger_backend
    if previous_backend is not None and previous_backend is not logger_backend:
        _close_logger_backend_sink(previous_backend)


def _close_logger_backend_sink(logger_backend: Any | None) -> None:
    if logger_backend is None:
        return
    try:
        console = getattr(logger_backend, "console", None)
        close_log_file = getattr(console, "close_log_file", None)
        if callable(close_log_file):
            close_log_file()
    except Exception:
        # Logger cleanup should never break caller flow.
        return


def get_global_logger(*, create_if_missing: bool = False) -> Any | None:
    """Get process-wide default logger backend.

    Returns the backend previously created by :func:`initialize_global_logger_once`.
    If ``create_if_missing`` is *False* (default) and no backend has been
    registered yet, returns *None* instead of attempting lazy creation.
    """
    if _GLOBAL_LOGGER is not None:
        return _GLOBAL_LOGGER

    if not create_if_missing:
        return None

    return _GLOBAL_LOGGER


_FALLBACK_APP_NAME = "AgentLoom"


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
    dynamically looks up the process-global backend via
    :func:`get_global_logger`; if none exists yet, it falls back to the
    stdlib ``logging`` hierarchy.

    *Bound mode* (``backend=<object>``): created by ``get_logger(backend_obj, __name__)``.
    Wraps a concrete backend (``EnhancedAgentLogger``, stdlib ``Logger``, etc.)
    and dispatches directly to it.  If the backend method is missing or fails,
    applies the same global → stdlib fallback chain as lazy mode.

    After ``initialize_global_logger_once(app_name)`` is called (typically by
    :func:`src.runner.run_app`), **all** lazy-mode instances automatically
    pick up the correct backend on subsequent calls.

    Usage::

        # Module level — safe, lazy mode
        logger = get_logger(__name__)

        # Inside a function — bound mode
        def build_agent(logger_backend):
            log = get_logger(logger_backend, __name__)
            log.info("building...")
    """

    __slots__ = ("_backend", "_name")

    def __init__(self, backend: Any = None, name: str | None = None):
        self._backend = backend
        self._name = name or __name__

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
            if isinstance(self._backend, logging.Logger):
                used_stdlib = True
            if _call_log_method(self._backend, method_name, rendered, **kwargs):
                # Also mirror to stdlib so caplog / third-party handlers work.
                if not used_stdlib:
                    _stdlib_emit(self._name, method_name, rendered)
                return
            # Backend method missing/failed — fall through to global/stdlib.

        # Lazy mode or bound-mode fallback: global backend → stdlib.
        backend = self._resolve_backend()
        if isinstance(backend, logging.Logger):
            used_stdlib = True
        _call_log_method(backend, method_name, rendered, **kwargs)

        # Mirror to stdlib if the backend was not stdlib itself.
        if not used_stdlib:
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
    name_or_backend: "str | Any | None" = None,
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
