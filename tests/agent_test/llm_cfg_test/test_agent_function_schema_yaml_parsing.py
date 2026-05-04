from pathlib import Path

import pytest

from src.lib.logging import initialize_global_logger_once, get_global_logger, set_global_logger
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory


@pytest.fixture(autouse=True)
def _ensure_global_logger():
    """Ensure a global logger exists for agent construction."""
    prev = get_global_logger(create_if_missing=False)
    if prev is None:
        initialize_global_logger_once("test_yaml_parsing")
    yield
    if prev is None:
        set_global_logger(prev)

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"


def test_real_worker_yaml_parses_and_registers_tool():
    worker_yaml = FIXTURE_ROOT / "worker/test_shell_persist_worker.yaml"

    config = YamlAgentFactory._load_config_from_file(worker_yaml)
    tool = YamlAgentFactory.create_agent_as_tool(config, model=object())

    assert tool is not None
    assert tool.__name__ == "shell_worker"
    assert "Args:" in (tool.__doc__ or "")
    assert "Returns:" in (tool.__doc__ or "")


def test_worker_without_agent_function_schema_is_not_registered():
    config = {
        "name": "demo_worker",
        "description": "worker desc",
        "tools": [],
        "workflow": "demo workflow",
    }

    tool = YamlAgentFactory.create_agent_as_tool(config, model=object())
    assert tool is None


def test_invalid_agent_function_schema_raises_value_error():
    config = {
        "name": "demo_worker",
        "description": "worker desc",
        "tools": [],
        "workflow": "demo workflow",
        "agent_function_schema": {
            "description": "invalid schema",
            "inputs": {
                "query": {
                    # missing description
                }
            },
            "output": {"description": "result text"},
        },
    }

    with pytest.raises(ValueError, match="description"):
        YamlAgentFactory.create_agent_as_tool(config, model=object())
