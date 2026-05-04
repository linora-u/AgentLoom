"""
Monkeypatch for smolagents ``extract_code_from_text`` / ``parse_code_blobs``.

Problem
-------
The upstream implementation uses a **non-greedy** regex::

    rf"{open_tag}(.*?){close_tag}"

When the LLM-generated Python code itself contains the closing delimiter
(e.g. triple-backtick inside a Markdown string literal), the non-greedy
quantifier matches the **first** occurrence of the closing tag *inside*
the code, truncating the extraction and producing a SyntaxError.

Fix
---

1. Use a **greedy** regex ``(.*){close_tag}`` so it matches the **last**
   closing delimiter – equivalent to ``lastIndexOf`` in JavaScript.
2. Validate the extracted code with ``ast.parse``.
3. If validation fails (possible but unlikely), progressively try shorter
   matches by searching for earlier closing tags via ``rfind``.
4. Fall back to the original non-greedy match as a last resort.
"""

from __future__ import annotations

import ast
import re
import sys
import warnings
from typing import Optional


# ---------------------------------------------------------------------------
#  Core helpers
# ---------------------------------------------------------------------------


def _is_valid_python(code: str) -> bool:
    """Return *True* if *code* can be parsed as valid Python."""
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _try_extract_greedy(
    text: str,
    open_tag: str,
    close_tag: str,
) -> list[str]:
    """Extract code blocks using a **greedy** strategy.

    For each ``open_tag`` found we locate the **last** ``close_tag`` that
    comes after it and work backwards until we find a valid Python snippet
    (or exhaust all candidates).
    """
    results: list[str] = []
    search_start = 0

    while True:
        open_idx = text.find(open_tag, search_start)
        if open_idx == -1:
            break

        code_start = open_idx + len(open_tag)

        # Find the LAST close_tag after code_start  (greedy / lastIndexOf)
        close_idx = text.rfind(close_tag, code_start)
        if close_idx == -1 or close_idx <= code_start:
            # No valid close tag found after this open tag – skip it
            search_start = code_start
            break

        # Try progressively shorter spans (last → earlier close tags)
        candidate_end = close_idx
        found = False
        while candidate_end > code_start:
            candidate = text[code_start:candidate_end].strip()
            if candidate and _is_valid_python(candidate):
                results.append(candidate)
                found = True
                # Continue searching after this complete block
                search_start = candidate_end + len(close_tag)
                break
            # Move to the previous close_tag occurrence
            candidate_end = text.rfind(close_tag, code_start, candidate_end)
            if candidate_end == -1 or candidate_end <= code_start:
                break

        if not found:
            # Greedy couldn't find a valid block from this open_tag
            # Fall through – the caller will try the original regex
            search_start = code_start
            break

    return results


def _try_extract_nongreedy(
    text: str,
    open_tag: str,
    close_tag: str,
) -> list[str]:
    """Original non-greedy extraction (upstream behaviour).

    NOTE: tags are used as-is (no ``re.escape``) to stay compatible with
    the upstream fallback pattern ``("```(?:python|py)", "\\n```")`` which
    contains regex syntax.
    """
    pattern = rf"{open_tag}(.*?){close_tag}"
    matches = re.findall(pattern, text, re.DOTALL)
    return [m.strip() for m in matches if m.strip()]


# ---------------------------------------------------------------------------
#  Public patched functions
# ---------------------------------------------------------------------------


def _patched_extract_code_from_text(
    text: str,
    code_block_tags: tuple[str, str],
) -> Optional[str]:
    """Drop-in replacement for ``smolagents.utils.extract_code_from_text``.

    Strategy (ordered):
    1. Greedy match (last close-tag) + ``ast.parse`` validation.
    2. Original non-greedy match as fallback.
    """
    open_tag, close_tag = code_block_tags

    # --- Strategy 1: greedy + validation ---
    greedy_matches = _try_extract_greedy(text, open_tag, close_tag)
    if greedy_matches:
        return "\n\n".join(greedy_matches)

    # --- Strategy 2: original non-greedy (backward-compat) ---
    nongreedy_matches = _try_extract_nongreedy(text, open_tag, close_tag)
    if nongreedy_matches:
        return "\n\n".join(nongreedy_matches)

    return None


def _patched_parse_code_blobs(
    text: str,
    code_block_tags: tuple[str, str],
) -> str:
    """Drop-in replacement for ``smolagents.utils.parse_code_blobs``.

    Identical logic to upstream except it delegates to our patched
    ``_patched_extract_code_from_text``.
    """
    from textwrap import dedent

    matches = _patched_extract_code_from_text(text, code_block_tags)
    if not matches:  # Fallback to markdown pattern
        matches = _patched_extract_code_from_text(
            text, ("```(?:python|py)", "\n```")
        )
    if matches:
        return matches

    # Maybe the LLM outputted a code blob directly
    try:
        ast.parse(text)
        return text
    except SyntaxError:
        pass

    if "final" in text and "answer" in text:
        raise ValueError(
            dedent(
                f"""
                Your code snippet is invalid, because the regex pattern {code_block_tags[0]}(.*?){code_block_tags[1]} was not found in it.
                Here is your code snippet:
                {text}
                It seems like you're trying to return the final answer, you can do it as follows:
                {code_block_tags[0]}
                final_answer("YOUR FINAL ANSWER HERE")
                {code_block_tags[1]}
                """
            ).strip()
        )
    raise ValueError(
        dedent(
            f"""
            Your code snippet is invalid, because the regex pattern {code_block_tags[0]}(.*?){code_block_tags[1]} was not found in it.
            Here is your code snippet:
            {text}
            Make sure to include code with the correct pattern, for instance:
            Thoughts: Your thoughts
            {code_block_tags[0]}
            # Your python code here
            {code_block_tags[1]}
            """
        ).strip()
    )


# ---------------------------------------------------------------------------
#  Patching entry-point
# ---------------------------------------------------------------------------

_PATCHED = False


def patch_smolagents_code_extraction() -> None:
    """Apply the greedy-extraction monkeypatch globally.

    Safe to call multiple times (idempotent).  Follows the same pattern
    established by ``disable_smolagents_truncation`` in *memory_truncate.py*.
    """
    global _PATCHED
    if _PATCHED:
        return

    try:
        from smolagents import utils as _utils

        # Patch the canonical location
        _utils.extract_code_from_text = _patched_extract_code_from_text
        _utils.parse_code_blobs = _patched_parse_code_blobs

        # Patch every smolagents sub-module that may have imported these names
        patched_count = 0
        modules_to_patch = [
            module
            for name, module in sys.modules.items()
            if name.startswith("smolagents")
        ]
        for module in modules_to_patch:
            for attr, replacement in (
                ("extract_code_from_text", _patched_extract_code_from_text),
                ("parse_code_blobs", _patched_parse_code_blobs),
            ):
                if hasattr(module, attr):
                    old_val = getattr(module, attr)
                    if old_val is not replacement:
                        setattr(module, attr, replacement)
                        patched_count += 1

        _PATCHED = True
    except Exception as exc:
        warnings.warn(
            f"Failed to patch smolagents code extraction: {exc}",
            RuntimeWarning,
        )
