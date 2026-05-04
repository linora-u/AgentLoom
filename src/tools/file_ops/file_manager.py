"""
File management operations for AI Agents.

Provides delete / move / rename / copy operations for files and directories.
All tools are designed for fully automated AI Agent use — no confirmation
prompts required.
"""

from src.lib.logging import get_logger
import shutil
from pathlib import Path

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

def delete_file(
    file_path: str,
    force: bool = False,
    missing_ok: bool = False,
) -> str:
    """
    Delete a file or directory from the filesystem.

    Args:
        file_path: Path to the file or directory to delete.
        force: If True, allow deleting directories recursively
            (uses ``shutil.rmtree``).  Default False — raises
            ``IsADirectoryError`` when the path is a directory.
        missing_ok: If True, silently return when the target does not
            exist instead of raising ``FileNotFoundError``.

    Returns:
        Descriptive result string.

    Raises:
        ValueError: If *file_path* is empty.
        FileNotFoundError: If the path does not exist and *missing_ok* is False.
        IsADirectoryError: If the path is a directory and *force* is False.
        PermissionError: If insufficient permissions.
    """
    if not file_path or not file_path.strip():
        raise ValueError("file_path cannot be empty")

    path = Path(file_path)

    if not path.exists():
        if missing_ok:
            return f"Path does not exist (skipped): {file_path}"
        raise FileNotFoundError(f"File not found: {file_path}")

    if path.is_dir():
        if not force:
            raise IsADirectoryError(
                f"Path is a directory: {file_path}. "
                f"Use force=True to delete directories recursively."
            )
        shutil.rmtree(path)
        logger.info("Deleted directory (recursive): %s", file_path)
        return f"Deleted directory (recursive): {file_path}"

    path.unlink()
    logger.info("Deleted file: %s", file_path)
    return f"Deleted file: {file_path}"


# ---------------------------------------------------------------------------
# move
# ---------------------------------------------------------------------------

def move_file(
    source: str,
    destination: str,
    overwrite: bool = False,
) -> str:
    """
    Move a file or directory to a new location.

    Parent directories of *destination* are created automatically.

    Args:
        source: Path to the source file or directory.
        destination: Target path.
        overwrite: If True, overwrite existing target. Default False.

    Returns:
        Descriptive result string.

    Raises:
        ValueError: If paths are empty.
        FileNotFoundError: If *source* does not exist.
        FileExistsError: If *destination* exists and *overwrite* is False.
        PermissionError: If insufficient permissions.
    """
    if not source or not source.strip():
        raise ValueError("source cannot be empty")
    if not destination or not destination.strip():
        raise ValueError("destination cannot be empty")

    src = Path(source)
    dst = Path(destination)

    if not src.exists():
        raise FileNotFoundError(f"Source not found: {source}")

    if dst.exists() and not overwrite:
        raise FileExistsError(
            f"Destination already exists: {destination}. "
            f"Use overwrite=True to replace."
        )

    # Auto-create parent directories
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Remove existing destination if overwriting
    if dst.exists() and overwrite:
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()

    shutil.move(str(src), str(dst))
    logger.info("Moved %s -> %s", source, destination)
    return f"Moved '{source}' -> '{destination}'"


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------

def rename_file(
    file_path: str,
    new_name: str,
    overwrite: bool = False,
) -> str:
    """
    Rename a file or directory (keeps it in the same parent directory).

    This is a convenience wrapper around :func:`move_file`.

    Args:
        file_path: Path to the file or directory to rename.
        new_name: The new name (basename only, no path separators).
        overwrite: If True, overwrite if the new name already exists.

    Returns:
        Descriptive result string.

    Raises:
        ValueError: If paths/names are empty or *new_name* contains separators.
        FileNotFoundError: If *file_path* does not exist.
        FileExistsError: If the new name exists and *overwrite* is False.
    """
    if not file_path or not file_path.strip():
        raise ValueError("file_path cannot be empty")
    if not new_name or not new_name.strip():
        raise ValueError("new_name cannot be empty")
    if "/" in new_name or "\\" in new_name:
        raise ValueError(
            f"new_name must be a basename without path separators, got: '{new_name}'. "
            f"Use move_file() for moving to a different directory."
        )

    src = Path(file_path)
    dst = src.parent / new_name

    return move_file(str(src), str(dst), overwrite=overwrite)


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------

def copy_file(
    source: str,
    destination: str,
    overwrite: bool = False,
) -> str:
    """
    Copy a file or directory to a new location.

    Parent directories of *destination* are created automatically.
    For directories, uses ``shutil.copytree``.

    Args:
        source: Path to the source file or directory.
        destination: Target path.
        overwrite: If True, overwrite existing target. Default False.

    Returns:
        Descriptive result string.

    Raises:
        ValueError: If paths are empty.
        FileNotFoundError: If *source* does not exist.
        FileExistsError: If *destination* exists and *overwrite* is False.
        PermissionError: If insufficient permissions.
    """
    if not source or not source.strip():
        raise ValueError("source cannot be empty")
    if not destination or not destination.strip():
        raise ValueError("destination cannot be empty")

    src = Path(source)
    dst = Path(destination)

    if not src.exists():
        raise FileNotFoundError(f"Source not found: {source}")

    if dst.exists() and not overwrite:
        raise FileExistsError(
            f"Destination already exists: {destination}. "
            f"Use overwrite=True to replace."
        )

    # Auto-create parent directories
    dst.parent.mkdir(parents=True, exist_ok=True)

    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(str(src), str(dst))
        logger.info("Copied directory %s -> %s", source, destination)
        return f"Copied directory '{source}' -> '{destination}'"
    else:
        shutil.copy2(str(src), str(dst))
        logger.info("Copied file %s -> %s", source, destination)
        return f"Copied file '{source}' -> '{destination}'"
