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
    get_current_skills_manager,
    set_current_skills_manager,
    clear_current_skills_manager,
    get_current_hook_manager,
    set_current_hook_manager,
    clear_current_hook_manager,
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
    'get_current_skills_manager',
    'set_current_skills_manager',
    'clear_current_skills_manager',
    'get_current_hook_manager',
    'set_current_hook_manager',
    'clear_current_hook_manager',
    'task_context',
    'sub_task_context',
]
