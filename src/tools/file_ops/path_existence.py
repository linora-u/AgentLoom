from src.lib.logging import get_logger

logger = get_logger(__name__)
from pathlib import Path


def check_path_exists(
    path_str: str,
    must_be_file: bool = True,
    must_be_directory: bool = False,
    follow_symlinks: bool = True,
    raise_if_not_exists: bool = False
) -> bool:
    """
    Check whether a path exists and optionally validate its type (file or directory).

    This utility verifies that the given path exists on the filesystem and, if requested,
    confirms whether it is a regular file or a directory. It can also be configured to
    treat symbolic links as mismatches when not following symlinks.

    Args:
        path_str: The path to check (relative or absolute)
        must_be_file: Require the path to be a regular file (default: True)
        must_be_directory: Require the path to be a directory (default: False)
        follow_symlinks: Whether to follow symbolic links when determining type (default: True).
                         If False and the path is a symlink, it will be treated as a mismatch.
        raise_if_not_exists: If True, raise FileNotFoundError when the path does not exist
                             or when it exists but does not match the required type (default: False)

    Returns:
        bool: True if the path exists (and matches the required type, if specified); otherwise False

    Raises:
        ValueError: If path_str is empty/whitespace or if both must_be_file and must_be_directory are True
        FileNotFoundError: If raise_if_not_exists is True and the path does not exist or type mismatches
        PermissionError: If a permission issue occurs while accessing the path metadata
        OSError: For other filesystem-related errors

    Examples:
        >>> check_path_exists("README.md")
        True
        >>> check_path_exists("data", must_be_directory=True)
        True
        >>> check_path_exists("some/unknown.file")
        False
        >>> check_path_exists("link_to_file", must_be_file=True, follow_symlinks=False)
        False
        >>> check_path_exists("config/settings.json", must_be_file=True, raise_if_not_exists=True)
        Traceback (most recent call last):
            ...
        FileNotFoundError: Path 'config/settings.json' does not exist.

    Notes:
        - On most platforms, Path.is_file()/is_dir() follow symlinks. Set follow_symlinks=False
          to treat symlinks as mismatches for type checks.
    """
    if not path_str:
        raise ValueError("path_str is required and cannot be empty")

    if not path_str.strip():
        raise ValueError("path_str cannot be just whitespace")

    if must_be_file and must_be_directory:
        raise ValueError("must_be_file and must_be_directory cannot both be True")

    path = Path(path_str)

    try:
        # Step 1: Existence check
        exists = path.exists()
        if not exists:
            msg = f"Path '{path_str}' does not exist."
            if raise_if_not_exists:
                logger.error(msg)
                raise FileNotFoundError(msg)
            logger.info(msg)
            return False

        # Step 2: Type validation (if required)
        if must_be_file or must_be_directory:
            # Handle symlink behavior
            if not follow_symlinks and path.is_symlink():
                mismatch_msg = (
                    f"Path '{path_str}' is a symbolic link; follow_symlinks=False treats it as a mismatch."
                )
                if raise_if_not_exists:
                    logger.error(mismatch_msg)
                    raise FileNotFoundError(mismatch_msg)
                logger.warning(mismatch_msg)
                return False

            if must_be_file:
                is_match = path.is_file()
                type_desc = "a regular file"
            else:
                is_match = path.is_dir()
                type_desc = "a directory"

            if not is_match:
                mismatch_msg = f"Path '{path_str}' exists but is not {type_desc}."
                if raise_if_not_exists:
                    logger.error(mismatch_msg)
                    raise FileNotFoundError(mismatch_msg)
                logger.warning(mismatch_msg)
                return False

        # Success
        logger.info(
            f"Path '{path_str}' exists"
            f"{' and is a file' if must_be_file else ''}"
            f"{' and is a directory' if must_be_directory else ''}."
        )
        return True

    except PermissionError as e:
        error_msg = f"Permission denied when accessing path '{path_str}': {e}"
        logger.error(error_msg)
        raise PermissionError(error_msg) from e
    except OSError as e:
        error_msg = f"Filesystem error when checking path '{path_str}': {e}"
        logger.error(error_msg)
        raise OSError(error_msg) from e
