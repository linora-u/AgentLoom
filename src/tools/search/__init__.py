"""Search tool compatibility exports, loaded one implementation module at a time."""

from typing import Any

from src.tools._lazy_exports import install_lazy_exports, resolve_lazy_export

_EXPORTS = {
    "grep_search": (".grep_tool", "grep_search"),
    "glob_search": (".glob_tool", "glob_search"),
    "ast_grep_search_file": (".ast_grep_tool", "ast_grep_search_file"),
    "lsp_find_definition": (".lsp_tool", "lsp_find_definition"),
    "lsp_find_references": (".lsp_tool", "lsp_find_references"),
    "lsp_get_document_symbols": (".lsp_tool", "lsp_get_document_symbols"),
    "lsp_hover": (".lsp_tool", "lsp_hover"),
    "lsp_get_workspace_symbols": (".lsp_tool", "lsp_get_workspace_symbols"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    return resolve_lazy_export(__name__, globals(), _EXPORTS, name)


install_lazy_exports(__name__)
