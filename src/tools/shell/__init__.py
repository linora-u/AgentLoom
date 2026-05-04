"""Shell tools package — command execution with security, path validation, and sandbox."""

from .shell_tool import shell_tool
from .process import ShellProcessRegistry, ExecResult
from .shell_session import ShellSession
from .background_task import BackgroundTaskRegistry, BackgroundTaskState
from .background_task_tools import (
    check_background_task,
    kill_background_task,
    list_background_tasks,
)

__all__ = [
    "shell_tool",
    "ShellProcessRegistry",
    "ShellSession",
    "ExecResult",
    "BackgroundTaskRegistry",
    "BackgroundTaskState",
    "check_background_task",
    "kill_background_task",
    "list_background_tasks",
]
