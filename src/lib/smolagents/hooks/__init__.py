"""Hook system for AI agents framework.

Provides the complete hook lifecycle: event types, command types (command /
prompt / http / agent), pattern matching, true parallel execution via
ThreadPoolExecutor, timeout enforcement pushed to each executor,
permission precedence aggregation (deny > allow > passthrough),
deduplication, once-flag auto-removal, JSON I/O schema validation,
async hook registry with process-group control, and configuration
management with YAML-to-callable bridge.
"""

from .types import (
    AggregatedHookResult,
    AgentHook,
    CommandHook,
    HookCommand,
    HookContext,
    HookEvent,
    HookMatcher,
    HookResult,
    HooksSettings,
    HttpHook,
    PromptHook,
    HOOK_EVENT_NAMES,
)
from .hook_manager import HookManager
from .hook_helpers import (
    matches_pattern,
    hook_dedup_key,
    add_arguments_to_prompt,
    kill_hook_process_group,
)
from .hooks_config import HooksConfigManager, HooksConfigSnapshot
from .async_hook_registry import AsyncHookRegistry
from src.lib.logging import get_logger

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
