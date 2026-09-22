"""Smol-native search tools; loaded individually by the catalog."""

from typing import Any

from agentloom.tools._lazy_exports import install_lazy_exports, resolve_lazy_export

_EXPORTS = {
    "grep_search": (".grep_tool", "grep_search"),
    "glob_search": (".glob_tool", "glob_search"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    return resolve_lazy_export(__name__, globals(), _EXPORTS, name)


install_lazy_exports(__name__)
