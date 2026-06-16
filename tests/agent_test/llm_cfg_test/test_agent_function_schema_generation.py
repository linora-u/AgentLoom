import json
from inspect import signature
from pathlib import Path

from smolagents.tools import get_json_schema

import src.lib.smolagents.agent.yaml_agent_factory as yaml_agent_factory
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory, YamlConfiguredAgent

FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"
WORKFLOW_INTRO = yaml_agent_factory.WORKFLOW_EXECUTION_INTRO
WORKFLOW_GUIDANCE = yaml_agent_factory.TASK_SPEC_WORKFLOW_GUIDANCE


def _build_worker(config: dict) -> YamlConfiguredAgent:
    worker = object.__new__(YamlConfiguredAgent)
    worker._config = config
    worker._normalized = None
    worker._validate_config()
    worker.run = lambda q, additional_args=None: f"RUN::{q}"
    worker.process_tool_query = lambda q: q
    return worker


def test_generated_function_signature_from_schema():
    config = {
        "name": "test_agent",
        "description": "test agent desc",
        "workflow": "test workflow",
        "tools": [],
        "agent_function_schema": {
            "description": "子 agent，用于隔离 shell 执行环境。",
            "inputs": {
                "query": {"description": "传递给 worker 的具体 shell 执行指令或任务描述。"},
                "source": {"description": "请求来源标识", "required": False},
            },
            "output": {"description": "worker 输出结果文本"},
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
    assert sig.parameters["query"].annotation is str
    assert sig.parameters["source"].default is None
    assert sig.return_annotation is str


def test_print_function_schema_generation_from_worker_yaml():
    worker_yaml = FIXTURE_ROOT / "worker/test_shell_persist_worker.yaml"
    config = YamlAgentFactory._load_config_from_file(worker_yaml)

    worker = _build_worker(config)
    tool_fn = worker.agent_as_tool()

    schema = get_json_schema(tool_fn)["function"]

    print("\n=== Input (YAML) ===")
    print(f"path: {worker_yaml}")
    print(json.dumps(config["agent_function_schema"], ensure_ascii=False, indent=2))

    print("\n=== Output (Function Schema) ===")
    print(json.dumps(schema, ensure_ascii=False, indent=2))

    assert schema["name"] == "shell_worker"
    assert schema["parameters"]["properties"]["query"]["type"] == "string"
    assert "query" in schema["parameters"]["required"]
    assert schema["return"]["type"] == "string"
    result = tool_fn("pwd")
    assert result.startswith("RUN::Task specification (what you must follow in this task):")
    assert "<task_spec>" in result
    assert "<inputs>" in result
    assert "<output>" in result
    assert "Expected output (what result you must produce):" in result
    assert config["agent_function_schema"]["output"]["description"].strip() in result
    assert "<workflow>" not in result
    assert WORKFLOW_INTRO not in result
    assert WORKFLOW_GUIDANCE not in result
    assert result.index("<task_spec>") < result.index("<inputs>") < result.index("<output>")
    assert "1. 传递给 worker 的具体 shell 执行指令或任务描述。: pwd" in result


def test_generated_tool_includes_optional_inputs_in_payload_block():
    config = {
        "name": "test_agent",
        "description": "test agent desc",
        "workflow": "test workflow",
        "tools": [],
        "agent_function_schema": {
            "description": "demo worker",
            "inputs": {
                "query": {"description": "primary request"},
                "tag": {"description": "optional tag", "required": False},
                "retry": {"description": "retry count", "required": False},
            },
            "output": {"description": "worker result"},
        },
    }

    worker = _build_worker(config)
    tool = worker.agent_as_tool()

    print("\n=== Invocation Input ===")
    print({"query": "run command", "tag": "nightly", "retry": 2})
    result = tool(query="run command", tag="nightly", retry=2)
    print("\n=== Invocation Output ===")
    print(result)

    assert "<inputs>" in result
    assert "<output>" in result
    assert "Expected output (what result you must produce):" in result
    assert "worker result" in result
    assert "<task_spec>" in result
    assert "<workflow>" not in result
    assert WORKFLOW_INTRO not in result
    assert WORKFLOW_GUIDANCE not in result
    assert result.index("<task_spec>") < result.index("<inputs>") < result.index("<output>")
    assert "1. primary request: run command" in result
    assert "2. optional tag: nightly" in result
    assert "3. retry count: 2" in result
