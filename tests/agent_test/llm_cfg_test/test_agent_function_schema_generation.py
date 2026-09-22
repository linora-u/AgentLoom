import json
from inspect import signature
from pathlib import Path

from agentloom.application.factory import YamlAgentFactory, YamlConfiguredAgent
from agentloom.execution.tool_gateway import bind_tool

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"


def _build_worker(config: dict) -> YamlConfiguredAgent:
    class WorkerFixture(YamlConfiguredAgent):
        def __init__(self, config, **kwargs):
            self._config = config
            self._normalized = None
            self._validate_config()

        def run(self, query, additional_args=None):
            return f"RUN::{query}"

    return WorkerFixture(config)


def test_generated_function_signature_from_schema():
    config = {
        "name": "test_agent",
        "agent_runtime": "smolagents",
        "description": "test agent desc",
        "workflow": "test workflow",
        "tools": [],
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "传递给 worker 的具体 shell 执行指令或任务描述。",
                },
                "source": {
                    "type": "string",
                    "description": "请求来源标识",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }

    worker = _build_worker(config)
    tool = worker.agent_as_tool()

    sig = signature(tool)

    print(f"\nFunction name: {tool.__name__}")
    print(f"Function signature: {sig}")
    for param_name, param in sig.parameters.items():
        print(f"Parameter: {param_name}, Type: {param.annotation}, Default: {param.default}")
    print(f"Return type: {sig.return_annotation}")
    print(f"\nDocstring passed to LLM:\n{tool.__doc__}")

    assert tool.__name__ == "test_agent"
    assert list(sig.parameters.keys()) == ["query", "source"]
    assert sig.parameters["query"].annotation is not str
    assert sig.parameters["source"].default is None
    assert bind_tool(tool).definition.parameters == config["input_schema"]


def test_print_function_schema_generation_from_worker_yaml():
    worker_yaml = FIXTURE_ROOT / "worker/test_shell_persist_worker.yaml"
    config = YamlAgentFactory._load_config_from_file(worker_yaml)

    worker = _build_worker(config)
    tool_fn = worker.agent_as_tool()

    schema = bind_tool(tool_fn).definition

    print("\n=== Input (YAML) ===")
    print(f"path: {worker_yaml}")
    print(json.dumps(config["input_schema"], ensure_ascii=False, indent=2))

    print("\n=== Output (Function Schema) ===")
    print(json.dumps(dict(schema.parameters), ensure_ascii=False, indent=2))

    assert schema.name == "shell_worker"
    assert schema.description == config["description"].strip()
    assert schema.parameters["properties"]["query"]["type"] == "string"
    assert "query" in schema.parameters["required"]
    result = tool_fn("pwd")
    assert result == "RUN::pwd"


def test_generated_tool_includes_optional_inputs_in_payload_block():
    config = {
        "name": "test_agent",
        "agent_runtime": "smolagents",
        "description": "test agent desc",
        "workflow": "test workflow",
        "tools": [],
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "primary request"},
                "tag": {"type": "string", "description": "optional tag"},
                "retry": {"type": "integer", "description": "retry count"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }

    worker = _build_worker(config)
    tool = worker.agent_as_tool()

    print("\n=== Invocation Input ===")
    print({"query": "run command", "tag": "nightly", "retry": 2})
    result = tool(query="run command", tag="nightly", retry=2)
    print("\n=== Invocation Output ===")
    print(result)

    assert result == 'RUN::{"query":"run command","retry":2,"tag":"nightly"}'
