from agentloom.app.factory import YamlConfiguredAgent


def test_native_input_schema_docstring_uses_agent_metadata():
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
                }
            },
            "required": ["query"],
            "additionalProperties": False,
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

    assert (tool.__doc__ or "").startswith("test agent desc")
    assert "Args:" in (tool.__doc__ or "")
    assert "query" in (tool.__doc__ or "")
    assert "传递给 worker" in (tool.__doc__ or "")
