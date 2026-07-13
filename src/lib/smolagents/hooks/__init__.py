"""Hook system for AI agents framework.

Provides the complete hook lifecycle: event types, command types (command /
prompt / http / agent), pattern matching, true parallel execution via
ThreadPoolExecutor, timeout enforcement pushed to each executor,
permission precedence aggregation (deny > allow > passthrough),
deduplication, once-flag auto-removal, JSON I/O schema validation,
async hook registry with process-group control, and configuration
management with YAML-to-callable bridge.
"""

from src.lib.logging import get_logger

from .async_hook_registry import AsyncHookRegistry
from .hook_helpers import (
    add_arguments_to_prompt,
    hook_dedup_key,
    kill_hook_process_group,
    matches_pattern,
)
from .hook_manager import HookManager
from .hooks_config import HooksConfigManager, HooksConfigSnapshot
from .types import (
    HOOK_EVENT_NAMES,
    AgentHook,
    AggregatedHookResult,
    CommandHook,
    HookCommand,
    HookContext,
    HookEvent,
    HookMatcher,
    HookResult,
    HooksSettings,
    HttpHook,
    PromptHook,
)

logger = get_logger(__name__)

_BUILTIN_HOOKS_REGISTERED_ATTR = "_builtin_hooks_registered"


def register_hook(event, pattern, func, **kwargs):
    """Helper to register hook on singleton manager."""
    HookManager.get_instance().register_hook(event, pattern, func, **kwargs)


def register_builtin_hooks(manager: HookManager) -> HookManager:
    """Register framework-provided hooks on the given manager once."""
    if bool(getattr(manager, _BUILTIN_HOOKS_REGISTERED_ATTR, False)):
        return manager

    try:
        from .path_validators import validate_workspace_path

        manager.register_hook(
            HookEvent.PRE_TOOL_USE,
            "*",
            validate_workspace_path,
        )
    except Exception as e:
        logger.warning("Failed to auto-register path validators: %s", e)

    try:
        from src.extensions.self_learning.session_recorder import session_recorder_hook

        for event in (
            HookEvent.SESSION_START,
            HookEvent.TASK_CREATED,
            HookEvent.TASK_COMPLETED,
            HookEvent.STOP_FAILURE,
            HookEvent.SUBAGENT_START,
            HookEvent.SUBAGENT_STOP,
            HookEvent.PRE_TOOL_USE,
            HookEvent.POST_TOOL_USE,
            HookEvent.POST_TOOL_USE_FAILURE,
        ):
            manager.register_hook(
                event,
                "*",
                session_recorder_hook,
                allow_duplicates=False,
                source="builtin:self_learning_recorder",
            )
    except Exception as e:
        logger.warning("Failed to auto-register self-learning recorder: %s", e)

    try:
        from src.extensions.self_learning.reviewer import learning_review_hook

        for event in (
            HookEvent.TASK_COMPLETED,
            HookEvent.STOP_FAILURE,
            HookEvent.POST_TOOL_USE_FAILURE,
        ):
            manager.register_hook(
                event,
                "*",
                learning_review_hook,
                allow_duplicates=False,
                source="builtin:self_learning_reviewer",
            )
    except Exception as e:
        logger.warning("Failed to auto-register self-learning reviewer: %s", e)

    try:
        from src.extensions.self_learning.finalizer import session_finalize_hook

        # SessionEnd has exactly one synchronous owner: it atomically records
        # the final event/outcome and inserts durable outbox rows.  Model calls,
        # auto-apply, retention, and artifact delivery run in the worker.
        manager.register_hook(
            HookEvent.SESSION_END,
            "*",
            session_finalize_hook,
            allow_duplicates=False,
            source="builtin:self_learning_finalizer",
        )
    except Exception as e:
        logger.warning("Failed to auto-register self-learning finalizer: %s", e)

    setattr(manager, _BUILTIN_HOOKS_REGISTERED_ATTR, True)
    return manager


__all__ = [
    # Types
    "HookEvent",
    "HookContext",
    "HookResult",
    "AggregatedHookResult",
    "HookCommand",
    "CommandHook",
    "PromptHook",
    "HttpHook",
    "AgentHook",
    "HookMatcher",
    "HooksSettings",
    "HOOK_EVENT_NAMES",
    # Manager
    "HookManager",
    # Config
    "HooksConfigManager",
    "HooksConfigSnapshot",
    # Async registry
    "AsyncHookRegistry",
    # Helpers
    "register_hook",
    "register_builtin_hooks",
    "matches_pattern",
    "hook_dedup_key",
    "add_arguments_to_prompt",
    "kill_hook_process_group",
]

register_builtin_hooks(HookManager.get_instance())
