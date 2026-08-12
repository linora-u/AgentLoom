"""P0 pure-Python compressors for ContextEngine."""

from __future__ import annotations

import json
import re

from .models import CompressionResult, ContentKind

ERROR_RE = re.compile(r"(error|exception|failed|failure|traceback|panic|fatal)", re.IGNORECASE)


def estimate_tokens(text: str) -> int:
    # Conservative enough for relative savings without importing provider tokenizers.
    return max(1, len(text) // 4) if text else 0


def compress_content(text: str, kind: ContentKind, preview_max_chars: int) -> CompressionResult:
    original = text or ""
    if kind == ContentKind.JSON:
        preview, strategy = _compress_json(original, preview_max_chars)
    elif kind == ContentKind.SEARCH:
        preview, strategy = _compress_search(original, preview_max_chars)
    elif kind == ContentKind.LOG:
        preview, strategy = _compress_log(original, preview_max_chars)
    elif kind == ContentKind.DIFF:
        preview, strategy = _compress_diff(original, preview_max_chars)
    else:
        preview, strategy = _compress_text(original, preview_max_chars), "head_tail"

    return CompressionResult(
        kind=kind,
        preview=preview,
        original_chars=len(original),
        preview_chars=len(preview),
        original_tokens_est=estimate_tokens(original),
        preview_tokens_est=estimate_tokens(preview),
        strategy=strategy,
    )


def _compress_json(text: str, limit: int) -> tuple[str, str]:
    try:
        value = json.loads(text)
    except Exception:
        return _compress_text(text, limit), "json_parse_fallback"

    if isinstance(value, list):
        all_error_items = [
            item
            for item in value
            if ERROR_RE.search(json.dumps(item, ensure_ascii=False, default=str))
        ]
        length = len(value)
        if length <= 12:
            compact = json.dumps(value, ensure_ascii=False, indent=2)
            return (
                _fit(
                    compact,
                    limit,
                    anchors=[
                        json.dumps(item, ensure_ascii=False, default=str)
                        for item in all_error_items[:10]
                    ],
                ),
                "json_small_list",
            )
        keep_indices = set(range(min(5, length))) | set(range(max(0, length - 3), length))
        for idx, item in enumerate(value):
            if idx in keep_indices:
                continue
            serialized = json.dumps(item, ensure_ascii=False, default=str)
            if ERROR_RE.search(serialized):
                keep_indices.add(idx)
        kept = [value[idx] for idx in sorted(keep_indices)]
        preview = {
            "_summary": {
                "type": "list",
                "original_items": length,
                "preview_items": len(kept),
                "dropped_items": max(0, length - len(kept)),
            },
            "items": kept,
        }
        return (
            _fit(
                json.dumps(preview, ensure_ascii=False, indent=2, default=str),
                limit,
                anchors=[json.dumps(item, ensure_ascii=False, default=str) for item in all_error_items[:10]],
            ),
            "smart_crusher_lite_list",
        )

    if isinstance(value, dict):
        keys = list(value)
        all_error_items = {
            key: value[key]
            for key in keys
            if ERROR_RE.search(str(key))
            or ERROR_RE.search(json.dumps(value.get(key), ensure_ascii=False, default=str))
        }
        if len(keys) <= 20:
            compact = json.dumps(value, ensure_ascii=False, indent=2, default=str)
            return (
                _fit(
                    compact,
                    limit,
                    anchors=[
                        json.dumps({key: item}, ensure_ascii=False, default=str)
                        for key, item in list(all_error_items.items())[:10]
                    ],
                ),
                "json_small_object",
            )
        kept_keys = keys[:12] + keys[-5:]
        for key in keys:
            if key in kept_keys:
                continue
            serialized = json.dumps(value.get(key), ensure_ascii=False, default=str)
            if ERROR_RE.search(str(key)) or ERROR_RE.search(serialized):
                kept_keys.append(key)
        preview = {
            "_summary": {
                "type": "object",
                "original_keys": len(keys),
                "preview_keys": len(dict.fromkeys(kept_keys)),
                "dropped_keys": max(0, len(keys) - len(dict.fromkeys(kept_keys))),
            },
            "items": {key: value[key] for key in dict.fromkeys(kept_keys)},
        }
        return (
            _fit(
                json.dumps(preview, ensure_ascii=False, indent=2, default=str),
                limit,
                anchors=[
                    json.dumps({key: item}, ensure_ascii=False, default=str)
                    for key, item in list(all_error_items.items())[:10]
                ],
            ),
            "smart_crusher_lite_object",
        )

    compact = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return _fit(compact, limit), "json_scalar"


def _compress_search(text: str, limit: int) -> tuple[str, str]:
    lines = text.splitlines()
    if len(text) <= limit:
        return text, "search_passthrough"
    selected = _first_last(lines, first=12, last=8)
    error_lines = [line for line in lines if ERROR_RE.search(line)]
    selected = _dedupe_keep_order(selected[:12] + error_lines[:10] + selected[12:])
    header = f"[Search summary: original_lines={len(lines)} preview_lines={len(selected)}]"
    return _fit("\n".join([header, *selected]), limit, anchors=error_lines[:10]), "search_first_last_errors"


def _compress_log(text: str, limit: int) -> tuple[str, str]:
    lines = text.splitlines()
    if len(text) <= limit:
        return text, "log_passthrough"
    selected: list[str] = []
    selected.extend(lines[:20])
    for idx, line in enumerate(lines):
        if ERROR_RE.search(line):
            start = max(0, idx - 3)
            end = min(len(lines), idx + 4)
            selected.extend(lines[start:end])
    selected.extend(lines[-40:])
    selected = _dedupe_keep_order(selected)
    header = f"[Log summary: original_lines={len(lines)} preview_lines={len(selected)} errors_preserved=yes]"
    error_lines = [line for line in lines if ERROR_RE.search(line)]
    return _fit("\n".join([header, *selected]), limit, anchors=error_lines[:10]), "log_errors_tail"


def _compress_diff(text: str, limit: int) -> tuple[str, str]:
    lines = text.splitlines()
    if len(text) <= limit:
        return text, "diff_passthrough"
    selected = []
    file_count = 0
    hunk_count = 0
    for line in lines:
        if line.startswith("diff --git "):
            file_count += 1
            if file_count <= 20:
                selected.append(line)
            continue
        if line.startswith(("@@ ", "--- ", "+++ ")):
            if line.startswith("@@ "):
                hunk_count += 1
            selected.append(line)
            continue
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            selected.append(line)
    selected = _dedupe_keep_order(selected)
    header = f"[Diff summary: original_lines={len(lines)} files_seen={file_count} hunks_seen={hunk_count}]"
    return _fit("\n".join([header, *selected]), limit), "diff_headers_changes"


def _compress_text(text: str, limit: int) -> str:
    return _fit(text, limit)


def _fit(text: str, limit: int, anchors: list[str] | None = None) -> str:
    if limit <= 0 or len(text) <= limit:
        return text

    marker = "\n...[preview omitted; retrieve ContextRef]...\n"
    priority = _dedupe_keep_order([anchor.strip() for anchor in anchors or [] if anchor.strip()])

    if priority:
        prefix = "[Priority excerpts]\n"
        priority_text = "\n".join(priority)
        reserved = len(prefix) + len(marker)
        if reserved >= limit:
            return (prefix + priority_text)[:limit]

        anchor_budget = min(len(priority_text), max(1, (limit - reserved) * 2 // 3))
        anchor_text = priority_text[:anchor_budget]
        remaining = limit - len(prefix) - len(anchor_text) - len(marker)
        head_chars = max(0, remaining // 2)
        tail_chars = max(0, remaining - head_chars)
        result = prefix + anchor_text + marker + text[:head_chars]
        if tail_chars:
            result += text[-tail_chars:]
        return result[:limit]

    available = limit - len(marker)
    if available <= 0:
        return marker[:limit]
    head_chars = available // 2
    tail_chars = available - head_chars
    result = text[:head_chars] + marker
    if tail_chars:
        result += text[-tail_chars:]
    return result[:limit]


def _first_last(lines: list[str], first: int, last: int) -> list[str]:
    if len(lines) <= first + last:
        return lines
    return lines[:first] + lines[-last:]


def _dedupe_keep_order(lines: list[str]) -> list[str]:
    seen = set()
    result = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        result.append(line)
    return result
