"""
Code edit tool for AI Agents — unified entry point.

Parses SEARCH/REPLACE blocks and applies each replacement using the
12-level matching engine. Designed for fully automated AI Agent use.
"""

import logging
from pathlib import Path

from ._file_io import create_backup as _create_backup
from ._file_io import read_text, write_text
from ._match_engine import (
    EditBlock,
    SearchReplaceError,
    find_similar_lines,
    parse_search_replace_blocks,
    replace_most_similar_chunk,
)

logger = logging.getLogger(__name__)


def code_edit(
    file_path: str,
    diff_content: str,
    create_backup: bool = False,
    encoding: str = "utf-8",
) -> str:
    """Edit a code file using SEARCH/REPLACE blocks with intelligent matching.

    This tool applies one or more SEARCH/REPLACE edits to a file. It uses a
    12-level matching engine (3 strategies × 4 pre-processors) that tolerates
    common LLM errors like indentation shifts.

    **SEARCH/REPLACE format** (can include multiple blocks)::

        <<<<<<< SEARCH
        exact code to find in the file
        =======
        new code to replace it with
        >>>>>>> REPLACE

    **Matching engine** (tried in order for each block):
    1. Exact string match (with 4 pre-processing combos)
    2. Git cherry-pick merge (handles whitespace via git machinery)
    3. DMP line-level match (most tolerant — handles content drift)
    4. Dotdotdots (...) ellipsis support
    5. Fuzzy edit-distance match (SequenceMatcher 0.8 threshold)

    **Special cases**:
    - Empty SEARCH block + non-existent file → creates the file
    - Empty SEARCH block + existing file → appends REPLACE content

    Args:
        file_path: Path to the file to edit.
        diff_content: One or more SEARCH/REPLACE blocks as described above.
        create_backup: Whether to create a .bak backup. Defaults to False.
        encoding: File encoding (default: utf-8).

    Returns:
        A description of the result, e.g.
        "Successfully applied 2 edit(s) to src/main.py (3 lines changed)".

    Raises:
        ValueError: If arguments are invalid or blocks fail to match
            (includes "Did you mean..." hints).
        FileNotFoundError: If the file does not exist and SEARCH is non-empty.
    """
    # --- Validate ---
    if not file_path or not file_path.strip():
        raise ValueError("file_path cannot be empty")
    if not diff_content or not diff_content.strip():
        raise ValueError("diff_content cannot be empty")

    try:
        blocks = parse_search_replace_blocks(diff_content)
    except SearchReplaceError as e:
        raise ValueError(f"Failed to parse SEARCH/REPLACE blocks: {e}") from e

    if not blocks:
        raise ValueError(
            "No SEARCH/REPLACE blocks found in diff_content. "
            "Expected format:\n"
            "<<<<<<< SEARCH\n"
            "original code\n"
            "=======\n"
            "replacement code\n"
            ">>>>>>> REPLACE"
        )

    path = Path(file_path)

    # --- New file creation ---
    if not path.exists():
        non_empty_search = [b for b in blocks if b.search.strip()]
        if non_empty_search:
            raise FileNotFoundError(
                f"File not found: {file_path}\n"
                f"Cannot apply SEARCH/REPLACE to a non-existent file "
                f"unless the SEARCH block is empty (for new file creation)."
            )
        new_content = "".join(b.replace for b in blocks)
        detected_enc = encoding
        write_text(file_path, new_content, encoding=detected_enc)
        logger.info(f"Created new file: {file_path}")
        return f"Created new file: {file_path} ({_count_lines(new_content)} lines)"

    # --- Read existing file ---
    content, detected_enc = read_text(file_path, encoding=encoding)

    if create_backup:
        _create_backup(file_path)

    # --- Apply blocks sequentially ---
    original_content = content
    failed_blocks: list[tuple[int, EditBlock]] = []
    passed_count = 0

    for idx, block in enumerate(blocks):
        if not block.search.strip():
            content = content + block.replace
            passed_count += 1
            continue

        new_content = replace_most_similar_chunk(content, block.search, block.replace)
        if new_content is not None:
            content = new_content
            passed_count += 1
        else:
            failed_blocks.append((idx, block))

    # --- Handle failures ---
    if failed_blocks:
        error_parts = [
            f"# {len(failed_blocks)} SEARCH/REPLACE block(s) failed to match!\n"
        ]
        for idx, block in failed_blocks:
            error_parts.append(
                f"## Block {idx + 1} failed to match in {file_path}\n"
                f"<<<<<<< SEARCH\n"
                f"{block.search}"
                f"=======\n"
                f"{block.replace}"
                f">>>>>>> REPLACE\n"
            )
            hint = find_similar_lines(block.search, original_content)
            if hint:
                error_parts.append(
                    f"Did you mean to match some of these actual lines "
                    f"from {file_path}?\n\n```\n{hint}\n```\n"
                )

        error_parts.append(
            "The SEARCH section must closely match an existing block of "
            "lines including whitespace, comments, indentation, etc.\n"
        )

        if passed_count > 0:
            error_parts.append(
                f"\n# The other {passed_count} SEARCH/REPLACE block(s) "
                f"were applied successfully.\n"
                f"Don't re-send them. Just reply with fixed versions of "
                f"the failed block(s).\n"
            )
            write_text(file_path, content, encoding=detected_enc)

        raise ValueError("\n".join(error_parts))

    # --- Write result ---
    if content == original_content:
        return f"No changes needed in {file_path}"

    write_text(file_path, content, encoding=detected_enc)

    lines_changed = _count_line_changes(original_content, content)
    logger.info(
        f"Edited {file_path}: {passed_count} block(s), {lines_changed} lines changed"
    )
    return (
        f"Successfully applied {passed_count} edit(s) to {file_path} "
        f"({lines_changed} lines changed)"
    )


def _count_lines(content: str) -> int:
    if not content:
        return 0
    return len(content.splitlines())


def _count_line_changes(original: str, new: str) -> int:
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
