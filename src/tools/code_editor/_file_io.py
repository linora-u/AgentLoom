"""
File I/O utilities for AI Agent code editing.

Provides file reading/writing with:
- Explicit encoding (default utf-8, AI passes the correct encoding)
- Auto-creation of parent directories
- Backup support
- Line ending preservation

Designed for AI Agent automated code editing — no human interaction.
"""

import logging
import shutil
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def read_text(
    path: str,
    encoding: str = "utf-8",
) -> Tuple[str, str]:
    """
    Read file content with the specified encoding.

    Args:
        path: File path.
        encoding: Encoding to use (default: utf-8).
            If the file cannot be decoded with this encoding,
            a UnicodeDecodeError is raised — the caller (AI)
            should retry with the correct encoding.

    Returns:
        Tuple of (content, encoding_used).

    Raises:
        FileNotFoundError: If file does not exist.
        ValueError: If path is not a file.
        UnicodeDecodeError: If file cannot be decoded with the given encoding.
    """
    file_path = Path(path)

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {path}")

    content = file_path.read_text(encoding=encoding)
    return content, encoding


def write_text(
    path: str,
    content: str,
    encoding: str = "utf-8",
    create_parents: bool = True,
) -> None:
    """
    Write content to a file.

    Automatically creates parent directories if they don't exist.
    No confirmation prompts — designed for automated AI Agent use.

    Args:
        path: Target file path.
        content: Content to write.
        encoding: Encoding to use (default: utf-8).
        create_parents: Whether to auto-create parent directories.

    Raises:
        PermissionError: If insufficient permissions.
        OSError: If write fails.
    """
    file_path = Path(path)

    if create_parents:
        file_path.parent.mkdir(parents=True, exist_ok=True)

    file_path.write_text(content, encoding=encoding)


def create_backup(path: str) -> Optional[str]:
    """
    Create a backup copy of a file.

    Args:
        path: File path to back up.

    Returns:
        Backup file path, or None if source doesn't exist.
    """
    file_path = Path(path)
    if not file_path.exists():
        return None

    backup_path = file_path.with_suffix(file_path.suffix + ".bak")

    # If backup already exists, add a number
    counter = 1
    while backup_path.exists():
        backup_path = file_path.with_suffix(f"{file_path.suffix}.bak{counter}")
        counter += 1

    shutil.copy2(file_path, backup_path)
    logger.info(f"Created backup: {backup_path}")
    return str(backup_path)
