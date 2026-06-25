"""
Task context management module.

Provides ContextVar-based task tracing context management, including task ID,
sub-task ID, and agent ID handling.

Note on threading:
    ContextVar values do NOT propagate automatically to child threads spawned
    by ``concurrent.futures.ThreadPoolExecutor``.  To ensure hooks and tools
    can still read the active agent identity/config when running inside a
    thread-pool worker, we maintain **thread-safe global fallbacks** that are
    updated by the matching ``set_current_*`` helpers.  Getters first check
    their ContextVar; if it is ``None``, they return the fallback.
"""

import logging
import threading
from contextvars import ContextVar
from contextlib import contextmanager
from typing import Any, Optional, Generator

from .id_generator import generate_id


logger = logging.getLogger(__name__)

# Context variables to store the current task/session ID and sub-task info
_current_task_id: ContextVar[Optional[str]] = ContextVar('current_task_id', default=None)
_current_sub_task_id: ContextVar[Optional[str]] = ContextVar('current_sub_task_id', default=None)
_current_agent_id: ContextVar[Optional[str]] = ContextVar('current_agent_id', default=None)
_current_agent_name: ContextVar[Optional[str]] = ContextVar('current_agent_name', default=None)
_current_agent_config: ContextVar[Optional[dict]] = ContextVar('current_agent_config', default=None)
_current_skills_manager: ContextVar[Optional[Any]] = ContextVar('current_skills_manager', default=None)
_current_hook_manager: ContextVar[Optional[Any]] = ContextVar('current_hook_manager', default=None)
_current_runtime_agent_path: ContextVar[Optional[str]] = ContextVar('current_runtime_agent_path', default=None)

# Thread-safe global fallbacks for values that must be accessible from
# ThreadPoolExecutor worker threads where ContextVar is not propagated.
_global_lock = threading.Lock()
_global_task_id_fallback: Optional[str] = None
_global_sub_task_id_fallback: Optional[str] = None
_global_agent_id_fallback: Optional[str] = None
_global_agent_config_fallback: Optional[dict] = None
_global_agent_name_fallback: Optional[str] = None
_global_runtime_agent_path_fallback: Optional[str] = None
_global_skills_manager_fallback: Optional[Any] = None
_global_hook_manager_fallback: Optional[Any] = None


def set_current_task_id(task_id: str) -> None:
    """Set the current task ID."""
    global _global_task_id_fallback
    _current_task_id.set(task_id)
    with _global_lock:
        _global_task_id_fallback = task_id
    logger.debug(f"Set task ID: {task_id}")


def get_current_task_id() -> Optional[str]:
    """Get the current task ID."""
    value = _current_task_id.get()
    if value is not None:
        return value
    with _global_lock:
        return _global_task_id_fallback


def clear_current_task_id() -> None:
    """Clear the current task ID."""
    global _global_task_id_fallback
    _current_task_id.set(None)
    with _global_lock:
        _global_task_id_fallback = None
    logger.debug("Cleared task ID")


def set_current_sub_task_id(sub_task_id: str) -> None:
    """Set the current sub-task ID."""
    global _global_sub_task_id_fallback
    _current_sub_task_id.set(sub_task_id)
    with _global_lock:
        _global_sub_task_id_fallback = sub_task_id
    logger.debug(f"Set sub-task ID: {sub_task_id}")


def get_current_sub_task_id() -> Optional[str]:
    """Get the current sub-task ID."""
    value = _current_sub_task_id.get()
    if value is not None:
        return value
    with _global_lock:
        return _global_sub_task_id_fallback


def clear_current_sub_task_id() -> None:
    """Clear the current sub-task ID."""
    global _global_sub_task_id_fallback
    _current_sub_task_id.set(None)
    with _global_lock:
        _global_sub_task_id_fallback = None
    logger.debug("Cleared sub-task ID")


def set_current_agent_id(agent_id: str) -> None:
    """Set the current agent ID."""
    global _global_agent_id_fallback
    _current_agent_id.set(agent_id)
    with _global_lock:
        _global_agent_id_fallback = agent_id
    logger.debug(f"Set agent ID: {agent_id}")


def get_current_agent_id() -> Optional[str]:
    """Get the current agent ID."""
    value = _current_agent_id.get()
    if value is not None:
        return value
    with _global_lock:
        return _global_agent_id_fallback


def clear_current_agent_id() -> None:
    """Clear the current agent ID."""
    global _global_agent_id_fallback
    _current_agent_id.set(None)
    with _global_lock:
        _global_agent_id_fallback = None
    logger.debug("Cleared agent ID")


def set_current_agent_name(agent_name: str) -> None:
    """Set the current agent name."""
    global _global_agent_name_fallback
    _current_agent_name.set(agent_name)
    with _global_lock:
        _global_agent_name_fallback = agent_name
    logger.debug(f"Set agent name: {agent_name}")


def get_current_agent_name() -> Optional[str]:
    """Get the current agent name."""
    value = _current_agent_name.get()
    if value is not None:
        return value
    with _global_lock:
        return _global_agent_name_fallback


def clear_current_agent_name() -> None:
    """Clear the current agent name."""
    global _global_agent_name_fallback
    _current_agent_name.set(None)
    with _global_lock:
        _global_agent_name_fallback = None
    logger.debug("Cleared agent name")


# ---------------------------------------------------------------------------
# Runtime agent path — hierarchical path used ONLY for .runtime directory
# nesting.  Separate from agent_name so that logs, checkpoints, and other
# consumers keep the original flat name.
# ---------------------------------------------------------------------------

def set_current_runtime_agent_path(path: str) -> None:
    """Set the hierarchical runtime agent path (e.g. 'parent/child')."""
    global _global_runtime_agent_path_fallback
    _current_runtime_agent_path.set(path)
    with _global_lock:
        _global_runtime_agent_path_fallback = path
    logger.debug(f"Set runtime agent path: {path}")


def get_current_runtime_agent_path() -> Optional[str]:
    """Get the hierarchical runtime agent path.

    Returns the ContextVar value if set, otherwise falls back to the
    global snapshot (useful inside ThreadPoolExecutor worker threads).
    """
    value = _current_runtime_agent_path.get()
    if value is not None:
        return value
    with _global_lock:
        return _global_runtime_agent_path_fallback


def clear_current_runtime_agent_path() -> None:
    """Clear the hierarchical runtime agent path."""
    global _global_runtime_agent_path_fallback
    _current_runtime_agent_path.set(None)
    with _global_lock:
        _global_runtime_agent_path_fallback = None
    logger.debug("Cleared runtime agent path")


def set_current_agent_config(agent_config: dict) -> None:
    """Set the current agent config.

    Also updates a global fallback so that ThreadPoolExecutor worker
    threads (where ContextVar is not propagated) can still read it.
    """
    global _global_agent_config_fallback
    _current_agent_config.set(agent_config)
    with _global_lock:
        _global_agent_config_fallback = agent_config
    logger.debug("Set agent config")


def get_current_agent_config() -> Optional[dict]:
    """Get the current agent config.

    Returns the ContextVar value if set, otherwise falls back to the
    global snapshot (useful inside ThreadPoolExecutor worker threads).
    """
    value = _current_agent_config.get()
    if value is not None:
        return value
    # Fallback for child threads spawned by ThreadPoolExecutor
    with _global_lock:
        return _global_agent_config_fallback


def clear_current_agent_config() -> None:
    """Clear the current agent config."""
    global _global_agent_config_fallback
    _current_agent_config.set(None)
    with _global_lock:
        _global_agent_config_fallback = None
    logger.debug("Cleared agent config")


def set_current_skills_manager(skills_manager: Any) -> None:
    """Set the current skills manager for the active agent context."""
    global _global_skills_manager_fallback
    _current_skills_manager.set(skills_manager)
    with _global_lock:
        _global_skills_manager_fallback = skills_manager
    logger.debug("Set current skills manager")


def get_current_skills_manager() -> Optional[Any]:
    """Get the current skills manager for the active agent context."""
    value = _current_skills_manager.get()
    if value is not None:
        return value
    with _global_lock:
        return _global_skills_manager_fallback


def clear_current_skills_manager() -> None:
    """Clear the current skills manager."""
    global _global_skills_manager_fallback
    _current_skills_manager.set(None)
    with _global_lock:
        _global_skills_manager_fallback = None
    logger.debug("Cleared current skills manager")


def set_current_hook_manager(hook_manager: Any) -> None:
    """Set the current hook manager for the active agent context."""
    global _global_hook_manager_fallback
    _current_hook_manager.set(hook_manager)
    with _global_lock:
        _global_hook_manager_fallback = hook_manager
    logger.debug("Set current hook manager")


def get_current_hook_manager() -> Optional[Any]:
    """Get the current hook manager for the active agent context."""
    value = _current_hook_manager.get()
    if value is not None:
        return value
    with _global_lock:
        return _global_hook_manager_fallback


def clear_current_hook_manager() -> None:
    """Clear the current hook manager."""
    global _global_hook_manager_fallback
    _current_hook_manager.set(None)
    with _global_lock:
        _global_hook_manager_fallback = None
    logger.debug("Cleared current hook manager")


@contextmanager
def task_context(task_id: Optional[str] = None) -> Generator[str, None, None]:
    """
    Task context manager that automatically manages task ID lifecycle.

    Args:
        task_id: Optional task ID. If omitted, one is generated automatically.

    Yields:
        str: Current task ID.

    Example:
        with task_context() as task_id:
            # All LLM calls in this context include this task_id.
            result = agent.run("some task")
    """
    if task_id is None:
        task_id = generate_id()

    # Save previous task ID.
    previous_task_id = get_current_task_id()

    try:
        # Set new task ID.
        set_current_task_id(task_id)
        yield task_id
    finally:
        # Restore previous task ID.
        if previous_task_id is not None:
            set_current_task_id(previous_task_id)
        else:
            clear_current_task_id()


@contextmanager
def sub_task_context(agent_name: str, sub_task_id: Optional[str] = None) -> Generator[str, None, None]:
    """
    Sub-task context manager that creates an independent tracing chain for worker agents.

    Args:
        agent_name: Agent name.
        sub_task_id: Optional sub-task ID. If omitted, one is generated automatically.

    Yields:
        str: Current sub-task ID.

    Example:
        with sub_task_context("search_agent") as sub_task_id:
            # All LLM calls in this context include sub-task information.
            result = worker_agent.run("some sub task")
    """
    if sub_task_id is None:
        sub_task_id = generate_id(agent_name, prefix="agent")

    # Save previous state.
    previous_sub_task_id = get_current_sub_task_id()
    previous_agent_id = get_current_agent_id()
    previous_agent_name = get_current_agent_name()

    try:
        # Set new sub-task and agent context.
        set_current_sub_task_id(sub_task_id)
        set_current_agent_id(agent_name)
        set_current_agent_name(agent_name)
        yield sub_task_id
    finally:
        # Restore previous state.
        if previous_sub_task_id is not None:
            set_current_sub_task_id(previous_sub_task_id)
        else:
            clear_current_sub_task_id()

        if previous_agent_id is not None:
            set_current_agent_id(previous_agent_id)
        else:
            clear_current_agent_id()
            
        if previous_agent_name is not None:
            set_current_agent_name(previous_agent_name)
        else:
            clear_current_agent_name()
