"""Shell tool compatibility exports, loaded one implementation module at a time."""

from typing import Any

from src.tools._lazy_exports import install_lazy_exports, resolve_lazy_export

_EXPORTS = {
    "shell_tool": (".shell_tool", "shell_tool"),
    "ShellProcessRegistry": (".process", "ShellProcessRegistry"),
    "ExecResult": (".process", "ExecResult"),
    "ShellSession": (".shell_session", "ShellSession"),
    "BackgroundTaskRegistry": (".background_task", "BackgroundTaskRegistry"),
    "BackgroundTaskState": (".background_task", "BackgroundTaskState"),
    "check_background_task": (".background_task_tools", "check_background_task"),
    "kill_background_task": (".background_task_tools", "kill_background_task"),
    "list_background_tasks": (".background_task_tools", "list_background_tasks"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    return resolve_lazy_export(__name__, globals(), _EXPORTS, name)


install_lazy_exports(__name__)
