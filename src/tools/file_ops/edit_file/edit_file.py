"""EditFile tool implementation.

Applies a batch of unique, non-overlapping text edits to an existing file.
Matching uses the same practical strategies as the previous editor:
exact → quote-normalized → whitespace-tolerant → token-based.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.lib.logging import get_logger

from .._read_file_state import get_read_file_state
from .._safety import MAX_EDIT_FILE_SIZE, normalize_path, validate_file_access
from .utils import (
    build_token_regex,
    build_whitespace_tolerant_regex,
    count_occurrences,
    detect_line_ending,
    find_actual_string,
    find_similar_lines,
    normalize_to_lf,
    preserve_quote_style,
    restore_line_ending,
)

logger = get_logger(__name__)


@dataclass(frozen=True)
class _ResolvedEdit:
    start: int
    end: int
    replacement: str
    strategy: str
    old_text: str


def edit_file(
    file_path: str,
    edits: list[dict[str, str]],
) -> str:
    """Apply multiple text edits to an existing file.

    Args:
        file_path: Absolute path to the file to edit.
        edits: List of ``{"old_text": "...", "new_text": "..."}`` objects.
            Every ``old_text`` must be non-empty, unique in the request, match
            exactly one region in the original file, and not overlap with any
            other edit.

    Returns:
        A success message or a precise validation failure.
    """
    if file_path:
        file_path = file_path.strip()

    validate_file_access(file_path, "write", tool_name="edit_file")
    try:
        normalized_edits = _normalize_edits(edits)
    except ValueError as exc:
        return str(exc)

    path_obj = normalize_path(file_path)
    if not path_obj.exists():
        return f"File does not exist: {file_path}"
    if path_obj.is_dir():
        return f"Path is a directory, not a file: {file_path}"

    state = get_read_file_state()
    stale_msg = state.check_staleness(path_obj)
    if stale_msg is not None:
        return stale_msg

    try:
        file_size = path_obj.stat().st_size
    except OSError as exc:
        return f"Cannot stat file '{file_path}': {exc}"

    if file_size > MAX_EDIT_FILE_SIZE:
        return (
            f"File '{file_path}' is too large to edit "
            f"({file_size / (1024 * 1024):.1f} MB)."
        )

    try:
        content = path_obj.read_bytes().decode("utf-8")
    except Exception as exc:
        return f"Failed to read file {file_path}: {exc}"

    original_eol = detect_line_ending(content)
    content_lf = normalize_to_lf(content)

    try:
        resolved = [
            _resolve_edit(content_lf, old_text, new_text, file_path)
            for old_text, new_text in normalized_edits
        ]
        _validate_non_overlapping(resolved, file_path)
    except ValueError as exc:
        return str(exc)

    new_content_lf = content_lf
    for item in sorted(resolved, key=lambda edit: edit.start, reverse=True):
        new_content_lf = (
            new_content_lf[: item.start]
            + item.replacement
            + new_content_lf[item.end :]
        )

    final_content = restore_line_ending(new_content_lf, original_eol)
    if final_content == content:
        return f"File content unchanged for {file_path}."

    try:
        path_obj.write_bytes(final_content.encode("utf-8"))
    except Exception as exc:
        return f"Failed to write file {file_path}: {exc}"

    state.update_after_write(path_obj, final_content)
    strategies = sorted({item.strategy for item in resolved if item.strategy != "exact"})
    suffix = f" [matched via {', '.join(strategies)}]" if strategies else ""
    logger.debug("edit_file %s edits=%d strategies=%s", file_path, len(resolved), strategies)
    return f"Successfully edited {file_path} ({len(resolved)} edits){suffix}"


def _normalize_edits(raw_edits: object) -> list[tuple[str, str]]:
    if not isinstance(raw_edits, list) or not raw_edits:
        raise ValueError("edits must be a non-empty list")

    result: list[tuple[str, str]] = []
    seen_old_texts: set[str] = set()
    for index, item in enumerate(raw_edits, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"edits[{index}] must be an object")
        old_text = item.get("old_text")
        new_text = item.get("new_text")
        if not isinstance(old_text, str) or not old_text:
            raise ValueError(f"edits[{index}].old_text must be a non-empty string")
        if not isinstance(new_text, str):
            raise ValueError(f"edits[{index}].new_text must be a string")
        if old_text in seen_old_texts:
            raise ValueError(f"Duplicate old_text in edits[{index}]")
        seen_old_texts.add(old_text)
        result.append((old_text, new_text))
    return result


def _resolve_edit(
    content_lf: str,
    old_text: str,
    new_text: str,
    file_path: str,
) -> _ResolvedEdit:
    old_lf = normalize_to_lf(old_text)
    new_lf = normalize_to_lf(new_text)

    if old_lf == new_lf:
        raise ValueError("No changes needed: old_text and new_text are identical.")

    exact_count = count_occurrences(content_lf, old_lf)
    if exact_count == 1:
        start = content_lf.index(old_lf)
        return _ResolvedEdit(start, start + len(old_lf), new_lf, "exact", old_text)
    if exact_count > 1:
        raise ValueError(f"Multiple exact matches found in {file_path}; old_text is not unique.")

    actual_old = find_actual_string(content_lf, old_lf)
    if actual_old is not None and actual_old != old_lf:
        quote_count = count_occurrences(content_lf, actual_old)
        if quote_count == 1:
            start = content_lf.index(actual_old)
            actual_new = preserve_quote_style(old_lf, actual_old, new_lf)
            return _ResolvedEdit(
                start,
                start + len(actual_old),
                actual_new,
                "quote-normalized",
                old_text,
            )
        if quote_count > 1:
            raise ValueError(f"Multiple quote-normalized matches found in {file_path}; old_text is not unique.")

    ws_regex = build_whitespace_tolerant_regex(old_lf)
    ws_matches = list(ws_regex.finditer(content_lf))
    if len(ws_matches) == 1:
        match = ws_matches[0]
        return _ResolvedEdit(match.start(), match.end(), new_lf, "whitespace-tolerant", old_text)
    if len(ws_matches) > 1:
        raise ValueError(f"Multiple whitespace-tolerant matches found in {file_path}; old_text is not unique.")

    token_regex = build_token_regex(old_lf)
    token_matches = list(token_regex.finditer(content_lf))
    if len(token_matches) == 1:
        match = token_matches[0]
        return _ResolvedEdit(match.start(), match.end(), new_lf, "token-based", old_text)
    if len(token_matches) > 1:
        raise ValueError(f"Multiple token-based matches found in {file_path}; old_text is not unique.")

    hint = find_similar_lines(content_lf, old_lf)
    message = (
        f"Could not find old_text in {file_path}\n"
        "Tried exact, quote-normalized, whitespace-flexible, and token-based matching."
    )
    if hint:
        message += f"\n\nDid you mean (most similar match)?\n{hint}"
    message += "\n\nUse read_file to check the actual file contents."
    raise ValueError(message)


def _validate_non_overlapping(edits: list[_ResolvedEdit], file_path: str) -> None:
    ordered = sorted(edits, key=lambda item: item.start)
    for previous, current in zip(ordered, ordered[1:]):
        if current.start < previous.end:
            raise ValueError(
                f"Overlapping edits in {file_path}; old_text regions must not overlap."
            )
