"""Goal lifecycle at the SmolagentsModelTurnBridge seam."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from agentloom.runtimes.smolagents.context_compression import (
    InternalChatMessage,
    summarize_conversation,
)
from agentloom.runtimes.smolagents.model_turn_bridge import SmolagentsModelTurnBridge
from agentloom.runtime.goal import GoalCompleteError, GoalState
from agentloom.runtime.goal.provider import GoalStateProvider, bind_goal_state_provider
from agentloom.runtime.model_binding import ModelTurnBinding
from agentloom.runtime.model_protocol import (
    FunctionCallItem,
    MessageItem,
    ModelTurnRequest,
    ModelTurnResult,
    ModelUsage,
)
from agentloom.runtime.trace import bind_local_run
from smolagents.models import ChatMessage, MessageRole


@dataclass
class _Tool:
    name: str
    description: str = "Tool."
    inputs = {}


class _UsageAdapter:
    adapter_id = "openai_chat"

    def __init__(self, *, input_tokens: int = 10, output_tokens: int = 5) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.requests: list[ModelTurnRequest] = []

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        self.requests.append(request)
        if request.tools:
            item = FunctionCallItem(
                call_id=f"call-{len(self.requests)}",
                name=request.tools[0].name,
                arguments_json="{}",
            )
        else:
            item = MessageItem(role="assistant", text="done")
        return ModelTurnResult(
            items=(item,),
            usage=ModelUsage(
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
            ),
        )


def _model(adapter: _UsageAdapter) -> SmolagentsModelTurnBridge:
    return SmolagentsModelTurnBridge(
        binding=ModelTurnBinding(
            model_type="test",
            model_id="opaque-model",
            adapter=adapter,
        )
    )


def _provider():
    return GoalStateProvider(
        GoalState.create(
            objective="Ship.",
            objective_fingerprint="abc",
        )
    )






def test_completion_allows_one_root_final_answer_settlement_request() -> None:
    adapter = _UsageAdapter()
    provider = _provider()
    provider.complete("Delivered.", settlement_run_id="root")
    tools = [_Tool("get_goal"), _Tool("final_answer")]
    model = _model(adapter)

    with bind_goal_state_provider(provider):
        with bind_local_run("worker"), pytest.raises(GoalCompleteError):
            model.generate([], tools_to_call_from=tools)
        with bind_local_run("root"):
            model.generate([], tools_to_call_from=tools)
            with pytest.raises(GoalCompleteError):
                model.generate([], tools_to_call_from=tools)

    assert [tool.name for tool in adapter.requests[0].tools] == ["final_answer"]
    assert provider.snapshot().status == "complete"


def test_completion_skips_planning_before_claiming_final_settlement() -> None:
    adapter = _UsageAdapter()
    provider = _provider()
    provider.complete("Delivered.", settlement_run_id="root")
    model = _model(adapter)

    with bind_goal_state_provider(provider), bind_local_run("root"):
        plan = model.generate([], stop_sequences=["<end_plan>"])
        model.generate([], tools_to_call_from=[_Tool("final_answer")])

    assert plan.content == "Goal is complete. Skip planning and deliver the final answer now."
    assert plan.token_usage.input_tokens == 0
    assert len(adapter.requests) == 1




def test_smart_summary_cannot_consume_completion_settlement(monkeypatch) -> None:
    from agentloom.runtimes.smolagents import context_compression

    monkeypatch.setattr(
        context_compression,
        "resolve_litellm_model_turn_binding",
        lambda *_args, **_kwargs: pytest.fail("summary model must not be called"),
    )
    provider = _provider()
    provider.complete("Delivered.", settlement_run_id="root")
    messages = [
        InternalChatMessage(ChatMessage(role=MessageRole.USER, content="task")),
        InternalChatMessage(ChatMessage(role=MessageRole.ASSISTANT, content="done")),
    ]

    with bind_goal_state_provider(provider), bind_local_run("root"):
        result = summarize_conversation(messages, "summary-model")

    assert result.error == "Goal completion settlement pending; smart summary skipped"
    assert provider.completion_settlement_pending(local_run_id="root") is True


def test_restored_complete_goal_has_no_final_delivery_allowance() -> None:
    completed = _provider()
    completed.complete("Delivered.", settlement_run_id="root")
    restored = GoalStateProvider(completed.snapshot())

    with pytest.raises(GoalCompleteError):
        restored.assert_request_allowed(
            local_run_id="root",
            allow_completion_settlement=True,
        )
