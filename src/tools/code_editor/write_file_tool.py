"""
Whole-file write tool for AI Agents.

Provides file creation and full overwrite capabilities.
Designed for fully automated AI Agent use — no confirmation prompts.
"""

import logging
from pathlib import Path

from ._file_io import create_backup as _create_backup
from ._file_io import read_text, write_text

logger = logging.getLogger(__name__)


def write_whole_file(
    file_path: str,
    content: str,
    encoding: str = "utf-8",
    create_backup: bool = False,
) -> str:
    """
    Create a new file or overwrite an existing file with the given content.

    This tool writes the complete content to a file. Use it when:
    - Creating a brand new file
    - Replacing the entire content of an existing file
    - The changes are too extensive for SEARCH/REPLACE blocks

    For smaller, targeted edits, prefer ``search_and_replace`` instead.

    Args:
        file_path: Path to the file to create or overwrite.
        content: The complete file content to write.
        encoding: File encoding (default: utf-8).
        create_backup: Whether to create a .bak backup of the existing file
            before overwriting. Defaults to False.

    Returns:
        A description of the result, e.g.
        "Created new file: src/utils.py (42 lines)" or
        "Overwrote file: src/utils.py (42 lines, was 35 lines)".

    Raises:
        ValueError: If file_path is empty.
        PermissionError: If insufficient permissions.
    """
    if not file_path or not file_path.strip():
        raise ValueError("file_path cannot be empty")

    path = Path(file_path)
    new_line_count = len(content.splitlines()) if content else 0

    if path.exists():
        # Existing file — detect encoding, optionally backup, then overwrite
        detected_enc = encoding

        old_line_count = len(path.read_text(encoding=detected_enc).splitlines())

        if create_backup:
            _create_backup(file_path)

        write_text(file_path, content, encoding=detected_enc)
        logger.info(f"Overwrote file: {file_path}")
        return (
            f"Overwrote file: {file_path} "
            f"({new_line_count} lines, was {old_line_count} lines)"
        )
    else:
        # New file — create with parent directories
        write_text(file_path, content, encoding=encoding)
        logger.info(f"Created new file: {file_path}")
        return f"Created new file: {file_path} ({new_line_count} lines)"
