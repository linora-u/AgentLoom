"""Runtime-neutral Rich logging backend."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agentloom.runtime.logging.levels import AgentLoomLogLevel
from agentloom.runtime.trace import capture_explicit_execution_context
from rich.console import Console
from rich.text import Text

TIMESTAMP_STYLE = "#808080"
TASK_ID_STYLE = "#00CED1"
SUBTASK_ID_STYLE = "#9370DB"
AGENT_ID_STYLE = "#FFD700"

_LEVEL_STYLE: dict[AgentLoomLogLevel, str] = {
    AgentLoomLogLevel.DEBUG: "bold cyan",
    AgentLoomLogLevel.INFO: "bold blue",
    AgentLoomLogLevel.WARNING: "bold yellow",
    AgentLoomLogLevel.ERROR: "bold red",
}
_MSG_STYLE: dict[AgentLoomLogLevel, str | None] = {
    AgentLoomLogLevel.DEBUG: None,
    AgentLoomLogLevel.INFO: None,
    AgentLoomLogLevel.WARNING: "bold yellow",
    AgentLoomLogLevel.ERROR: "bold red",
}


class RichLoggerBackend:
    """Run-scoped Rich backend independent of any Agent runtime."""

    def __init__(
        self,
        *,
        level: AgentLoomLogLevel = AgentLoomLogLevel.INFO,
        console: Console | None = None,
        show_timestamp: bool = True,
        timestamp_format: str = "%Y-%m-%d %H:%M:%S",
        show_trace_info: bool = True,
        truncate_id_length: int = 8,
    ) -> None:
        self.level = level
        self.console = console or Console(highlight=False)
        self.show_timestamp = show_timestamp
        self.timestamp_format = timestamp_format
        self.show_trace_info = show_trace_info
        self.truncate_id_length = truncate_id_length

    def _truncate_id(self, id_str: str) -> str:
        if self.truncate_id_length > 0 and len(id_str) > self.truncate_id_length:
            return f"...{id_str[-self.truncate_id_length:]}"
        return id_str

    def _build_prefix(self, level: AgentLoomLogLevel) -> Text:
        prefix = Text()
        if self.show_timestamp:
            prefix.append(
                f"[{datetime.now().strftime(self.timestamp_format)}]",
                style=TIMESTAMP_STYLE,
            )
        if self.show_trace_info:
            execution = capture_explicit_execution_context()
            if execution.task_id:
                prefix.append(
                    f"[task:{self._truncate_id(execution.task_id)}]",
                    style=TASK_ID_STYLE,
                )
            if execution.sub_task_id:
                prefix.append(
                    f"[subtask:{self._truncate_id(execution.sub_task_id)}]",
                    style=SUBTASK_ID_STYLE,
                )
            if execution.agent_name:
                prefix.append(
                    f"[agent:{execution.agent_name}]",
                    style=AGENT_ID_STYLE,
                )
        prefix.append(
            f"[{level.name}] ",
            style=_LEVEL_STYLE.get(level, "bold blue"),
        )
        return prefix

    @staticmethod
    def _level(value: AgentLoomLogLevel | str | int) -> AgentLoomLogLevel:
        if isinstance(value, AgentLoomLogLevel):
            return value
        if isinstance(value, str):
            try:
                return AgentLoomLogLevel.from_str(value)
            except ValueError:
                return AgentLoomLogLevel.INFO
        return AgentLoomLogLevel.from_int(value)

    def log(
        self,
        *args: Any,
        level: AgentLoomLogLevel | str | int = AgentLoomLogLevel.INFO,
        **kwargs: Any,
    ) -> None:
        resolved = self._level(level)
        if resolved < self.level or not args:
            return
        prefix = self._build_prefix(resolved)
        first_arg = args[0]
        if isinstance(first_arg, str):
            style = _MSG_STYLE.get(resolved)
            if style and "style" not in kwargs:
                kwargs = dict(kwargs, style=style)
            self.console.print(prefix, end="")
            self.console.print(*args, **kwargs)
        elif isinstance(first_arg, Text):
            self.console.print(prefix + first_arg, *args[1:], **kwargs)
        else:
            self.console.print(prefix)
            self.console.print(*args, **kwargs)

    def debug(self, msg: Any, **kwargs: Any) -> None:
        self.log(msg, level=AgentLoomLogLevel.DEBUG, **kwargs)

    def info(self, msg: Any, **kwargs: Any) -> None:
        self.log(msg, level=AgentLoomLogLevel.INFO, **kwargs)

    def warning(self, msg: Any, **kwargs: Any) -> None:
        self.log(msg, level=AgentLoomLogLevel.WARNING, **kwargs)

    def error(self, msg: Any, **kwargs: Any) -> None:
        self.log(msg, level=AgentLoomLogLevel.ERROR, **kwargs)
