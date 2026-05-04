"""Tests for LSP tool multi-language support.

Covers:
- Python LSP (jedi-language-server) — full LSP
- Go LSP (gopls via go-bin) — full LSP
- TypeScript LSP (typescript-language-server via nodejs-bin) — full LSP
- C (no LSP server) — tree-sitter fallback
- Rust (no LSP server configured) — tree-sitter fallback
- Validates non-empty results for every operation
"""

import os
import textwrap
from pathlib import Path

import pytest

from src.tools.search.lsp_tool.lsp_tool import (
    lsp_find_definition,
    lsp_find_references,
    lsp_get_document_symbols,
    lsp_hover,
    lsp_get_workspace_symbols,
)


# ---------------------------------------------------------------------------
# Fixtures — create real multi-language test projects
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def multi_lang_dir(tmp_path_factory):
    """Create multi-language test projects."""
    base = tmp_path_factory.mktemp("lsp_multi_lang")

    # Python project
    py_dir = base / "py_project"
    py_dir.mkdir()
    (py_dir / "pyproject.toml").write_text('[project]\nname = "test"\n')
    (py_dir / "app.py").write_text(textwrap.dedent("""\
        class UserService:
            def __init__(self, db):
                self.db = db

            def get_user(self, user_id: int) -> dict:
                return self.db.find(user_id)

        def create_service(db) -> UserService:
            return UserService(db)

        service = create_service(None)
    """))

    # Go project
    go_dir = base / "go_project"
    go_dir.mkdir()
    (go_dir / "go.mod").write_text("module example.com/test\ngo 1.21\n")
    (go_dir / "main.go").write_text(textwrap.dedent("""\
        package main

        import "fmt"

        type Handler interface {
            Handle(msg string) error
        }

        type LogHandler struct {
            Prefix string
        }

        func (h *LogHandler) Handle(msg string) error {
            fmt.Printf("[%s] %s\\n", h.Prefix, msg)
            return nil
        }

        func NewLogHandler(prefix string) *LogHandler {
            return &LogHandler{Prefix: prefix}
        }

        func main() {
            h := NewLogHandler("INFO")
            h.Handle("started")
        }
    """))

    # TypeScript project
    ts_dir = base / "ts_project"
    ts_dir.mkdir()
    (ts_dir / "package.json").write_text('{"name": "test", "version": "1.0.0"}')
    (ts_dir / "tsconfig.json").write_text(
        '{"compilerOptions": {"target": "ES2020", "module": "commonjs", "strict": true}}'
    )
    (ts_dir / "service.ts").write_text(textwrap.dedent("""\
        interface Config {
            host: string;
            port: number;
        }

        class Server {
            private config: Config;

            constructor(config: Config) {
                this.config = config;
            }

            start(): string {
                return `Listening on ${this.config.host}:${this.config.port}`;
            }
        }

        function createServer(host: string, port: number): Server {
            return new Server({ host, port });
        }

        const srv = createServer("localhost", 8080);
        console.log(srv.start());
    """))

    # C project (no LSP — tree-sitter fallback)
    c_dir = base / "c_project"
    c_dir.mkdir()
    (c_dir / "calc.c").write_text(textwrap.dedent("""\
        #include <stdio.h>

        typedef struct {
            double x;
            double y;
        } Point;

        Point create_point(double x, double y) {
            Point p = {x, y};
            return p;
        }

        double distance(Point a, Point b) {
            double dx = a.x - b.x;
            double dy = a.y - b.y;
            return dx * dx + dy * dy;
        }

        int main() {
            Point p1 = create_point(0, 0);
            Point p2 = create_point(3, 4);
            printf("dist = %f\\n", distance(p1, p2));
            return 0;
        }
    """))

    # Rust project (tree-sitter fallback — no Cargo.toml needed for ts only)
    rs_dir = base / "rs_project"
    rs_dir.mkdir()
    (rs_dir / "lib.rs").write_text(textwrap.dedent("""\
        pub struct Config {
            pub name: String,
            pub value: i32,
        }

        impl Config {
            pub fn new(name: &str, value: i32) -> Self {
                Config { name: name.to_string(), value }
            }

            pub fn display(&self) -> String {
                format!("{}: {}", self.name, self.value)
            }
        }

        pub fn default_config() -> Config {
            Config::new("default", 0)
        }
    """))

    return base


# ===========================================================================
# Python LSP tests (full LSP via jedi-language-server)
# ===========================================================================

class TestPythonLSP:
    """Python has full LSP support via jedi-language-server."""

    def test_document_symbols_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "py_project" / "app.py")
        result = lsp_get_document_symbols(f)
        assert result and len(result.strip()) > 0, "Document symbols should not be empty"
        assert "UserService" in result, "Should find class UserService"
        assert "get_user" in result, "Should find method get_user"
        assert "create_service" in result, "Should find function create_service"

    def test_find_definition_precise(self, multi_lang_dir):
        f = str(multi_lang_dir / "py_project" / "app.py")
        # line 8: "def create_service" → character 5 points to "create_service"
        result = lsp_find_definition(f, line=8, character=5)
        assert result and len(result.strip()) > 0, "Definition should not be empty"
        assert "app.py" in result, "Definition should reference app.py"

    def test_find_references_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "py_project" / "app.py")
        # line 2: "self.db = db" → character 14 = "db"
        result = lsp_find_references(f, line=2, character=14)
        assert result and len(result.strip()) > 0, "References should not be empty"
        # "db" appears multiple times
        assert "references" in result.lower() or "db" in result.lower()

    def test_hover_returns_type_info(self, multi_lang_dir):
        f = str(multi_lang_dir / "py_project" / "app.py")
        # line 8: "def create_service"
        result = lsp_hover(f, line=8, character=5)
        assert result and len(result.strip()) > 0, "Hover should not be empty"
        # Should contain function signature or type info
        assert "create_service" in result or "LSP" in result or "Hover" in result


# ===========================================================================
# Go LSP tests (gopls via go-bin)
# ===========================================================================

class TestGoLSP:
    """Go has LSP support via gopls (installed by go-bin)."""

    def test_document_symbols_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "go_project" / "main.go")
        result = lsp_get_document_symbols(f)
        assert result and len(result.strip()) > 0, "Document symbols should not be empty"
        assert "Handler" in result or "LogHandler" in result, "Should find Go types"
        assert "Handle" in result or "NewLogHandler" in result, "Should find Go funcs"

    def test_find_definition_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "go_project" / "main.go")
        # line 18: "func NewLogHandler" → char 6
        result = lsp_find_definition(f, line=18, character=6)
        assert result and len(result.strip()) > 0, "Definition should not be empty"
        assert "main.go" in result or "NewLogHandler" in result

    def test_find_references_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "go_project" / "main.go")
        # line 10: "type LogHandler struct" → char 6
        result = lsp_find_references(f, line=10, character=6)
        assert result and len(result.strip()) > 0, "References should not be empty"
        assert "LogHandler" in result or "references" in result.lower()


# ===========================================================================
# TypeScript LSP tests (typescript-language-server via nodejs-bin)
# ===========================================================================

class TestTypeScriptLSP:
    """TypeScript has LSP support via typescript-language-server."""

    def test_document_symbols_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "ts_project" / "service.ts")
        result = lsp_get_document_symbols(f)
        assert result and len(result.strip()) > 0, "Document symbols should not be empty"
        assert "Config" in result or "Server" in result, "Should find TS types"
        assert "createServer" in result or "start" in result, "Should find TS functions"

    def test_find_definition_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "ts_project" / "service.ts")
        # line 13: "function createServer(host: string, port: number): Server"
        result = lsp_find_definition(f, line=13, character=10)
        assert result and len(result.strip()) > 0, "Definition should not be empty"
        assert "service.ts" in result or "createServer" in result

    def test_find_references_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "ts_project" / "service.ts")
        # line 1: "interface Config" → char 11
        result = lsp_find_references(f, line=1, character=11)
        assert result and len(result.strip()) > 0, "References should not be empty"
        assert "Config" in result or "references" in result.lower() or "service.ts" in result


# ===========================================================================
# C — tree-sitter fallback (no multilspy LSP server for C)
# ===========================================================================

class TestCTreeSitterFallback:
    """C has no LSP server in multilspy — uses tree-sitter fallback."""

    def test_document_symbols_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "c_project" / "calc.c")
        result = lsp_get_document_symbols(f)
        assert result and len(result.strip()) > 0, "Symbols should not be empty (tree-sitter)"
        assert "Point" in result or "create_point" in result, "Should find C symbols"
        assert "distance" in result or "main" in result, "Should find C functions"

    def test_find_definition_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "c_project" / "calc.c")
        # line 8: "Point create_point" → char 7
        result = lsp_find_definition(f, line=8, character=7)
        assert result and len(result.strip()) > 0, "Definition should not be empty"
        assert "create_point" in result or "tree-sitter" in result

    def test_find_references_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "c_project" / "calc.c")
        # line 8: "create_point" → char 7
        result = lsp_find_references(f, line=8, character=7)
        assert result and len(result.strip()) > 0, "References should not be empty"
        # ripgrep fallback should find "create_point" in the file
        assert "create_point" in result or "references" in result.lower()

    def test_hover_shows_fallback_message(self, multi_lang_dir):
        f = str(multi_lang_dir / "c_project" / "calc.c")
        result = lsp_hover(f, line=8, character=7)
        assert result and len(result.strip()) > 0, "Hover should return non-empty"
        # C has no LSP or tree-sitter hover, so should get informative message
        assert "language server" in result.lower() or "multilspy" in result.lower()

    def test_workspace_symbols_non_empty(self, multi_lang_dir):
        d = str(multi_lang_dir / "c_project")
        result = lsp_get_workspace_symbols(d, query="create_point")
        assert result and len(result.strip()) > 0, "Workspace symbols should not be empty"
        assert "create_point" in result


# ===========================================================================
# Rust — tree-sitter fallback
# ===========================================================================

class TestRustTreeSitterFallback:
    """Rust currently uses tree-sitter fallback (no Cargo.toml in test dir)."""

    def test_document_symbols_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "rs_project" / "lib.rs")
        result = lsp_get_document_symbols(f)
        assert result and len(result.strip()) > 0, "Symbols should not be empty"
        assert "Config" in result, "Should find struct Config"
        assert "new" in result or "display" in result, "Should find impl methods"
        assert "default_config" in result, "Should find function"

    def test_find_definition_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "rs_project" / "lib.rs")
        # line 16: "pub fn default_config" → char 8
        result = lsp_find_definition(f, line=16, character=8)
        assert result and len(result.strip()) > 0, "Definition should not be empty"
        assert "default_config" in result

    def test_find_references_non_empty(self, multi_lang_dir):
        f = str(multi_lang_dir / "rs_project" / "lib.rs")
        # line 1: "pub struct Config" → char 12
        result = lsp_find_references(f, line=1, character=12)
        assert result and len(result.strip()) > 0, "References should not be empty"
        assert "Config" in result


# ===========================================================================
# Cross-language: workspace_symbols
# ===========================================================================

class TestWorkspaceSymbols:
    """Test workspace-wide symbol search across languages."""

    def test_python_workspace_symbols(self, multi_lang_dir):
        d = str(multi_lang_dir / "py_project")
        result = lsp_get_workspace_symbols(d, query="UserService")
        assert result and len(result.strip()) > 0
        assert "UserService" in result

    def test_go_workspace_symbols(self, multi_lang_dir):
        d = str(multi_lang_dir / "go_project")
        result = lsp_get_workspace_symbols(d, query="LogHandler", language="go")
        assert result and len(result.strip()) > 0
        assert "LogHandler" in result

    def test_workspace_symbols_no_results(self, multi_lang_dir):
        d = str(multi_lang_dir / "py_project")
        result = lsp_get_workspace_symbols(d, query="NonExistentSymbolXYZ123")
        assert "No symbols found" in result


# ===========================================================================
# Edge cases: non-existent files, empty positions
# ===========================================================================

class TestEdgeCases:
    """Edge case and boundary condition tests."""

    def test_nonexistent_file(self):
        with pytest.raises(FileNotFoundError):
            lsp_get_document_symbols("/nonexistent/file.py")

    def test_non_file_path(self, multi_lang_dir):
        with pytest.raises(ValueError):
            lsp_get_document_symbols(str(multi_lang_dir))

    def test_definition_at_empty_line(self, multi_lang_dir):
        f = str(multi_lang_dir / "py_project" / "app.py")
        # line 11 might be empty or end of file
        result = lsp_find_definition(f, line=100, character=1)
        # Should return something (not crash), either "No symbol" or a definition
        assert isinstance(result, str) and len(result) > 0

    def test_references_at_string_literal(self, multi_lang_dir):
        f = str(multi_lang_dir / "go_project" / "main.go")
        # line 14: fmt.Printf("[%s] %s\n", ...) — position in a string
        result = lsp_find_references(f, line=14, character=20)
        # Should return something (not crash)
        assert isinstance(result, str) and len(result) > 0

    def test_nonexistent_directory_workspace_symbols(self):
        with pytest.raises(FileNotFoundError):
            lsp_get_workspace_symbols("/nonexistent/dir")
