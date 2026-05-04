"""
LSPTool — code intelligence via long-lived LSP servers.

Architecture::

    system.yaml lsp_servers config
         │
    runner.py → LSPServerManager.initialize() (pre-warm at startup)
         │
    lsp_find_definition() etc.
         │
         ▼
    LSPServerManager.get_server_for_file()
         │
         ├── LSPServerInstance (running) → solidlsp request → result
         │
         └── None (no server) → tree-sitter fallback (46+ languages)

Features:
- goToDefinition  → lsp_find_definition
- findReferences  → lsp_find_references
- documentSymbol  → lsp_get_document_symbols
- hover           → lsp_hover
- workspaceSymbol → lsp_get_workspace_symbols
"""

import os
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.lib.logging import get_logger

from . import treesitter_fallback as ts_fb

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Project root inference
# ---------------------------------------------------------------------------

def _find_project_root(file_path: str) -> str:
    """Get project root, preferring C.agent_root when file is within it.

    Uses the shared config mechanism (``C.agent_root``) when the given
    *file_path* resides under the configured project root.  Falls back to
    walking up from *file_path* looking for common project markers when
    the file is external or the config is unavailable.
    """
    resolved = Path(file_path).resolve()
    try:
        from src.lib.config import C
        agent_root = Path(C.agent_root).resolve()
        # Only use agent_root when the file is within the project tree
        if resolved == agent_root or agent_root in resolved.parents:
            return str(agent_root)
    except Exception:
        pass

    # Fallback: walk up to find markers (original behavior)
    p = resolved
    if p.is_file():
        p = p.parent
    markers = {".git", "pyproject.toml", "package.json", "go.mod", "Cargo.toml", "pom.xml"}
    for d in [p, *p.parents]:
        if any((d / m).exists() for m in markers):
            return str(d)
    return str(p)


# ---------------------------------------------------------------------------
# Manager access helper
# ---------------------------------------------------------------------------

def _get_lsp_instance(file_path: str):
    """Get the LSPServerInstance for a file, or None if unavailable."""
    try:
        from src.services.lsp import LSPServerManager
        manager = LSPServerManager.get_instance()
        if not manager.is_initialized:
            return None
        return manager.get_server_for_file(file_path)
    except Exception:
        return None


# =========================================================================
# Public API — 5 functions
# =========================================================================

def lsp_find_definition(
    file_path: str,
    line: int,
    character: int,
    language: str = "",
) -> str:
    """Find where a symbol is defined (go-to-definition).

    Code intelligence via LSP. Uses real language servers when available
    (Python, TypeScript, Go, Rust, Java, ...), falls back to tree-sitter
    AST analysis (46+ languages).

    Examples:
        lsp_find_definition("src/main.py", line=42, character=15)

    Args:
        file_path: Path to the file containing the symbol.
        line: Line number (1-based, as shown in editors).
        character: Character offset on the line (1-based).
        language: Language hint (e.g. ``"python"``).  Auto-detected if empty.

    Returns:
        Formatted definition location(s), or a message if not found.
    """
    file_path = str(Path(file_path).resolve())
    _validate_file(file_path)
    lsp_line = line - 1
    lsp_char = character - 1

    # Try LSP server via Manager
    instance = _get_lsp_instance(file_path)
    if instance and instance.is_healthy:
        result = _do_lsp_definition(instance, file_path, lsp_line, lsp_char)
        if result is not None:
            return result

    # Tree-sitter fallback
    symbol_name = _read_symbol_at(file_path, lsp_line, lsp_char)
    if not symbol_name:
        return (
            f"No definition found at {Path(file_path).name}:{line}:{character}. "
            "This may occur if the cursor is not on a symbol."
        )

    project_root = _find_project_root(file_path)
    defs = ts_fb.ts_find_definitions_in_directory(project_root, symbol_name, max_results=10)
    if not defs:
        return f"No definition found for '{symbol_name}'"

    lines_out = [f"Definitions of '{symbol_name}':"]
    for d in defs:
        rel = _to_relative(d.file_path, Path(project_root))
        lines_out.append(f"  {rel}:{d.line + 1}: [{d.kind}] {d.name}")
    lines_out.append(f"\n[{len(defs)} definitions found, source: tree-sitter]")
    return "\n".join(lines_out)


def lsp_find_references(
    file_path: str,
    line: int,
    character: int,
    language: str = "",
    max_results: int = 50,
) -> str:
    """Find all references to a symbol across the codebase.

    Code intelligence via LSP. Uses real language servers when available,
    falls back to ripgrep word-boundary search.

    Examples:
        lsp_find_references("src/utils.py", line=10, character=5)

    Args:
        file_path: Path to the file containing the symbol.
        line: Line number (1-based).
        character: Character offset (1-based).
        language: Language hint.  Auto-detected if empty.
        max_results: Maximum number of references to return.

    Returns:
        Formatted list of references grouped by file.
    """
    file_path = str(Path(file_path).resolve())
    _validate_file(file_path)
    lsp_line = line - 1
    lsp_char = character - 1

    # Try LSP server
    instance = _get_lsp_instance(file_path)
    if instance and instance.is_healthy:
        result = _do_lsp_references(instance, file_path, lsp_line, lsp_char, max_results)
        if result is not None:
            return result

    # Fallback: ripgrep word-boundary
    symbol_name = _read_symbol_at(file_path, lsp_line, lsp_char)
    if not symbol_name:
        return (
            f"No references found at {Path(file_path).name}:{line}:{character}. "
            "This may occur if the cursor is not on a symbol."
        )

    project_root = _find_project_root(file_path)
    refs = ts_fb.ts_find_references(project_root, symbol_name, max_results=max_results)
    if not refs:
        return f"No references found for '{symbol_name}'"

    grouped: Dict[str, List[Tuple[int, str]]] = OrderedDict()
    for rel_path, lnum, text in refs:
        grouped.setdefault(rel_path, []).append((lnum, text))

    lines_out = [f"References to '{symbol_name}':"]
    for fpath, entries in grouped.items():
        lines_out.append(f"\n# {fpath}")
        for lnum, text in entries:
            lines_out.append(f"  {str(lnum).rjust(4)} | {text}")
    lines_out.append(f"\n[{len(refs)} references in {len(grouped)} files, source: ripgrep]")
    return "\n".join(lines_out)


def lsp_get_document_symbols(
    file_path: str,
    language: str = "",
) -> str:
    """Get all symbols (functions, classes, variables) in a file.

    Code intelligence via LSP. Uses real language servers when available,
    falls back to tree-sitter AST analysis (46+ languages).

    Examples:
        lsp_get_document_symbols("src/main.py")

    Args:
        file_path: Path to the file to analyse.
        language: Language hint.  Auto-detected if empty.

    Returns:
        Formatted symbol list grouped by kind.
    """
    file_path = str(Path(file_path).resolve())
    _validate_file(file_path)

    # Try LSP server
    instance = _get_lsp_instance(file_path)
    if instance and instance.is_healthy:
        result = _do_lsp_document_symbols(instance, file_path)
        if result is not None:
            return result

    # Fallback: tree-sitter
    symbols = ts_fb.ts_get_symbols(file_path)
    if symbols:
        return _format_symbols(symbols, file_path, "tree-sitter")

    # Final fallback: get_file_outline
    try:
        from src.tools.file_ops.file_outliner import get_file_outline
        return get_file_outline(file_path, detail_level="detailed")
    except Exception as exc:
        logger.debug("get_file_outline failed: %s", exc)
        return f"No symbols found in {file_path}"


def lsp_hover(
    file_path: str,
    line: int,
    character: int,
    language: str = "",
) -> str:
    """Get hover information (type signature, documentation) for a symbol.

    Code intelligence via LSP. Requires a real language server — no tree-sitter
    fallback (type inference needs semantic analysis).

    Examples:
        lsp_hover("src/main.py", line=42, character=15)

    Args:
        file_path: Path to the file.
        line: Line number (1-based).
        character: Character offset (1-based).
        language: Language hint.

    Returns:
        Type signature and documentation, or a message if unavailable.
    """
    file_path = str(Path(file_path).resolve())
    _validate_file(file_path)
    lsp_line = line - 1
    lsp_char = character - 1

    instance = _get_lsp_instance(file_path)
    if instance and instance.is_healthy:
        result = _do_lsp_hover(instance, file_path, lsp_line, lsp_char)
        if result is not None:
            return result

    return (
        "Hover information requires a running language server.\n"
        "Ensure lsp_servers is enabled in config/system.yaml.\n"
        "Tree-sitter cannot provide type inference."
    )


def lsp_get_workspace_symbols(
    directory: str,
    query: str = "",
    language: str = "",
    max_results: int = 50,
) -> str:
    """Search for symbols across the entire workspace.

    Code intelligence via LSP. Uses real language servers when available,
    falls back to tree-sitter directory-wide symbol extraction (46+ languages).

    Examples:
        lsp_get_workspace_symbols("src/", query="Agent")

    Args:
        directory: Project root directory to search.
        query: Symbol name filter (empty = all symbols).
        language: Language hint.
        max_results: Maximum symbols to return.

    Returns:
        Formatted symbol list grouped by file.
    """
    dir_path = Path(directory).resolve()
    if not dir_path.is_dir():
        raise FileNotFoundError(f"Directory does not exist: {directory}")

    # Try LSP workspace symbols
    instance = None
    if language:
        try:
            from src.services.lsp import LSPServerManager
            manager = LSPServerManager.get_instance()
            instance = manager.get_server_for_language(language)
        except Exception:
            pass

    if instance and instance.is_healthy:
        result = _do_lsp_workspace_symbols(instance, str(dir_path), query, max_results)
        if result is not None:
            return result

    # Fallback: tree-sitter directory scan
    if query:
        defs = ts_fb.ts_find_definitions_in_directory(
            str(dir_path), query, language=language, max_results=max_results,
        )
    else:
        defs = _ts_scan_all_symbols(str(dir_path), language, max_results)

    if not defs:
        msg = f"No symbols found in {directory}"
        if query:
            msg += f" matching '{query}'"
        return msg

    return _format_workspace_symbols(defs, dir_path, "tree-sitter")


# =========================================================================
# LSP request wrappers (use long-lived server from Manager)
# =========================================================================

def _do_lsp_definition(instance, file_path: str, line: int, char: int) -> Optional[str]:
    """Request definition from a running LSP server instance."""
    project_root = instance.project_root
    rel_path = os.path.relpath(file_path, project_root)
    try:
        result = instance.request("request_definition", rel_path, line, char)
        if not result:
            return None

        lines_out = ["Definitions found:"]
        for loc in result:
            uri = loc.get("uri", loc.get("targetUri", ""))
            rng = loc.get("range", loc.get("targetRange", {}))
            start = rng.get("start", {})
            def_line = start.get("line", 0) + 1
            def_char = start.get("character", 0) + 1
            def_path = _uri_to_path(uri, project_root)
            lines_out.append(f"  {def_path}:{def_line}:{def_char}")
        lines_out.append(f"\n[{len(result)} definitions, source: LSP/{instance.language}]")
        return "\n".join(lines_out)
    except Exception as exc:
        logger.debug("LSP definition failed: %s", exc)
        return None


def _do_lsp_references(instance, file_path: str, line: int, char: int, max_results: int) -> Optional[str]:
    project_root = instance.project_root
    rel_path = os.path.relpath(file_path, project_root)
    try:
        result = instance.request("request_references", rel_path, line, char)
        if not result:
            return None

        grouped: Dict[str, List[str]] = OrderedDict()
        count = 0
        for loc in result:
            if max_results > 0 and count >= max_results:
                break
            uri = loc.get("uri", "")
            rng = loc.get("range", {})
            start = rng.get("start", {})
            ref_line = start.get("line", 0) + 1
            ref_path = _uri_to_path(uri, project_root)
            grouped.setdefault(ref_path, []).append(str(ref_line))
            count += 1

        lines_out = ["References found:"]
        for fpath, ref_lines in grouped.items():
            lines_out.append(f"\n# {fpath}")
            for rl in ref_lines:
                lines_out.append(f"  line {rl}")
        lines_out.append(f"\n[{count} references in {len(grouped)} files, source: LSP/{instance.language}]")
        return "\n".join(lines_out)
    except Exception as exc:
        logger.debug("LSP references failed: %s", exc)
        return None


def _do_lsp_document_symbols(instance, file_path: str) -> Optional[str]:
    project_root = instance.project_root
    rel_path = os.path.relpath(file_path, project_root)
    try:
        result = instance.request("request_document_symbols", rel_path)
        if not result:
            return None

        lines_out = [f"Symbols in {os.path.basename(file_path)}:"]
        sym_list = result.root_symbols if hasattr(result, "root_symbols") else result
        for sym in sym_list:
            name = sym.get("name", "?") if isinstance(sym, dict) else getattr(sym, "name", "?")
            kind = sym.get("kind", 0) if isinstance(sym, dict) else getattr(sym, "kind", 0)
            kind_name = _symbol_kind_name(kind) if isinstance(kind, int) else str(kind)
            rng = sym.get("range", sym.get("selectionRange", {})) if isinstance(sym, dict) else {}
            start = rng.get("start", {}) if isinstance(rng, dict) else {}
            sym_line = (start.get("line", 0) if isinstance(start, dict) else 0) + 1
            lines_out.append(f"  L{sym_line:4d}: [{kind_name}] {name}")

            # Nested children
            children = sym.get("children", []) if isinstance(sym, dict) else getattr(sym, "children", [])
            for child in (children or []):
                c_name = child.get("name", "?") if isinstance(child, dict) else getattr(child, "name", "?")
                c_kind = child.get("kind", 0) if isinstance(child, dict) else getattr(child, "kind", 0)
                c_kind_name = _symbol_kind_name(c_kind) if isinstance(c_kind, int) else str(c_kind)
                c_rng = child.get("range", {}) if isinstance(child, dict) else {}
                c_start = c_rng.get("start", {}) if isinstance(c_rng, dict) else {}
                c_line = (c_start.get("line", 0) if isinstance(c_start, dict) else 0) + 1
                lines_out.append(f"  L{c_line:4d}:   [{c_kind_name}] {c_name}")

        count = len(sym_list) if hasattr(sym_list, "__len__") else 0
        lines_out.append(f"\n[{count} top-level symbols, source: LSP/{instance.language}]")
        return "\n".join(lines_out)
    except Exception as exc:
        logger.debug("LSP document_symbols failed: %s", exc)
        return None


def _do_lsp_hover(instance, file_path: str, line: int, char: int) -> Optional[str]:
    project_root = instance.project_root
    rel_path = os.path.relpath(file_path, project_root)
    try:
        result = instance.request("request_hover", rel_path, line, char)
        if not result:
            return None

        # Extract text from various hover formats
        contents = result.get("contents", "") if isinstance(result, dict) else getattr(result, "contents", "")
        if isinstance(contents, dict):
            text = contents.get("value", "")
        elif isinstance(contents, list):
            text = "\n".join(
                c.get("value", c) if isinstance(c, dict) else str(c)
                for c in contents
            )
        else:
            text = str(contents)

        if not text.strip():
            return None
        return f"Hover info:\n{text}\n\n[source: LSP/{instance.language}]"
    except Exception as exc:
        logger.debug("LSP hover failed: %s", exc)
        return None


def _do_lsp_workspace_symbols(instance, project_root: str, query: str, max_results: int) -> Optional[str]:
    try:
        result = instance.request("request_workspace_symbol", query or "")
        if not result:
            return None

        grouped: Dict[str, List[str]] = OrderedDict()
        count = 0
        for sym in result:
            if max_results > 0 and count >= max_results:
                break
            name = sym.get("name", "?") if isinstance(sym, dict) else getattr(sym, "name", "?")
            kind = sym.get("kind", 0) if isinstance(sym, dict) else getattr(sym, "kind", 0)
            kind_name = _symbol_kind_name(kind) if isinstance(kind, int) else str(kind)
            loc = sym.get("location", {}) if isinstance(sym, dict) else {}
            uri = loc.get("uri", "") if isinstance(loc, dict) else ""
            rng = loc.get("range", {}) if isinstance(loc, dict) else {}
            start = rng.get("start", {}) if isinstance(rng, dict) else {}
            sym_line = (start.get("line", 0) if isinstance(start, dict) else 0) + 1
            sym_path = _uri_to_path(uri, project_root)
            entry = f"  L{sym_line:4d}: [{kind_name}] {name}"
            grouped.setdefault(sym_path, []).append(entry)
            count += 1

        lines_out = ["Workspace symbols:"]
        for fpath, entries in grouped.items():
            lines_out.append(f"\n# {fpath}")
            lines_out.extend(entries)
        lines_out.append(f"\n[{count} symbols in {len(grouped)} files, source: LSP/{instance.language}]")
        return "\n".join(lines_out)
    except Exception as exc:
        logger.debug("LSP workspace_symbols failed: %s", exc)
        return None


# =========================================================================
# Tree-sitter directory scan
# =========================================================================

def _ts_scan_all_symbols(directory: str, language: str, max_results: int) -> List[ts_fb.SymbolDef]:
    results: List[ts_fb.SymbolDef] = []
    dir_path = Path(directory)
    skip = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv"}
    for root, dirs, files in os.walk(dir_path):
        dirs[:] = [d for d in dirs if d not in skip]
        dirs.sort()
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            lang = ts_fb._infer_language(fpath)
            if not lang:
                continue
            if language and lang != language.lower():
                continue
            syms = ts_fb.ts_get_symbols(fpath)
            results.extend(syms)
            if max_results > 0 and len(results) >= max_results:
                return results[:max_results]
    return results


# =========================================================================
# Formatting helpers
# =========================================================================

_SYMBOL_KIND_MAP = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package",
    5: "Class", 6: "Method", 7: "Property", 8: "Field",
    9: "Constructor", 10: "Enum", 11: "Interface", 12: "Function",
    13: "Variable", 14: "Constant", 15: "String", 16: "Number",
    17: "Boolean", 18: "Array", 19: "Object", 20: "Key",
    21: "Null", 22: "EnumMember", 23: "Struct", 24: "Event",
    25: "Operator", 26: "TypeParameter",
}


def _symbol_kind_name(kind: int) -> str:
    return _SYMBOL_KIND_MAP.get(kind, f"kind-{kind}")


def _format_symbols(symbols: List[ts_fb.SymbolDef], file_path: str, source: str) -> str:
    by_kind: Dict[str, List[ts_fb.SymbolDef]] = {}
    for s in symbols:
        by_kind.setdefault(s.kind, []).append(s)

    fname = os.path.basename(file_path)
    lines = [f"Symbols in {fname}:"]
    for kind, items in by_kind.items():
        lines.append(f"\n  {kind.upper()}S ({len(items)}):")
        for s in items:
            lines.append(f"    L{s.line + 1:4d}: {s.name}")
    lines.append(f"\n[{len(symbols)} symbols, source: {source}]")
    return "\n".join(lines)


def _format_workspace_symbols(defs: List[ts_fb.SymbolDef], base_dir: Path, source: str) -> str:
    grouped: Dict[str, List[ts_fb.SymbolDef]] = OrderedDict()
    for d in defs:
        rel = _to_relative(d.file_path, base_dir)
        grouped.setdefault(rel, []).append(d)

    lines = ["Workspace symbols:"]
    for fpath, syms in grouped.items():
        lines.append(f"\n# {fpath}")
        for s in syms:
            lines.append(f"  L{s.line + 1:4d}: [{s.kind}] {s.name}")
    lines.append(f"\n[{len(defs)} symbols in {len(grouped)} files, source: {source}]")
    return "\n".join(lines)


# =========================================================================
# Utility helpers
# =========================================================================

def _validate_file(file_path: str) -> None:
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File does not exist: {file_path}")
    if not p.is_file():
        raise ValueError(f"Path is not a file: {file_path}")


def _read_symbol_at(file_path: str, line: int, char: int) -> Optional[str]:
    """Read the identifier at a 0-based line:char position."""
    import re
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None

    if line < 0 or line >= len(lines):
        return None

    text = lines[line]
    if char < 0 or char >= len(text):
        char = min(max(char, 0), len(text) - 1) if text else 0

    if char >= len(text):
        return None

    left = char
    while left > 0 and (text[left - 1].isalnum() or text[left - 1] == "_"):
        left -= 1
    right = char
    while right < len(text) and (text[right].isalnum() or text[right] == "_"):
        right += 1

    word = text[left:right].strip()
    if not word or not re.match(r"^[a-zA-Z_]\w*$", word):
        return None
    return word


def _uri_to_path(uri: str, project_root: str) -> str:
    if uri.startswith("file://"):
        abs_path = uri[7:]
        try:
            from urllib.parse import unquote
            abs_path = unquote(abs_path)
        except ImportError:
            pass
        try:
            return str(Path(abs_path).relative_to(project_root))
        except ValueError:
            return abs_path
    return uri


def _to_relative(abs_path: str, base: Path) -> str:
    try:
        return str(Path(abs_path).relative_to(base))
    except ValueError:
        return abs_path
