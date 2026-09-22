"""Tests for fixed tool arguments configured in Agent YAML."""

from __future__ import annotations

import inspect

import pytest

from agentloom.application import factory as yaml_agent_factory
from agentloom.application.factory import YamlAgentFactory
from agentloom.runtimes.smolagents.tools.tools import ensure_tool_wrapped


def sample_tool(prompt: str, cwd: str = ".", sandbox: str = "", search: str = "") -> str:
    """Sample tool used to verify fixed argument binding.

    Args:
        prompt: Task prompt.
        cwd: Working directory.
        sandbox: Sandbox mode.
        search: Search flag.

    Returns:
        Encoded argument values.
    """
    return f"prompt={prompt};cwd={cwd};sandbox={sandbox};search={search}"


def test_fixed_args_are_hidden_from_llm_schema_and_applied(monkeypatch):
    monkeypatch.setattr(yaml_agent_factory, "resolve_tool_function", lambda name: sample_tool)

    tools, _ = YamlAgentFactory.get_tools_from_config(
        {
            "tools": [
                {
                    "name": "sample_tool",
                    "fixed_args": {
                        "cwd": "/repo",
                        "sandbox": "workspace-write",
                        "search": "false",
                    },
                }
            ]
        },
        effective_agent_config={"default_toolsets": []},
    )

    tool_func = tools[0]
    assert list(inspect.signature(tool_func).parameters) == ["prompt"]

    wrapped_tool = ensure_tool_wrapped([tool_func])[0]
    assert set(wrapped_tool.inputs) == {"prompt"}

    result = tool_func(prompt="summarize", cwd="/tmp", sandbox="danger-full-access", search="true")
    assert result == "prompt=summarize;cwd=/repo;sandbox=workspace-write;search=false"


def test_explicit_tool_config_overrides_same_named_default_toolset(tmp_path):
    target = tmp_path / "configured.txt"
    tools, _ = YamlAgentFactory.get_tools_from_config(
        {
            "tools": [
                {
                    "name": "write_file",
                    "fixed_args": {"file_path": str(target)},
                }
            ]
        },
        effective_agent_config={"default_toolsets": ["core_file"]},
    )

    selected = [tool for tool in tools if tool.__name__ == "write_file"]
    assert len(selected) == 1
    tool_func = selected[0]
    assert "file_path" not in inspect.signature(tool_func).parameters
    tool_func(content="configured target", file_path=str(tmp_path / "ignored.txt"))
    assert target.read_text() == "configured target"
    assert not (tmp_path / "ignored.txt").exists()
    from agentloom.execution.tool_gateway import bind_tool
    manifest = bind_tool(tool_func).manifest_entry
    assert manifest.fixed_arguments == {"file_path": str(target)}


def test_dynamic_tool_fixed_args_use_yaml_name_as_exposed_tool_name():
    tools, _ = YamlAgentFactory.get_tools_from_config(
        {
            "tools": [
                {
                    "name": "first_task",
                    "module": __name__,
                    "function": "sample_tool",
                    "fixed_args": {
                        "prompt": "first prompt",
                        "cwd": "/repo",
                        "sandbox": "read-only",
                        "search": "false",
                    },
                },
                {
                    "name": "second_task",
                    "module": __name__,
                    "function": "sample_tool",
                    "fixed_args": {
                        "prompt": "second prompt",
                        "cwd": "/repo",
                        "sandbox": "workspace-write",
                        "search": "true",
                    },
                },
            ]
        },
        effective_agent_config={"default_toolsets": []},
    )

    assert [tool.__name__ for tool in tools] == ["first_task", "second_task"]
    assert all(list(inspect.signature(tool).parameters) == [] for tool in tools)

    wrapped_tools = ensure_tool_wrapped(tools)
    assert [tool.name for tool in wrapped_tools] == ["first_task", "second_task"]
    assert all(tool.inputs == {} for tool in wrapped_tools)

    assert tools[0](prompt="ignored") == "prompt=first prompt;cwd=/repo;sandbox=read-only;search=false"
    assert tools[1](prompt="ignored") == "prompt=second prompt;cwd=/repo;sandbox=workspace-write;search=true"


def test_fixed_args_reject_unknown_parameters(monkeypatch):
    monkeypatch.setattr(yaml_agent_factory, "resolve_tool_function", lambda name: sample_tool)

    with pytest.raises(ValueError, match="Unknown fixed_args"):
        YamlAgentFactory.get_tools_from_config(
            {
                "tools": [
                    {
                        "name": "sample_tool",
                        "fixed_args": {"missing_arg": "value"},
                    }
                ]
            },
            effective_agent_config={"default_toolsets": []},
        )


def test_fixed_args_must_be_a_mapping():
    with pytest.raises(ValueError, match="fixed_args"):
        YamlAgentFactory.get_tools_from_config(
            {
                "tools": [
                    {
                        "name": "sample_tool",
                        "fixed_args": ["cwd", "/repo"],
                    }
                ]
            },
            effective_agent_config={"default_toolsets": []},
        )
