from pathlib import Path

import pytest
from agentloom.application.factory import YamlAgentFactory
from agentloom.execution.logging import get_global_logger, initialize_global_logger_once, set_global_logger
from agentloom.execution.model_binding import ModelTurnBinding
from agentloom.execution.model_protocol import ModelTurnResult
from agentloom.execution.tool_gateway import bind_tool


class _NoopAdapter:
    adapter_id = "openai_chat"

    def turn(self, _request):
        return ModelTurnResult()


def make_test_model_binding():
    return ModelTurnBinding(
        model_type="test",
        model_id="test/model",
        adapter=_NoopAdapter(),
    )


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
    tool = YamlAgentFactory.create_agent_as_tool(
        config,
        model_binding=make_test_model_binding(),
    )

    assert tool is not None
    assert tool.__name__ == "shell_worker"
    assert "Args:" in (tool.__doc__ or "")
    assert bind_tool(tool).definition.description == config["description"].strip()


def test_worker_without_input_schema_registers_default_task_tool():
    config = {
        "name": "demo_worker",
        "agent_runtime": "smolagents",
        "description": "worker desc",
        "tools": [],
        "workflow": "demo workflow",
    }

    tool = YamlAgentFactory.create_agent_as_tool(
        config,
        model_binding=make_test_model_binding(),
    )
    assert tool is not None
    assert bind_tool(tool).definition.parameters == {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Task for this Agent.",
            },
        },
        "required": ["task"],
        "additionalProperties": False,
    }


def test_explicit_input_schema_preserves_typed_tool_contract():
    config = {
        "name": "demo_worker",
        "agent_runtime": "smolagents",
        "description": "worker desc",
        "tools": [],
        "workflow": "demo workflow",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer"},
                "dry_run": {"type": "boolean"},
            },
            "required": ["count"],
            "additionalProperties": False,
        },
    }

    tool = YamlAgentFactory.create_agent_as_tool(
        config,
        model_binding=make_test_model_binding(),
    )
    assert tool is not None
    assert bind_tool(tool).definition.parameters == config["input_schema"]


def test_removed_agent_function_schema_is_rejected():
    config = {
        "name": "demo_worker",
        "agent_runtime": "smolagents",
        "description": "worker desc",
        "tools": [],
        "workflow": "demo workflow",
        "agent_function_schema": {},
    }

    with pytest.raises(ValueError, match="agent_function_schema was removed"):
        YamlAgentFactory.create_agent_as_tool(
            config,
            model_binding=make_test_model_binding(),
        )
