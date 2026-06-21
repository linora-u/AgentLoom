"""File operations tools package."""

# -- Core tools (aligned with upstream) ------------------------------------
from .read_file import read_file
from .edit_file import edit_file
from .write_file import write_file

# -- Optional toolsets -----------------------------------------------------
from .file_outliner import get_file_outline
from .directory_browser import list_directory, quick_list_directory
from .path_existence import check_path_exists
from .markdown_writer import (
    write_markdown_file,
    write_markdown_file_raw,
    append_markdown_sections,
)

__all__ = [
    # Core file tools (aligned with upstream)
    "read_file",
    "edit_file",
    "write_file",
    "list_directory",
    # File outline
    "get_file_outline",
    # Directory listing helper
    "quick_list_directory",
    # Path checking
    "check_path_exists",
    # Markdown
    "write_markdown_file",
    "write_markdown_file_raw",
    "append_markdown_sections",
]
