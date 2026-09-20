"""Todo capability configuration and prompt policy contracts."""

from __future__ import annotations

import pytest
from agentloom.adapters.smolagents.options import normalize_runtime_options
from agentloom.runtime.prompts.prompt_builder import todo_policy_for_mode


@pytest.mark.parametrize("mode", ["auto", "on", "off"])
def test_todo_mode_accepts_supported_values(mode: str, tmp_path) -> None:
    options, _ = normalize_runtime_options(
        {"runtime_options": {"todo_mode": mode}}, agent_root=tmp_path,
    )
    assert options["todo_mode"] == mode


def test_todo_mode_defaults_to_auto(tmp_path) -> None:
    options, _ = normalize_runtime_options({}, agent_root=tmp_path)
    assert options["todo_mode"] == "auto"


@pytest.mark.parametrize("value", [None, "always", "enabled", True, False, 1, {}, []])
def test_todo_mode_rejects_invalid_canonical_values(value, tmp_path) -> None:
    with pytest.raises(ValueError, match="todo_mode"):
        normalize_runtime_options(
            {"runtime_options": {"todo_mode": value}}, agent_root=tmp_path,
        )


@pytest.mark.parametrize("value", ["auto", None, {"mode": "on"}, {"mode": True},
                                   {"mode": "invalid", "first_step": True}, []])
def test_legacy_todo_is_ignored_without_conversion(value, tmp_path) -> None:
    options, sources = normalize_runtime_options({"todo": value}, agent_root=tmp_path)
    assert options["todo_mode"] == "auto"
    assert sources["todo_mode"] == "default:smolagents"


def test_legacy_todo_does_not_conflict_with_canonical_mode(tmp_path) -> None:
    options, _ = normalize_runtime_options(
        {"todo": {"mode": "on"}, "runtime_options": {"todo_mode": "off"}},
        agent_root=tmp_path,
    )
    assert options["todo_mode"] == "off"


def test_auto_policy_is_advisory() -> None:
    policy = todo_policy_for_mode("auto")

    assert "decide whether" in policy
    assert "three or more" in policy
    assert "MUST" not in policy


def test_on_policy_is_strong_without_literal_first_tool_gate() -> None:
    policy = todo_policy_for_mode("on")

    assert "first tool call MUST be one standalone `todo_write`" in policy
    assert "before substantial execution" in policy
    assert "read-only discovery" in policy
    assert "never issue it in parallel" in policy
    assert "before calling `final_answer`" in policy
    assert "pure questions or answers" in policy


def test_off_policy_is_empty() -> None:
    assert todo_policy_for_mode("off") == ""


def test_current_snapshot_is_injected_as_trusted_system_context() -> None:
    from dataclasses import replace

    from agentloom.adapters.smolagents.loom_mixin import append_current_todo_state
    from agentloom.runtime.todo import TodoStateProvider, bind_todo_state_provider
    from agentloom.runtime.trace import bind_explicit_execution_context, capture_explicit_execution_context
    from smolagents.models import ChatMessage, MessageRole

    provider = TodoStateProvider()
    provider.replace(
        "supervisor",
        [{"content": "Run real application", "status": "in_progress"}],
    )
    execution = replace(
        capture_explicit_execution_context(),
        runtime_agent_path="supervisor",
    )
    messages = [ChatMessage(role=MessageRole.USER, content="continue")]

    with bind_explicit_execution_context(execution):
        with bind_todo_state_provider(provider):
            hydrated = append_current_todo_state(messages, todo_mode="auto")

    assert hydrated[:-1] == messages
    assert hydrated[-1].role == MessageRole.SYSTEM
    text = hydrated[-1].content[0]["text"]
    assert '<current-todos revision="1">' in text
    assert "Run real application" in text


def test_current_snapshot_is_not_injected_when_off_or_empty() -> None:
    from agentloom.adapters.smolagents.loom_mixin import append_current_todo_state
    from agentloom.runtime.todo import TodoStateProvider, bind_todo_state_provider
    from smolagents.models import ChatMessage, MessageRole

    messages = [ChatMessage(role=MessageRole.USER, content="continue")]
    with bind_todo_state_provider(TodoStateProvider()):
        assert append_current_todo_state(messages, todo_mode="auto") is messages
        assert append_current_todo_state(messages, todo_mode="on") is messages
        assert append_current_todo_state(messages, todo_mode="off") is messages


def test_summary_mode_model_context_also_receives_current_snapshot() -> None:
    from dataclasses import replace

    from agentloom.adapters.smolagents.loom_mixin import LoomAgentMixin
    from agentloom.runtime.todo import TodoStateProvider, bind_todo_state_provider
    from agentloom.runtime.trace import bind_explicit_execution_context, capture_explicit_execution_context
    from smolagents.models import ChatMessage, MessageRole

    class MemoryWriter:
        def write_memory_to_messages(self, summary_mode: bool = False):
            return [ChatMessage(role=MessageRole.USER, content="summary")]

    class Agent(LoomAgentMixin, MemoryWriter):
        pass

    provider = TodoStateProvider()
    provider.replace("supervisor", [{"content": "Keep planning", "status": "pending"}])
    execution = replace(
        capture_explicit_execution_context(),
        runtime_agent_path="supervisor",
    )
    agent = Agent()
    agent._agent_loom_todo_mode = "auto"

    with bind_explicit_execution_context(execution):
        with bind_todo_state_provider(provider):
            messages = agent.write_memory_to_messages(summary_mode=True)

    assert messages[-1].role == MessageRole.SYSTEM
    assert "Keep planning" in messages[-1].content[0]["text"]
