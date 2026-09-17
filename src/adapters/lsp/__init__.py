"""LSP service management — aligned with Claude Code's three-layer architecture.

Components:
    LSPServerManager   — global singleton, routes files to language servers
    LSPServerInstance  — single server lifecycle with state machine + crash recovery
    LSPConfig          — declarative configuration from system.yaml
"""

from .lsp_server_manager import LSPServerManager

__all__ = ["LSPServerManager"]
