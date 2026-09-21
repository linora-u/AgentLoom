"""AgentLoom-owned file helper tools."""

from typing import Any

from agentloom.tools._lazy_exports import install_lazy_exports, resolve_lazy_export

_EXPORTS = {
    "get_file_outline": (".file_outliner", "get_file_outline"),
    "write_markdown_file": (".markdown_writer", "write_markdown_file"),
    "write_markdown_file_raw": (".markdown_writer", "write_markdown_file_raw"),
    "append_markdown_sections": (".markdown_writer", "append_markdown_sections"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    return resolve_lazy_export(__name__, globals(), _EXPORTS, name)


install_lazy_exports(__name__)
