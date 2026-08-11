"""
Tracing system package.

Provides task tracing, ID generation, and context management.

Main components:
- id_generator: Snowflake-based ID generation
- task_context: Task context management (ContextVar + context managers)
"""

# ID generator.
from .id_generator import generate_id

# Task context management.
from .task_context import (
    # ContextVar getters/setters
    get_current_task_id,
    set_current_task_id,
    clear_current_task_id,
    get_current_sub_task_id,
    set_current_sub_task_id,
    clear_current_sub_task_id,
    get_current_agent_id,
    set_current_agent_id,
    clear_current_agent_id,
    get_current_agent_name,
    set_current_agent_name,
    clear_current_agent_name,
    get_current_runtime_agent_path,
    set_current_runtime_agent_path,
    clear_current_runtime_agent_path,
    get_current_agent_config,
    set_current_agent_config,
    clear_current_agent_config,
    get_current_skill_catalog,
    set_current_skill_catalog,
    clear_current_skill_catalog,
    get_current_hook_run,
    set_current_hook_run,
    clear_current_hook_run,
    get_current_session_run_id,
    set_current_session_run_id,
    clear_current_session_run_id,
    get_current_local_run_id,
    MissingRunContextError,
    ExplicitExecutionContext,
    capture_explicit_execution_context,
    bind_explicit_execution_context,
    bind_local_run,
    bind_root_run,
    require_local_run_id,
    require_root_run_id,
    require_root_run_state,
    # Context managers
    task_context,
    sub_task_context,
)

__all__ = [
    # ID generation
    'generate_id',
    # Task context
    'get_current_task_id',
    'set_current_task_id',
    'clear_current_task_id',
    'get_current_sub_task_id',
    'set_current_sub_task_id',
    'clear_current_sub_task_id',
    'get_current_agent_id',
    'set_current_agent_id',
    'clear_current_agent_id',
    'get_current_agent_name',
    'set_current_agent_name',
    'clear_current_agent_name',
    'get_current_runtime_agent_path',
    'set_current_runtime_agent_path',
    'clear_current_runtime_agent_path',
    'get_current_agent_config',
    'set_current_agent_config',
    'clear_current_agent_config',
    'get_current_skill_catalog',
    'set_current_skill_catalog',
    'clear_current_skill_catalog',
    'get_current_hook_run',
    'set_current_hook_run',
    'clear_current_hook_run',
    'get_current_session_run_id',
    'set_current_session_run_id',
    'clear_current_session_run_id',
    'get_current_local_run_id',
    'MissingRunContextError',
    'ExplicitExecutionContext',
    'capture_explicit_execution_context',
    'bind_explicit_execution_context',
    'bind_local_run',
    'bind_root_run',
    'require_local_run_id',
    'require_root_run_id',
    'require_root_run_state',
    'task_context',
    'sub_task_context',
]
