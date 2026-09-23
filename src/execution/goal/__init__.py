"""Goal mode public contracts."""

from .model import (
    GOAL_SCHEMA_VERSION,
    GoalConfig,
    GoalState,
    build_goal_objective,
    goal_completion_output,
    goal_continuation_prompt,
    goal_objective_fingerprint,
    normalize_goal_config,
    validate_goal_state,
)
from .provider import (
    GoalCompleteError,
    GoalStateProvider,
    bind_goal_state_provider,
    get_current_goal_provider,
)

__all__ = [
    "GOAL_SCHEMA_VERSION",
    "GoalConfig",
    "GoalState",
    "build_goal_objective",
    "goal_completion_output",
    "goal_continuation_prompt",
    "goal_objective_fingerprint",
    "normalize_goal_config",
    "validate_goal_state",
    "GoalCompleteError",
    "GoalStateProvider",
    "bind_goal_state_provider",
    "get_current_goal_provider",
]
