"""Goal mode public contracts."""

from .model import (
    GOAL_SCHEMA_VERSION,
    GoalConfig,
    GoalState,
    build_goal_objective,
    goal_objective_fingerprint,
    normalize_goal_config,
    normalize_workflow_for_goal,
    validate_goal_state,
)
from .provider import (
    GoalBudgetLimitedError,
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
    "goal_objective_fingerprint",
    "normalize_goal_config",
    "normalize_workflow_for_goal",
    "validate_goal_state",
    "GoalBudgetLimitedError",
    "GoalCompleteError",
    "GoalStateProvider",
    "bind_goal_state_provider",
    "get_current_goal_provider",
]
