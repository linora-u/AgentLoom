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
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Generator, Optional

from src.lib.runtime.context import RootRunState

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
_current_session_run_id: ContextVar[Optional[str]] = ContextVar('current_session_run_id', default=None)
_current_local_run_id: ContextVar[Optional[str]] = ContextVar('current_local_run_id', default=None)
_current_root_run_state: ContextVar[RootRunState | None] = ContextVar(
    'current_root_run_state', default=None
)

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
# NOTE: the session run id deliberately has NO global fallback. Worker threads
# inherit it via contextvars.copy_context() (see ParallelAgentExecutor), and a
# process-wide scalar would leak run A's identity into a concurrently running
# run B — history and curated-memory writes would be attributed to the wrong root.


class MissingRunContextError(RuntimeError):
    """Raised when run-scoped code executes without an explicit root binding."""


@dataclass(frozen=True)
class ExplicitExecutionContext:
    """Immutable values required when execution crosses a thread boundary.

    The regular getters in this module retain legacy process-wide fallbacks for
    non-run-scoped integrations.  Runtime tools and hooks must not use those
    fallbacks: a value from another concurrent run is worse than a missing
    value.  This snapshot is captured from ContextVars only and can be rebound
    independently in any number of worker threads.
    """

    task_id: Optional[str]
    sub_task_id: Optional[str]
    agent_id: Optional[str]
    agent_name: Optional[str]
    agent_config: Optional[dict]
    skills_manager: Optional[Any]
    hook_manager: Optional[Any]
    runtime_agent_path: Optional[str]
    root_run_id: Optional[str]
    local_run_id: Optional[str]
    root_run_state: RootRunState | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        root_run_id = str(self.root_run_id or "").strip()
        state = self.root_run_state
        if root_run_id and (state is None or state.root_run_id != root_run_id):
            object.__setattr__(self, "root_run_state", RootRunState(root_run_id))
        elif not root_run_id and state is not None:
            object.__setattr__(self, "root_run_state", None)


def capture_explicit_execution_context() -> ExplicitExecutionContext:
    """Capture the active execution context without consulting fallbacks."""

    return ExplicitExecutionContext(
        task_id=_current_task_id.get(),
        sub_task_id=_current_sub_task_id.get(),
        agent_id=_current_agent_id.get(),
        agent_name=_current_agent_name.get(),
        agent_config=_current_agent_config.get(),
        skills_manager=_current_skills_manager.get(),
        hook_manager=_current_hook_manager.get(),
        runtime_agent_path=_current_runtime_agent_path.get(),
        root_run_id=_current_session_run_id.get(),
        local_run_id=_current_local_run_id.get(),
        root_run_state=_current_root_run_state.get(),
    )


@contextmanager
def bind_explicit_execution_context(
    context: ExplicitExecutionContext,
) -> Generator[None, None, None]:
    """Rebind a captured context with ContextVar tokens only.

    Deliberately bypasses the public setters because those also maintain
    legacy global fallbacks.  Crossing a runtime thread must never mutate a
    process-wide value shared by another run.
    """

    root_run_id = str(context.root_run_id or "").strip()
    root_run_state = context.root_run_state
    if root_run_id:
        if root_run_state is None or root_run_state.root_run_id != root_run_id:
            root_run_state = RootRunState(root_run_id)
    else:
        root_run_state = None

    bindings = (
        (_current_task_id, context.task_id),
        (_current_sub_task_id, context.sub_task_id),
        (_current_agent_id, context.agent_id),
        (_current_agent_name, context.agent_name),
        (_current_agent_config, context.agent_config),
        (_current_skills_manager, context.skills_manager),
        (_current_hook_manager, context.hook_manager),
        (_current_runtime_agent_path, context.runtime_agent_path),
        (_current_session_run_id, context.root_run_id),
        (_current_local_run_id, context.local_run_id),
        (_current_root_run_state, root_run_state),
    )
    tokens = [(variable, variable.set(value)) for variable, value in bindings]
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


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
# Runtime agent path — hierarchical identity used only for agent workspace
# nesting. Separate from agent_name so logs, checkpoints, and other consumers
# keep the original flat name.
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


# ---------------------------------------------------------------------------
# Session run id — the TOP-LEVEL run's session identity, set only by the
# session-owning (supervisor) agent.  Workers keep their own hook managers
# (event attribution stays per-worker) but share this id, so run-scoped
# run-owned history/review state accrues to the root that gets the SessionEnd.
# Pure ContextVar: worker threads see it through copy_context() propagation
# at the spawn site, and concurrent runs in one process stay isolated.
# ---------------------------------------------------------------------------

def set_current_session_run_id(run_id: str) -> None:
    """Set the top-level session run id for the active run."""
    normalized = str(run_id or "").strip()
    if not normalized:
        raise ValueError("root run id must be a non-empty string")
    _current_session_run_id.set(normalized)
    _current_root_run_state.set(RootRunState(normalized))
    logger.debug(f"Set session run id: {normalized}")


def get_current_session_run_id() -> Optional[str]:
    """Get the top-level session run id for the current context."""
    return _current_session_run_id.get()


def clear_current_session_run_id() -> None:
    """Clear the top-level session run id."""
    _current_session_run_id.set(None)
    _current_root_run_state.set(None)
    logger.debug("Cleared session run id")


def get_current_local_run_id() -> Optional[str]:
    """Return the current agent invocation id without any global fallback."""

    return _current_local_run_id.get()


def require_local_run_id() -> str:
    """Return the explicitly bound local run id or fail closed."""

    run_id = _current_local_run_id.get()
    if not isinstance(run_id, str) or not run_id.strip():
        raise MissingRunContextError("missing explicit local run context")
    return run_id.strip()


@contextmanager
def bind_local_run(run_id: str) -> Generator[None, None, None]:
    """Bind one agent invocation id, restoring the parent invocation on exit."""

    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("local run id must be a non-empty string")
    token = _current_local_run_id.set(run_id.strip())
    try:
        yield
    finally:
        _current_local_run_id.reset(token)


def require_root_run_id() -> str:
    """Return the explicitly bound root run id or fail closed.

    This accessor deliberately reads only the run ``ContextVar``.  In
    particular, it must never consult the process-global hook-manager
    fallback because that can belong to a concurrent run.
    """
    run_id = _current_session_run_id.get()
    if not isinstance(run_id, str) or not run_id.strip():
        raise MissingRunContextError("missing explicit root run context")
    return run_id.strip()


def require_root_run_state() -> RootRunState:
    """Return the state object shared by the current root and its workers."""

    root_run_id = require_root_run_id()
    state = _current_root_run_state.get()
    if state is None or state.root_run_id != root_run_id:
        state = RootRunState(root_run_id)
        _current_root_run_state.set(state)
    return state


@contextmanager
def bind_root_run(run_id: str) -> Generator[bool, None, None]:
    """Bind the first run id in a call tree and report whether this call owns it.

    Nested agents inherit the existing root even when their local hook manager
    has a different session id.  Only the outer owner resets the binding, using
    the ``ContextVar`` token so sibling/concurrent contexts remain isolated.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("root run id must be a non-empty string")

    current = _current_session_run_id.get()
    if isinstance(current, str) and current.strip():
        state = _current_root_run_state.get()
        if state is not None and state.root_run_id == current.strip():
            yield False
            return
        state_token = _current_root_run_state.set(RootRunState(current.strip()))
        try:
            yield False
        finally:
            _current_root_run_state.reset(state_token)
        return

    normalized = run_id.strip()
    token = _current_session_run_id.set(normalized)
    state_token = _current_root_run_state.set(RootRunState(normalized))
    try:
        yield True
    finally:
        _current_root_run_state.reset(state_token)
        _current_session_run_id.reset(token)


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
