"""
Code search tool for AI Agents.

Provides intelligent code search/location capabilities using multi-level
matching strategies. Designed for fully automated AI Agent use.

This tool only READS files — it never modifies them.
"""

import logging

from ._file_io import read_text
from ._match_engine import search_in_file

logger = logging.getLogger(__name__)


def code_search(
    file_path: str,
    search_text: str,
    context_lines: int = 3,
    max_results: int = 5,
    encoding: str = "utf-8",
) -> str:
    """Search for a code snippet in a file using intelligent matching.

    This tool locates code in a file WITHOUT modifying it. It uses
    multi-level matching: exact → whitespace-tolerant → fuzzy.

    Use this tool to:
    - Find the exact location of a function/class/block before editing
    - Verify whether a code pattern exists in a file
    - Get line numbers and surrounding context for a code snippet

    Args:
        file_path: Path to the file to search in.
        search_text: The code snippet to search for (can be approximate).
        context_lines: Number of context lines to include before/after
            each match. Defaults to 3.
        max_results: Maximum number of matches to return. Defaults to 5.
        encoding: File encoding (default: utf-8).

    Returns:
        A formatted string describing all matches found, including
        line numbers, matched text, similarity score, and context.
        Returns "No matches found" with suggestions if nothing matches.

    Raises:
        ValueError: If file_path or search_text is empty.
        FileNotFoundError: If the file does not exist.
    """
    if not file_path or not file_path.strip():
        raise ValueError("file_path cannot be empty")
    if not search_text or not search_text.strip():
        raise ValueError("search_text cannot be empty")

    content, _ = read_text(file_path, encoding=encoding)

    results = search_in_file(
        content,
        search_text,
        context_lines=context_lines,
        max_results=max_results,
    )

    if not results:
        return (
            f"No matches found in {file_path} for the given search text.\n"
            f"The file has {len(content.splitlines())} lines.\n"
            f"Make sure the search text closely matches the actual code "
            f"(including function names, variable names, etc.)."
        )

    parts = [f"Found {len(results)} match(es) in {file_path}:\n"]

    for idx, match in enumerate(results, 1):
        parts.append(f"--- Match {idx} (lines {match.start_line}-{match.end_line}, "
                      f"similarity: {match.similarity:.0%}) ---")
        if match.context_before:
            for line in match.context_before.splitlines():
                parts.append(f"  {line}")
        for line in match.matched_text.splitlines():
            parts.append(f"> {line}")
        if match.context_after:
            for line in match.context_after.splitlines():
                parts.append(f"  {line}")
        parts.append("")

    return "\n".join(parts)
