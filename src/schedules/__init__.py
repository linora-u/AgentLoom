"""Durable, project-scoped AgentLoom schedules.

The storage/claim design is informed by NousResearch/hermes-agent's
``cron/jobs.py`` (Copyright (c) 2025 Nous Research, MIT License, commit
``29e3983fa``), but is independently written for AgentLoom's much smaller
execution boundary: ``python -m agentloom run``.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runner import ScheduleRunner
    from .schedule import cron_schedule, interval_schedule, next_run, once_schedule
    from .service import ScheduleService
    from .store import ScheduleStore

__all__ = [
    "ScheduleRunner",
    "ScheduleService",
    "ScheduleStore",
    "cron_schedule",
    "interval_schedule",
    "next_run",
    "once_schedule",
]

_EXPORT_MODULES = {
    "ScheduleRunner": "runner",
    "ScheduleService": "service",
    "ScheduleStore": "store",
    "cron_schedule": "schedule",
    "interval_schedule": "schedule",
    "next_run": "schedule",
    "once_schedule": "schedule",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
