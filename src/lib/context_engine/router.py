"""Content type routing for tool results."""

from __future__ import annotations

import json
import re

from .models import ContentKind


_LOG_HINTS = re.compile(
    r"(traceback|exception|error:|failed|failure|warning:|\bpytest\b|\bnpm ERR!\b|\bpanic:)",
    flags=re.IGNORECASE,
)
_SEARCH_LINE = re.compile(
    r"^(?:\.{0,2}/)?[\w./-]+\.[A-Za-z0-9_+-]+:\d+(?::\d+)?:",
    flags=re.MULTILINE,
)
_DIFF_HINTS = ("diff --git ", "\n@@ ", "\n--- ", "\n+++ ")
_CODE_HINTS = re.compile(r"^\s*(def |class |import |from |package |func |const |let |var |interface |type )", re.MULTILINE)


def route_content(text: str, tool_name: str = "default") -> ContentKind:
    name = (tool_name or "").lower()
    stripped = (text or "").strip()

    if name in {"grep_search", "git_grep_files", "search_files", "code_search", "ast_grep_search_file"}:
        return ContentKind.SEARCH
    if re.search(r"(^|[_-])(log|logs|test|tests|build|pytest)([_-]|$)", name):
        return ContentKind.LOG
    if name in {"shell_tool", "python_interpreter", "run_skill_script"}:
        return ContentKind.LOG if _LOG_HINTS.search(stripped) else ContentKind.TEXT
    if name in {"get_git_diff_content", "git_diff"}:
        return ContentKind.DIFF
    if name in {"read_file", "get_file_outline"}:
        return ContentKind.CODE if _CODE_HINTS.search(stripped) else ContentKind.TEXT

    if _looks_like_json(stripped):
        return ContentKind.JSON
    if stripped.startswith("diff --git ") or all(hint in stripped for hint in _DIFF_HINTS[1:3]):
        return ContentKind.DIFF
    if _SEARCH_LINE.search(stripped):
        return ContentKind.SEARCH
    if _LOG_HINTS.search(stripped):
        return ContentKind.LOG
    if _CODE_HINTS.search(stripped):
        return ContentKind.CODE
    return ContentKind.TEXT


def _looks_like_json(text: str) -> bool:
    if not text or text[0] not in "[{":
        return False
    try:
        json.loads(text)
    except Exception:
        return False
    return True
