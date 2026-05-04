"""
File outline generator using tree-sitter for accurate code structure extraction.

Uses grep_ast + tree-sitter-language-pack for AST-based definition extraction,
supporting 40+ programming languages.  Falls back to simple regex for Markdown,
JSON/YAML/TOML, and unsupported languages.

get_file_outline()
    ├─ Code files  → _outline_code_with_treesitter()   (AST-accurate)
    ├─ Markdown    → _analyze_markdown_file()           (regex, sufficient)
    ├─ Data files  → _analyze_data_file()               (regex / json.loads)
    └─ Unknown     → _analyze_generic_file()            (statistics only)
"""

import json
from src.lib.logging import get_logger
import re
import warnings
from collections import namedtuple
from pathlib import Path
from typing import Dict, List, Optional



logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# tree-sitter bootstrap  (lazy, tolerant of missing deps)
# ---------------------------------------------------------------------------
_TS_AVAILABLE: Optional[bool] = None
_TS_QUERIES_DIR = Path(__file__).resolve().parent.parent / "queries"

Def = namedtuple("Def", "name kind line")


def _ensure_treesitter() -> bool:
    """Lazy-init tree-sitter deps.  Returns True if available."""
    global _TS_AVAILABLE
    if _TS_AVAILABLE is not None:
        return _TS_AVAILABLE
    try:
        from grep_ast import filename_to_lang          # noqa: F401
        from grep_ast.tsl import get_language, get_parser  # noqa: F401
        from tree_sitter import Query                  # noqa: F401

        _TS_AVAILABLE = True
    except ImportError:
        _TS_AVAILABLE = False
        logger.warning(
            "tree-sitter / grep_ast not available; file outliner will use regex fallback"
        )
    return _TS_AVAILABLE


def _get_scm_path(lang: str) -> Optional[Path]:
    """Locate the *.scm* tags-query file shipped with the project."""
    for subdir in ("tree-sitter-language-pack", "tree-sitter-languages"):
        p = _TS_QUERIES_DIR / subdir / f"{lang}-tags.scm"
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------------------
# Public API  (signature unchanged — fully backward-compatible)
# ---------------------------------------------------------------------------

def get_file_outline(
    file_path: str,
    detail_level: str = "detailed",
    max_size_mb: float = 50.0,
    encoding: str = "utf-8",
    include_line_numbers: bool = True,
    max_items_per_section: int = 50,
) -> str:
    """
    Generate a structural outline / summary of a file.

    Uses **tree-sitter** AST parsing for code files (40+ languages) and
    simple regex / ``json.loads`` for Markdown, JSON, YAML, TOML.

    Args:
        file_path: Path to the file to analyse (relative or absolute).
        detail_level: ``"brief"`` | ``"detailed"`` | ``"full"``.

            - *brief*  – definition names only.
            - *detailed* – names + line numbers + first-line signature.
            - *full* – names + line numbers + TreeContext surrounding code.
        max_size_mb: Maximum file size in MB (default 50).
        encoding: Encoding to use (default: utf-8).
        include_line_numbers: Include line numbers in output.
        max_items_per_section: Cap per section to avoid overwhelming output.

    Returns:
        Structured outline as a single string.

    Raises:
        ValueError, FileNotFoundError, PermissionError, OSError.

    Examples:
        >>> get_file_outline("large_module.py")
        >>> get_file_outline("app.js", detail_level="brief")
        >>> get_file_outline("README.md", detail_level="full", include_line_numbers=False)
    """
    # ---- validate --------------------------------------------------------
    if not file_path or not file_path.strip():
        raise ValueError("file_path is required and cannot be empty")
    if detail_level not in ("brief", "detailed", "full"):
        raise ValueError("detail_level must be one of: brief, detailed, full")
    if max_size_mb <= 0:
        raise ValueError("max_size_mb must be positive")
    if max_items_per_section < 1:
        raise ValueError("max_items_per_section must be at least 1")

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File '{file_path}' does not exist")
    if not path.is_file():
        raise ValueError(f"Path '{file_path}' is not a file")

    file_size = path.stat().st_size
    if file_size > max_size_mb * 1024 * 1024:
        raise ValueError(
            f"File '{file_path}' is too large "
            f"({file_size / 1024 / 1024:.2f} MB, max {max_size_mb} MB)."
        )

    # ---- encoding --------------------------------------------------------
    actual_enc = encoding

    file_type = _determine_file_type(path)

    # ---- read once -------------------------------------------------------
    with open(path, "r", encoding=actual_enc, errors="replace") as fh:
        lines = fh.readlines()
    total_lines = len(lines)

    # ---- header ----------------------------------------------------------
    parts: List[str] = [
        f"File Outline: {file_path}",
        f"Type: {file_type.title()}",
        f"Lines: {total_lines:,}",
        f"Size: {file_size:,} bytes",
        "=" * 50,
    ]

    # ---- route by file type ----------------------------------------------
    if file_type == "markdown":
        parts.extend(
            _analyze_markdown_file(lines, detail_level, include_line_numbers, max_items_per_section)
        )
    elif file_type in ("json", "yaml", "toml"):
        parts.extend(
            _analyze_data_file(lines, file_type, detail_level, include_line_numbers, max_items_per_section)
        )
    elif _is_code_file(file_type):
        parts.extend(
            _outline_code(str(path), lines, detail_level, include_line_numbers, max_items_per_section)
        )
    else:
        parts.extend(
            _analyze_generic_file(lines, detail_level, include_line_numbers, max_items_per_section)
        )

    logger.info(
        f"Generated outline for: {path} (type: {file_type}, detail: {detail_level})"
    )
    return "\n".join(parts)


# =========================================================================
# File-type detection
# =========================================================================

_CODE_TYPES = frozenset({
    "python", "javascript", "typescript", "go", "java", "c", "cpp",
    "rust", "ruby", "php", "csharp", "swift", "kotlin", "scala",
    "lua", "elixir", "dart", "shell", "haskell", "ocaml", "zig",
})

_EXT_MAP: Dict[str, str] = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hxx": "cpp",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".kt": "kotlin", ".kts": "kotlin",
    ".scala": "scala",
    ".lua": "lua",
    ".ex": "elixir", ".exs": "elixir",
    ".dart": "dart",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".hs": "haskell",
    ".ml": "ocaml", ".mli": "ocaml",
    ".zig": "zig",
    # Markup / data
    ".md": "markdown", ".markdown": "markdown",
    ".json": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".html": "html", ".htm": "html",
    ".xml": "xml",
    ".sql": "sql",
    ".dockerfile": "dockerfile",
}


def _determine_file_type(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext in _EXT_MAP:
        return _EXT_MAP[ext]
    name = file_path.name.lower()
    if name in ("makefile", "gnumakefile"):
        return "makefile"
    if name == "dockerfile":
        return "dockerfile"
    return "text"


def _is_code_file(file_type: str) -> bool:
    return file_type in _CODE_TYPES


# =========================================================================
# Code outline — tree-sitter  (primary)  +  regex fallback
# =========================================================================

def _outline_code(
    file_path: str,
    lines: List[str],
    detail_level: str,
    include_line_numbers: bool,
    max_items: int,
) -> List[str]:
    """Outline a code file — try tree-sitter first, then regex fallback."""
    if _ensure_treesitter():
        result = _outline_code_with_treesitter(
            file_path, lines, detail_level, include_line_numbers, max_items,
        )
        if result is not None:
            return result
    return _regex_fallback_outline(lines, detail_level, include_line_numbers, max_items)


def _outline_code_with_treesitter(
    file_path: str,
    lines: List[str],
    detail_level: str,
    include_line_numbers: bool,
    max_items: int,
) -> Optional[List[str]]:
    """Extract definitions via tree-sitter AST queries.

    Returns *None* when the language is unsupported (caller should fall back).
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from grep_ast import TreeContext, filename_to_lang
        from grep_ast.tsl import get_language, get_parser
        from tree_sitter import Query

    lang = filename_to_lang(file_path)
    if not lang:
        return None

    scm_path = _get_scm_path(lang)
    if scm_path is None:
        return None

    try:
        language = get_language(lang)
        parser = get_parser(lang)
    except Exception as exc:
        logger.debug("tree-sitter init failed for %s: %s", lang, exc)
        return None

    code = "".join(lines)
    tree = parser.parse(bytes(code, "utf-8"))
    query_scm = scm_path.read_text(encoding="utf-8")

    try:
        query = Query(language, query_scm)
    except Exception as exc:
        logger.debug("tree-sitter query compile failed for %s: %s", lang, exc)
        return None

    # Run captures  (compatible with tree-sitter 0.23 and 0.25+)
    if hasattr(query, "captures"):
        captures = query.captures(tree.root_node)
    else:
        from tree_sitter import QueryCursor
        cursor = QueryCursor(query)
        captures = cursor.captures(tree.root_node)

    # --- extract definitions ------------------------------------------------
    defs: List[Def] = []
    for tag, nodes in captures.items():
        if not tag.startswith("name.definition."):
            continue
        kind = tag[len("name.definition."):]
        for node in nodes:
            defs.append(Def(
                name=node.text.decode("utf-8"),
                kind=kind,
                line=node.start_point[0],          # 0-based
            ))

    # Sort by line, deduplicate
    defs.sort(key=lambda d: d.line)
    seen: set = set()
    unique: List[Def] = []
    for d in defs:
        key = (d.name, d.line)
        if key not in seen:
            seen.add(key)
            unique.append(d)
    defs = unique

    if not defs:
        return ["\n(No definitions found by tree-sitter)"]

    # --- format based on detail_level --------------------------------------
    if detail_level == "full":
        return _format_defs_full(file_path, code, defs, lines, include_line_numbers, max_items, TreeContext)
    elif detail_level == "detailed":
        return _format_defs_detailed(defs, lines, include_line_numbers, max_items)
    else:  # brief
        return _format_defs_brief(defs, include_line_numbers, max_items)


# ---- formatters ----------------------------------------------------------

_KIND_ICONS: Dict[str, str] = {
    "class": "🏛️", "function": "⚙️", "method": "🔧",
    "interface": "🔗", "type": "🏗️", "module": "📦",
    "struct": "🏗️", "enum": "📊", "trait": "🔗",
    "macro": "🔧", "constant": "🔢", "impl": "🔧",
}


def _format_defs_brief(
    defs: List[Def], include_line_numbers: bool, max_items: int,
) -> List[str]:
    """Compact name list grouped by kind."""
    out: List[str] = []
    by_kind = _group_by_kind(defs)
    for kind, items in by_kind.items():
        icon = _KIND_ICONS.get(kind, "•")
        out.append(f"\n{icon} {kind.upper()}S ({len(items)}):")
        for d in items[:max_items]:
            if include_line_numbers:
                out.append(f"  L{d.line + 1:4d}: {d.name}")
            else:
                out.append(f"  {d.name}")
        if len(items) > max_items:
            out.append(f"  ... and {len(items) - max_items} more")
    return out


def _format_defs_detailed(
    defs: List[Def], lines: List[str], include_line_numbers: bool, max_items: int,
) -> List[str]:
    """Names + first-line signature, grouped by kind."""
    out: List[str] = []
    by_kind = _group_by_kind(defs)
    for kind, items in by_kind.items():
        icon = _KIND_ICONS.get(kind, "•")
        out.append(f"\n{icon} {kind.upper()}S ({len(items)}):")
        for d in items[:max_items]:
            sig = lines[d.line].rstrip() if d.line < len(lines) else ""
            if include_line_numbers:
                out.append(f"  L{d.line + 1:4d}: {sig}")
            else:
                out.append(f"  {sig}")
        if len(items) > max_items:
            out.append(f"  ... and {len(items) - max_items} more")
    return out


def _format_defs_full(
    file_path: str,
    code: str,
    defs: List[Def],
    lines: List[str],
    include_line_numbers: bool,
    max_items: int,
    TreeContext,
) -> List[str]:
    """Rich contextual output using grep_ast.TreeContext."""
    out: List[str] = [f"\n📐 CODE STRUCTURE ({len(defs)} definitions):"]
    try:
        context = TreeContext(
            file_path,
            code,
            color=False,
            line_number=True,
            child_context=False,
            last_line=False,
            margin=0,
            mark_lois=False,
            loi_pad=0,
            show_top_of_file_parent_scope=False,
        )
        lois = [d.line for d in defs[:max_items]]
        context.add_lines_of_interest(lois)
        context.add_context()
        rendered = context.format()
        if rendered and rendered.strip():
            out.append(rendered)
            return out
    except Exception as exc:
        logger.debug("TreeContext failed: %s", exc)
    # Fall through to detailed if TreeContext gives empty / errors
    return _format_defs_detailed(defs, lines, include_line_numbers, max_items)


def _group_by_kind(defs: List[Def]) -> Dict[str, List[Def]]:
    by_kind: Dict[str, List[Def]] = {}
    for d in defs:
        by_kind.setdefault(d.kind, []).append(d)
    return by_kind


# =========================================================================
# Regex fallback  (when tree-sitter is unavailable)
# =========================================================================

_FALLBACK_PATTERNS = [
    # Python
    (re.compile(r"^(\s*)class\s+(\w+)"), "class"),
    (re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)"), "function"),
    # JS / TS
    (re.compile(r"^(\s*)(?:export\s+)?(?:default\s+)?class\s+(\w+)"), "class"),
    (re.compile(r"^(\s*)(?:export\s+)?(?:async\s+)?function\s+(\w+)"), "function"),
    (re.compile(r"^(\s*)(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:\([^)]*\)|[^=])*=>"), "function"),
    # Go
    (re.compile(r"^func\s+(?:\([^)]*\)\s+)?(\w+)\s*\("), "function"),
    (re.compile(r"^type\s+(\w+)\s+(?:struct|interface)"), "type"),
    # Rust
    (re.compile(r"^(\s*)(?:pub\s+)?fn\s+(\w+)"), "function"),
    (re.compile(r"^(\s*)(?:pub\s+)?struct\s+(\w+)"), "struct"),
    (re.compile(r"^(\s*)(?:pub\s+)?enum\s+(\w+)"), "enum"),
    (re.compile(r"^(\s*)(?:pub\s+)?trait\s+(\w+)"), "trait"),
    # Java / C# / C++
    (re.compile(r"^(\s*)(?:public|private|protected|static|abstract|final|\s)*class\s+(\w+)"), "class"),
    (re.compile(r"^(\s*)(?:public|private|protected|static|abstract|final|\s)*interface\s+(\w+)"), "interface"),
    # C / C++
    (re.compile(r"^(\s*)struct\s+(\w+)"), "struct"),
    (re.compile(r"^#define\s+(\w+)"), "macro"),
]


def _regex_fallback_outline(
    lines: List[str],
    detail_level: str,
    include_line_numbers: bool,
    max_items: int,
) -> List[str]:
    """Simple multi-language regex fallback when tree-sitter is unavailable."""
    defs: List[Def] = []
    for i, line in enumerate(lines):
        for pattern, kind in _FALLBACK_PATTERNS:
            m = pattern.match(line)
            if m:
                name = m.group(m.lastindex) if m.lastindex else m.group(1)
                defs.append(Def(name=name, kind=kind, line=i))
                break

    if not defs:
        return _analyze_generic_file(lines, detail_level, include_line_numbers, max_items)

    out: List[str] = ["\n⚠️  (regex fallback — install grep-ast for accurate results)"]
    if detail_level == "brief":
        out.extend(_format_defs_brief(defs, include_line_numbers, max_items))
    else:
        out.extend(_format_defs_detailed(defs, lines, include_line_numbers, max_items))
    return out


# =========================================================================
# Markdown analyser  (kept — regex is perfectly fine for headers)
# =========================================================================

def _analyze_markdown_file(
    lines: List[str],
    detail_level: str,
    include_line_numbers: bool,
    max_items: int,
) -> List[str]:
    out: List[str] = []
    headers: List[tuple] = []
    code_blocks: List[tuple] = []
    in_code_block = False

    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            text = stripped.lstrip("#").strip()
            headers.append((i, level, text))
        elif stripped.startswith("```"):
            if not in_code_block:
                in_code_block = True
                lang = stripped[3:].strip() or "text"
                code_blocks.append((i, lang))
            else:
                in_code_block = False

    if headers:
        out.append("\n📋 HEADERS:")
        for item in headers[:max_items]:
            ln, level, text = item
            indent = "  " * level
            prefix = f"  L{ln:4d}: " if include_line_numbers else "  "
            out.append(f"{prefix}{indent}{'#' * level} {text}")
        if len(headers) > max_items:
            out.append(f"  ... and {len(headers) - max_items} more headers")

    if code_blocks and detail_level in ("detailed", "full"):
        out.append("\n💻 CODE BLOCKS:")
        for item in code_blocks[:max_items]:
            prefix = f"  L{item[0]:4d}: " if include_line_numbers else "  "
            out.append(f"{prefix}```{item[1]}")
        if len(code_blocks) > max_items:
            out.append(f"  ... and {len(code_blocks) - max_items} more code blocks")

    return out


# =========================================================================
# Data-file analyser  (JSON / YAML / TOML)
# =========================================================================

def _analyze_data_file(
    lines: List[str],
    file_type: str,
    detail_level: str,
    include_line_numbers: bool,
    max_items: int,
) -> List[str]:
    out: List[str] = []

    if file_type == "json":
        try:
            data = json.loads("".join(lines))
            out.append("\n📊 JSON STRUCTURE:")
            out.extend(_json_structure(data, detail_level, max_items))
        except json.JSONDecodeError as e:
            out.append(f"\n❌ JSON PARSE ERROR: {e}")
            out.extend(
                _analyze_generic_file(lines, detail_level, include_line_numbers, max_items)
            )

    elif file_type == "yaml":
        keys: List[tuple] = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and ":" in stripped and not line[0].isspace():
                keys.append((i, stripped.split(":")[0].strip()))
        if keys:
            out.append("\n🔑 TOP-LEVEL KEYS:")
            for item in keys[:max_items]:
                prefix = f"  L{item[0]:4d}: " if include_line_numbers else "  "
                out.append(f"{prefix}{item[1]}")
            if len(keys) > max_items:
                out.append(f"  ... and {len(keys) - max_items} more keys")

    elif file_type == "toml":
        sections: List[tuple] = []
        tkeys: List[tuple] = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                sections.append((i, stripped[1:-1]))
            elif "=" in stripped and not stripped.startswith("#"):
                tkeys.append((i, stripped.split("=")[0].strip()))
        if sections:
            out.append("\n📂 SECTIONS:")
            for item in sections[:max_items]:
                prefix = f"  L{item[0]:4d}: " if include_line_numbers else "  "
                out.append(f"{prefix}[{item[1]}]")
            if len(sections) > max_items:
                out.append(f"  ... and {len(sections) - max_items} more sections")
        if tkeys and detail_level in ("detailed", "full"):
            out.append("\n🔑 KEYS:")
            for item in tkeys[:max_items]:
                prefix = f"  L{item[0]:4d}: " if include_line_numbers else "  "
                out.append(f"{prefix}{item[1]}")
            if len(tkeys) > max_items:
                out.append(f"  ... and {len(tkeys) - max_items} more keys")

    return out


def _json_structure(
    data, detail_level: str, max_items: int, indent: int = 0,
) -> List[str]:
    out: List[str] = []
    pad = "  " * indent
    if isinstance(data, dict):
        for i, (k, v) in enumerate(data.items()):
            if i >= max_items:
                out.append(f"{pad}... and {len(data) - max_items} more keys")
                break
            if isinstance(v, (dict, list)):
                out.append(f"{pad}{k}: {type(v).__name__} ({len(v)} items)")
                if detail_level in ("detailed", "full") and indent < 2:
                    out.extend(_json_structure(v, detail_level, max_items, indent + 1))
            else:
                out.append(f"{pad}{k}: {type(v).__name__}")
    elif isinstance(data, list):
        out.append(f"{pad}Array with {len(data)} items")
        if detail_level in ("detailed", "full") and data and indent < 2:
            first = data[0]
            if isinstance(first, (dict, list)):
                out.append(f"{pad}Item structure:")
                out.extend(_json_structure(first, detail_level, max_items, indent + 1))
    return out


# =========================================================================
# Generic file analyser  (statistics)
# =========================================================================

def _analyze_generic_file(
    lines: List[str],
    detail_level: str,
    include_line_numbers: bool,
    max_items: int,
) -> List[str]:
    total = len(lines)
    non_empty = sum(1 for ln in lines if ln.strip())
    total_chars = sum(len(ln) for ln in lines)

    out = [
        "\n📊 STATISTICS:",
        f"  Total lines: {total:,}",
        f"  Non-empty lines: {non_empty:,}",
        f"  Total characters: {total_chars:,}",
    ]

    if detail_level == "full":
        out.append("\n👀 CONTENT PREVIEW:")
        for i, line in enumerate(lines[:10], 1):
            clean = line.rstrip()[:100]
            prefix = f"  {i:4d}: " if include_line_numbers else "  "
            out.append(f"{prefix}{clean}")
        if total > 10:
            out.append(f"  ... and {total - 10} more lines")
    return out
