"""
Checkpoint / Resume infrastructure for AgentLoom agent framework.

Provides:
- ``CheckpointSerializer``:    Serialize / deserialize smolagents MemoryStep objects.
- ``CheckpointManager``:       Persist and restore agent state across runs.
- ``CheckpointCoordinator``:   Single owner of checkpoint logic for one task run.
"""

from src.lib.checkpoint.serializer import CheckpointSerializer
from src.lib.checkpoint.checkpoint_manager import (
    CheckpointManager,
    cleanup_expired_tasks,
    delete_checkpoint_task_if_inactive,
)
from src.lib.checkpoint.coordinator import CheckpointCoordinator
from src.lib.checkpoint.conversation_recovery import (
    TurnInterruptionState,
    prepare_steps_for_resume,
)

__all__ = [
    "CheckpointManager",
    "cleanup_expired_tasks",
    "delete_checkpoint_task_if_inactive",
    "CheckpointSerializer",
    "CheckpointCoordinator",
    "TurnInterruptionState",
    "prepare_steps_for_resume",
]
