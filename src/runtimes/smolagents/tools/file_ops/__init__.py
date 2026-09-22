"""Smol-native file ops tools; loaded individually by the catalog."""

from typing import Any

from agentloom.tools._lazy_exports import install_lazy_exports, resolve_lazy_export

_EXPORTS = {
    "read_file": (".read_file", "read_file"),
    "edit_file": (".edit_file", "edit_file"),
    "write_file": (".write_file", "write_file"),
    "list_directory": (".directory_browser", "list_directory"),
    "quick_list_directory": (".directory_browser", "quick_list_directory"),
    "check_path_exists": (".path_existence", "check_path_exists"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    return resolve_lazy_export(__name__, globals(), _EXPORTS, name)


install_lazy_exports(__name__)
