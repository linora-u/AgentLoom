from pathlib import Path

import pytest

from src.runner import validate_required_yaml_fields


def _base_config() -> dict:
    return {
        "name": "demo_agent",
        "description": "demo",
        "workflow": "Run the workflow.",
    }


def test_runner_validation_accepts_list_workflow() -> None:
    config = {
        **_base_config(),
        "workflow": [
            "First workflow item.",
            "Second workflow item.",
        ],
    }

    validate_required_yaml_fields(config, Path("agent.yaml"))


@pytest.mark.parametrize(
    "workflow_value",
    [
        [],
        ["First workflow item.", ""],
        ["First workflow item.", "   "],
        ["First workflow item.", 2],
        {"bad": True},
    ],
)
def test_runner_validation_rejects_invalid_workflow_values(workflow_value) -> None:
    config = {**_base_config(), "workflow": workflow_value}

    with pytest.raises(ValueError, match="workflow"):
        validate_required_yaml_fields(config, Path("agent.yaml"))
