"""
Tree-sitter fallback layer for LSPTool.

Used when multilspy is unavailable or the language is not supported.
Provides definition search, symbol extraction, and reference search
using tree-sitter AST queries + ripgrep.

Supports 46+ languages via .scm tag-query files shipped in
``src/tools/queries/``.
"""

import os
import re
import shutil
import subprocess
import sys
import warnings
from collections import namedtuple
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.lib.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# tree-sitter bootstrap (mirrors file_outliner.py logic)
# ---------------------------------------------------------------------------
_TS_AVAILABLE: Optional[bool] = None
_TS_QUERIES_DIR = Path(__file__).resolve().parent.parent.parent / "queries"

SymbolDef = namedtuple("SymbolDef", "name kind line file_path")


def _ensure_treesitter() -> bool:
    """Lazy-init tree-sitter.  Returns True if available."""
    global _TS_AVAILABLE
    if _TS_AVAILABLE is not None:
        return _TS_AVAILABLE
    try:
        from grep_ast import filename_to_lang  # noqa: F401
        from grep_ast.tsl import get_language, get_parser  # noqa: F401
        from tree_sitter import Query  # noqa: F401

        _TS_AVAILABLE = True
    except ImportError:
        _TS_AVAILABLE = False
        logger.warning("tree-sitter not available; LSP fallback will be limited")
    return _TS_AVAILABLE


def _get_scm_path(lang: str) -> Optional[Path]:
    """Locate the .scm tags-query file for *lang*."""
    for subdir in ("tree-sitter-language-pack", "tree-sitter-languages"):
        p = _TS_QUERIES_DIR / subdir / f"{lang}-tags.scm"
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------------------
# Extension → language mapping (subset; grep_ast handles most)
# ---------------------------------------------------------------------------
_EXT_LANG: Dict[str, str] = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".rb": "ruby",
    ".cs": "csharp",
    ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala",
    ".swift": "swift",
    ".dart": "dart",
    ".lua": "lua",
    ".php": "php",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".hs": "haskell",
    ".ml": "ocaml", ".mli": "ocaml",
    ".zig": "zig",
    ".ex": "elixir", ".exs": "elixir",
}


def _infer_language(file_path: str) -> Optional[str]:
    """Infer tree-sitter language from file path.  Uses grep_ast first."""
    if _ensure_treesitter():
        try:
            from grep_ast import filename_to_lang
            lang = filename_to_lang(file_path)
            if lang:
                return lang
        except Exception:
            pass
    ext = Path(file_path).suffix.lower()
    return _EXT_LANG.get(ext)


# =========================================================================
# Symbol extraction from a single file
# =========================================================================

def ts_get_symbols(file_path: str) -> List[SymbolDef]:
    """Extract all definition symbols from a file using tree-sitter.

    Returns a list of ``SymbolDef(name, kind, line, file_path)`` sorted by line.
    Returns empty list if tree-sitter is unavailable or the language is unsupported.
    """
    if not _ensure_treesitter():
        return []

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from grep_ast import filename_to_lang
        from grep_ast.tsl import get_language, get_parser
        from tree_sitter import Query

    lang = filename_to_lang(file_path)
    if not lang:
        return []

    scm_path = _get_scm_path(lang)
    if scm_path is None:
        return []

    try:
        language = get_language(lang)
        parser = get_parser(lang)
    except Exception as exc:
        logger.debug("tree-sitter init failed for %s: %s", lang, exc)
        return []

    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return []

    code = path.read_text(encoding="utf-8", errors="replace")
    tree = parser.parse(bytes(code, "utf-8"))
    query_scm = scm_path.read_text(encoding="utf-8")

    try:
        query = Query(language, query_scm)
    except Exception:
        return []

    # Run captures (compatible with tree-sitter 0.23 and 0.25+)
    if hasattr(query, "captures"):
        captures = query.captures(tree.root_node)
    else:
        from tree_sitter import QueryCursor
        cursor = QueryCursor(query)
        captures = cursor.captures(tree.root_node)

    defs: List[SymbolDef] = []
    seen: set = set()
    for tag, nodes in captures.items():
        if not tag.startswith("name.definition."):
            continue
        kind = tag[len("name.definition."):]
        for node in nodes:
            name = node.text.decode("utf-8")
            line = node.start_point[0]  # 0-based
            key = (name, line)
            if key not in seen:
                seen.add(key)
                defs.append(SymbolDef(name=name, kind=kind, line=line, file_path=file_path))

    defs.sort(key=lambda d: d.line)
    return defs


# =========================================================================
# Find definitions (single file or directory)
# =========================================================================

def ts_find_definitions(file_path: str, symbol_name: str) -> List[SymbolDef]:
    """Find definitions of *symbol_name* in a single file."""
    symbols = ts_get_symbols(file_path)
    return [s for s in symbols if s.name == symbol_name]


def ts_find_definitions_in_directory(
    directory: str,
    symbol_name: str,
    language: str = "",
    max_results: int = 20,
) -> List[SymbolDef]:
    """Search for *symbol_name* definitions across all files in *directory*.

    Walks the directory tree, extracting symbols from each supported file.
    """
    results: List[SymbolDef] = []
    dir_path = Path(directory).resolve()
    if not dir_path.is_dir():
        return results

    skip_dirs = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv"}

    for root, dirs, files in os.walk(dir_path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        dirs.sort()

        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            lang = _infer_language(fpath)
            if not lang:
                continue
            if language and lang != language:
                continue

            found = ts_find_definitions(fpath, symbol_name)
            results.extend(found)
            if max_results > 0 and len(results) >= max_results:
                return results[:max_results]

    return results


# =========================================================================
# Find references (ripgrep word-boundary search)
# =========================================================================

_VENV_BIN = os.path.dirname(sys.executable)
_RG_PATH: Optional[str] = shutil.which("rg", path=_VENV_BIN) or shutil.which("rg")


def ts_find_references(
    directory: str,
    symbol_name: str,
    include: str = "",
    max_results: int = 50,
) -> List[Tuple[str, int, str]]:
    r"""Find references to *symbol_name* via ripgrep word-boundary search.

    Returns list of ``(rel_path, line_num, line_text)``.
    """
    dir_path = Path(directory).resolve()
    if not dir_path.is_dir():
        return []

    # Use ripgrep with word boundary
    pattern = rf"\b{re.escape(symbol_name)}\b"

    if _RG_PATH:
        return _rg_find_refs(pattern, dir_path, include, max_results)
    else:
        return _py_find_refs(pattern, dir_path, include, max_results)


def _rg_find_refs(
    pattern: str, dir_path: Path, include: str, max_results: int,
) -> List[Tuple[str, int, str]]:
    """ripgrep-based reference search."""
    args = [_RG_PATH, "-n", "--hidden", "--sort=modified"]
    for excl in (".git", ".svn", ".hg", "node_modules", "__pycache__"):
        args.extend(["--glob", f"!{excl}"])
    if include:
        args.extend(["-g", include])
    args.extend(["-e", pattern, str(dir_path)])

    try:
        result = subprocess.run(
            args, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []

    refs: List[Tuple[str, int, str]] = []
    for line in result.stdout.splitlines():
        # Format: path:line:text
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        fpath, lnum, text = parts[0], parts[1], parts[2]
        try:
            line_num = int(lnum)
        except ValueError:
            continue
        rel = _to_relative(fpath, dir_path)
        refs.append((rel, line_num, text.strip()))
        if max_results > 0 and len(refs) >= max_results:
            break

    return refs


def _py_find_refs(
    pattern: str, dir_path: Path, include: str, max_results: int,
) -> List[Tuple[str, int, str]]:
    """Python fallback for reference search."""
    import fnmatch as _fnmatch

    regex = re.compile(pattern)
    include_fn = (lambda n: _fnmatch.fnmatch(n, include)) if include else None
    skip = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv"}

    refs: List[Tuple[str, int, str]] = []
    for root, dirs, files in os.walk(dir_path):
        dirs[:] = [d for d in dirs if d not in skip]
        for fname in sorted(files):
            if include_fn and not include_fn(fname):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, dir_path)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if regex.search(line):
                            refs.append((rel, i, line.strip()))
                            if max_results > 0 and len(refs) >= max_results:
                                return refs
            except OSError:
                continue
    return refs


# =========================================================================
# Helpers
# =========================================================================

def _to_relative(abs_path: str, base: Path) -> str:
    try:
        return str(Path(abs_path).relative_to(base))
    except ValueError:
        return abs_path
