"""Runtime-level Todo behavior with a deterministic model."""

from __future__ import annotations

import json
from dataclasses import replace

from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
)

from src.lib.smolagents.agent.base_agent import ToolCallingAgentV2
from src.lib.todo import TodoStateProvider, bind_todo_state_provider
from src.tools.todo import todo_write
from src.trace import bind_explicit_execution_context, capture_explicit_execution_context


def _tool_call(name: str, arguments: dict, call_id: str) -> ChatMessage:
    return ChatMessage(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[
            ChatMessageToolCall(
                id=call_id,
                type="function",
                function=ChatMessageToolCallFunction(
                    name=name,
                    arguments=arguments,
                ),
            )
        ],
    )


class TodoThenFinalModel:
    model_id = "fake-todo-then-final"

    def __init__(self) -> None:
        self.calls = 0
        self.inputs: list[list[ChatMessage]] = []

    def generate(self, messages, stop_sequences=None, tools_to_call_from=None, **kwargs):
        self.calls += 1
        self.inputs.append(list(messages))
        if self.calls == 1:
            return _tool_call(
                "todo_write",
                {
                    "todos": [
                        {
                            "content": "Leave this pending to prove final is not gated",
                            "status": "pending",
                        }
                    ]
                },
                "todo-1",
            )
        return _tool_call("final_answer", {"answer": "finished"}, "final-1")


class FinalOnlyModel:
    model_id = "fake-final-only"

    def __init__(self) -> None:
        self.inputs: list[list[ChatMessage]] = []

    def generate(self, messages, stop_sequences=None, tools_to_call_from=None, **kwargs):
        self.inputs.append(list(messages))
        return _tool_call("final_answer", {"answer": "resumed"}, "final-resume")


def _message_text(messages: list[ChatMessage]) -> str:
    return json.dumps(
        [message.content for message in messages],
        ensure_ascii=False,
        default=str,
    )


def test_pending_todo_is_hydrated_without_extra_call_or_final_gate() -> None:
    model = TodoThenFinalModel()
    agent = ToolCallingAgentV2(
        tools=[todo_write],
        model=model,
        max_steps=3,
        max_tokens=4096,
        verbosity_level=0,
    )
    agent._agent_loom_todo_mode = "auto"
    provider = TodoStateProvider()
    execution = replace(
        capture_explicit_execution_context(),
        runtime_agent_path="supervisor",
    )

    with bind_explicit_execution_context(execution):
        with bind_todo_state_provider(provider):
            result = agent.run("Track one item, then finish without resolving it.")

    assert result == "finished"
    assert model.calls == 2
    assert "current-todos" not in _message_text(model.inputs[0])
    assert "Leave this pending" in _message_text(model.inputs[1])
    assert provider.load("supervisor")["items"][0]["status"] == "pending"


def test_checkpoint_snapshot_is_visible_on_first_resumed_model_action(tmp_path) -> None:
    from src.lib.checkpoint.checkpoint_manager import CheckpointManager
    from src.lib.checkpoint.coordinator import CheckpointCoordinator

    task_id = "resume-task"
    manager = CheckpointManager(
        "supervisor",
        checkpoint_dir=tmp_path / "checkpoints" / "app" / task_id,
        run_id="resume-run",
    )
    manager.replace_todos(
        task_id,
        "supervisor",
        [{"content": "Restored from checkpoint", "status": "in_progress"}],
    )
    coordinator = CheckpointCoordinator.activate(
        manager,
        task_id,
        "resume task",
        resume=True,
    )
    model = FinalOnlyModel()
    agent = ToolCallingAgentV2(
        tools=[todo_write],
        model=model,
        max_steps=1,
        max_tokens=4096,
        verbosity_level=0,
    )
    agent._agent_loom_todo_mode = "auto"
    execution = replace(
        capture_explicit_execution_context(),
        runtime_agent_path="supervisor",
    )

    try:
        with bind_explicit_execution_context(execution):
            with bind_todo_state_provider(TodoStateProvider()):
                assert agent.run("Resume and answer.") == "resumed"
    finally:
        CheckpointCoordinator.deactivate(coordinator)
        manager.close()

    assert "Restored from checkpoint" in _message_text(model.inputs[0])
