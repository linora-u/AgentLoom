#!/usr/bin/env python3
"""
Direct worker tool validation (no supervisor).

This script loads a worker YAML, creates worker-as-tool directly,
prints registration/schema information, then calls the tool once.
"""
import os
import sys
from pathlib import Path

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from smolagents.tools import get_json_schema

from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory


def run_direct_worker_demo() -> None:
    current_dir = Path(__file__).parent
    worker_yaml = current_dir / "workflows" / "worker_agents" / "test_worker_schema_direct.yaml"

    print(f"Loading worker yaml: {worker_yaml}")
    if not worker_yaml.exists():
        print(f"Error: worker yaml not found: {worker_yaml}")
        return

    config = YamlAgentFactory._load_config_from_file(worker_yaml)
    print("\n--- YAML agent_function_schema ---")
    print(config.get("agent_function_schema"))

    tool_fn = YamlAgentFactory.create_agent_as_tool(config)

    print("\n--- Registration ---")
    print(f"tool registered: {tool_fn is not None}")
    if tool_fn is None:
        print("No tool registered. Check agent_function_schema.")
        return
    print(f"tool name: {tool_fn.__name__}")
    print(f"tool docstring:\n{tool_fn.__doc__}")

    schema = get_json_schema(tool_fn)["function"]
    print("\n--- Generated Function Schema ---")
    print(schema)

    print("\n--- Direct Invocation ---")
    try:
        result = tool_fn(query="请回显这是一条直连输入", tag="direct-demo")
        print("Call succeeded.")
        print("Output:")
        print(result)
    except Exception as e:
        print("Call failed.")
        print(f"Error: {e}")


if __name__ == "__main__":
    run_direct_worker_demo()
