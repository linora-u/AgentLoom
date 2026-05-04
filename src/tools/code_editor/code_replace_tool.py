"""
Code replace tool for AI Agents.

Provides single-replacement code editing with a 12-level matching engine.
Designed for fully automated AI Agent use — no human interaction.
"""

import logging
from pathlib import Path

from ._file_io import create_backup as _create_backup
from ._file_io import read_text, write_text
from ._match_engine import (
    find_similar_lines,
    flexible_search_and_replace,
)

logger = logging.getLogger(__name__)


def code_replace(
    file_path: str,
    search_text: str,
    replace_text: str,
    create_backup: bool = False,
    encoding: str = "utf-8",
) -> str:
    """Replace a code snippet in a file using intelligent matching.

    This tool performs a single search-and-replace operation with a
    12-level matching engine (3 strategies × 4 pre-processors) that
    tolerates common LLM errors like indentation shifts.

    **Matching chain** (tried in order):
    1. Exact string match
    2. Git cherry-pick (handles whitespace/indent via merge machinery)
    3. DMP line-level match (most tolerant — handles content drift)
    Each with 4 pre-processing combos: raw / strip-blank / relative-indent / both.

    Use ``code_edit`` instead if you have multiple SEARCH/REPLACE blocks.

    Args:
        file_path: Path to the file to edit.
        search_text: The code to find (can be approximate — engine will
            try to match even with indentation differences).
        replace_text: The replacement code.
        create_backup: Whether to create a .bak backup. Defaults to False.
        encoding: File encoding (default: utf-8).

    Returns:
        A description of the result, e.g.
        "Replaced code in src/main.py (lines 10-25, 5 lines changed)".

    Raises:
        ValueError: If arguments are empty or match fails (includes hints).
        FileNotFoundError: If the file does not exist.
    """
    if not file_path or not file_path.strip():
        raise ValueError("file_path cannot be empty")
    if not search_text:
        raise ValueError("search_text cannot be empty")

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    content, detected_enc = read_text(file_path, encoding=encoding)

    # Ensure texts end with newline
    s = search_text if search_text.endswith("\n") else search_text + "\n"
    r = replace_text if replace_text.endswith("\n") else replace_text + "\n"
    o = content if content.endswith("\n") else content + "\n"

    # Apply 12-level matching chain
    texts = (s, r, o)
    new_content = flexible_search_and_replace(texts)

    if new_content is None:
        # Generate hint
        hint = find_similar_lines(search_text, content)
        error_msg = (
            f"Failed to match search text in {file_path}.\n"
            f"The 12-level matching chain (exact → cherry-pick → DMP) "
            f"could not locate the code.\n"
        )
        if hint:
            error_msg += (
                f"\nDid you mean to match these lines?\n"
                f"```\n{hint}\n```\n"
            )
        error_msg += (
            "\nThe search text must closely match actual code in the file "
            "(function names, variable names, structure)."
        )
        raise ValueError(error_msg)

    if new_content == content:
        return f"No changes needed in {file_path}"

    if create_backup:
        _create_backup(file_path)

    write_text(file_path, new_content, encoding=detected_enc)

    lines_changed = _count_line_changes(content, new_content)
    logger.info(f"Replaced code in {file_path}: {lines_changed} lines changed")
    return f"Replaced code in {file_path} ({lines_changed} lines changed)"


def _count_line_changes(original: str, new: str) -> int:
    """Count the number of changed lines between two strings."""
    orig_lines = original.splitlines()
    new_lines = new.splitlines()
    max_lines = max(len(orig_lines), len(new_lines))
    changes = 0
    for i in range(max_lines):
        orig_line = orig_lines[i] if i < len(orig_lines) else ""
        new_line = new_lines[i] if i < len(new_lines) else ""
        if orig_line != new_line:
            changes += 1
    return changes
