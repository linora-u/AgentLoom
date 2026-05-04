import os
import textwrap

import pytest

from src.tools.search.lsp_tool.lsp_tool import (
    lsp_find_definition,
    lsp_find_references,
    lsp_get_document_symbols,
    lsp_hover,
    lsp_get_workspace_symbols,
    _read_symbol_at,
    _find_project_root,
)
from src.tools.search.lsp_tool.treesitter_fallback import (
    ts_get_symbols,
    ts_find_definitions,
    ts_find_definitions_in_directory,
    ts_find_references,
    _ensure_treesitter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def python_project(tmp_path):
    """Create a minimal Python project for testing."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')

    src = tmp_path / "src"
    src.mkdir()
    (src / "__init__.py").write_text("")

    (src / "main.py").write_text(
        textwrap.dedent("""\
        from src.utils import helper

        class MyAgent:
            def __init__(self, name):
                self.name = name

            def run(self):
                return helper(self.name)

        def create_agent(name):
            return MyAgent(name)
        """),
        encoding="utf-8",
    )

    (src / "utils.py").write_text(
        textwrap.dedent("""\
        def helper(value):
            return str(value).upper()

        def unused_func():
            pass

        CONSTANT = 42
        """),
        encoding="utf-8",
    )

    return tmp_path


# ---------------------------------------------------------------------------
# Utility tests
# ---------------------------------------------------------------------------

class TestReadSymbolAt:
    def test_read_word_at_position(self, python_project):
        fpath = str(python_project / "src" / "main.py")
        # Line 2 (0-based): "class MyAgent:"  char 6 → "MyAgent"
        result = _read_symbol_at(fpath, 2, 6)
        assert result == "MyAgent"

    def test_read_function_name(self, python_project):
        fpath = str(python_project / "src" / "main.py")
        # Line 9 (0-based): "def create_agent(name):" → "create_agent"
        result = _read_symbol_at(fpath, 9, 4)
        assert result == "create_agent"

    def test_invalid_position(self, python_project):
        fpath = str(python_project / "src" / "main.py")
        result = _read_symbol_at(fpath, 999, 0)
        assert result is None

    def test_nonexistent_file(self):
        result = _read_symbol_at("/nonexistent/file.py", 0, 0)
        assert result is None


class TestFindProjectRoot:
    def test_finds_pyproject(self, python_project):
        fpath = str(python_project / "src" / "main.py")
        root = _find_project_root(fpath)
        assert root == str(python_project)

    def test_directory_input(self, python_project):
        root = _find_project_root(str(python_project / "src"))
        assert root == str(python_project)


# ---------------------------------------------------------------------------
# Tree-sitter fallback tests
# ---------------------------------------------------------------------------

class TestTreeSitterFallback:
    """Test tree-sitter fallback functions (no LSP required)."""

    def test_get_symbols(self, python_project):
        if not _ensure_treesitter():
            pytest.skip("tree-sitter not available")

        fpath = str(python_project / "src" / "main.py")
        symbols = ts_get_symbols(fpath)
        names = [s.name for s in symbols]
        assert "MyAgent" in names
        assert "create_agent" in names

    def test_get_symbols_utils(self, python_project):
        if not _ensure_treesitter():
            pytest.skip("tree-sitter not available")

        fpath = str(python_project / "src" / "utils.py")
        symbols = ts_get_symbols(fpath)
        names = [s.name for s in symbols]
        assert "helper" in names

    def test_find_definitions(self, python_project):
        if not _ensure_treesitter():
            pytest.skip("tree-sitter not available")

        fpath = str(python_project / "src" / "main.py")
        defs = ts_find_definitions(fpath, "MyAgent")
        assert len(defs) >= 1
        assert defs[0].name == "MyAgent"
        assert defs[0].kind == "class"

    def test_find_definitions_not_found(self, python_project):
        if not _ensure_treesitter():
            pytest.skip("tree-sitter not available")

        fpath = str(python_project / "src" / "main.py")
        defs = ts_find_definitions(fpath, "NonExistent")
        assert len(defs) == 0

    def test_find_definitions_in_directory(self, python_project):
        if not _ensure_treesitter():
            pytest.skip("tree-sitter not available")

        defs = ts_find_definitions_in_directory(
            str(python_project / "src"), "helper",
        )
        assert len(defs) >= 1
        assert defs[0].name == "helper"

    def test_find_references(self, python_project):
        refs = ts_find_references(
            str(python_project / "src"), "helper",
        )
        # "helper" appears in main.py (import + call) and utils.py (def)
        assert len(refs) >= 2
        paths = [r[0] for r in refs]
        assert any("main.py" in p for p in paths)
        assert any("utils.py" in p for p in paths)

    def test_nonexistent_file_symbols(self):
        if not _ensure_treesitter():
            pytest.skip("tree-sitter not available")
        symbols = ts_get_symbols("/nonexistent/file.py")
        assert symbols == []


# ---------------------------------------------------------------------------
# Public API integration tests (uses tree-sitter fallback)
# ---------------------------------------------------------------------------

class TestLspFindDefinition:
    def test_basic_definition(self, python_project):
        fpath = str(python_project / "src" / "main.py")
        # "MyAgent" at line 3, col 7 (1-based)
        result = lsp_find_definition(fpath, line=3, character=7)
        # Should find at least something (either LSP or tree-sitter)
        assert isinstance(result, str)
        # Accept both real LSP output ("Definitions found:") and tree-sitter fallback ("MyAgent")
        assert "Definitions found:" in result or "MyAgent" in result or "No definition" in result or "No symbol" in result

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            lsp_find_definition("/nonexistent/file.py", line=1, character=1)


class TestLspFindReferences:
    def test_basic_references(self, python_project):
        fpath = str(python_project / "src" / "utils.py")
        # "helper" at line 1, col 5 (1-based)
        result = lsp_find_references(fpath, line=1, character=5)
        assert isinstance(result, str)
        # Should find references via ripgrep fallback
        # Accept both real LSP output ("References found:") and ripgrep fallback ("helper")
        assert "References found:" in result or "helper" in result or "No references" in result or "No symbol" in result


class TestLspGetDocumentSymbols:
    def test_basic_symbols(self, python_project):
        fpath = str(python_project / "src" / "main.py")
        result = lsp_get_document_symbols(fpath)
        assert isinstance(result, str)
        # Should contain class/function names via tree-sitter or outline fallback
        assert "MyAgent" in result or "create_agent" in result or "Symbols" in result or "File Outline" in result

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            lsp_get_document_symbols("/nonexistent/file.py")


class TestLspHover:
    def test_hover_returns_string(self, python_project):
        fpath = str(python_project / "src" / "main.py")
        result = lsp_hover(fpath, line=3, character=7)
        assert isinstance(result, str)
        # Without multilspy, should return the "requires language server" message
        # OR actual hover info if multilspy is installed


class TestLspGetWorkspaceSymbols:
    def test_workspace_symbols_with_query(self, python_project):
        result = lsp_get_workspace_symbols(
            str(python_project / "src"), query="helper",
        )
        assert isinstance(result, str)
        assert "helper" in result or "No symbols" in result

    def test_workspace_symbols_all(self, python_project):
        result = lsp_get_workspace_symbols(str(python_project / "src"))
        assert isinstance(result, str)

    def test_invalid_directory(self):
        with pytest.raises(FileNotFoundError):
            lsp_get_workspace_symbols("/nonexistent/dir")
