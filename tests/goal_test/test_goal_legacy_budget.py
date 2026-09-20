"""Old budget metadata is inert while Goal execution remains available."""

import pytest

from agentloom.runtime.goal import GoalConfig, normalize_goal_config, validate_goal_state


@pytest.mark.parametrize("budget", [1, 0, -1, None, True, "ignored", {"old": "format"}])
@pytest.mark.parametrize("enabled", [False, True])
def test_legacy_budget_is_silently_ignored(budget, enabled):
    config = normalize_goal_config(
        {"goal": {"enabled": enabled, "token_budget": budget}}, source="legacy.yaml"
    )
    assert config == GoalConfig(enabled=enabled)
    assert not hasattr(config, "token_budget")


@pytest.mark.parametrize("status, expected", [("budget_limited", "active"), ("active", "active"), ("complete", "complete")])
def test_legacy_checkpoint_budget_fields_do_not_affect_goal_state(status, expected):
    state = validate_goal_state({
        "schema_version": 1,
        "goal_id": "goal_legacy",
        "objective": "Finish the report.",
        "objective_fingerprint": "abc",
        "status": status,
        "goal_started": True,
        "created_at": "2026-09-20T00:00:00Z",
        "updated_at": "2026-09-20T00:00:00Z",
        "completed_at": "2026-09-20T00:00:00Z" if status == "complete" else None,
        "evidence": "report verified" if status == "complete" else None,
        "token_budget": "obsolete",
        "prompt_tokens": -1,
        "completion_tokens": None,
        "used_tokens": "obsolete",
        "remaining_tokens": -100,
    })
    assert state.goal_id == "goal_legacy"
    assert state.status == expected
    assert state.goal_started is True
    assert not {"token_budget", "used_tokens", "remaining_tokens", "prompt_tokens", "completion_tokens"} & state.to_dict().keys()
