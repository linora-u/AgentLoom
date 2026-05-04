#!/usr/bin/env python3
"""Direct worker-agent validation from YAML (no supervisor).

This script loads a worker YAML under applications/test_demo/workflows,
creates worker-as-tool directly, invokes it once, and prints logs/output
for manual review of input/output behavior.
"""

import os
import sys
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from smolagents.tools import get_json_schema

from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory


def _default_yaml_path() -> Path:
    current_dir = Path(__file__).parent
    return current_dir / "workflows" / "worker_agents" / "test_worker_schema_direct.yaml"


def run_worker_direct_log_demo(worker_yaml: Path) -> None:
    print(f"Loading worker yaml: {worker_yaml}")
    if not worker_yaml.exists():
        raise FileNotFoundError(f"Worker yaml not found: {worker_yaml}")

    config = YamlAgentFactory._load_config_from_file(worker_yaml)
    print("\n--- agent_function_schema ---")
    print(config.get("agent_function_schema"))

    tool_fn = YamlAgentFactory.create_agent_as_tool(config)

    print("\n--- Registration ---")
    print(f"tool registered: {tool_fn is not None}")
    if tool_fn is None:
        print("No worker tool registered from this YAML.")
        return
    print(f"Tool name: {tool_fn.__name__}")
    print(f"Tool docstring:\n{tool_fn.__doc__}")

    schema = get_json_schema(tool_fn)["function"]
    print("\n--- Generated Function Schema ---")
    print(schema)
    parameters_schema = schema.get("parameters") or {}
    print(
        "Schema note: parameters.type represents the function argument container, "
        f"current value={parameters_schema.get('type')!r} (expected 'object')."
    )
    return_schema = schema.get("return") or {}
    print(f"Schema return: type={return_schema.get('type')!r}, description={return_schema.get('description')!r}")

    print("\n--- Direct Invocation (manual review target) ---")
    expected_keys = {
        "query",
        "tag",
        "scene",
        "retry_count",
        "dry_run",
        "threshold",
        "context",
        "checkpoints",
    }
    schema_keys = set((schema.get("parameters") or {}).get("properties", {}).keys())
    if schema_keys != expected_keys:
        raise ValueError(
            f"Generated function schema keys mismatch. expected={sorted(expected_keys)}, got={sorted(schema_keys)}"
        )
    if parameters_schema.get("type") != "object":
        raise ValueError(f"Expected parameters container type to be 'object', got: {parameters_schema.get('type')}")
    schema_properties = (schema.get("parameters") or {}).get("properties", {})
    non_string_fields = [
        key for key, value in schema_properties.items()
        if not isinstance(value, dict) or value.get("type") != "string"
    ]
    if non_string_fields:
        raise ValueError(f"Expected all schema parameter types to be string, got non-string fields: {non_string_fields}")
    if return_schema.get("type") != "string":
        raise ValueError(f"Expected return type to be string, got: {return_schema.get('type')}")
    if not isinstance(return_schema.get("description"), str) or not return_schema.get("description", "").strip():
        raise ValueError("Expected return.description to be a non-empty string")

    demo_inputs = {
        "query": "请把我传给你的所有参数完整输出出来（包含每个参数名和参数值）。",
        "tag": "demo-tag",
        "scene": "manual-review",
        "retry_count": "1",
        "dry_run": "true",
        "threshold": "1.25",
        "context": "{\"demo_key\": \"demo-context\"}",
        "checkpoints": "[\"schema\", \"invoke\", \"output\"]",
    }
    print("\n--- Manual Runtime Log Checklist ---")
    print("1) Prompt starts with 'Task specification (what you must follow in this task):'")
    print("2) Prompt contains <task_spec>, <inputs>, and <output> sections in this order")
    print("3) <workflow> appears only when workflow contains mermaid block(s)")
    print("4) Final model output satisfies return.description contract")
    print(f"Invocation input kwargs: {demo_inputs}")
    result = tool_fn(**demo_inputs)
    if not isinstance(result, str):
        raise ValueError(f"Expected tool return value to be str, got: {type(result)}")
    print("Invocation returned output:")
    print(result)


def main() -> None:
    worker_yaml = _default_yaml_path()
    if len(sys.argv) > 1:
        worker_yaml = Path(sys.argv[1]).expanduser().resolve()
    run_worker_direct_log_demo(worker_yaml)


if __name__ == "__main__":
    main()
