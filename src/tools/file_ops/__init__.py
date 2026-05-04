"""File operations tools package."""

# -- Core tools (aligned with upstream) ------------------------------------
from .read_file import read_file
from .edit_file import edit_file
from .write_file import write_file

# -- Kept tools ------------------------------------------------------------
from .file_outliner import get_file_outline
from .directory_browser import browse_directory, quick_browse_directory
from .path_existence import check_path_exists
from .markdown_writer import (
    write_markdown_file,
    write_markdown_file_raw,
    append_markdown_sections,
)
from .file_manager import delete_file, move_file, rename_file, copy_file
from .file_searcher import search_files

__all__ = [
    # Core file tools (aligned with upstream)
    "read_file",
    "edit_file",
    "write_file",
    # File management (delete / move / rename / copy)
    "delete_file",
    "move_file",
    "rename_file",
    "copy_file",
    # File search
    "search_files",
    # File outline
    "get_file_outline",
    # Directory browsing
    "browse_directory",
    "quick_browse_directory",
    # Path checking
    "check_path_exists",
    # Markdown
    "write_markdown_file",
    "write_markdown_file_raw",
    "append_markdown_sections",
]
