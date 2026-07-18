"""AgentLoom's single Hook Plan and per-invocation Hook Run contract."""

from __future__ import annotations

from .config import HookConfigLayer, HookPlanCompiler, ShellHookSpec
from .runtime import HookPlan, HookRun, wrap_in_system_reminder
from .types import (
    GATE_EVENTS,
    HOOK_EVENT_NAMES,
    OBSERVER_EVENTS,
    HookContext,
    HookEvent,
    HookHandler,
    HookResult,
)


def builtin_hook_handlers() -> tuple[HookHandler, ...]:
    """Return trusted framework handlers in their stable execution order."""

    from src.extensions.self_learning.session_recorder import session_recorder_hook

    recorder_events = (
        HookEvent.SESSION_START,
        HookEvent.SESSION_END,
        HookEvent.TASK_CREATED,
        HookEvent.TASK_COMPLETED,
        HookEvent.STOP_FAILURE,
        HookEvent.SUBAGENT_START,
        HookEvent.SUBAGENT_STOP,
        HookEvent.POST_TOOL_USE,
        HookEvent.POST_TOOL_USE_FAILURE,
    )
    return tuple(
        HookHandler(
            event,
            "*",
            session_recorder_hook,
            source="builtin:self_learning_recorder",
        )
        for event in recorder_events
    )


__all__ = [
    "GATE_EVENTS",
    "HOOK_EVENT_NAMES",
    "OBSERVER_EVENTS",
    "HookContext",
    "HookConfigLayer",
    "HookEvent",
    "HookHandler",
    "HookPlan",
    "HookPlanCompiler",
    "HookResult",
    "HookRun",
    "ShellHookSpec",
    "builtin_hook_handlers",
    "wrap_in_system_reminder",
]
