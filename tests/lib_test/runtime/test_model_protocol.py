from __future__ import annotations

from typing import Any, cast

import pytest
from agentloom.integrations.litellm import (
    AnthropicMessagesModelTurnAdapter,
    OpenAIChatModelTurnAdapter,
    OpenAIResponsesModelTurnAdapter,
    create_model_turn_adapter,
)
from agentloom.runtime.model_protocol import (
    FunctionCallItem,
    FunctionCallOutputItem,
    MessageItem,
    ModelProtocolError,
    ModelTurnRequest,
    ReasoningItem,
    ToolDefinition,
    model_item_from_dict,
    model_item_to_dict,
)
from litellm.types.llms.openai import ResponseAPIUsage, ResponsesAPIResponse
from litellm.types.utils import ModelResponse
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseReasoningItem,
)
from openai.types.responses.response_input_param import FunctionCallOutput
from openai.types.responses.response_reasoning_item import Summary
from pydantic import TypeAdapter


class _RecordingTransport:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    def __call__(self, **request: Any) -> Any:
        self.requests.append(request)
        return self.response


@pytest.mark.parametrize(
    "item",
    [
        MessageItem(role="assistant", text="done", item_id="msg_1"),
        FunctionCallItem(
            call_id="call_1",
            name="weather",
            arguments_json='{"city":"Shanghai"}',
            item_id="fc_1",
        ),
        FunctionCallOutputItem(
            call_id="call_1",
            output='{"temperature":20}',
            is_error=False,
        ),
        ReasoningItem(
            item_id="rs_1",
            summary=("check weather",),
            replay_payload={"type": "reasoning", "encrypted_content": "cipher"},
        ),
    ],
)
def test_canonical_model_items_round_trip_through_json_shape(item) -> None:
    assert model_item_from_dict(model_item_to_dict(item)) == item


def test_openai_chat_maps_native_tool_calls_to_canonical_items() -> None:
    transport = _RecordingTransport(
        {
            "id": "chatcmpl_1",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "I will check.",
                        "tool_calls": [
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {
                                    "name": "weather",
                                    "arguments": '{"city":"Shanghai"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        }
    )
    adapter = OpenAIChatModelTurnAdapter(transport=transport)

    result = adapter.turn(
        ModelTurnRequest(
            model="provider/model-name-is-opaque",
            items=(
                MessageItem(role="user", text="Check Shanghai"),
                FunctionCallItem(
                    call_id="call_1",
                    name="weather",
                    arguments_json='{"city":"Beijing"}',
                ),
                FunctionCallOutputItem(call_id="call_1", output='{"temperature":20}'),
            ),
            tools=(
                ToolDefinition(
                    name="weather",
                    description="Get weather",
                    parameters={
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                ),
            ),
        )
    )

    assert transport.requests == [
        {
            "model": "provider/model-name-is-opaque",
            "custom_llm_provider": "openai",
            "messages": [
                {"role": "user", "content": "Check Shanghai"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "weather",
                                "arguments": '{"city":"Beijing"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": '{"temperature":20}',
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "description": "Get weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                }
            ],
        }
    ]
    assert result.response_id == "chatcmpl_1"
    assert result.items == (
        MessageItem(role="assistant", text="I will check."),
        FunctionCallItem(
            call_id="call_2",
            name="weather",
            arguments_json='{"city":"Shanghai"}',
        ),
    )
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7
    assert result.usage.total_tokens == 18


@pytest.mark.parametrize(
    ("adapter_type", "request_field", "block_type"),
    [
        (OpenAIChatModelTurnAdapter, "messages", "text"),
        (AnthropicMessagesModelTurnAdapter, "messages", "text"),
    ],
)
def test_model_adapters_apply_configured_system_prompt_cache_boundary(
    adapter_type: type,
    request_field: str,
    block_type: str,
) -> None:
    response = {
        "id": "msg_1",
        "choices": [{"message": {"role": "assistant", "content": "done"}}],
        "usage": {},
    }
    transport = _RecordingTransport(response)
    adapter = adapter_type(
        transport=transport,
        context_cache=True,
        system_prompt_boundary="<dynamic>",
    )

    adapter.turn(
        ModelTurnRequest(
            model="opaque-model",
            items=(
                MessageItem(
                    role="system",
                    text="Stable instructions<dynamic>Per-run context",
                ),
                MessageItem(role="user", text="Run"),
            ),
        )
    )

    system_message = transport.requests[0][request_field][0]
    assert system_message == {
        "role": "system",
        "content": [
            {
                "type": block_type,
                "text": "Stable instructions",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": block_type,
                "text": "Per-run context",
            },
        ],
    }


@pytest.mark.parametrize("context_cache", [False, True])
@pytest.mark.parametrize("system_prompt_boundary", [None, "<dynamic>"])
def test_responses_cache_configuration_preserves_valid_system_input(
    context_cache: bool,
    system_prompt_boundary: str | None,
) -> None:
    transport = _RecordingTransport({"id": "resp_cached", "output": []})
    adapter = OpenAIResponsesModelTurnAdapter(
        transport=transport,
        context_cache=context_cache,
        system_prompt_boundary=system_prompt_boundary,
    )
    adapter.turn(ModelTurnRequest(
        model="opaque-model",
        instructions="Additional instructions",
        items=(
            MessageItem(role="system", text="Stable instructions<dynamic>Per-run context"),
            MessageItem(role="user", text="Run"),
        ),
        options={"prompt_cache_key": "application-cache-key"},
    ))

    assert transport.requests[0]["input"] == [
        {"role": "system", "content": "Stable instructions<dynamic>Per-run context"},
        {"role": "user", "content": "Run"},
    ]
    assert transport.requests[0]["instructions"] == "Additional instructions"
    assert transport.requests[0]["prompt_cache_key"] == "application-cache-key"


def test_openai_responses_preserves_order_reasoning_and_replay_payload() -> None:
    transport = _RecordingTransport(
        {
            "id": "resp_1",
            "output": [
                {
                    "id": "rs_1",
                    "type": "reasoning",
                    "status": "completed",
                    "summary": [{"type": "summary_text", "text": "Check weather"}],
                    "encrypted_content": "encrypted-reasoning",
                },
                {
                    "id": "fc_1",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": "call_1",
                    "name": "weather",
                    "arguments": '{"city":"Shanghai"}',
                },
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Checking now.",
                            "annotations": [],
                        }
                    ],
                },
            ],
            "usage": {
                "input_tokens": 10,
                "output_tokens": 6,
                "total_tokens": 16,
                "input_tokens_details": {"cached_tokens": 4},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        }
    )
    adapter = OpenAIResponsesModelTurnAdapter(transport=transport)
    replay_reasoning = {
        "id": "rs_previous",
        "type": "reasoning",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": "Earlier plan"}],
        "encrypted_content": "earlier-encrypted-reasoning",
    }

    result = adapter.turn(
        ModelTurnRequest(
            model="provider/model-name-is-opaque",
            instructions="Be concise.",
            items=(
                MessageItem(role="user", text="Check Shanghai"),
                ReasoningItem(
                    item_id="rs_previous",
                    status="completed",
                    summary=("Earlier plan",),
                    replay_payload=replay_reasoning,
                ),
                FunctionCallItem(
                    item_id="fc_previous",
                    status="completed",
                    call_id="call_previous",
                    name="weather",
                    arguments_json='{"city":"Beijing"}',
                ),
                FunctionCallOutputItem(
                    call_id="call_previous",
                    output='{"temperature":20}',
                    replay_payload={
                        "record": {
                            "call_id": "call_previous",
                            "tool_name": "weather",
                            "status": "completed",
                        }
                    },
                ),
            ),
            tools=(
                ToolDefinition(
                    name="weather",
                    description="Get weather",
                    parameters={"type": "object"},
                    strict=True,
                ),
            ),
        )
    )

    assert transport.requests == [
        {
            "model": "provider/model-name-is-opaque",
            "custom_llm_provider": "openai",
            "input": [
                {"role": "user", "content": "Check Shanghai"},
                replay_reasoning,
                {
                    "id": "fc_previous",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": "call_previous",
                    "name": "weather",
                    "arguments": '{"city":"Beijing"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_previous",
                    "output": '{"temperature":20}',
                },
            ],
            "instructions": "Be concise.",
            "include": ["reasoning.encrypted_content"],
            "tools": [
                {
                    "type": "function",
                    "name": "weather",
                    "description": "Get weather",
                    "parameters": {"type": "object"},
                    "strict": True,
                }
            ],
        }
    ]
    assert [type(item) for item in result.items] == [
        ReasoningItem,
        FunctionCallItem,
        MessageItem,
    ]
    assert result.items[0] == ReasoningItem(
        text=None,
        summary=("Check weather",),
        item_id="rs_1",
        status="completed",
        replay_payload={
            "id": "rs_1",
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": "Check weather"}],
            "encrypted_content": "encrypted-reasoning",
        },
    )
    assert result.items[1] == FunctionCallItem(
        call_id="call_1",
        name="weather",
        arguments_json='{"city":"Shanghai"}',
        item_id="fc_1",
        status="completed",
        replay_payload={
            "id": "fc_1",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_1",
            "name": "weather",
            "arguments": '{"city":"Shanghai"}',
        },
    )
    assert result.items[2] == MessageItem(
        role="assistant",
        text="Checking now.",
        item_id="msg_1",
        status="completed",
        replay_payload={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": "Checking now.",
                    "annotations": [],
                }
            ],
        },
    )
    assert result.response_id == "resp_1"
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 6
    assert result.usage.total_tokens == 16
    assert result.usage.cached_input_tokens == 4
    assert result.usage.reasoning_tokens == 2


@pytest.mark.parametrize("status", ["completed", "error", "blocked"])
def test_openai_responses_replays_tool_outcomes_with_valid_wire_status(status: str) -> None:
    output = f'{{"status":"{status}","message":"tool outcome"}}'
    item = FunctionCallOutputItem(
        call_id="call_outcome",
        output=output,
        status=status,
        is_error=status != "completed",
    )

    def transport(**request: Any) -> dict[str, Any]:
        wire = TypeAdapter(FunctionCallOutput).validate_python(request["input"][1])
        assert wire["call_id"] == "call_outcome"
        assert wire["output"] == output
        return {"id": "resp_recovered", "output": []}

    result = OpenAIResponsesModelTurnAdapter(transport=transport).turn(
        ModelTurnRequest(
            model="opaque-model",
            items=(
                FunctionCallItem(call_id="call_outcome", name="probe", arguments_json="{}"),
                item,
            ),
        )
    )

    assert result.response_id == "resp_recovered"
    assert item.status == status
    assert item.is_error is (status != "completed")


def test_openai_responses_rejects_unknown_executable_output() -> None:
    transport = _RecordingTransport(
        {
            "id": "resp_unsupported",
            "output": [
                {
                    "id": "shell_1",
                    "type": "local_shell_call",
                    "call_id": "call_1",
                    "action": {"type": "exec", "command": ["pwd"]},
                }
            ],
        }
    )
    adapter = OpenAIResponsesModelTurnAdapter(transport=transport)

    with pytest.raises(ModelProtocolError, match="local_shell_call"):
        adapter.turn(
            ModelTurnRequest(
                model="provider/model-name-is-opaque",
                items=(MessageItem(role="user", text="Run pwd"),),
            )
        )


def test_openai_chat_skips_canonical_reasoning_without_exposing_it() -> None:
    transport = _RecordingTransport(
        {
            "id": "chatcmpl_2",
            "choices": [
                {"message": {"role": "assistant", "content": "done"}}
            ],
            "usage": {},
        }
    )
    adapter = OpenAIChatModelTurnAdapter(transport=transport)

    adapter.turn(
        ModelTurnRequest(
            model="opaque-model",
            items=(
                MessageItem(role="user", text="Continue"),
                ReasoningItem(
                    text="private reasoning",
                    replay_payload={
                        "type": "reasoning",
                        "encrypted_content": "cipher",
                    },
                ),
                MessageItem(role="assistant", text="Previous answer"),
            ),
        )
    )

    messages = transport.requests[0]["messages"]
    assert messages == [
        {"role": "user", "content": "Continue"},
        {"role": "assistant", "content": "Previous answer"},
    ]
    assert "private reasoning" not in str(messages)
    assert "cipher" not in str(messages)


def test_openai_responses_rejects_anthropic_reasoning_without_exposing_it() -> None:
    transport = _RecordingTransport(
        {"id": "resp_2", "output": [], "usage": {}}
    )
    adapter = OpenAIResponsesModelTurnAdapter(transport=transport)

    with pytest.raises(
        ModelProtocolError,
        match="cannot replay reasoning from another protocol",
    ):
        adapter.turn(
            ModelTurnRequest(
                model="opaque-model",
                items=(
                    MessageItem(role="user", text="Continue"),
                    ReasoningItem(
                        text="private thinking",
                        replay_payload={
                            "type": "thinking",
                            "thinking": "private thinking",
                            "signature": "anthropic-signature",
                        },
                    ),
                    MessageItem(role="assistant", text="Previous answer"),
                ),
            )
        )

    assert transport.requests == []


def test_anthropic_messages_rejects_responses_reasoning_without_exposing_it() -> None:
    transport = _RecordingTransport(
        {
            "id": "msg_3",
            "choices": [
                {"message": {"role": "assistant", "content": "done"}}
            ],
            "usage": {},
        }
    )
    adapter = AnthropicMessagesModelTurnAdapter(transport=transport)

    with pytest.raises(
        ModelProtocolError,
        match="cannot replay reasoning from another protocol",
    ):
        adapter.turn(
            ModelTurnRequest(
                model="opaque-model",
                items=(
                    MessageItem(role="user", text="Continue"),
                    ReasoningItem(
                        summary=("private summary",),
                        replay_payload={
                            "type": "reasoning",
                            "encrypted_content": "responses-cipher",
                        },
                    ),
                    MessageItem(role="assistant", text="Previous answer"),
                ),
            )
        )

    assert transport.requests == []


def test_anthropic_messages_preserves_thinking_signature_and_native_tool_use() -> None:
    transport = _RecordingTransport(
        {
            "id": "msg_2",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "I will check.",
                        "thinking_blocks": [
                            {
                                "type": "thinking",
                                "thinking": "Need current weather.",
                                "signature": "signature-2",
                            },
                            {
                                "type": "redacted_thinking",
                                "data": "redacted-2",
                            },
                        ],
                        "tool_calls": [
                            {
                                "id": "toolu_2",
                                "type": "function",
                                "function": {
                                    "name": "weather",
                                    "arguments": '{"city":"Shanghai"}',
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "total_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 3},
            },
        }
    )
    adapter = AnthropicMessagesModelTurnAdapter(transport=transport)
    earlier_thinking = {
        "type": "thinking",
        "thinking": "Earlier reasoning.",
        "signature": "signature-1",
    }

    result = adapter.turn(
        ModelTurnRequest(
            model="model-name-is-opaque",
            instructions="Be concise.",
            items=(
                MessageItem(role="user", text="Check Shanghai"),
                ReasoningItem(
                    text="Earlier reasoning.",
                    replay_payload=earlier_thinking,
                ),
                FunctionCallItem(
                    call_id="toolu_1",
                    name="weather",
                    arguments_json='{"city":"Beijing"}',
                ),
                FunctionCallOutputItem(
                    call_id="toolu_1",
                    output='{"temperature":20}',
                ),
            ),
            tools=(
                ToolDefinition(
                    name="weather",
                    description="Get weather",
                    parameters={"type": "object"},
                ),
            ),
        )
    )

    assert transport.requests == [
        {
            "model": "model-name-is-opaque",
            "custom_llm_provider": "anthropic",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Check Shanghai"},
                {
                    "role": "assistant",
                    "content": None,
                    "thinking_blocks": [earlier_thinking],
                    "tool_calls": [
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {
                                "name": "weather",
                                "arguments": '{"city":"Beijing"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "toolu_1",
                    "content": '{"temperature":20}',
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "description": "Get weather",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        }
    ]
    assert result.items == (
        ReasoningItem(
            text="Need current weather.",
            replay_payload={
                "type": "thinking",
                "thinking": "Need current weather.",
                "signature": "signature-2",
            },
        ),
        ReasoningItem(
            replay_payload={
                "type": "redacted_thinking",
                "data": "redacted-2",
            },
        ),
        MessageItem(role="assistant", text="I will check."),
        FunctionCallItem(
            call_id="toolu_2",
            name="weather",
            arguments_json='{"city":"Shanghai"}',
        ),
    )
    assert result.response_id == "msg_2"
    assert result.usage.input_tokens == 12
    assert result.usage.output_tokens == 8
    assert result.usage.total_tokens == 20
    assert result.usage.cached_input_tokens == 3


@pytest.mark.parametrize(
    ("adapter_id", "adapter_type"),
    [
        ("openai_chat", OpenAIChatModelTurnAdapter),
        ("openai_responses", OpenAIResponsesModelTurnAdapter),
        ("anthropic_messages", AnthropicMessagesModelTurnAdapter),
    ],
)
def test_adapter_factory_requires_an_explicit_supported_protocol(
    adapter_id: str,
    adapter_type: type,
) -> None:
    assert isinstance(
        create_model_turn_adapter(adapter_id, transport=_RecordingTransport({})),
        adapter_type,
    )


def test_adapter_factory_rejects_unknown_protocol_without_fallback() -> None:
    with pytest.raises(ValueError, match="unknown model adapter"):
        create_model_turn_adapter("responses", transport=_RecordingTransport({}))


def test_openai_chat_accepts_locked_litellm_response_objects() -> None:
    response = ModelResponse(
        id="chatcmpl_litellm",
        model="test-model",
        choices=[
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_litellm",
                            "type": "function",
                            "function": {
                                "name": "weather",
                                "arguments": '{"city":"Shanghai"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    )

    result = OpenAIChatModelTurnAdapter(
        transport=_RecordingTransport(response)
    ).turn(
        ModelTurnRequest(
            model="test-model",
            items=(MessageItem(role="user", text="Check weather"),),
        )
    )

    assert result.items == (
        FunctionCallItem(
            call_id="call_litellm",
            name="weather",
            arguments_json='{"city":"Shanghai"}',
        ),
    )
    assert result.response_id == "chatcmpl_litellm"


def test_openai_responses_accepts_locked_litellm_response_objects() -> None:
    output = cast(
        Any,
        [
            ResponseReasoningItem(
                id="reasoning_litellm",
                type="reasoning",
                status="completed",
                summary=[Summary(type="summary_text", text="Plan")],
                encrypted_content="ciphertext",
            ),
            ResponseFunctionToolCall(
                id="call_item_litellm",
                type="function_call",
                status="completed",
                call_id="call_litellm",
                name="weather",
                arguments='{"city":"Shanghai"}',
            ),
        ],
    )
    response = ResponsesAPIResponse(
        id="resp_litellm",
        created_at=1,
        model="test-model",
        output=output,
        usage=ResponseAPIUsage(input_tokens=2, output_tokens=3, total_tokens=5),
    )

    result = OpenAIResponsesModelTurnAdapter(
        transport=_RecordingTransport(response)
    ).turn(
        ModelTurnRequest(
            model="test-model",
            items=(MessageItem(role="user", text="Check weather"),),
        )
    )

    assert result.response_id == "resp_litellm"
    assert [type(item) for item in result.items] == [
        ReasoningItem,
        FunctionCallItem,
    ]


def test_prose_and_xml_are_plain_message_text_not_tool_calls() -> None:
    transport = _RecordingTransport(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '<tool_call><name>weather</name></tool_call>',
                    }
                }
            ]
        }
    )

    result = OpenAIChatModelTurnAdapter(transport=transport).turn(
        ModelTurnRequest(
            model="test-model",
            items=(MessageItem(role="user", text="Check weather"),),
        )
    )

    assert result.items == (
        MessageItem(
            role="assistant",
            text="<tool_call><name>weather</name></tool_call>",
        ),
    )


def test_provider_errors_propagate_without_cross_adapter_fallback() -> None:
    provider_error = RuntimeError("provider rejected request")

    def fail(**_: Any) -> Any:
        raise provider_error

    with pytest.raises(RuntimeError) as captured:
        OpenAIResponsesModelTurnAdapter(transport=fail).turn(
            ModelTurnRequest(
                model="test-model",
                items=(MessageItem(role="user", text="Hello"),),
            )
        )

    assert captured.value is provider_error


def test_options_cannot_override_structural_protocol_fields() -> None:
    adapter = OpenAIResponsesModelTurnAdapter(transport=_RecordingTransport({}))

    with pytest.raises(ValueError, match="reserved model turn option"):
        adapter.turn(
            ModelTurnRequest(
                model="test-model",
                items=(MessageItem(role="user", text="Hello"),),
                options={"adapter": "openai_chat"},
            )
        )


def test_openai_chat_groups_parallel_calls_in_one_assistant_turn() -> None:
    transport = _RecordingTransport(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "done",
                    }
                }
            ]
        }
    )

    OpenAIChatModelTurnAdapter(transport=transport).turn(
        ModelTurnRequest(
            model="opaque-model",
            instructions="Use tools.",
            items=(
                MessageItem(role="user", text="Check two cities"),
                MessageItem(role="assistant", text="Checking."),
                FunctionCallItem(
                    call_id="call_1",
                    name="weather",
                    arguments_json='{"city":"Shanghai"}',
                ),
                FunctionCallItem(
                    call_id="call_2",
                    name="weather",
                    arguments_json='{"city":"Beijing"}',
                ),
                FunctionCallOutputItem(call_id="call_1", output="20"),
                FunctionCallOutputItem(call_id="call_2", output="18"),
            ),
        )
    )

    assert transport.requests[0]["messages"] == [
        {"role": "system", "content": "Use tools."},
        {"role": "user", "content": "Check two cities"},
        {
            "role": "assistant",
            "content": "Checking.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "arguments": '{"city":"Shanghai"}',
                    },
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {
                        "name": "weather",
                        "arguments": '{"city":"Beijing"}',
                    },
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "20"},
        {"role": "tool", "tool_call_id": "call_2", "content": "18"},
    ]


@pytest.mark.parametrize(
    ("adapter", "expected_provider"),
    [
        (OpenAIChatModelTurnAdapter, "openai"),
        (OpenAIResponsesModelTurnAdapter, "openai"),
        (AnthropicMessagesModelTurnAdapter, "anthropic"),
    ],
)
def test_adapter_selects_provider_while_preserving_opaque_model_name(
    adapter: type,
    expected_provider: str,
) -> None:
    response: dict[str, Any]
    if adapter is OpenAIResponsesModelTurnAdapter:
        response = {"id": "response", "output": []}
    else:
        response = {"id": "response", "choices": [{"message": {"content": "done"}}]}
    transport = _RecordingTransport(response)

    adapter(transport=transport).turn(
        ModelTurnRequest(
            model="unchanged-model-name",
            items=(MessageItem(role="user", text="Hello"),),
        )
    )

    assert transport.requests[0]["model"] == "unchanged-model-name"
    assert transport.requests[0]["custom_llm_provider"] == expected_provider
