import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path


from pygments.lexers import (
    get_lexer_for_filename,
    guess_lexer,
    guess_lexer_for_filename,
)
from pygments.util import ClassNotFound

_AST_GREP_TIMEOUT_SECONDS = 60
_VENV_BIN = os.path.dirname(sys.executable)
_AST_GREP_PATH = (
    shutil.which("ast-grep", path=_VENV_BIN)
    or shutil.which("ast-grep")
    or shutil.which("sg", path=_VENV_BIN)
    or shutil.which("sg")
)


def _get_ast_grep_path() -> str:
    if _AST_GREP_PATH:
        return _AST_GREP_PATH
    raise RuntimeError(
        "ast-grep is not installed or not found in PATH.\n"
        "To install it in your current environment, run:\n"
        "  uv pip install ast-grep-cli"
    )


@lru_cache(maxsize=1)
def _discover_rule_files() -> dict[str, Path]:
    rule_dir = Path(__file__).resolve().parent / "grep_config"
    rule_files: dict[str, Path] = {}
    for path in sorted(rule_dir.glob("*.yaml")):
        rule_files[path.stem.casefold()] = path
    return rule_files

def _extract_language_aliases(yaml_content: str) -> set[str]:
    """Extract all language aliases from YAML content."""
    pattern = re.compile(r"^\s*language:\s*([A-Za-z0-9_+-]+)\s*$", re.MULTILINE)
    return {match.group(1).casefold() for match in pattern.finditer(yaml_content)}


@lru_cache(maxsize=1)
def _discover_rule_aliases() -> dict[str, str]:
    alias_to_canonical: dict[str, str] = {}
    for canonical, path in _discover_rule_files().items():
        aliases = {canonical}
        text = path.read_text(encoding="utf-8")
        aliases.update(_extract_language_aliases(text))
        for alias in aliases:
            alias_to_canonical[alias] = canonical
    return alias_to_canonical


def _parse_supported_languages(help_text: str) -> set[str] | None:
    """Parse supported languages from ast-grep help output."""
    pattern = re.compile(r"Supported languages are:\s*\[(.*?)\]", re.IGNORECASE | re.DOTALL)
    match = pattern.search(help_text)
    if not match:
        return None
    return {
        language.strip().casefold()
        for language in match.group(1).split(",")
        if language.strip()
    }


@lru_cache(maxsize=1)
def _get_supported_ast_grep_languages() -> set[str]:
    cmd = [_get_ast_grep_path(), "run", "-h"]
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_AST_GREP_TIMEOUT_SECONDS,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        logging.warning(
            "Failed to inspect ast-grep supported languages; fallback to yaml rule aliases."
        )
        return set(_discover_rule_aliases())

    help_text = f"{completed.stdout}\n{completed.stderr}"
    languages = _parse_supported_languages(help_text)
    if languages is None:
        logging.warning(
            "Cannot parse ast-grep supported language list from help output; fallback to yaml rule aliases."
        )
        return set(_discover_rule_aliases())

    return languages


@lru_cache(maxsize=256)
def _is_ast_grep_language(language: str) -> bool:
    normalized = language.casefold().strip()
    if not normalized:
        return False

    if normalized in _get_supported_ast_grep_languages():
        return True

    cmd = [_get_ast_grep_path(), "run", "--stdin", "-p", "$A", "-l", normalized]
    completed = subprocess.run(
        cmd,
        input="x\n",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=_AST_GREP_TIMEOUT_SECONDS,
    )
    if completed.returncode in (0, 1):
        return True
    if "is not supported" in (completed.stderr or "").casefold():
        return False
    return False


def _language_candidates() -> list[str]:
    candidates = set(_get_supported_ast_grep_languages())
    candidates.update(_discover_rule_aliases())
    candidates.update(_discover_rule_files())
    return sorted(candidates)


def _suggest_languages(language: str) -> list[str]:
    return difflib.get_close_matches(
        language.casefold(),
        _language_candidates(),
        n=3,
        cutoff=0.5,
    )


def _supported_languages_text() -> str:
    yaml_languages = ", ".join(sorted(_discover_rule_files()))
    ast_grep_languages = ", ".join(sorted(_get_supported_ast_grep_languages()))
    return f"yaml rule languages: {yaml_languages}; ast-grep languages: {ast_grep_languages}"


def _resolve_rule_path(language: str) -> Path | None:
    rule_path = _discover_rule_files().get(language.casefold())
    if rule_path and not rule_path.exists():
        raise FileNotFoundError(f"YAML file not found: {rule_path}")
    return rule_path


def _normalize_explicit_language(language: str) -> str:
    normalized = language.casefold().strip()
    if not normalized:
        raise ValueError("language must be None or a non-empty string; use None for auto inference")

    alias_map = _discover_rule_aliases()
    canonical = alias_map.get(normalized)
    if canonical:
        return canonical

    if _is_ast_grep_language(normalized):
        return normalized

    suggestions = _suggest_languages(normalized)
    suggestion_text = f"; did you mean: {', '.join(suggestions)}" if suggestions else ""
    raise ValueError(
        f"language '{language}' is not supported; supported languages: {_supported_languages_text()}"
        f"{suggestion_text}"
    )


def _map_lexer_alias_to_language(lexer) -> str | None:
    alias_map = _discover_rule_aliases()
    aliases = [alias.casefold() for alias in getattr(lexer, "aliases", [])]

    for alias in aliases:
        canonical = alias_map.get(alias)
        if canonical:
            return canonical

    for alias in aliases:
        if _is_ast_grep_language(alias):
            return alias

    lexer_name = getattr(lexer, "name", "")
    if isinstance(lexer_name, str):
        normalized_name = lexer_name.casefold()
        canonical = alias_map.get(normalized_name)
        if canonical:
            return canonical
        if _is_ast_grep_language(normalized_name):
            return normalized_name
    return None


def infer_language_from_file(file_path: str) -> str:
    """
    Infer a language for one source file.

    Preferred output is a canonical YAML-rule language (`py`/`go`/`ts` today).
    If no YAML rule matches but ast-grep supports the detected language, this
    function returns that ast-grep language for generic fallback search.
    """
    file_path = str(file_path).strip()
    if not file_path:
        raise ValueError("file_path is required")

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"file_path does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"file_path must be a file: {path}")

    content = path.read_text(encoding="utf-8", errors="ignore")
    detectors = (
        lambda: get_lexer_for_filename(path.name, content),
        lambda: guess_lexer_for_filename(path.name, content),
        lambda: guess_lexer(content),
    )

    detected_lexers = []
    for detect in detectors:
        try:
            lexer = detect()
        except ClassNotFound:
            continue
        detected_lexers.append(lexer)
        language = _map_lexer_alias_to_language(lexer)
        if language:
            return language

    if detected_lexers:
        detected = ", ".join(
            f"{lexer.name}({','.join(getattr(lexer, 'aliases', []))})"
            for lexer in detected_lexers
        )
    else:
        detected = "none"

    raise ValueError(
        f"cannot infer supported language from file: {path}. "
        f"Detected lexers: {detected}. Supported languages: {_supported_languages_text()}"
    )


def _parse_scan_result(stdout: str) -> list[dict[str, str]]:
    try:
        payload = json.loads(stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Failed to parse ast-grep output as JSON") from exc

    if not isinstance(payload, list):
        raise RuntimeError("Failed to parse ast-grep output as JSON")

    parsed: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        content = item.get("lines")
        if not content:
            content = item.get("text", "")
        parsed.append(
            {
                "file_path": str(item.get("file", "")),
                "content": str(content),
            }
        )
    return parsed


def _run_ast_grep_json(cmd: list[str]) -> str:
    try:
        scan_result = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_AST_GREP_TIMEOUT_SECONDS,
        )
        return json.dumps(_parse_scan_result(scan_result.stdout), ensure_ascii=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"grep timeout after {_AST_GREP_TIMEOUT_SECONDS}s") from exc
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return "[]"
        raise RuntimeError(f"grep error: {exc.stderr}") from exc


def _scan_with_rule(file_path: str, keyword: str, rule_path: Path) -> str:
    escaped_keyword = re.escape(keyword)
    content = rule_path.read_text(encoding="utf-8").replace("{WORDS}", escaped_keyword)

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".yaml",
        prefix="processed_",
        delete=False,
        encoding="utf-8",
    ) as temp_file:
        temp_file.write(content)
        temp_path = temp_file.name

    logging.info("temp_path: %s", temp_path)
    cmd = [
        _get_ast_grep_path(),
        "scan",
        file_path,
        "-r",
        temp_path,
        "--json=compact",
    ]

    try:
        return _run_ast_grep_json(cmd)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _scan_with_generic_fallback(file_path: str, keyword: str, language: str) -> str:
    cmd = [
        _get_ast_grep_path(),
        "run",
        file_path,
        "-p",
        keyword,
        "-l",
        language,
        "--json=compact",
    ]
    return _run_ast_grep_json(cmd)


def ast_grep_search_file(
    file_path: str,
    keyword: str,
    language: str = "",
) -> str:
    """
    Search for code structures (functions, classes, methods, etc.) by keyword using AST-based matching.

    Unlike plain text search, this uses ast-grep to perform syntax-aware code structure matching,
    accurately finding definitions of functions, classes, structs, interfaces, and other language constructs.

    Args:
        file_path: Absolute path to the source file. Must exist and be a regular file.
        keyword: Identifier name to search (e.g., function/class name). Matched against structure
                 names, not plain text. Automatically escaped for regex safety.
        language: Programming language. Options:
                  - "" (default): Auto-detect from file extension using Pygments
                  - "python"/"go"/"typescript"/etc.: Explicitly specify language

    Returns:
        JSON string (compact format) with array of matches. Each match contains:
        - "file_path": The file path (same as input)
        - "content": Full matched code block (function/class definition with body)

        Example: [{"file_path": "/path/to/file.py", "content": "def foo():\n    pass"}]
        Returns "[]" if no matches or language cannot be inferred.

    Language Detection:
        Automatically detects language from file extension. Uses two search strategies:
        1. Rule-based search (preferred): More accurate, language-specific pattern matching
        2. Generic fallback: Works for 50+ languages but less precise

    Usage Examples:
        # Auto-detect language
        result = ast_grep_search_file("/path/script.py", "my_function")

        # Explicit language
        result = ast_grep_search_file("/path/file.go", "MyStruct", language="go")

    Common AI Agent Use Cases:
        - Code navigation: Find function/class definition before reading/modifying
        - Refactoring: Locate definitions to rename or restructure
        - Code understanding: Quickly locate structures in unfamiliar codebases

    Raises:
        ValueError: If file_path/keyword empty, language is empty string, or unsupported language
        FileNotFoundError: If file_path doesn't exist
    """
    file_path = str(file_path).strip()
    keyword = str(keyword).strip()

    if not file_path:
        raise ValueError("file_path is required")
    if not keyword:
        raise ValueError("keyword is required")

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"file_path does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"file_path must be a file: {path}")

    if not language:
        try:
            resolved_language = infer_language_from_file(file_path)
        except ValueError as exc:
            suffix = path.suffix.lstrip(".")
            suggestions = _suggest_languages(suffix) if suffix else []
            suggestion_text = (
                f"; did you mean: {', '.join(suggestions)}" if suggestions else ""
            )
            logging.warning(
                "Cannot infer language for %s: %s. Supported languages: %s%s",
                file_path,
                exc,
                _supported_languages_text(),
                suggestion_text,
            )
            return "[]"
    else:
        resolved_language = _normalize_explicit_language(str(language))

    rule_path = _resolve_rule_path(resolved_language)
    if rule_path is not None:
        return _scan_with_rule(file_path, keyword, rule_path)

    logging.warning(
        "No YAML rule for language '%s'; using generic fallback with ast-grep run. "
        "Consider defining grep_config/%s.yaml for more accurate matching.",
        resolved_language,
        resolved_language,
    )
    return _scan_with_generic_fallback(file_path, keyword, resolved_language)


# Backward compatibility alias
grep_func_content_by_keywords = ast_grep_search_file
search_code_by_keyword = ast_grep_search_file
