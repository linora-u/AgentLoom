from pathlib import Path

import src.lib.smolagents.agent.yaml_agent_factory as yaml_agent_factory
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory, YamlConfiguredSupervisorAgent

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
WORKFLOW_INTRO = yaml_agent_factory.WORKFLOW_EXECUTION_INTRO
WORKFLOW_GUIDANCE = yaml_agent_factory.TASK_SPEC_WORKFLOW_GUIDANCE


def _make_supervisor_from_yaml(relative_yaml_path: str) -> YamlConfiguredSupervisorAgent:
    config = YamlAgentFactory._load_config_from_file(FIXTURE_ROOT / relative_yaml_path)
    supervisor = object.__new__(YamlConfiguredSupervisorAgent)
    supervisor._config = config
    supervisor._logger = None
    return supervisor


def test_transform_task_uses_task_spec_without_mermaid():
    supervisor = _make_supervisor_from_yaml("supervisor/test_shell_persist_supervisor.yaml")
    out = supervisor._transform_task("analyze this module")

    assert "Task specification (what you must follow in this task):" in out
    assert "<task_spec>" in out
    assert "</task_spec>" in out
    assert "<workflow>" not in out
    assert WORKFLOW_INTRO not in out
    assert WORKFLOW_GUIDANCE not in out
    assert "<task_request>" in out
    assert "analyze this module" in out


def test_transform_task_wraps_mermaid_in_workflow():
    supervisor = _make_supervisor_from_yaml("supervisor/test_supervisor_code_review_agent.yaml")
    out = supervisor._transform_task("run all checks")

    assert "<task_spec>" in out
    assert WORKFLOW_GUIDANCE in out
    assert WORKFLOW_INTRO in out
    assert "<workflow>" in out
    assert "\n  <workflow>\n" in out
    assert "\n    flowchart TD\n" in out
    assert "flowchart TD" in out
    assert "A[Receive review request]" in out
    assert "<task_request>" in out
    assert "run all checks" in out


def test_transform_tasks_keeps_single_string_workflow_behavior():
    supervisor = _make_supervisor_from_yaml("supervisor/test_shell_persist_supervisor.yaml")

    transformed_tasks = supervisor._transform_tasks("analyze this module")

    assert transformed_tasks == [supervisor._transform_task("analyze this module")]


def test_transform_tasks_list_workflow_returns_user_items_without_stage_wrappers():
    supervisor = object.__new__(YamlConfiguredSupervisorAgent)
    supervisor._config = {
        "name": "multi_workflow",
        "description": "Overall task description.",
        "workflow": [
            "First workflow item.\nUse exactly this instruction.",
            "Second workflow item.\nContinue from previous memory.",
        ],
    }
    supervisor._logger = None

    transformed_tasks = supervisor._transform_tasks("ignored task request")

    # List workflows return one item per step for sequential execution.
    assert len(transformed_tasks) == 2
    # First item contains the workflow text plus the task data in <inputs> block.
    assert "First workflow item.\nUse exactly this instruction." in transformed_tasks[0]
    assert "<inputs>" in transformed_tasks[0]
    assert "ignored task request" in transformed_tasks[0]
    # Subsequent items are raw workflow text without task data injection.
    assert transformed_tasks[1] == "Second workflow item.\nContinue from previous memory."
    # No task_spec wrapping or stage labels on any item.
    assert all("Task specification" not in task for task in transformed_tasks)
    assert all("Stage" not in task for task in transformed_tasks)


def test_transform_tasks_list_workflow_empty_task_returns_raw_items():
    supervisor = object.__new__(YamlConfiguredSupervisorAgent)
    supervisor._config = {
        "name": "multi_workflow",
        "description": "Overall task description.",
        "workflow": [
            "First workflow item.\nUse exactly this instruction.",
            "Second workflow item.\nContinue from previous memory.",
        ],
    }
    supervisor._logger = None

    transformed_tasks = supervisor._transform_tasks("")

    # When task is empty, no <inputs> block is injected.
    assert transformed_tasks == [
        "First workflow item.\nUse exactly this instruction.",
        "Second workflow item.\nContinue from previous memory.",
    ]


def test_goal_mode_merges_list_workflow_into_one_goal_context():
    supervisor = object.__new__(YamlConfiguredSupervisorAgent)
    supervisor._config = {
        "name": "goal_workflow",
        "description": "Deliver the complete change.",
        "workflow": [
            "Inspect the implementation.",
            "Implement and verify the change.",
        ],
        "goal": {"enabled": True},
    }
    supervisor._logger = None

    transformed_tasks = supervisor._transform_tasks("Add Goal mode.")

    assert len(transformed_tasks) == 1
    prompt = transformed_tasks[0]
    assert "1. Inspect the implementation." in prompt
    assert "2. Implement and verify the change." in prompt
    assert "Deliver the complete change." in prompt
    assert "Add Goal mode." in prompt
