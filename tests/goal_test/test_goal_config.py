from pathlib import Path

import pytest

from src.lib.goal import GoalConfig, normalize_goal_config
from src.lib.smolagents.agent.runtime_validation import (
    validate_runtime_agent_config,
    validate_runtime_worker_config,
)


def _config(**overrides):
    return {
        "name": "goal-test",
        "description": "Finish the requested work.",
        "workflow": "Inspect, implement, and verify.",
        "tools": [],
        **overrides,
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, GoalConfig(enabled=False, token_budget=None)),
        (False, GoalConfig(enabled=False, token_budget=None)),
        (True, GoalConfig(enabled=True, token_budget=None)),
        ({"enabled": False}, GoalConfig(enabled=False, token_budget=None)),
        ({"enabled": True}, GoalConfig(enabled=True, token_budget=None)),
        (
            {"enabled": True, "token_budget": 120_000},
            GoalConfig(enabled=True, token_budget=120_000),
        ),
    ],
)
def test_normalize_goal_config_accepts_only_supported_forms(raw, expected):
    config = {} if raw is None else {"goal": raw}
    assert normalize_goal_config(config, source="agent.yaml") == expected


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"token_budget": 10},
        {"enabled": "true"},
        {"enabled": True, "token_budget": None},
        {"enabled": True, "token_budget": True},
        {"enabled": True, "token_budget": "10"},
        {"enabled": True, "token_budget": 0},
        {"enabled": True, "token_budget": -1},
        {"enabled": False, "token_budget": 10},
        {"enabled": True, "unknown": 1},
        [],
        "true",
    ],
)
def test_normalize_goal_config_rejects_ambiguous_or_invalid_forms(raw):
    with pytest.raises(ValueError, match="goal"):
        normalize_goal_config({"goal": raw}, source="agent.yaml")


def test_runtime_supervisor_validation_accepts_goal(tmp_path: Path):
    validate_runtime_agent_config(
        _config(goal={"enabled": True, "token_budget": 100}),
        tmp_path / "supervisor.yaml",
        agent_root=tmp_path,
    )


@pytest.mark.parametrize("goal", [False, True, {"enabled": False}, {"enabled": True}])
def test_runtime_worker_validation_rejects_any_goal_key(tmp_path: Path, goal):
    config = _config(
        goal=goal,
        agent_function_schema={
            "description": "worker",
            "inputs": {"task": {"description": "task"}},
            "output": {"description": "result"},
        },
    )
    with pytest.raises(ValueError, match="Worker Agent.*goal"):
        validate_runtime_worker_config(
            config,
            tmp_path / "worker.yaml",
            agent_root=tmp_path,
        )
