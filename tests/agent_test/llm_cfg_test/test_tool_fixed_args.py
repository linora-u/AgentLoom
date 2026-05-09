"""Tests for fixed tool arguments configured in Agent YAML."""

from __future__ import annotations

import inspect

import pytest

from src.lib.smolagents.agent import yaml_agent_factory
from src.lib.smolagents.agent.yaml_agent_factory import YamlAgentFactory
from src.lib.smolagents.tools.tools import ensure_tool_wrapped


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
                    "name": "codex",
                    "fixed_args": {
                        "cwd": "/repo",
                        "sandbox": "workspace-write",
                        "search": "false",
                    },
                }
            ]
        },
        effective_agent_config={"default_loaded_tools": []},
    )

    tool_func = tools[0]
    assert list(inspect.signature(tool_func).parameters) == ["prompt"]

    wrapped_tool = ensure_tool_wrapped([tool_func])[0]
    assert set(wrapped_tool.inputs) == {"prompt"}

    result = tool_func(prompt="summarize", cwd="/tmp", sandbox="danger-full-access", search="true")
    assert result == "prompt=summarize;cwd=/repo;sandbox=workspace-write;search=false"


def test_dynamic_tool_fixed_args_use_yaml_name_as_exposed_tool_name(monkeypatch):
    monkeypatch.setattr(yaml_agent_factory, "load_function", lambda module, function: sample_tool)

    tools, _ = YamlAgentFactory.get_tools_from_config(
        {
            "tools": [
                {
                    "name": "codex1",
                    "module": "src.tools.codex.codex_tool",
                    "function": "codex",
                    "fixed_args": {
                        "prompt": "first prompt",
                        "cwd": "/repo",
                        "sandbox": "read-only",
                        "search": "false",
                    },
                },
                {
                    "name": "codex2",
                    "module": "src.tools.codex.codex_tool",
                    "function": "codex",
                    "fixed_args": {
                        "prompt": "second prompt",
                        "cwd": "/repo",
                        "sandbox": "workspace-write",
                        "search": "true",
                    },
                },
            ]
        },
        effective_agent_config={"default_loaded_tools": []},
    )

    assert [tool.__name__ for tool in tools] == ["codex1", "codex2"]
    assert all(list(inspect.signature(tool).parameters) == [] for tool in tools)

    wrapped_tools = ensure_tool_wrapped(tools)
    assert [tool.name for tool in wrapped_tools] == ["codex1", "codex2"]
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
                        "name": "codex",
                        "fixed_args": {"missing_arg": "value"},
                    }
                ]
            },
            effective_agent_config={"default_loaded_tools": []},
        )


def test_fixed_args_must_be_a_mapping():
    with pytest.raises(ValueError, match="fixed_args"):
        YamlAgentFactory.get_tools_from_config(
            {
                "tools": [
                    {
                        "name": "codex",
                        "fixed_args": ["cwd", "/repo"],
                    }
                ]
            },
            effective_agent_config={"default_loaded_tools": []},
        )
