"""
String-matching utilities for the edit_file tool.

Provides multi-strategy matching (exact → quote-normalized → whitespace-
tolerant → token-based) and helper functions for line-ending handling.
"""

from __future__ import annotations

import difflib
import re
from typing import Optional


# ---------------------------------------------------------------------------
# Curly-quote normalization (aligned with upstream)
# ---------------------------------------------------------------------------
LEFT_SINGLE_CURLY = "\u2018"   # '
RIGHT_SINGLE_CURLY = "\u2019"  # '
LEFT_DOUBLE_CURLY = "\u201c"   # "
RIGHT_DOUBLE_CURLY = "\u201d"  # "


def normalize_quotes(s: str) -> str:
    """Convert curly/smart quotes to ASCII straight quotes."""
    return (
        s
        .replace(LEFT_SINGLE_CURLY, "'")
        .replace(RIGHT_SINGLE_CURLY, "'")
        .replace(LEFT_DOUBLE_CURLY, '"')
        .replace(RIGHT_DOUBLE_CURLY, '"')
    )


def _is_opening_context(chars: list[str], index: int) -> bool:
    """Return True if the quote at *index* is in opening position."""
    if index == 0:
        return True
    prev = chars[index - 1]
    return prev in (" ", "\t", "\n", "\r", "(", "[", "{", "\u2014", "\u2013")


def _apply_curly_double_quotes(s: str) -> str:
    """Replace ASCII double-quotes with context-appropriate curly doubles."""
    chars = list(s)
    result: list[str] = []
    for i, ch in enumerate(chars):
        if ch == '"':
            result.append(
                LEFT_DOUBLE_CURLY if _is_opening_context(chars, i)
                else RIGHT_DOUBLE_CURLY
            )
        else:
            result.append(ch)
    return "".join(result)


def _apply_curly_single_quotes(s: str) -> str:
    """Replace ASCII single-quotes with context-appropriate curly singles.

    Handles contractions (e.g. "don't") by detecting letter adjacency.
    """
    chars = list(s)
    result: list[str] = []
    for i, ch in enumerate(chars):
        if ch == "'":
            prev = chars[i - 1] if i > 0 else ""
            nxt = chars[i + 1] if i < len(chars) - 1 else ""
            prev_is_letter = bool(re.match(r"\w", prev, re.UNICODE)) if prev else False
            next_is_letter = bool(re.match(r"\w", nxt, re.UNICODE)) if nxt else False
            if prev_is_letter and next_is_letter:
                # Contraction — always right-curly
                result.append(RIGHT_SINGLE_CURLY)
            else:
                result.append(
                    LEFT_SINGLE_CURLY if _is_opening_context(chars, i)
                    else RIGHT_SINGLE_CURLY
                )
        else:
            result.append(ch)
    return "".join(result)


def preserve_quote_style(
    old_string: str,
    actual_old_string: str,
    new_string: str,
) -> str:
    """Re-apply the file's curly-quote style to *new_string*.

    If the file used curly quotes (detected by comparing *old_string* from
    the model with *actual_old_string* from the file), convert ASCII quotes
    in *new_string* to matching curly quotes.
    """
    if old_string == actual_old_string:
        return new_string

    has_double = (
        LEFT_DOUBLE_CURLY in actual_old_string
        or RIGHT_DOUBLE_CURLY in actual_old_string
    )
    has_single = (
        LEFT_SINGLE_CURLY in actual_old_string
        or RIGHT_SINGLE_CURLY in actual_old_string
    )

    if not has_double and not has_single:
        return new_string

    result = new_string
    if has_double:
        result = _apply_curly_double_quotes(result)
    if has_single:
        result = _apply_curly_single_quotes(result)
    return result


def find_actual_string(file_content: str, search_string: str) -> Optional[str]:
    """Find *search_string* in *file_content*, trying quote normalization.

    Returns the **original substring from the file** (preserving its quote
    style), or ``None`` if not found even after normalization.
    """
    # Exact match first
    if search_string in file_content:
        return search_string

    # Try with normalized quotes
    norm_search = normalize_quotes(search_string)
    norm_file = normalize_quotes(file_content)

    idx = norm_file.find(norm_search)
    if idx != -1:
        return file_content[idx : idx + len(search_string)]

    return None


# ---------------------------------------------------------------------------
# Line-ending utilities
# ---------------------------------------------------------------------------

def count_occurrences(content: str, substring: str) -> int:
    """Count non-overlapping occurrences of *substring* in *content*."""
    if not substring:
        return 0
    return content.count(substring)


def normalize_to_lf(content: str) -> str:
    """Normalize all line endings to LF."""
    return content.replace("\r\n", "\n")


def detect_line_ending(content: str) -> str:
    """Detect the predominant line ending in *content*."""
    if "\r\n" in content:
        return "\r\n"
    return "\n"


def restore_line_ending(content_lf: str, line_ending: str) -> str:
    """Restore *content_lf* (LF-only) to the original *line_ending*."""
    if line_ending == "\n":
        return content_lf
    return content_lf.replace("\n", "\r\n")


# ---------------------------------------------------------------------------
# Whitespace-tolerant and token-based regex builders
# ---------------------------------------------------------------------------

def build_whitespace_tolerant_regex(old_lf: str) -> re.Pattern:
    """Build a regex matching *old_lf* with flexible whitespace."""
    if not old_lf:
        return re.compile(r"(?!)")  # Never matches

    parts = re.findall(r"\s+|\S+", old_lf)
    pattern_parts: list[str] = []
    for part in parts:
        if part and part.isspace():
            pattern_parts.append(r"\s+" if "\n" in part else r"[ \t]+")
        else:
            pattern_parts.append(re.escape(part))

    return re.compile("".join(pattern_parts), re.MULTILINE)


def build_token_regex(old_lf: str) -> re.Pattern:
    """Build a regex matching the token sequence of *old_lf* ignoring whitespace."""
    tokens = old_lf.split()
    if not tokens:
        return re.compile(r"(?!)")
    return re.compile(r"\s+".join(map(re.escape, tokens)), re.MULTILINE)


# ---------------------------------------------------------------------------
# Similar-line finder (helpful error messages)
# ---------------------------------------------------------------------------

def find_similar_lines(
    content: str,
    search_text: str,
    max_preview_lines: int = 5,
) -> str:
    """Find the most similar chunk in *content* to *search_text*.

    Returns a formatted preview with line numbers, or empty string.
    """
    search_lines = search_text.splitlines()
    if not search_lines:
        return ""

    content_lines = content.splitlines()
    window = len(search_lines)
    if window > len(content_lines):
        return ""

    best_ratio = 0.0
    best_start = 0

    for start in range(len(content_lines) - window + 1):
        candidate = content_lines[start : start + window]
        ratio = difflib.SequenceMatcher(
            None, "\n".join(search_lines), "\n".join(candidate)
        ).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start

    if best_ratio < 0.4:
        return ""

    preview = content_lines[best_start : best_start + min(window, max_preview_lines)]
    lines_text = "\n".join(
        f"  L{best_start + i + 1:4d}: {ln}" for i, ln in enumerate(preview)
    )
    if window > max_preview_lines:
        lines_text += f"\n  ... ({window - max_preview_lines} more lines)"

    return (
        f"Lines {best_start + 1}-{best_start + window} "
        f"(similarity: {best_ratio:.0%}):\n{lines_text}"
    )
