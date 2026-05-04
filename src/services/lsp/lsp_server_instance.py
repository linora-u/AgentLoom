"""Single LSP server lifecycle — aligned with Claude Code's LSPServerInstance.

State machine::

    STOPPED ──start()──→ STARTING ──ok──→ RUNNING
       ↑                    │                │
       │                    └─fail──→ ERROR  │
       │                               │    │
       └────────stop()─────────────────┘    │
                                            [crash]
                                              │
                                              ▼
                                           ERROR
                                        [auto restart
                                         if count ≤ max]
"""

from __future__ import annotations

import os
import sys
from enum import Enum
from typing import Any, Optional

from src.lib.logging import get_logger

logger = get_logger(__name__)


class LSPServerState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"


# ---------------------------------------------------------------------------
# PATH helpers (ensure .venv/bin + ~/go/bin are discoverable)
# ---------------------------------------------------------------------------
_PATH_ENSURED = False


def ensure_lsp_paths() -> None:
    """Add venv bin and GOPATH/bin to PATH for LSP server discovery.

    Called once during manager initialization.  Ensures that binaries
    installed by ``go-bin`` (.venv/bin/go), ``nodejs-bin`` (.venv/bin/node),
    ``jedi-language-server`` (.venv/bin/jedi-language-server), and ``gopls``
    (~/go/bin/gopls) are discoverable by solidlsp subprocess spawns.
    """
    global _PATH_ENSURED
    if _PATH_ENSURED:
        return

    current_path = os.environ.get("PATH", "")
    parts = current_path.split(os.pathsep)
    additions = []

    # .venv/bin — go, node, npm, jedi-language-server, etc.
    venv_bin = os.path.dirname(sys.executable)
    if venv_bin not in parts:
        additions.append(venv_bin)

    # ~/go/bin — gopls (installed via 'go install')
    gopath_bin = os.path.join(os.path.expanduser("~"), "go", "bin")
    if os.path.isdir(gopath_bin) and gopath_bin not in parts:
        additions.append(gopath_bin)

    if additions:
        os.environ["PATH"] = os.pathsep.join(additions) + os.pathsep + current_path
        logger.debug("Added to PATH for LSP discovery: %s", additions)

    _PATH_ENSURED = True


# ---------------------------------------------------------------------------
# solidlsp availability check
# ---------------------------------------------------------------------------
_SOLIDLSP_AVAILABLE: Optional[bool] = None


def _check_solidlsp() -> bool:
    global _SOLIDLSP_AVAILABLE
    if _SOLIDLSP_AVAILABLE is not None:
        return _SOLIDLSP_AVAILABLE
    try:
        from solidlsp.ls import SolidLanguageServer  # noqa: F401
        _SOLIDLSP_AVAILABLE = True
    except ImportError:
        _SOLIDLSP_AVAILABLE = False
        logger.warning(
            "solidlsp not installed; LSP features will use tree-sitter fallback. "
            "Install with: uv add serena-agent"
        )
    return _SOLIDLSP_AVAILABLE


# ---------------------------------------------------------------------------
# Language name → solidlsp Language enum mapping
# ---------------------------------------------------------------------------

_LANG_NAME_TO_ENUM = None  # Lazy-built


def _resolve_language_enum(language_name: str):
    """Convert a language name string to a solidlsp Language enum value.

    Raises ValueError if the language is not supported.
    """
    global _LANG_NAME_TO_ENUM
    if _LANG_NAME_TO_ENUM is None:
        from solidlsp.ls_config import Language
        _LANG_NAME_TO_ENUM = {member.value: member for member in Language}
        # Also add uppercase names as keys
        for member in Language:
            _LANG_NAME_TO_ENUM[member.name.lower()] = member

    key = language_name.lower().strip()
    if key in _LANG_NAME_TO_ENUM:
        return _LANG_NAME_TO_ENUM[key]

    available = sorted(set(m.value for m in _LANG_NAME_TO_ENUM.values()))
    raise ValueError(
        f"Unsupported LSP language: '{language_name}'. "
        f"Available: {available}"
    )


class LSPServerInstance:
    """Manages a single LSP server lifecycle.

    Wraps a ``solidlsp.SolidLanguageServer``.  Provides:
    - Idempotent start/stop
    - State tracking
    - Automatic crash recovery (up to *max_restarts*)
    - Request proxy with auto-restart on failure
    """

    def __init__(
        self,
        language: str,
        project_root: str,
        max_restarts: int = 3,
    ):
        self.language = language
        self.project_root = project_root
        self.state = LSPServerState.STOPPED
        self._server: Any = None
        self._crash_count = 0
        self._max_restarts = max_restarts

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the language server.  Idempotent — no-op if already running."""
        if self.state == LSPServerState.RUNNING:
            return
        if not _check_solidlsp():
            self.state = LSPServerState.ERROR
            raise RuntimeError("solidlsp not installed")

        self.state = LSPServerState.STARTING
        try:
            from solidlsp.ls import SolidLanguageServer
            from solidlsp.ls_config import LanguageServerConfig, Language

            # Map string language name to solidlsp Language enum
            lang_enum = _resolve_language_enum(self.language)
            config = LanguageServerConfig(code_language=lang_enum)
            self._server = SolidLanguageServer.create(config, self.project_root)
            self._server.start()
            self.state = LSPServerState.RUNNING
            logger.info("LSP server running: %s at %s", self.language, self.project_root)
        except Exception as exc:
            self.state = LSPServerState.ERROR
            logger.warning("LSP server start failed (%s): %s", self.language, exc)
            raise

    def stop(self) -> None:
        """Stop the server gracefully."""
        if self._server is not None:
            try:
                self._server.stop(shutdown_timeout=2.0)
            except Exception as exc:
                logger.debug("LSP stop error (%s): %s", self.language, exc)
            self._server = None
        self.state = LSPServerState.STOPPED

    def restart(self) -> None:
        """Restart with crash recovery limit check."""
        self._crash_count += 1
        if self._crash_count > self._max_restarts:
            self.state = LSPServerState.ERROR
            raise RuntimeError(
                f"LSP {self.language}: max restarts ({self._max_restarts}) exceeded"
            )
        logger.info(
            "LSP server restarting (%s), attempt %d/%d",
            self.language, self._crash_count, self._max_restarts,
        )
        self.stop()
        self.start()

    # ------------------------------------------------------------------
    # Request proxy
    # ------------------------------------------------------------------

    def request(self, method: str, *args, **kwargs) -> Any:
        """Call a method on the underlying server with auto-restart on crash.

        If the server is not running, start it first.
        If the request fails, try restart once then retry.
        """
        if self.state != LSPServerState.RUNNING:
            try:
                self.start()
            except Exception:
                return None

        try:
            fn = getattr(self._server, method)
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.debug("LSP request %s failed (%s): %s", method, self.language, exc)
            if self._crash_count < self._max_restarts:
                try:
                    self.restart()
                    fn = getattr(self._server, method)
                    return fn(*args, **kwargs)
                except Exception as retry_exc:
                    logger.warning(
                        "LSP request %s retry failed (%s): %s",
                        method, self.language, retry_exc,
                    )
            return None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_healthy(self) -> bool:
        return self.state == LSPServerState.RUNNING and self._server is not None
