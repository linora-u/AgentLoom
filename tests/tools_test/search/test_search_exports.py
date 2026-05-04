"""Tests for search tool exports and convention-based resolution.

Verifies that:
1. Search tools are exported and callable from the package.
2. Tools can be resolved via ``resolve_tool_function()``.
"""

import pytest
from src.tools import resolve_tool_function
from src.tools.search import (
    grep_search,
    glob_search,
    ast_grep_search_file,
    lsp_find_definition,
    lsp_find_references,
    lsp_get_document_symbols,
    lsp_hover,
    lsp_get_workspace_symbols,
)


class TestSearchExportsCallable:
    """Search tools are exported and callable."""

    def test_grep_search_callable(self):
        assert callable(grep_search)

    def test_glob_search_callable(self):
        assert callable(glob_search)

    def test_ast_grep_search_file_callable(self):
        assert callable(ast_grep_search_file)

    def test_lsp_find_definition_callable(self):
        assert callable(lsp_find_definition)

    def test_lsp_find_references_callable(self):
        assert callable(lsp_find_references)

    def test_lsp_get_document_symbols_callable(self):
        assert callable(lsp_get_document_symbols)

    def test_lsp_hover_callable(self):
        assert callable(lsp_hover)

    def test_lsp_get_workspace_symbols_callable(self):
        assert callable(lsp_get_workspace_symbols)


class TestConventionBasedResolution:
    """Tools resolve via ``resolve_tool_function()``."""

    def test_grep_search_resolves(self):
        assert resolve_tool_function("grep_search") is grep_search

    def test_glob_search_resolves(self):
        assert resolve_tool_function("glob_search") is glob_search

    def test_ast_grep_search_file_resolves(self):
        assert resolve_tool_function("ast_grep_search_file") is ast_grep_search_file

    def test_lsp_find_definition_resolves(self):
        assert resolve_tool_function("lsp_find_definition") is lsp_find_definition

    def test_lsp_find_references_resolves(self):
        assert resolve_tool_function("lsp_find_references") is lsp_find_references

    def test_lsp_get_document_symbols_resolves(self):
        assert resolve_tool_function("lsp_get_document_symbols") is lsp_get_document_symbols

    def test_nonexistent_tool_raises(self):
        with pytest.raises(ValueError, match="not found"):
            resolve_tool_function("nonexistent_tool_xyz")
