"""Global LSP server orchestrator — aligned with Claude Code's LSPServerManager.

Responsibilities:
- Route file requests to the correct language server by extension
- Pre-warm servers at agent startup (long-lived, not per-request)
- Manage shutdown via atexit
- Singleton pattern — one manager per process
"""

from __future__ import annotations

import atexit
import os
from pathlib import Path
from typing import Dict, List, Optional

from src.lib.logging import get_logger

from .config import LSPConfig, LSPServerConfig
from .lsp_server_instance import LSPServerInstance, ensure_lsp_paths

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Extension → language mapping
# ---------------------------------------------------------------------------
_LANG_EXTENSIONS: Dict[str, List[str]] = {
    "python": [".py", ".pyw"],
    "go": [".go"],
    "typescript": [".ts", ".tsx"],
    "javascript": [".js", ".jsx", ".mjs"],
    "rust": [".rs"],
    "java": [".java"],
    "csharp": [".cs"],
    "kotlin": [".kt", ".kts"],
    "ruby": [".rb"],
    "dart": [".dart"],
    "c": [".c", ".h"],
    "cpp": [".cpp", ".cc", ".cxx", ".hpp", ".hxx"],
    "lua": [".lua"],
    "php": [".php"],
    "swift": [".swift"],
    "scala": [".scala"],
    "haskell": [".hs"],
    "elixir": [".ex", ".exs"],
    "bash": [".sh", ".bash", ".zsh"],
    "zig": [".zig"],
    "ocaml": [".ml", ".mli"],
    "fortran": [".f90", ".f95", ".f03"],
    "r": [".r", ".R"],
    "perl": [".pl", ".pm"],
    "yaml": [".yaml", ".yml"],
}

# Reverse map: extension → language
_EXT_TO_LANG: Dict[str, str] = {}
for _lang, _exts in _LANG_EXTENSIONS.items():
    for _ext in _exts:
        _EXT_TO_LANG[_ext] = _lang


class LSPServerManager:
    """Global LSP server manager — singleton.

    Usage::

        # At agent startup (runner.py):
        manager = LSPServerManager.get_instance()
        manager.initialize(config, project_root)

        # From tools:
        instance = manager.get_server_for_file("src/main.py")
        if instance and instance.is_healthy:
            result = instance.request("request_definition", rel_path, line, col)
    """

    _instance: Optional[LSPServerManager] = None

    def __init__(self) -> None:
        self._servers: Dict[str, LSPServerInstance] = {}  # lang → instance
        self._extension_map: Dict[str, str] = {}  # .py → "python"
        self._config: Optional[LSPConfig] = None
        self._project_root: str = ""
        self._initialized = False
        self._atexit_registered = False

    @classmethod
    def get_instance(cls) -> LSPServerManager:
        """Get or create the singleton manager."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton — for testing only."""
        if cls._instance is not None:
            cls._instance.shutdown()
            cls._instance = None

    def initialize(self, config: LSPConfig, project_root: str) -> None:
        """Initialize and pre-warm all configured language servers.

        Called once from ``runner.py`` at agent startup.  Servers remain
        alive for the entire agent session.

        Args:
            config: LSP configuration from system.yaml.
            project_root: Absolute path to the project root.
        """
        if self._initialized:
            logger.debug("LSP manager already initialized, skipping")
            return

        self._config = config
        self._project_root = str(Path(project_root).resolve())

        if not config.enabled:
            logger.info("LSP servers disabled by configuration")
            self._initialized = True
            return

        # Ensure .venv/bin and ~/go/bin are in PATH
        ensure_lsp_paths()

        # Start each configured server
        for server_config in config.servers:
            if not server_config.enabled:
                continue
            if not server_config.language:
                continue

            instance = LSPServerInstance(
                language=server_config.language,
                project_root=self._project_root,
                max_restarts=server_config.max_restarts,
            )

            try:
                instance.start()
                self._servers[server_config.language] = instance

                # Build extension map for this language
                for ext in _LANG_EXTENSIONS.get(server_config.language, []):
                    self._extension_map[ext] = server_config.language

                logger.info("LSP server pre-warmed: %s", server_config.language)
            except Exception as exc:
                logger.warning(
                    "Failed to pre-warm LSP server %s: %s (tree-sitter fallback will be used)",
                    server_config.language, exc,
                )

        # Register atexit cleanup
        if not self._atexit_registered:
            atexit.register(self.shutdown)
            self._atexit_registered = True

        self._initialized = True
        started = [lang for lang, inst in self._servers.items() if inst.is_healthy]
        logger.info("LSP manager initialized: %d/%d servers running %s",
                     len(started), len(config.servers), started)

    def get_server_for_file(self, file_path: str) -> Optional[LSPServerInstance]:
        """Route a file to the correct language server by extension.

        Returns None if no server is configured for this file type.
        """
        ext = Path(file_path).suffix.lower()
        lang = self._extension_map.get(ext)
        if not lang:
            return None
        return self._servers.get(lang)

    def get_server_for_language(self, language: str) -> Optional[LSPServerInstance]:
        """Get server by language name directly."""
        return self._servers.get(language.lower())

    @property
    def project_root(self) -> str:
        return self._project_root

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    @property
    def running_servers(self) -> List[str]:
        """List of languages with healthy running servers."""
        return [lang for lang, inst in self._servers.items() if inst.is_healthy]

    def shutdown(self) -> None:
        """Stop all servers — called at atexit or explicitly."""
        for lang, instance in list(self._servers.items()):
            try:
                instance.stop()
                logger.debug("LSP server stopped: %s", lang)
            except Exception as exc:
                logger.debug("LSP shutdown error (%s): %s", lang, exc)
        self._servers.clear()
        self._extension_map.clear()
        self._initialized = False
