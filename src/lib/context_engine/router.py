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

    if re.search(r"(^|[_-])(log|logs|test|tests|build|pytest)([_-]|$)", name):
        return ContentKind.LOG
    if name in {"shell_tool", "python_interpreter", "run_skill_script"}:
        return ContentKind.LOG if _LOG_HINTS.search(stripped) else ContentKind.TEXT

    registry_kind = _registry_content_kind(name)
    if registry_kind is not None:
        if registry_kind == ContentKind.CODE and not _CODE_HINTS.search(stripped):
            return ContentKind.TEXT
        return registry_kind

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


def _registry_content_kind(tool_name: str) -> ContentKind | None:
    try:
        from src.tools import get_tool_spec

        output_kind = get_tool_spec(tool_name).output_kind
    except ValueError:
        return None
    if output_kind == "search":
        return ContentKind.SEARCH
    if output_kind == "log":
        return ContentKind.LOG
    if output_kind == "diff":
        return ContentKind.DIFF
    if output_kind == "json":
        return ContentKind.JSON
    if output_kind == "code":
        return ContentKind.CODE
    return None


def _looks_like_json(text: str) -> bool:
    if not text or text[0] not in "[{":
        return False
    try:
        json.loads(text)
    except Exception:
        return False
    return True
