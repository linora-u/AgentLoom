"""Self-learning tool exports, loaded one implementation module at a time."""

from typing import Any

from src.tools._lazy_exports import install_lazy_exports, resolve_lazy_export

_EXPORTS = {
    "memory": (".memory_tool", "memory"),
    "session_scroll": (".session_tools", "session_scroll"),
    "session_search": (".session_tools", "session_search"),
    "skill_manage": (".skill_manage_tool", "skill_manage"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    return resolve_lazy_export(__name__, globals(), _EXPORTS, name)


install_lazy_exports(__name__)
