"""File tool compatibility exports, loaded one implementation module at a time."""

from typing import Any

from src.tools._lazy_exports import install_lazy_exports, resolve_lazy_export

_EXPORTS = {
    "read_file": (".read_file", "read_file"),
    "edit_file": (".edit_file", "edit_file"),
    "write_file": (".write_file", "write_file"),
    "list_directory": (".directory_browser", "list_directory"),
    "quick_list_directory": (".directory_browser", "quick_list_directory"),
    "get_file_outline": (".file_outliner", "get_file_outline"),
    "check_path_exists": (".path_existence", "check_path_exists"),
    "write_markdown_file": (".markdown_writer", "write_markdown_file"),
    "write_markdown_file_raw": (".markdown_writer", "write_markdown_file_raw"),
    "append_markdown_sections": (".markdown_writer", "append_markdown_sections"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    return resolve_lazy_export(__name__, globals(), _EXPORTS, name)


install_lazy_exports(__name__)
