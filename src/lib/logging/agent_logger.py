#!/usr/bin/env python
# coding=utf-8

from datetime import datetime
from enum import IntEnum
from rich.console import Console
from rich.text import Text
from smolagents import AgentLogger
from smolagents import LogLevel as SmolaLogLevel

from src.trace import (
    get_current_task_id,
    get_current_sub_task_id,
    get_current_agent_name,
)


class AgentLoomLogLevel(IntEnum):
    """Standard log levels for AgentLoom.

    Filtering rule: a message is emitted when ``msg_level >= logger.level``.
    Higher numeric value means more severe (less verbose).

    Mapping to smolagents.LogLevel (used by smolagents internals):
        smolagents DEBUG(2)  -> AgentLoomLogLevel.DEBUG(10)
        smolagents INFO(1)   -> AgentLoomLogLevel.INFO(20)
        smolagents ERROR(0)  -> AgentLoomLogLevel.ERROR(40)
        smolagents OFF(-1)   -> AgentLoomLogLevel.OFF(50)
    """
    DEBUG   = 10
    INFO    = 20
    WARNING = 30
    ERROR   = 40
    OFF     = 50

    @classmethod
    def from_str(cls, value: str) -> "AgentLoomLogLevel":
        """Parse a string level name into AgentLoomLogLevel (case-insensitive)."""
        normalized = value.strip().upper()
        _MAP = {
            "DEBUG":    cls.DEBUG,
            "INFO":     cls.INFO,
            "WARNING":  cls.WARNING,
            "WARN":     cls.WARNING,
            "ERROR":    cls.ERROR,
            "CRITICAL": cls.ERROR,
            "OFF":      cls.OFF,
            "DISABLE":  cls.OFF,
            "DISABLED": cls.OFF,
        }
        if normalized not in _MAP:
            raise ValueError(f"Unknown log level: {value!r}")
        return _MAP[normalized]

    @classmethod
    def from_int(cls, value: int) -> "AgentLoomLogLevel":
        """Convert a stdlib logging int level to AgentLoomLogLevel."""
        import logging as _logging
        if value >= _logging.CRITICAL:   # 50
            return cls.OFF
        if value >= _logging.ERROR:      # 40
            return cls.ERROR
        if value >= _logging.WARNING:    # 30
            return cls.WARNING
        if value >= _logging.INFO:       # 20
            return cls.INFO
        return cls.DEBUG

    @classmethod
    def from_smola(cls, smola_level: "SmolaLogLevel | int") -> "AgentLoomLogLevel":
        """Convert a smolagents LogLevel to AgentLoomLogLevel.

        smolagents uses reverse numeric order (higher = more verbose):
            OFF=-1, ERROR=0, INFO=1, DEBUG=2
        We map to standard order so our >= filter works correctly.
        """
        # Treat raw int or SmolaLogLevel enum the same way.
        v = int(smola_level)
        if v <= int(SmolaLogLevel.OFF):      # -1
            return cls.OFF
        if v <= int(SmolaLogLevel.ERROR):    # 0
            return cls.ERROR
        if v <= int(SmolaLogLevel.INFO):     # 1
            return cls.INFO
        # DEBUG = 2 or higher
        return cls.DEBUG


TIMESTAMP_STYLE  = "#808080"  # Gray
TASK_ID_STYLE    = "#00CED1"  # Dark cyan
SUBTASK_ID_STYLE = "#9370DB"  # Medium purple
AGENT_ID_STYLE   = "#FFD700"  # Gold

_LEVEL_STYLE: dict[AgentLoomLogLevel, str] = {
    AgentLoomLogLevel.DEBUG:   "bold cyan",
    AgentLoomLogLevel.INFO:    "bold blue",
    AgentLoomLogLevel.WARNING: "bold yellow",
    AgentLoomLogLevel.ERROR:   "bold red",
}
_MSG_STYLE: dict[AgentLoomLogLevel, str | None] = {
    AgentLoomLogLevel.DEBUG:   None,
    AgentLoomLogLevel.INFO:    None,
    AgentLoomLogLevel.WARNING: "bold yellow",
    AgentLoomLogLevel.ERROR:   "bold red",
}


class EnhancedAgentLogger(AgentLogger):
    """Enhanced logger with standard level filtering and dual console output.

    Inherits ``AgentLogger`` so smolagents internals (log_rule, log_code, etc.)
    work unchanged.  Overrides ``log()`` to apply AgentLoomLogLevel semantics:

        DEBUG(10) < INFO(20) < WARNING(30) < ERROR(40) < OFF(50)

    A message is printed only when ``msg_level >= self.level``.
    smolagents passes its own LogLevel enum into ``log()``; we convert it via
    ``AgentLoomLogLevel.from_smola()`` before comparing.
    """

    def __init__(
        self,
        level: AgentLoomLogLevel = AgentLoomLogLevel.INFO,
        console: Console | None = None,
        show_timestamp: bool = True,
        timestamp_format: str = "%Y-%m-%d %H:%M:%S",
        show_trace_info: bool = True,
        truncate_id_length: int = 8,
    ):
        # Pass a dummy smolagents level; we ignore it and use self._agent_loom_level.
        super().__init__(level=SmolaLogLevel.DEBUG, console=console)
        self._agent_loom_level: AgentLoomLogLevel = level
        self.show_timestamp = show_timestamp
        self.timestamp_format = timestamp_format
        self.show_trace_info = show_trace_info
        self.truncate_id_length = truncate_id_length

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _truncate_id(self, id_str: str) -> str:
        if self.truncate_id_length > 0 and len(id_str) > self.truncate_id_length:
            return f"...{id_str[-self.truncate_id_length:]}"
        return id_str

    def _build_prefix(self, level: AgentLoomLogLevel) -> Text:
        prefix = Text()
        if self.show_timestamp:
            ts = datetime.now().strftime(self.timestamp_format)
            prefix.append(f"[{ts}]", style=TIMESTAMP_STYLE)
        if self.show_trace_info:
            task_id    = get_current_task_id()
            sub_id     = get_current_sub_task_id()
            agent_name = get_current_agent_name()
            if task_id:
                prefix.append(f"[task:{self._truncate_id(task_id)}]", style=TASK_ID_STYLE)
            if sub_id:
                prefix.append(f"[subtask:{self._truncate_id(sub_id)}]", style=SUBTASK_ID_STYLE)
            if agent_name:
                prefix.append(f"[agent:{agent_name}]", style=AGENT_ID_STYLE)
        tag_style = _LEVEL_STYLE.get(level, "bold blue")
        prefix.append(f"[{level.name}] ", style=tag_style)
        return prefix

    def _to_agent_loom_level(self, level: "int | str | SmolaLogLevel | AgentLoomLogLevel") -> AgentLoomLogLevel:
        """Normalise any level representation to AgentLoomLogLevel."""
        if isinstance(level, AgentLoomLogLevel):
            return level
        if isinstance(level, str):
            # Try AgentLoomLogLevel first, fall back to smolagents name mapping.
            try:
                return AgentLoomLogLevel.from_str(level)
            except ValueError:
                pass
            try:
                smola = SmolaLogLevel[level.upper()]
                return AgentLoomLogLevel.from_smola(smola)
            except KeyError:
                return AgentLoomLogLevel.INFO
        # int or SmolaLogLevel (which is IntEnum)
        return AgentLoomLogLevel.from_smola(level)

    # ------------------------------------------------------------------
    # Core log override — all smolagents internals funnel through here
    # ------------------------------------------------------------------

    def log(self, *args, level: "int | str | SmolaLogLevel | AgentLoomLogLevel" = SmolaLogLevel.INFO, **kwargs) -> None:  # type: ignore[override]
        """Emit args if resolved level >= ``self._agent_loom_level``."""
        agent_loom_level = self._to_agent_loom_level(level)
        if agent_loom_level < self._agent_loom_level:
            return
        if not args:
            return

        prefix = self._build_prefix(agent_loom_level)
        first_arg = args[0]

        if isinstance(first_arg, str):
            msg_style = _MSG_STYLE.get(agent_loom_level)
            if msg_style and "style" not in kwargs:
                kwargs = dict(kwargs, style=msg_style)
            # Use end="" to keep prefix on the same line, then let console.print 
            # parse the markup in first_arg (e.g., "[bold]Error[/bold]").
            self.console.print(prefix, end="")
            self.console.print(*args, **kwargs)
        elif isinstance(first_arg, Text):
            self.console.print(prefix + first_arg, *args[1:], **kwargs)
        else:
            # Rich renderables (Panel, Rule, Group, …): print prefix then object.
            self.console.print(prefix)
            self.console.print(*args, **kwargs)

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def debug(self, msg: str, **_kwargs) -> None:
        self.log(msg, level=AgentLoomLogLevel.DEBUG)

    def info(self, msg: str, **_kwargs) -> None:
        self.log(msg, level=AgentLoomLogLevel.INFO)

    def warning(self, msg: str, **_kwargs) -> None:
        self.log(msg, level=AgentLoomLogLevel.WARNING)

    def error(self, msg: str, **_kwargs) -> None:
        self.log(msg, level=AgentLoomLogLevel.ERROR)

    def log_task(
        self,
        content: str | None = None,
        subtitle: str = "",
        title: str | None = None,
        level: "int | str | SmolaLogLevel | AgentLoomLogLevel" = AgentLoomLogLevel.INFO,
        **kwargs
    ) -> None:
        """Log the current task being executed by the agent."""
        # 兼容 smolagents 内部调用 (content, subtitle 等) 和旧代码可能带的 task 参数
        task_obj = kwargs.get("task")
        if content:
            task_str = str(content)
        elif task_obj:
            task_str = str(task_obj)
        else:
            task_str = "Unknown Task"

        from rich.panel import Panel
        try:
            from smolagents.utils import escape_code_brackets
            task_str = escape_code_brackets(task_str)
        except ImportError:
            pass

        panel = Panel(
            f"\n[bold]{task_str}\n",
            title="[bold]New run" + (f" - {title}" if title else ""),
            subtitle=subtitle,
            border_style="#d4b702",  # YELLOW_HEX from smolagents
            subtitle_align="left"
        )
        self.log(panel, level=level)
