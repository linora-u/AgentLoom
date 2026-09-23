import json
from pathlib import Path

import pytest
from agentloom.application.factory import (
    YamlAgentFactory,
    YamlConfiguredAgent,
    YamlConfiguredSupervisorAgent,
)
from agentloom.execution.logging import (
    get_global_logger,
    initialize_global_logger_once,
    set_global_logger,
)
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
    prev = get_global_logger(create_if_missing=False)
    if prev is None:
        initialize_global_logger_once("test_yaml_tool_defaults")
    yield
    if prev is None:
        set_global_logger(prev)


def _make_worker(config: dict) -> YamlConfiguredAgent:
    class WorkerFixture(YamlConfiguredAgent):
        def __init__(self, config, **kwargs):
            self._config = config
            self._normalized = None

        def run(self, query, additional_args=None):
            return f"RUN::{query}"

    return WorkerFixture(config)


def test_agent_as_tool_uses_default_task_schema_and_plain_user_input():
    worker = _make_worker(
        {
            "name": "demo_worker",
            "agent_runtime": "smolagents",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
        }
    )

    worker._validate_config()
    tool = worker.agent_as_tool()
    binding = bind_tool(tool)

    assert binding.definition.name == "demo_worker"
    assert binding.definition.description == "worker desc"
    assert binding.definition.parameters == {
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
    assert tool("hello") == "RUN::hello"


def test_agent_as_tool_preserves_typed_multi_field_input_as_bare_json():
    worker = _make_worker(
        {
            "name": "demo_worker",
            "agent_runtime": "smolagents",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "count": {"type": "integer"},
                    "dry_run": {"type": "boolean"},
                },
                "required": ["query", "count"],
                "additionalProperties": False,
            },
        }
    )

    worker._validate_config()
    tool = worker.agent_as_tool()

    assert tool(query="hello", count=2, dry_run=True) == (
        'RUN::{"count":2,"dry_run":true,"query":"hello"}'
    )
    with pytest.raises(Exception, match="integer"):
        tool(query="hello", count="two")


def test_agent_as_tool_allows_schema_valid_additional_properties():
    worker = _make_worker(
        {
            "name": "open_worker",
            "agent_runtime": "smolagents",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": {"type": "integer"},
            },
        }
    )

    worker._validate_config()
    tool = worker.agent_as_tool()

    assert tool(query="hello", priority=2) == (
        'RUN::{"priority":2,"query":"hello"}'
    )
    with pytest.raises(Exception, match="integer"):
        tool(query="hello", priority="high")


def test_agent_as_tool_accepts_json_property_names_that_are_not_python_identifiers():
    worker = _make_worker(
        {
            "name": "external_id_worker",
            "agent_runtime": "smolagents",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
            "input_schema": {
                "type": "object",
                "properties": {"user-id": {"type": "string"}},
                "required": ["user-id"],
                "additionalProperties": False,
            },
        }
    )

    worker._validate_config()
    tool = worker.agent_as_tool()

    assert bind_tool(tool).definition.parameters == worker._config["input_schema"]
    assert tool(**{"user-id": "alice"}) == "RUN::alice"


def test_agent_as_tool_supports_an_object_root_local_reference():
    input_schema = {
        "$defs": {
            "request": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["query", "count"],
                "additionalProperties": False,
            },
        },
        "$ref": "#/$defs/request",
    }
    worker = _make_worker(
        {
            "name": "referenced_worker",
            "agent_runtime": "smolagents",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
            "input_schema": input_schema,
        }
    )

    worker._validate_config()
    tool = worker.agent_as_tool()

    assert bind_tool(tool).definition.parameters == input_schema
    assert tool(query="hello", count=2) == 'RUN::{"count":2,"query":"hello"}'


def test_agent_as_tool_preserves_structured_result():
    worker = _make_worker(
        {
            "name": "demo_worker",
            "agent_runtime": "smolagents",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
        }
    )
    type(worker).run = lambda self, _query, additional_args=None: {
        "answer": '用户说"拍照"，工具返回成功。',
    }
    worker._validate_config()

    assert worker.agent_as_tool()("hello") == {
        "answer": '用户说"拍照"，工具返回成功。',
    }


def test_mermaid_workflow_is_not_added_to_worker_user_input():
    worker = _make_worker(
        {
            "name": "demo_worker",
            "agent_runtime": "smolagents",
            "description": "worker desc",
            "tools": [],
            "workflow": "```mermaid\nflowchart TD\nA-->B\n```",
        }
    )
    worker._validate_config()

    assert worker.agent_as_tool()("hello") == "RUN::hello"


def test_removed_agent_function_schema_is_rejected():
    worker = _make_worker(
        {
            "name": "demo_worker",
            "agent_runtime": "smolagents",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
            "agent_function_schema": {},
        }
    )

    with pytest.raises(ValueError, match="agent_function_schema was removed"):
        worker._validate_config()


def test_factory_always_exports_a_worker_tool():
    tool = YamlAgentFactory.create_agent_as_tool(
        {
            "name": "demo_worker",
            "agent_runtime": "smolagents",
            "description": "worker desc",
            "tools": [],
            "workflow": "demo workflow",
        },
        model_binding=make_test_model_binding(),
    )

    assert tool is not None
    assert tool.__name__ == "demo_worker"


def test_supervisor_respects_empty_toolsets():
    supervisor = YamlConfiguredSupervisorAgent(
        config={
            "name": "demo",
            "agent_runtime": "smolagents",
            "description": "demo supervisor",
            "workflow": "demo workflow",
            "tools": [],
            "worker_agents": [],
            "toolsets": [],
            "_yaml_file_path": str(
                Path(
                    "applications/test_demo/workflows/"
                    "test_multi_workflow_agent.yaml"
                ).resolve()
            ),
        },
        model="dummy",
    )

    assert supervisor._get_tools() == []


def test_multi_field_projection_is_stable_json():
    config = {
        "name": "stable_json",
        "agent_runtime": "smolagents",
        "description": "stable",
        "workflow": "workflow",
        "tools": [],
        "input_schema": {
            "type": "object",
            "properties": {
                "z": {"type": "array"},
                "a": {"type": "object"},
            },
            "required": ["z", "a"],
            "additionalProperties": False,
        },
    }
    worker = _make_worker(config)
    worker._validate_config()

    output = worker.agent_as_tool()(z=[1], a={"x": True})

    assert output == 'RUN::{"a":{"x":true},"z":[1]}'
    assert json.loads(output.removeprefix("RUN::")) == {
        "a": {"x": True},
        "z": [1],
    }
