"""LSPTool

Uses multilspy (real LSP servers) when available, falls back to tree-sitter.
"""

from .lsp_tool import (
    lsp_find_definition,
    lsp_find_references,
    lsp_get_document_symbols,
    lsp_hover,
    lsp_get_workspace_symbols,
)

__all__ = [
    "lsp_find_definition",
    "lsp_find_references",
    "lsp_get_document_symbols",
    "lsp_hover",
    "lsp_get_workspace_symbols",
]
