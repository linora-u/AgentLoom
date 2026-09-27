import pytest
from agentloom.execution.checkpoint import CheckpointManager
from agentloom.execution.checkpoint.coordinator import CheckpointCoordinator
from agentloom.execution.goal import GoalConfig, GoalState, normalize_goal_config
from agentloom.execution.goal.provider import (
    GoalCompleteError,
    GoalStateProvider,
    bind_goal_state_provider,
    get_current_goal_provider,
)


def _state() -> GoalState:
    return GoalState.create(objective="Ship Goal mode.", objective_fingerprint="abc123")


def test_checkpoint_goal_state_roundtrip_and_corruption_is_fatal(tmp_path):
    manager = CheckpointManager("supervisor", checkpoints_root=tmp_path)
    try:
        state = _state()
        manager.save_goal("task-1", state.to_dict())
        assert manager.load_goal("task-1") == state.to_dict()
        goal_path = tmp_path / "task-1" / "goal.json"
        goal_path.write_text("{broken", encoding="utf-8")
        with pytest.raises(ValueError, match="corrupt Goal state"):
            manager.load_goal("task-1")
    finally:
        manager.close()


def test_provider_persists_start_and_idempotent_completion(tmp_path, monkeypatch):
    manager = CheckpointManager("supervisor", checkpoints_root=tmp_path)
    coord = CheckpointCoordinator.activate(manager, "task-1", "task")
    try:
        provider = GoalStateProvider.initialize(
            config=GoalConfig(enabled=True), objective="Ship Goal mode.",
            objective_fingerprint="abc123", resume=False,
        )
        provider.assert_request_allowed()
        provider.mark_started()
        assert manager.load_goal("task-1")["goal_started"] is True
        save_goal = coord.save_goal

        def fail_completion(state):
            if state["status"] == "complete":
                raise OSError("injected Goal state write interruption")
            return save_goal(state)

        monkeypatch.setattr(coord, "save_goal", fail_completion)
        with pytest.raises(OSError, match="Goal state write interruption"):
            provider.complete("Verified report.")
        assert manager.load_goal("task-1")["status"] == "active"
        monkeypatch.setattr(coord, "save_goal", save_goal)
        completed = provider.complete("Verified report.")
        assert provider.complete("Do not replace the first evidence.") == completed
        assert manager.load_goal("task-1") == completed.to_dict()
        with pytest.raises(GoalCompleteError):
            provider.assert_request_allowed()
    finally:
        CheckpointCoordinator.deactivate(coord)
        manager.close()


def test_provider_resumes_legacy_budget_stop_without_changing_identity(tmp_path):
    manager = CheckpointManager("supervisor", checkpoints_root=tmp_path)
    original = _state().with_started()
    legacy = original.to_dict() | {
        "status": "budget_limited", "token_budget": 1,
        "prompt_tokens": 9000, "completion_tokens": 1000,
        "used_tokens": 10000, "remaining_tokens": 0,
    }
    with manager.task_storage("task-1") as storage:
        storage.atomic_write_json("goal.json", legacy)
    coord = CheckpointCoordinator.activate(manager, "task-1", "task", resume=True)
    try:
        with pytest.raises(ValueError, match="objective changed"):
            GoalStateProvider.initialize(
                config=GoalConfig(enabled=True), objective="Changed.",
                objective_fingerprint="different", resume=True,
            )
        provider = GoalStateProvider.initialize(
            config=normalize_goal_config({"goal": {"enabled": True, "token_budget": 0}}, source="legacy"),
            objective=original.objective,
            objective_fingerprint=original.objective_fingerprint, resume=True,
        )
        assert provider.snapshot() == original
        provider.assert_request_allowed()
        assert manager.load_goal("task-1") == original.to_dict()
    finally:
        CheckpointCoordinator.deactivate(coord)
        manager.close()


def test_goal_provider_binding_is_run_scoped():
    provider = GoalStateProvider(_state())
    assert get_current_goal_provider() is None
    with bind_goal_state_provider(provider):
        assert get_current_goal_provider(required=True) is provider
    assert get_current_goal_provider() is None
