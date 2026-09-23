from pathlib import Path

import agentloom.execution.goal as goal_contract
import pytest
from agentloom.application.readiness import (
    validate_runtime_agent_config,
    validate_runtime_worker_config,
)
from agentloom.execution.goal import (
    GoalConfig,
    build_goal_objective,
    normalize_goal_config,
)


def _config(**overrides):
    return {
        "name": "goal-test",
        "agent_runtime": "smolagents",
        "description": "Finish the requested work.",
        "workflow": "Inspect, implement, and verify.",
        "tools": [],
        **overrides,
    }


def test_goal_contract_has_no_list_workflow_normalizer():
    assert not hasattr(goal_contract, "normalize_workflow_for_goal")


def test_goal_objective_excludes_agent_description_metadata():
    assert build_goal_objective(
        workflow="Inspect, implement, and verify.",
        task="Repair the release.",
    ) == (
        "Workflow:\nInspect, implement, and verify.\n\n"
        "Runtime request:\nRepair the release."
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, GoalConfig(enabled=False)),
        (False, GoalConfig(enabled=False)),
        (True, GoalConfig(enabled=True)),
        ({"enabled": False}, GoalConfig(enabled=False)),
        ({"enabled": True}, GoalConfig(enabled=True)),
        (
            {"enabled": True, "token_budget": 120_000},
            GoalConfig(enabled=True),
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
        input_schema={
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "required": ["task"],
            "additionalProperties": False,
        },
    )
    with pytest.raises(ValueError, match="Worker Agent.*goal"):
        validate_runtime_worker_config(
            config,
            tmp_path / "worker.yaml",
            agent_root=tmp_path,
        )
