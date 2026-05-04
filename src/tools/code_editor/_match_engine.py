"""
Core matching engine for SEARCH/REPLACE code editing.

Provides multi-level matching strategies inspired by Aider's search_replace.py.
Designed for AI Agent automated code editing — no human interaction required.

**12-level matching chain** (3 strategies × 4 pre-processors):

Strategies (tried in order):
1. simple_search_and_replace — exact string.replace
2. git_cherry_pick           — leverages git merge machinery for whitespace tolerance
3. dmp_lines_apply           — diff-match-patch line-level matching (most robust)

Pre-processors (tried for each strategy):
A. raw text
B. strip leading/trailing blank lines
C. relative indentation normalization
D. strip blank lines + relative indentation

References:
- Aider search_replace.py — RelativeIndenter, dmp_lines_apply, flexible_search_and_replace
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SearchReplaceError(Exception):
    """
    Raised when a SEARCH/REPLACE operation fails.

    The error message is designed to be AI-friendly, containing:
    - Which SEARCH block failed
    - "Did you mean..." suggestions with the most similar content
    - Guidance on how to fix the block
    """

    pass


# ---------------------------------------------------------------------------
# Block parsing
# ---------------------------------------------------------------------------

HEAD = r"^<{5,9} SEARCH>?\s*$"
DIVIDER = r"^={5,9}\s*$"
UPDATED = r"^>{5,9} REPLACE\s*$"

_head_re = re.compile(HEAD)
_divider_re = re.compile(DIVIDER)
_updated_re = re.compile(UPDATED)


class EditBlock:
    """A single SEARCH/REPLACE block."""

    __slots__ = ("search", "replace")

    def __init__(self, search: str, replace: str):
        self.search = search
        self.replace = replace

    def __repr__(self) -> str:
        return f"EditBlock(search={self.search!r:.60}, replace={self.replace!r:.60})"


def parse_search_replace_blocks(diff_content: str) -> List[EditBlock]:
    """Parse one or more SEARCH/REPLACE blocks from diff content.

    Expected format (each block)::

        <<<<<<< SEARCH
        original code
        =======
        replacement code
        >>>>>>> REPLACE

    Supports 5-9 angle brackets / equals signs for flexibility.
    """
    if not diff_content or not diff_content.strip():
        return []

    blocks: List[EditBlock] = []
    lines = diff_content.splitlines(keepends=True)
    i = 0

    while i < len(lines):
        line = lines[i]

        if _head_re.match(line.strip()):
            search_lines: list[str] = []
            i += 1
            while i < len(lines) and not _divider_re.match(lines[i].strip()):
                search_lines.append(lines[i])
                i += 1

            if i >= len(lines):
                raise SearchReplaceError(
                    "Malformed SEARCH/REPLACE block: missing '=======' divider.\n"
                    "Block started with SEARCH but no ======= was found."
                )
            i += 1

            replace_lines: list[str] = []
            while i < len(lines) and not _updated_re.match(lines[i].strip()):
                replace_lines.append(lines[i])
                i += 1

            if i >= len(lines):
                raise SearchReplaceError(
                    "Malformed SEARCH/REPLACE block: missing '>>>>>>> REPLACE' footer.\n"
                    "Found ======= but no >>>>>>> REPLACE."
                )
            i += 1

            blocks.append(EditBlock("".join(search_lines), "".join(replace_lines)))
        else:
            i += 1

    return blocks


# ============================================================================
# RelativeIndenter — ported from Aider search_replace.py
# ============================================================================


class RelativeIndenter:
    """Rewrites text to use relative indentation.

    Converts absolute indentation to relative (change-from-previous-line),
    making it easier to match code blocks that differ only in overall
    indentation level.

    Example::

        # Absolute (input):              Relative (output):
                Foo                               Foo
                    Bar                       Bar       (4 more)
                    Baz                   Baz           (same)
                Fob                   ←←←←Fob          (4 less)
    """

    def __init__(self, texts: list[str]):
        chars: set[str] = set()
        for text in texts:
            chars.update(text)

        ARROW = "←"
        if ARROW not in chars:
            self.marker = ARROW
        else:
            self.marker = self._select_unique_marker(chars)

    @staticmethod
    def _select_unique_marker(chars: set[str]) -> str:
        for codepoint in range(0x10FFFF, 0x10000, -1):
            marker = chr(codepoint)
            if marker not in chars:
                return marker
        raise ValueError("Could not find a unique marker character")

    def make_relative(self, text: str) -> str:
        """Transform text to use relative indents."""
        if self.marker in text:
            raise ValueError(
                f"Text already contains the outdent marker: {self.marker}"
            )

        lines = text.splitlines(keepends=True)
        output: list[str] = []
        prev_indent = ""

        for line in lines:
            line_without_end = line.rstrip("\n\r")
            len_indent = len(line_without_end) - len(line_without_end.lstrip())
            indent = line[:len_indent]
            change = len_indent - len(prev_indent)

            if change > 0:
                cur_indent = indent[-change:]
            elif change < 0:
                cur_indent = self.marker * (-change)
            else:
                cur_indent = ""

            out_line = cur_indent + "\n" + line[len_indent:]
            output.append(out_line)
            prev_indent = indent

        return "".join(output)

    def make_absolute(self, text: str) -> str:
        """Transform text from relative back to absolute indents."""
        lines = text.splitlines(keepends=True)
        output: list[str] = []
        prev_indent = ""

        for i in range(0, len(lines), 2):
            dent = lines[i].rstrip("\r\n")
            non_indent = lines[i + 1] if i + 1 < len(lines) else ""

            if dent.startswith(self.marker):
                len_outdent = len(dent)
                cur_indent = (
                    prev_indent[:-len_outdent]
                    if len_outdent <= len(prev_indent)
                    else ""
                )
            else:
                cur_indent = prev_indent + dent

            if not non_indent.rstrip("\r\n"):
                out_line = non_indent
            else:
                out_line = cur_indent + non_indent

            output.append(out_line)
            prev_indent = cur_indent

        res = "".join(output)
        if self.marker in res:
            raise ValueError("Error transforming text back to absolute indents")
        return res


# ============================================================================
# DMP (diff-match-patch) line-level matching — ported from Aider
# ============================================================================


def _dmp_lines_to_chars(lines: str, mapping: list) -> str:
    """Convert line-character codes back to original line strings."""
    return "".join(mapping[ord(char)] for char in lines)


def dmp_lines_apply(texts: tuple) -> Optional[str]:
    """Apply search→replace as a line-level diff-match-patch operation.

    Converts text lines to single-character codes, runs DMP patch on
    the character representation, then converts back. Extremely tolerant
    of content differences.

    Args:
        texts: (search_text, replace_text, original_text) — all must end with \\n

    Returns:
        New text with replacement applied, or None if patching failed.
    """
    from diff_match_patch import diff_match_patch

    search_text, replace_text, original_text = texts

    for t in texts:
        if t and not t.endswith("\n"):
            return None

    dmp = diff_match_patch()
    dmp.Diff_Timeout = 5
    dmp.Match_Threshold = 0.1
    dmp.Match_Distance = 100_000
    dmp.Match_MaxBits = 32
    dmp.Patch_Margin = 1

    all_text = search_text + replace_text + original_text
    all_lines, _, mapping = dmp.diff_linesToChars(all_text, "")

    search_num = len(search_text.splitlines())
    replace_num = len(replace_text.splitlines())

    search_lines = all_lines[:search_num]
    replace_lines = all_lines[search_num : search_num + replace_num]
    original_lines = all_lines[search_num + replace_num :]

    diff_lines_result = dmp.diff_main(search_lines, replace_lines, None)
    dmp.diff_cleanupSemantic(diff_lines_result)
    dmp.diff_cleanupEfficiency(diff_lines_result)

    patches = dmp.patch_make(search_lines, diff_lines_result)
    new_lines, success = dmp.patch_apply(patches, original_lines)

    if False in success:
        return None

    return _dmp_lines_to_chars(new_lines, mapping)


# ============================================================================
# Git cherry-pick strategy wrapper
# ============================================================================


def _git_cherry_pick_strategy(texts: tuple) -> Optional[str]:
    """Use git cherry-pick machinery to apply search→replace onto original."""
    search_text, replace_text, original_text = texts
    try:
        from ._git_ops import git_cherry_pick_apply

        return git_cherry_pick_apply(original_text, search_text, replace_text)
    except Exception:
        return None


# ============================================================================
# Simple search-and-replace strategy (exact string match)
# ============================================================================


def _simple_search_and_replace(texts: tuple) -> Optional[str]:
    """Exact string replacement — the simplest strategy."""
    search_text, replace_text, original_text = texts
    if original_text.count(search_text) == 0:
        return None
    return original_text.replace(search_text, replace_text)


# ============================================================================
# Pre-processor helpers
# ============================================================================


def _relative_indent(
    texts: tuple,
) -> tuple:
    """Apply relative indentation normalization."""
    ri = RelativeIndenter(list(texts))
    return ri, tuple(ri.make_relative(t) for t in texts)


def _strip_blank_lines(texts: tuple) -> tuple:
    """Strip leading/trailing blank lines from all texts."""
    return tuple(text.strip("\n") + "\n" for text in texts)


# ============================================================================
# 12-level flexible matching chain — ported from Aider
# ============================================================================

ALL_PREPROCS = [
    (False, False),  # raw
    (True, False),   # strip blank lines only
    (False, True),   # relative indent only
    (True, True),    # both
]

EDITBLOCK_STRATEGIES = [
    (_simple_search_and_replace, ALL_PREPROCS),
    (_git_cherry_pick_strategy, ALL_PREPROCS),
    (dmp_lines_apply, ALL_PREPROCS),
]


def _try_strategy(
    texts: tuple,
    strategy,
    preproc: tuple,
) -> Optional[str]:
    """Apply pre-processor, run strategy, reverse pre-processing."""
    do_strip, do_relative = preproc
    ri = None
    working_texts = texts

    if do_strip:
        working_texts = _strip_blank_lines(working_texts)
    if do_relative:
        try:
            ri, working_texts = _relative_indent(working_texts)
        except ValueError:
            return None

    res = strategy(working_texts)

    if res and do_relative and ri:
        try:
            res = ri.make_absolute(res)
        except ValueError:
            return None

    return res


def flexible_search_and_replace(
    texts: tuple,
    strategies: list | None = None,
) -> Optional[str]:
    """Try a series of search/replace methods with increasing flexibility.

    Starts from the most literal interpretation (exact string match)
    and progresses to more flexible methods (DMP line-level matching).

    Args:
        texts: (search_text, replace_text, original_text)
        strategies: List of (strategy_fn, preprocs) pairs. Defaults to
            EDITBLOCK_STRATEGIES (12 combinations).

    Returns:
        New text with replacement applied, or None if all strategies failed.
    """
    if strategies is None:
        strategies = EDITBLOCK_STRATEGIES

    for strategy, preprocs in strategies:
        for preproc in preprocs:
            res = _try_strategy(texts, strategy, preproc)
            if res:
                return res

    return None


# ============================================================================
# Legacy 4-level sub-strategies (kept for dotdotdots + fuzzy fallback)
# ============================================================================


def _prep(content: str) -> Tuple[str, List[str]]:
    """Ensure content ends with newline, return (content, lines)."""
    if content and not content.endswith("\n"):
        content += "\n"
    return content, content.splitlines(keepends=True)


def _perfect_replace(
    whole_lines: List[str], part_lines: List[str], replace_lines: List[str]
) -> Optional[str]:
    """Exact line-by-line match and replace."""
    part_tup = tuple(part_lines)
    part_len = len(part_lines)
    for i in range(len(whole_lines) - part_len + 1):
        if tuple(whole_lines[i : i + part_len]) == part_tup:
            result = whole_lines[:i] + replace_lines + whole_lines[i + part_len :]
            return "".join(result)
    return None


def _perfect_or_whitespace(
    whole_lines: List[str], part_lines: List[str], replace_lines: List[str]
) -> Optional[str]:
    """Try perfect match first, then whitespace-tolerant."""
    res = _perfect_replace(whole_lines, part_lines, replace_lines)
    if res:
        return res
    return _replace_with_leading_whitespace_fix(whole_lines, part_lines, replace_lines)


def _replace_with_leading_whitespace_fix(
    whole_lines: List[str], part_lines: List[str], replace_lines: List[str]
) -> Optional[str]:
    """Fix LLM's common indentation errors."""
    leading = [
        len(p) - len(p.lstrip())
        for p in part_lines + replace_lines
        if p.strip()
    ]

    if leading and min(leading):
        num_leading = min(leading)
        part_lines = [p[num_leading:] if p.strip() else p for p in part_lines]
        replace_lines = [p[num_leading:] if p.strip() else p for p in replace_lines]

    num_part = len(part_lines)
    for i in range(len(whole_lines) - num_part + 1):
        add_leading = _match_ignoring_leading_ws(
            whole_lines[i : i + num_part], part_lines
        )
        if add_leading is None:
            continue
        fixed_replace = [
            add_leading + line if line.strip() else line for line in replace_lines
        ]
        result = whole_lines[:i] + fixed_replace + whole_lines[i + num_part :]
        return "".join(result)
    return None


def _match_ignoring_leading_ws(
    whole_lines: List[str], part_lines: List[str]
) -> Optional[str]:
    """Check if lines match ignoring leading whitespace."""
    num = len(whole_lines)
    if not all(
        whole_lines[i].lstrip() == part_lines[i].lstrip() for i in range(num)
    ):
        return None
    offsets = set(
        whole_lines[i][: len(whole_lines[i]) - len(part_lines[i])]
        for i in range(num)
        if whole_lines[i].strip()
    )
    if len(offsets) != 1:
        return None
    return offsets.pop()


def _try_dotdotdots(whole: str, part: str, replace: str) -> Optional[str]:
    """Handle `...` lines that skip intermediate code."""
    dots_re = re.compile(r"(^\s*\.\.\.\n)", re.MULTILINE | re.DOTALL)
    part_pieces = re.split(dots_re, part)
    replace_pieces = re.split(dots_re, replace)

    if len(part_pieces) != len(replace_pieces):
        raise ValueError("Unpaired ... in SEARCH/REPLACE block")
    if len(part_pieces) == 1:
        return None
    if not all(
        part_pieces[i] == replace_pieces[i]
        for i in range(1, len(part_pieces), 2)
    ):
        raise ValueError("Unmatched ... in SEARCH/REPLACE block")

    part_chunks = [part_pieces[i] for i in range(0, len(part_pieces), 2)]
    replace_chunks = [replace_pieces[i] for i in range(0, len(replace_pieces), 2)]

    for p, r in zip(part_chunks, replace_chunks):
        if not p and not r:
            continue
        if not p and r:
            if not whole.endswith("\n"):
                whole += "\n"
            whole += r
            continue
        if whole.count(p) == 0:
            raise ValueError("Dotdotdots chunk not found in file")
        if whole.count(p) > 1:
            raise ValueError("Dotdotdots chunk found multiple times in file")
        whole = whole.replace(p, r, 1)
    return whole


def _fuzzy_replace(
    whole_lines: List[str],
    part: str,
    part_lines: List[str],
    replace_lines: List[str],
    threshold: float = 0.8,
) -> Optional[str]:
    """Find the most similar chunk using SequenceMatcher and replace it."""
    if not part_lines:
        return None

    max_similarity = 0.0
    best_start = -1
    best_end = -1

    scale = 0.1
    min_len = math.floor(len(part_lines) * (1 - scale))
    max_len = math.ceil(len(part_lines) * (1 + scale))

    for length in range(min_len, max_len + 1):
        for i in range(len(whole_lines) - length + 1):
            chunk = "".join(whole_lines[i : i + length])
            similarity = SequenceMatcher(None, chunk, part).ratio()
            if similarity > max_similarity:
                max_similarity = similarity
                best_start = i
                best_end = i + length

    if max_similarity < threshold:
        return None

    result = whole_lines[:best_start] + replace_lines + whole_lines[best_end:]
    return "".join(result)


# ============================================================================
# Main entry: replace_most_similar_chunk (enhanced with 12-level chain)
# ============================================================================


def replace_most_similar_chunk(
    whole: str, part: str, replace: str
) -> Optional[str]:
    """Best-effort find ``part`` in ``whole`` and replace with ``replace``.

    Uses a multi-level matching strategy:

    **Level 1 — 12-level flexible chain** (3 strategies × 4 pre-processors):
      simple_replace → git_cherry_pick → dmp_lines_apply,
      each with raw / strip_blank / relative_indent / both.

    **Level 2 — Legacy sub-strategies** (fallback):
      perfect → whitespace_fix → dotdotdots → fuzzy

    Args:
        whole: The complete file content.
        part: The SEARCH text to find.
        replace: The REPLACE text to substitute.

    Returns:
        New content with replacement applied, or None if no match found.
    """
    if whole and not whole.endswith("\n"):
        whole += "\n"
    if part and not part.endswith("\n"):
        part += "\n"
    if replace and not replace.endswith("\n"):
        replace += "\n"

    # --- Level 1: 12-level flexible chain ---
    texts = (part, replace, whole)
    res = flexible_search_and_replace(texts)
    if res:
        return res

    # --- Level 2: Legacy sub-strategies (dotdotdots + fuzzy) ---
    whole_content, whole_lines = _prep(whole)
    part_content, part_lines = _prep(part)
    replace_content, replace_lines = _prep(replace)

    # LLM sometimes adds a spurious leading blank line
    if len(part_lines) > 2 and not part_lines[0].strip():
        res = _perfect_or_whitespace(whole_lines, part_lines[1:], replace_lines)
        if res:
            return res

    # Dotdotdots
    try:
        res = _try_dotdotdots(whole_content, part_content, replace_content)
        if res:
            return res
    except ValueError:
        pass

    # Fuzzy edit-distance match
    res = _fuzzy_replace(whole_lines, part_content, part_lines, replace_lines)
    if res:
        return res

    return None


# ============================================================================
# search_in_file — for code_search tool
# ============================================================================


@dataclass
class MatchResult:
    """A single search match result."""

    start_line: int
    end_line: int
    matched_text: str
    similarity: float
    context_before: str = ""
    context_after: str = ""


def search_in_file(
    content: str,
    search_text: str,
    *,
    context_lines: int = 3,
    max_results: int = 5,
) -> List[MatchResult]:
    """Search for code in file content using multi-level matching.

    Tries exact match, whitespace-tolerant match, then fuzzy match.

    Args:
        content: The complete file content.
        search_text: The code snippet to search for.
        context_lines: Number of context lines before/after each match.
        max_results: Maximum number of results to return.

    Returns:
        List of MatchResult sorted by similarity descending.
    """
    if not content or not search_text or not search_text.strip():
        return []

    content_lines = content.splitlines()
    search_lines = search_text.splitlines()

    if not content_lines or not search_lines:
        return []

    results: list[MatchResult] = []
    search_len = len(search_lines)

    # Strategy 1: Exact line match
    for i in range(len(content_lines) - search_len + 1):
        chunk = content_lines[i : i + search_len]
        if chunk == search_lines:
            results.append(
                _build_match(content_lines, i, search_len, 1.0, context_lines)
            )

    if results:
        return results[:max_results]

    # Strategy 2: Whitespace-tolerant match
    stripped_search = [line.lstrip() for line in search_lines]
    for i in range(len(content_lines) - search_len + 1):
        chunk_stripped = [
            line.lstrip() for line in content_lines[i : i + search_len]
        ]
        if chunk_stripped == stripped_search:
            chunk_raw = "\n".join(content_lines[i : i + search_len])
            similarity = SequenceMatcher(
                None, "\n".join(search_lines), chunk_raw
            ).ratio()
            results.append(
                _build_match(content_lines, i, search_len, similarity, context_lines)
            )

    if results:
        results.sort(key=lambda r: r.similarity, reverse=True)
        return results[:max_results]

    # Strategy 3: Fuzzy sliding window
    search_str = "\n".join(search_lines)
    scale = 0.1
    min_len = max(1, math.floor(search_len * (1 - scale)))
    max_len_val = math.ceil(search_len * (1 + scale))

    candidates: list[tuple[float, int, int]] = []
    for length in range(min_len, max_len_val + 1):
        for i in range(len(content_lines) - length + 1):
            chunk = "\n".join(content_lines[i : i + length])
            similarity = SequenceMatcher(None, search_str, chunk).ratio()
            if similarity >= 0.6:
                candidates.append((similarity, i, length))

    candidates.sort(key=lambda x: x[0], reverse=True)
    used_lines: set[int] = set()
    for sim, start, length in candidates:
        line_range = set(range(start, start + length))
        if line_range & used_lines:
            continue
        used_lines.update(line_range)
        results.append(
            _build_match(content_lines, start, length, sim, context_lines)
        )
        if len(results) >= max_results:
            break

    results.sort(key=lambda r: r.similarity, reverse=True)
    return results[:max_results]


def _build_match(
    content_lines: list[str],
    start: int,
    length: int,
    similarity: float,
    context_lines: int,
) -> MatchResult:
    """Build a MatchResult with context."""
    ctx_start = max(0, start - context_lines)
    ctx_end = min(len(content_lines), start + length + context_lines)
    return MatchResult(
        start_line=start + 1,
        end_line=start + length,
        matched_text="\n".join(content_lines[start : start + length]),
        similarity=similarity,
        context_before="\n".join(content_lines[ctx_start:start]),
        context_after="\n".join(content_lines[start + length : ctx_end]),
    )


# ============================================================================
# Error helper: "Did you mean..."
# ============================================================================


def find_similar_lines(
    search_text: str, content: str, threshold: float = 0.6
) -> str:
    """Find the most similar chunk in content for "Did you mean..." hints."""
    search_lines = search_text.splitlines()
    content_lines = content.splitlines()

    if not search_lines or not content_lines:
        return ""

    best_ratio = 0.0
    best_match: Optional[List[str]] = None
    best_idx = 0

    for i in range(len(content_lines) - len(search_lines) + 1):
        chunk = content_lines[i : i + len(search_lines)]
        ratio = SequenceMatcher(None, search_lines, chunk).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = chunk
            best_idx = i

    if best_ratio < threshold or best_match is None:
        return ""

    if best_match[0] == search_lines[0] and best_match[-1] == search_lines[-1]:
        return "\n".join(best_match)

    ctx = 5
    start = max(0, best_idx - ctx)
    end = min(len(content_lines), best_idx + len(search_lines) + ctx)
    return "\n".join(content_lines[start:end])
