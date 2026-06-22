from src.lib.logging import get_logger

logger = get_logger(__name__)
import time
from pathlib import Path
from typing import List, Tuple, Dict


# Default exclusion patterns.
DEFAULT_EXCLUDE_PATTERNS = [
    # Dependency directories
    "node_modules/", "site-packages/", ".venv/", "venv/", "env/",
    # Version control
    ".git/", ".svn/", ".hg/",
    # Cache directories
    "__pycache__/", ".pytest_cache/", ".mypy_cache/", ".tox/",
    # Build directories
    "target/", "build/", "dist/", "out/", "bin/", "obj/",
    # IDE directories
    ".vscode/", ".idea/", ".vs/",
    # Compiled files
    "*.pyc", "*.pyo", "*.pyd", "*.so", "*.dll", "*.dylib",
    "*.class", "*.jar", "*.war", "*.ear",
    # System files
    ".DS_Store", "Thumbs.db", "desktop.ini",
    # Log files
    "*.log", "*.log.*",
    # Large data files
    "*.db", "*.sqlite", "*.sqlite3",
]

# Important file patterns (shown first).
IMPORTANT_FILE_PATTERNS = [
    "README*", "readme*", "CHANGELOG*", "LICENSE*", "CONTRIBUTING*",
    "package.json", "requirements.txt", "Pipfile", "pyproject.toml",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "Makefile",
    "Dockerfile", "docker-compose.yml", ".gitignore"
]


def should_exclude_path(path: Path, exclude_patterns: List[str]) -> bool:
    """
    Check whether a path should be excluded.

    Args:
        path: Path to check.
        exclude_patterns: Exclusion pattern list.

    Returns:
        bool: Returns True if the path should be excluded.
    """
    import os
    import fnmatch

    path_str = str(path)
    name = path.name

    for pattern in exclude_patterns:
        # Directory pattern (ending with "/")
        if pattern.endswith('/'):
            pattern_name = pattern.rstrip('/')
            if name == pattern_name:
                return True
            # Cross-platform path check.
            if path_str.endswith(os.sep + pattern_name) or path_str.endswith('/' + pattern_name):
                return True
        # File pattern (wildcard)
        elif '*' in pattern:
            if fnmatch.fnmatch(name, pattern):
                return True
        # Exact match
        else:
            if name == pattern:
                return True

    return False


def is_important_file(path: Path) -> bool:
    """
    Check whether a file is important.

    Args:
        path: File path.

    Returns:
        bool: Returns True if it is an important file.
    """
    import fnmatch
    name = path.name

    for pattern in IMPORTANT_FILE_PATTERNS:
        if fnmatch.fnmatch(name, pattern):
            return True

    return False


def count_items_with_timeout(directory: Path, timeout_seconds: float = 2.0) -> str:
    """
    Count items in a directory within a timeout limit.

    Args:
        directory: Directory path.
        timeout_seconds: Timeout in seconds.

    Returns:
        str: Item-count string; returns "?" on timeout.
    """
    start_time = time.time()

    try:
        file_count = 0
        dir_count = 0

        for item in directory.iterdir():
            if time.time() - start_time > timeout_seconds:
                return "?"

            if item.is_file():
                file_count += 1
            elif item.is_dir():
                dir_count += 1

        if file_count == 0 and dir_count == 0:
            return "empty"
        elif file_count == 0:
            return f"{dir_count}d"
        elif dir_count == 0:
            return f"{file_count}f"
        else:
            return f"{file_count}f,{dir_count}d"

    except (PermissionError, OSError):
        return "?"


def get_file_size_category(size_bytes: int) -> str:
    """
    Get file-size category.

    Args:
        size_bytes: File size (bytes).

    Returns:
        str: Size category label.
    """
    if size_bytes < 1024:
        return ""  # Do not show for small files.
    elif size_bytes < 1024 * 1024:
        return f"({size_bytes // 1024}K)"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"({size_bytes // (1024 * 1024)}M)"
    else:
        return f"({size_bytes // (1024 * 1024 * 1024)}G)"


def get_file_type_prefix(path: Path) -> str:
    """
    Get a simple prefix by file type.

    Args:
        path: File path.

    Returns:
        str: File type prefix.
    """
    if path.is_dir():
        return "[DIR]"
    else:
        return "[FILE]"


def collect_directory_items(
    directory: Path,
    max_depth: int,
    exclude_patterns: List[str],
    include_hidden: bool = False
) -> List[Tuple[Path, str]]:
    """
    Collect items in a directory and flatten all files/directories.

    Args:
        directory: Directory path.
        max_depth: Maximum depth.
        exclude_patterns: Exclusion patterns.
        include_hidden: Whether to include hidden files.

    Returns:
        List: Item list, each item is (absolute_path, relative_path).
    """
    all_items = []

    def collect_items(current_dir: Path, current_depth: int):
        if current_depth > max_depth:
            return

        try:
            # Get all items in current directory.
            dir_items = list(current_dir.iterdir())

            # Filter items.
            filtered_items = []
            for item in dir_items:
                # Skip hidden files unless explicitly requested.
                if not include_hidden and item.name.startswith('.'):
                    continue

                # Skip excluded items.
                if should_exclude_path(item, exclude_patterns):
                    continue

                filtered_items.append(item)

            # Classify and sort.
            directories = [item for item in filtered_items if item.is_dir()]
            files = [item for item in filtered_items if item.is_file()]

            # Show important files first.
            important_files = [f for f in files if is_important_file(f)]
            regular_files = [f for f in files if not is_important_file(f)]

            # Sort.
            directories.sort(key=lambda x: x.name.lower())
            important_files.sort(key=lambda x: x.name.lower())
            regular_files.sort(key=lambda x: x.name.lower())

            # Add to result.
            for item in directories + important_files + regular_files:
                if current_depth == 0:
                    relative_path = item.name
                else:
                    relative_path = str(item.relative_to(directory))
                all_items.append((item, relative_path))

            # Recursively process subdirectories.
            if current_depth < max_depth:
                for subdir in directories:
                    collect_items(subdir, current_depth + 1)

        except (PermissionError, OSError) as e:
            logger.warning(f"Cannot access directory {current_dir}: {e}")

    collect_items(directory, 0)
    return all_items


def format_directory_tree(
    items: List[Tuple[Path, str]],
    max_output_lines: int,
    show_file_counts: bool,
    show_file_info: bool,
    count_timeout: float
) -> Tuple[List[str], bool, Dict[str, int]]:
    """
    Format directory tree output.

    Args:
        items: Item list.
        max_output_lines: Maximum output lines.
        show_file_counts: Whether to display file counts.
        show_file_info: Whether to display file info.
        count_timeout: Count timeout.

    Returns:
        Tuple[List[str], bool, Dict[str, int]]: (output_lines, truncated, stats)
    """
    output_lines = []
    truncated = False
    stats = {"total_files": 0, "total_dirs": 0, "displayed_items": 0}

    # Reserve lines for hint information.
    max_content_lines = max_output_lines - 15

    for item_path, relative_path in items:
        if len(output_lines) >= max_content_lines:
            truncated = True
            break

        # Get type prefix.
        type_prefix = get_file_type_prefix(item_path)

        # Use relative path as display name.
        name = relative_path
        if item_path.is_dir():
            name += "/"
            stats["total_dirs"] += 1
        else:
            stats["total_files"] += 1

        # Append file-count information.
        count_info = ""
        if show_file_counts and item_path.is_dir():
            count = count_items_with_timeout(item_path, count_timeout)
            if count != "empty":
                count_info = f" ({count})"

        # Append file-size information.
        size_info = ""
        if show_file_info and item_path.is_file():
            try:
                size = item_path.stat().st_size
                size_info = get_file_size_category(size)
                if size_info:
                    size_info = f" {size_info}"
            except (OSError, PermissionError):
                pass

        # Compose output line.
        line = f"{type_prefix} {name}{count_info}{size_info}"
        output_lines.append(line)
        stats["displayed_items"] += 1

    return output_lines, truncated, stats


def list_directory(
    directory_path: str,
    max_depth: int = 2,
    max_output_lines: int = 150,
    show_file_counts: bool = False,
    show_file_info: bool = True,
    include_hidden: bool = False,
    exclude_patterns: List[str] = [],
    count_timeout_seconds: float = 2.0
) -> str:
    """
    List directory structure for repository exploration.

    Provides smart directory browsing functionality, especially suitable for understanding
    large code repository structures. Automatically excludes common non-user code directories
    (like node_modules, __pycache__, etc.) and provides helpful suggestions when output
    exceeds limits.

    Args:
        directory_path: Directory path to browse (relative or absolute path)
        max_depth: Maximum display depth, 1=current level only, 2=show subdirectory contents (default: 2, min: 1)
        max_output_lines: Maximum output lines, truncates and provides suggestions when exceeded (default: 150)
        show_file_counts: Whether to display file count statistics in directories (default: False, may be slow)
        show_file_info: Whether to display file size and other info (default: True)
        include_hidden: Whether to include hidden files and directories (default: False)
        exclude_patterns: Custom exclusion pattern list, merged with default patterns (default: empty list)
        count_timeout_seconds: Timeout for file counting to avoid hanging on large directories (default: 2.0)

    Returns:
        str: Formatted directory tree structure with file type icons and statistics

    Raises:
        ValueError: If parameters are invalid
        FileNotFoundError: If directory does not exist
        PermissionError: If access permissions are insufficient
        OSError: If directory access fails

    Examples:
        >>> list_directory(".")  # List current directory
        >>> list_directory("/path/to/project", max_depth=1)  # Show first level only
        >>> list_directory("src", show_file_counts=True)  # Show file counts
        >>> list_directory(".", exclude_patterns=["*.tmp", "cache/"])  # Custom exclusions
    """
    # Validate arguments.
    if not directory_path or not directory_path.strip():
        raise ValueError("directory_path cannot be empty")

    if max_depth < 1:
        raise ValueError("max_depth must be >= 1")

    if max_output_lines < 10:
        raise ValueError("max_output_lines must be >= 10")

    if count_timeout_seconds <= 0:
        raise ValueError("count_timeout_seconds must be > 0")

    # Convert to Path object.
    directory = Path(directory_path)

    # Check whether directory exists.
    if not directory.exists():
        raise FileNotFoundError(f"Directory '{directory_path}' does not exist")

    if not directory.is_dir():
        raise ValueError(f"Path '{directory_path}' is not a directory")

    try:
        # Merge exclusion patterns.
        all_exclude_patterns = DEFAULT_EXCLUDE_PATTERNS.copy()
        if exclude_patterns:
            all_exclude_patterns.extend(exclude_patterns)

        # Collect directory items.
        items = collect_directory_items(
            directory, max_depth, all_exclude_patterns, include_hidden
        )

        # Format output.
        output_lines, truncated, stats = format_directory_tree(
            items, max_output_lines,
            show_file_counts, show_file_info, count_timeout_seconds
        )

        # Build final output.
        result_lines = []

        # Add title.
        result_lines.append(f"Directory: {directory.resolve()}")
        result_lines.append("=" * 60)

        # Add directory tree.
        result_lines.extend(output_lines)

        # Add statistics.
        result_lines.append("")
        result_lines.append("=" * 60)
        result_lines.append(f"Summary: {stats['displayed_items']} items displayed "
                          f"({stats['total_files']} files, {stats['total_dirs']} directories)")

        # Add truncation tips.
        if truncated:
            result_lines.append("")
            result_lines.append("Output truncated - Suggestions:")
            result_lines.append(f"   • Reduce depth: list_directory('{directory_path}', max_depth=1)")
            result_lines.append(f"   • Browse subdirectory: list_directory('{directory_path}/subdir_name')")
            result_lines.append(f"   • Increase line limit: list_directory('{directory_path}', max_output_lines=300)")
            if not show_file_counts:
                result_lines.append(f"   • Show file counts: list_directory('{directory_path}', show_file_counts=True)")

        # Log operation.
        logger.info(f"Listing directory: {directory} (depth={max_depth}, displayed={stats['displayed_items']} items, truncated={truncated})")

        return '\n'.join(result_lines)

    except PermissionError as e:
        error_msg = f"Insufficient permission to access directory '{directory_path}': {e}"
        logger.error(error_msg)
        raise PermissionError(error_msg) from e

    except OSError as e:
        error_msg = f"Failed to access directory '{directory_path}': {e}"
        logger.error(error_msg)
        raise OSError(error_msg) from e


def quick_list_directory(
    directory_path: str,
    show_only_dirs: bool = False,
    max_items: int = 50
) -> str:
    """
    Quickly list first-level directory content for fast structure overview.

    This is a simplified version of `list_directory`, showing only the first
    level for faster response. It is especially useful for large projects.

    Args:
        directory_path: Directory path to browse.
        show_only_dirs: Whether to show directories only, ignoring files (default: False).
        max_items: Maximum number of items to display (default: 50).

    Returns:
        str: Simplified directory listing.

    Examples:
        >>> quick_list_directory(".")  # Quick view of current directory
        >>> quick_list_directory("/path/to/project", show_only_dirs=True)  # Directories only
    """
    # Validate arguments.
    if not directory_path or not directory_path.strip():
        raise ValueError("directory_path cannot be empty")

    if max_items < 1:
        raise ValueError("max_items must be >= 1")

    # Convert to Path object.
    directory = Path(directory_path)

    # Check whether directory exists.
    if not directory.exists():
        raise FileNotFoundError(f"Directory '{directory_path}' does not exist")

    if not directory.is_dir():
        raise ValueError(f"Path '{directory_path}' is not a directory")

    try:
        # Get directory contents.
        all_items = list(directory.iterdir())

        # Filter and classify.
        directories = []
        files = []

        for item in all_items:
            # Skip hidden files.
            if item.name.startswith('.'):
                continue

            # Skip excluded items.
            if should_exclude_path(item, DEFAULT_EXCLUDE_PATTERNS):
                continue

            if item.is_dir():
                directories.append(item)
            elif not show_only_dirs:
                files.append(item)

        # Sort.
        directories.sort(key=lambda x: x.name.lower())
        files.sort(key=lambda x: x.name.lower())

        # Important files first.
        important_files = [f for f in files if is_important_file(f)]
        regular_files = [f for f in files if not is_important_file(f)]

        # Combine results.
        all_display_items = directories + important_files + regular_files

        # Limit item count.
        display_items = all_display_items[:max_items]
        truncated = len(all_display_items) > max_items

        # Build output.
        result_lines = []
        result_lines.append(f"Directory: {directory.resolve()}")
        result_lines.append("-" * 40)

        if not display_items:
            result_lines.append("(Empty directory or all items filtered)")
        else:
            for item in display_items:
                type_prefix = get_file_type_prefix(item)
                name = item.name
                if item.is_dir():
                    name += "/"
                result_lines.append(f"{type_prefix} {name}")

        # Add statistics.
        total_dirs = len(directories)
        total_files = len(files)
        result_lines.append("-" * 40)
        result_lines.append(f"Summary: {len(display_items)} items displayed")
        if show_only_dirs:
            result_lines.append(f"   {total_dirs} directories")
        else:
            result_lines.append(f"   {total_dirs} directories, {total_files} files")

        if truncated:
            hidden_count = len(all_display_items) - max_items
            result_lines.append(f"   (+{hidden_count} items not shown)")
            result_lines.append(f"Tip: Use list_directory('{directory_path}') for full structure")

        return '\n'.join(result_lines)

    except PermissionError as e:
        error_msg = f"Insufficient permission to access directory '{directory_path}': {e}"
        logger.error(error_msg)
        raise PermissionError(error_msg) from e

    except OSError as e:
        error_msg = f"Failed to access directory '{directory_path}': {e}"
        logger.error(error_msg)
        raise OSError(error_msg) from e
