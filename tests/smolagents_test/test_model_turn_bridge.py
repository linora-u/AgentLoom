from __future__ import annotations

from dataclasses import dataclass

import pytest
from agentloom.adapters.smolagents.model_turn_bridge import SmolagentsModelTurnBridge
from agentloom.runtime.goal import GoalBudgetLimitedError, GoalCompleteError, GoalState
from agentloom.runtime.goal.provider import (
    GoalStateProvider,
    bind_goal_state_provider,
)
from agentloom.runtime.model_protocol import (
    FunctionCallItem,
    FunctionCallOutputItem,
    MessageItem,
    ModelProtocolError,
    ModelTurnRequest,
    ModelTurnResult,
    ModelUsage,
    ReasoningItem,
)
from agentloom.runtime.tool_protocol import TOOL_RESULT_RAW_KEY, ToolCallRecord
from agentloom.runtime.trace import bind_local_run
from smolagents.models import ChatMessage, MessageRole
from smolagents.monitoring import TokenUsage


@dataclass
class _Tool:
    name: str = "weather"
    description: str = "Get weather."
    inputs = {
        "city": {
            "type": "string",
            "description": "City name",
        }
    }


class _RecordingTurnAdapter:
    adapter_id = "openai_responses"

    def __init__(self) -> None:
        self.requests: list[ModelTurnRequest] = []

    def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
        self.requests.append(request)
        tool_name = request.tools[0].name if request.tools else "weather"
        return ModelTurnResult(
            items=(
                FunctionCallItem(
                    call_id="call_weather",
                    name=tool_name,
                    arguments_json='{"city":"Shanghai"}',
                ),
            ),
            response_id="resp_1",
            usage=ModelUsage(input_tokens=11, output_tokens=5, total_tokens=16),
        )


def test_bridge_converts_smolagents_messages_and_native_tool_calls() -> None:
    adapter = _RecordingTurnAdapter()
    model = SmolagentsModelTurnBridge(
        adapter=adapter,
        model_id="opaque-model",
        options={"temperature": 0.2},
    )

    result = model.generate(
        [ChatMessage(role=MessageRole.USER, content="Check Shanghai")],
        tools_to_call_from=[_Tool()],
    )

    assert adapter.requests == [
        ModelTurnRequest(
            model="opaque-model",
            items=(MessageItem(role="user", text="Check Shanghai"),),
            tools=adapter.requests[0].tools,
            options={"temperature": 0.2},
        )
    ]
    assert adapter.requests[0].tools[0].name == "weather"
    assert adapter.requests[0].tools[0].parameters["required"] == ["city"]
    assert result.tool_calls is not None
    assert result.tool_calls[0].id == "call_weather"
    assert result.tool_calls[0].function.name == "weather"
    assert result.tool_calls[0].function.arguments == {"city": "Shanghai"}
    assert result.token_usage == TokenUsage(input_tokens=11, output_tokens=5)


def test_bridge_rejects_plain_text_when_tool_call_is_required() -> None:
    class _TextAdapter:
        adapter_id = "openai_chat"

        def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
            return ModelTurnResult(
                items=(MessageItem(role="assistant", text="I cannot call it."),)
            )

    model = SmolagentsModelTurnBridge(
        adapter=_TextAdapter(),
        model_id="opaque-model",
    )

    with model.require_tool_calls(), pytest.raises(
        ModelProtocolError,
        match="structured tool call",
    ):
        model.generate(
            [ChatMessage(role=MessageRole.USER, content="Call weather")],
            tools_to_call_from=[_Tool()],
        )


def test_bridge_parse_tool_calls_never_parses_text_fallback() -> None:
    model = SmolagentsModelTurnBridge(
        adapter=_RecordingTurnAdapter(),
        model_id="opaque-model",
    )

    with pytest.raises(ModelProtocolError, match="native structured tool_calls"):
        model.parse_tool_calls(
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content='{"name":"weather","arguments":{"city":"Shanghai"}}',
            )
        )


def test_bridge_replays_restored_canonical_items_without_observation_parsing() -> None:
    adapter = _RecordingTurnAdapter()
    model = SmolagentsModelTurnBridge(
        adapter=adapter,
        model_id="opaque-model",
    )
    reasoning = ReasoningItem(
        item_id="reasoning-1",
        summary=("Inspect the repository",),
        replay_payload={
            "type": "reasoning",
            "encrypted_content": "opaque-ciphertext",
        },
    )
    function_call = FunctionCallItem(
        call_id="call-1",
        name="weather",
        arguments_json='{"city":"Shanghai"}',
        item_id="function-1",
    )
    tool_record = ToolCallRecord.completed(
        call_id="call-1",
        tool_name="weather",
        input={"city": "Shanghai"},
        output={"temperature": 20},
    )

    model.generate(
        [
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content="not used for replay",
                raw={
                    "agentloom_model_items": (reasoning, function_call),
                    "agentloom_model_response_id": "response-1",
                },
            ),
            ChatMessage(
                role=MessageRole.TOOL_RESPONSE,
                content="not parsed from observation text",
                raw={TOOL_RESULT_RAW_KEY: tool_record.to_dict()},
            ),
        ],
        tools_to_call_from=[_Tool()],
    )

    assert adapter.requests[0].items == (
        reasoning,
        function_call,
        FunctionCallOutputItem(
            call_id="call-1",
            output=tool_record.model_content(),
            status="completed",
            replay_payload={"record": tool_record.to_dict()},
        ),
    )


def test_bridge_accounts_usage_and_fences_the_next_goal_request() -> None:
    adapter = _RecordingTurnAdapter()
    model = SmolagentsModelTurnBridge(
        adapter=adapter,
        model_id="opaque-model",
    )
    provider = GoalStateProvider(
        GoalState.create(
            objective="Ship.",
            objective_fingerprint="goal",
            token_budget=15,
        )
    )

    with bind_goal_state_provider(provider):
        model.generate(
            [ChatMessage(role=MessageRole.USER, content="Run")],
            tools_to_call_from=[_Tool()],
        )
        with pytest.raises(GoalBudgetLimitedError):
            model.generate(
                [ChatMessage(role=MessageRole.USER, content="Run again")],
                tools_to_call_from=[_Tool()],
            )

    assert len(adapter.requests) == 1
    assert provider.snapshot().used_tokens == 16


def test_bridge_completion_settlement_exposes_only_final_answer_once() -> None:
    adapter = _RecordingTurnAdapter()
    model = SmolagentsModelTurnBridge(
        adapter=adapter,
        model_id="opaque-model",
    )
    provider = GoalStateProvider(
        GoalState.create(
            objective="Ship.",
            objective_fingerprint="goal",
            token_budget=None,
        )
    )
    provider.complete("done", settlement_run_id="root")
    tools = [
        _Tool(),
        _Tool(name="final_answer", description="Finish."),
    ]

    with bind_goal_state_provider(provider), bind_local_run("root"):
        model.generate(
            [ChatMessage(role=MessageRole.USER, content="Finish")],
            tools_to_call_from=tools,
        )
        with pytest.raises(GoalCompleteError):
            model.generate(
                [ChatMessage(role=MessageRole.USER, content="Again")],
                tools_to_call_from=tools,
            )

    assert [tool.name for tool in adapter.requests[0].tools] == ["final_answer"]
