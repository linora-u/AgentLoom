from src.lib.smolagents.agent.yaml_agent_factory import YamlConfiguredAgent


def test_agent_function_schema_docstring_rendering():
    config = {
        "name": "test_agent",
        "description": "test agent desc",
        "workflow": "test workflow",
        "tools": [],
        "agent_function_schema": {
            "description": "子 agent，用于隔离 shell 执行环境。",
            "inputs": {
                "query": {
                    "description": "传递给 worker 的具体 shell 执行指令或任务描述。",
                }
            },
            "output": {
                "description": "shell 的真实执行输出结果文本。"
            },
        },
    }

    worker = object.__new__(YamlConfiguredAgent)
    worker._config = config
    worker._normalized = None
    worker._validate_config()

    worker.run = lambda q: f"RUN::{q}"
    worker.process_tool_query = lambda q: q
    tool = worker.agent_as_tool()

    assert tool is not None

    assert "Args:" in (tool.__doc__ or "")
    assert "Returns:" in (tool.__doc__ or "")
    assert "query" in (tool.__doc__ or "")
