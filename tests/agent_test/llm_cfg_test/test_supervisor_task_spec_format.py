from pathlib import Path

from agentloom.app.factory import (
    YamlAgentFactory,
    YamlConfiguredSupervisorAgent,
)

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"


def _make_supervisor_from_yaml(
    relative_yaml_path: str,
) -> YamlConfiguredSupervisorAgent:
    config = YamlAgentFactory._load_config_from_file(
        FIXTURE_ROOT / relative_yaml_path
    )
    supervisor = object.__new__(YamlConfiguredSupervisorAgent)
    supervisor._config = config
    supervisor._logger = None
    return supervisor


def test_supervisor_passes_the_runtime_task_without_prompt_wrapping() -> None:
    supervisor = _make_supervisor_from_yaml(
        "supervisor/test_shell_persist_supervisor.yaml"
    )

    assert supervisor._transform_task("analyze this module") == "analyze this module"
    assert supervisor._transform_tasks("analyze this module") == [
        "analyze this module"
    ]


def test_supervisor_does_not_parse_mermaid_from_workflow() -> None:
    supervisor = _make_supervisor_from_yaml(
        "supervisor/test_supervisor_code_review_agent.yaml"
    )

    assert supervisor._transform_task("run all checks") == "run all checks"


def test_supervisor_preserves_an_absent_runtime_task() -> None:
    supervisor = _make_supervisor_from_yaml(
        "supervisor/test_shell_persist_supervisor.yaml"
    )

    assert supervisor._transform_task(None) is None
    assert supervisor._transform_tasks(None) == [None]
