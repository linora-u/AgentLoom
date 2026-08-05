import threading

import pytest

from src.lib.checkpoint import CheckpointManager
from src.lib.checkpoint.coordinator import CheckpointCoordinator
from src.lib.goal import GoalConfig, GoalState
from src.lib.goal.provider import (
    GoalBudgetLimitedError,
    GoalStateProvider,
    bind_goal_state_provider,
    get_current_goal_provider,
)


def _state(*, budget=100) -> GoalState:
    return GoalState.create(
        objective="Ship Goal mode.",
        objective_fingerprint="abc123",
        token_budget=budget,
    )


def test_goal_state_soft_budget_counts_prompt_and_completion_tokens():
    state = _state(budget=100).with_usage(80, 25)

    assert state.used_tokens == 105
    assert state.status == "budget_limited"


def test_goal_state_exact_budget_boundary_is_limited():
    state = _state(budget=100).with_usage(75, 25)

    assert state.used_tokens == 100
    assert state.to_dict()["remaining_tokens"] == 0
    assert state.status == "budget_limited"


def test_goal_state_unlimited_budget_never_limits():
    state = _state(budget=None).with_usage(1_000_000, 500_000)

    assert state.used_tokens == 1_500_000
    assert state.status == "active"


def test_goal_state_resume_budget_may_increase_or_be_removed_but_not_decrease():
    state = _state(budget=100).with_usage(90, 20)

    assert state.with_resumed_budget(200).status == "active"
    assert state.with_resumed_budget(None).status == "active"
    with pytest.raises(ValueError, match="cannot be decreased"):
        state.with_resumed_budget(50)

    with pytest.raises(ValueError, match="cannot be decreased"):
        _state(budget=None).with_resumed_budget(200)


def test_goal_provider_serializes_parallel_in_flight_usage_after_soft_crossing():
    provider = GoalStateProvider(_state(budget=100))
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def finish_in_flight_response() -> None:
        try:
            provider.assert_request_allowed()
            barrier.wait(timeout=2)
            provider.record_usage(prompt_tokens=25, completion_tokens=10)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=finish_in_flight_response) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert errors == []
    assert provider.snapshot().used_tokens == 140
    assert provider.snapshot().status == "budget_limited"
    with pytest.raises(GoalBudgetLimitedError):
        provider.assert_request_allowed()


def test_checkpoint_goal_state_roundtrip_and_corruption_is_fatal(tmp_path):
    manager = CheckpointManager("supervisor", checkpoints_root=tmp_path)
    state = _state()

    manager.save_goal("task-1", state.to_dict())
    assert manager.load_goal("task-1") == state.to_dict()

    goal_path = tmp_path / "task-1" / "goal.json"
    goal_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="corrupt Goal state"):
        manager.load_goal("task-1")


def test_provider_persists_usage_and_fences_later_requests(tmp_path):
    manager = CheckpointManager("supervisor", checkpoints_root=tmp_path)
    coord = CheckpointCoordinator.activate(manager, "task-1", "task")
    try:
        provider = GoalStateProvider.initialize(
            config=GoalConfig(enabled=True, token_budget=100),
            objective="Ship Goal mode.",
            objective_fingerprint="abc123",
            resume=False,
        )
        provider.assert_request_allowed()
        provider.record_usage(prompt_tokens=90, completion_tokens=20)

        assert provider.snapshot().status == "budget_limited"
        with pytest.raises(GoalBudgetLimitedError):
            provider.assert_request_allowed()
        assert manager.load_goal("task-1")["used_tokens"] == 110
    finally:
        CheckpointCoordinator.deactivate(coord)


def test_provider_resume_rejects_changed_objective_and_reactivates_higher_budget(tmp_path):
    manager = CheckpointManager("supervisor", checkpoints_root=tmp_path)
    manager.save_goal("task-1", _state(budget=100).with_usage(90, 20).to_dict())
    coord = CheckpointCoordinator.activate(manager, "task-1", "task", resume=True)
    try:
        with pytest.raises(ValueError, match="objective changed"):
            GoalStateProvider.initialize(
                config=GoalConfig(enabled=True, token_budget=200),
                objective="Changed.",
                objective_fingerprint="different",
                resume=True,
            )

        provider = GoalStateProvider.initialize(
            config=GoalConfig(enabled=True, token_budget=200),
            objective="Ship Goal mode.",
            objective_fingerprint="abc123",
            resume=True,
        )
        assert provider.snapshot().status == "active"
        assert provider.snapshot().used_tokens == 110
    finally:
        CheckpointCoordinator.deactivate(coord)


def test_goal_provider_binding_is_run_scoped():
    provider = GoalStateProvider(_state())
    assert get_current_goal_provider() is None

    with bind_goal_state_provider(provider):
        assert get_current_goal_provider(required=True) is provider

    assert get_current_goal_provider() is None
