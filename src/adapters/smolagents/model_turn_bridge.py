"""smolagents Model facade backed by AgentLoom's model-turn seam."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from agentloom.adapters.smolagents.error_recovery import RUNTIME_FEEDBACK_RAW_KEY
from agentloom.runtime.model_binding import ModelTurnBinding
from agentloom.runtime.model_protocol import (
    MODEL_ITEMS_RAW_KEY,
    MODEL_RESPONSE_ID_RAW_KEY,
    FunctionCallItem,
    FunctionCallOutputItem,
    MessageItem,
    ModelItem,
    ModelProtocolError,
    ModelTurnResult,
    ReasoningItem,
    ToolDefinition,
    model_item_from_dict,
    model_item_to_dict,
)
from agentloom.runtime.tool_protocol import TOOL_CALL_RAW_KEY, TOOL_RESULT_RAW_KEY, ToolCallRecord
from smolagents.models import (
    ChatMessage,
    ChatMessageToolCall,
    ChatMessageToolCallFunction,
    MessageRole,
    Model,
    get_tool_json_schema,
)
from smolagents.monitoring import TokenUsage


def _message_role(role: Any) -> str:
    value = getattr(role, "value", role)
    aliases = {
        "tool-call": "assistant",
        "tool_call": "assistant",
        "tool-response": "tool",
        "tool_response": "tool",
    }
    return aliases.get(str(value), str(value))


def _message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, Mapping) or item.get("type") != "text":
                raise ModelProtocolError("smolagents message contains unsupported content")
            text = item.get("text")
            if not isinstance(text, str):
                raise ModelProtocolError("smolagents text content must be a string")
            parts.append(text)
        return "".join(parts)
    raise ModelProtocolError("smolagents message content must be text")


def _model_items_from_raw(raw: Any) -> tuple[ModelItem, ...] | None:
    if not isinstance(raw, Mapping):
        return None
    items = raw.get(MODEL_ITEMS_RAW_KEY)
    if items is None:
        return None
    if not isinstance(items, (list, tuple)):
        raise ModelProtocolError("stored AgentLoom model items are invalid")
    result: list[ModelItem] = []
    for item in items:
        if isinstance(
            item,
            (
                MessageItem,
                FunctionCallItem,
                FunctionCallOutputItem,
                ReasoningItem,
            ),
        ):
            result.append(item)
            continue
        if not isinstance(item, Mapping):
            raise ModelProtocolError("stored AgentLoom model item is invalid")
        try:
            result.append(model_item_from_dict(item))
        except (TypeError, ValueError) as exc:
            raise ModelProtocolError(
                "stored AgentLoom model item is invalid"
            ) from exc
    return tuple(result)


def _tool_response_item(*, content: Any, raw: Any) -> ModelItem:
    record_raw = raw.get(TOOL_RESULT_RAW_KEY) if isinstance(raw, Mapping) else None
    if isinstance(record_raw, Mapping):
        record = ToolCallRecord.from_dict(dict(record_raw))
        return FunctionCallOutputItem(
            call_id=record.call_id,
            output=record.model_content(),
            status=record.status,
            is_error=record.status != "completed",
            replay_payload={"record": record.to_dict()},
        )

    if isinstance(raw, Mapping) and raw.get(RUNTIME_FEEDBACK_RAW_KEY) is True:
        # smolagents uses TOOL_RESPONSE for its own model/tool parsing feedback
        # and converts that role to USER before provider calls.  This is not a
        # completed AgentLoom tool invocation and therefore has no call record.
        return MessageItem(
            role="user",
            text=_message_text(content),
            replay_payload={RUNTIME_FEEDBACK_RAW_KEY: True},
        )

    raise ModelProtocolError(
        "tool response is missing the structured ToolCallRecord marker"
    )


def _messages_to_items(messages: list[ChatMessage | dict]) -> tuple[ModelItem, ...]:
    result: list[ModelItem] = []
    for message in messages:
        if isinstance(message, ChatMessage):
            stored = _model_items_from_raw(message.raw)
            if stored is not None:
                result.extend(stored)
                continue
            role = _message_role(message.role)
            if role == "tool":
                result.append(
                    _tool_response_item(
                        content=message.content,
                        raw=message.raw,
                    )
                )
                continue
            if role == "assistant" and message.tool_calls:
                text = _message_text(message.content)
                if text:
                    result.append(MessageItem(role="assistant", text=text))
                for call in message.tool_calls:
                    arguments = call.function.arguments
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
                    result.append(
                        FunctionCallItem(
                            call_id=call.id,
                            name=call.function.name,
                            arguments_json=arguments,
                        )
                    )
                continue
            if role not in {"system", "developer", "user", "assistant"}:
                raise ModelProtocolError(f"unsupported smolagents message role: {role!r}")
            result.append(MessageItem(role=role, text=_message_text(message.content)))
            continue

        if not isinstance(message, Mapping):
            raise ModelProtocolError("model history contains an unsupported message")
        role = _message_role(message.get("role") or "")
        if role == "tool":
            result.append(
                _tool_response_item(
                    content=message.get("content"),
                    raw=message.get("raw"),
                )
            )
            continue
        if role not in {"system", "developer", "user", "assistant"}:
            raise ModelProtocolError(f"unsupported model message role: {role!r}")
        result.append(MessageItem(role=role, text=_message_text(message.get("content"))))
    return tuple(result)


def _tool_definition(tool: Any) -> ToolDefinition:
    canonical = getattr(tool, "_agentloom_tool_definition", None)
    if isinstance(canonical, ToolDefinition):
        return canonical
    schema = get_tool_json_schema(tool)
    function = schema["function"]
    return ToolDefinition(
        name=function["name"],
        description=function.get("description") or "",
        parameters=function["parameters"],
        strict=function.get("strict"),
    )


class SmolagentsModelTurnBridge(Model):
    """Project smolagents model calls through one resolved model binding."""

    def __init__(
        self,
        *,
        binding: ModelTurnBinding,
    ) -> None:
        super().__init__(model_id=binding.model_id)
        self.binding = binding
        self._require_tool_calls: ContextVar[bool] = ContextVar(
            f"agentloom_bridge_require_tool_calls_{id(self)}",
            default=False,
        )
        self._agent_id: ContextVar[Any | None] = ContextVar(
            f"agentloom_bridge_agent_id_{id(self)}",
            default=None,
        )
        self._turn_observer: ContextVar[
            Callable[[ModelTurnResult], None] | None
        ] = ContextVar(
            f"agentloom_bridge_turn_observer_{id(self)}",
            default=None,
        )

    @property
    def agent_id(self) -> Any | None:
        return self._agent_id.get()

    @agent_id.setter
    def agent_id(self, value: Any | None) -> None:
        self._agent_id.set(value)

    @contextmanager
    def require_tool_calls(self) -> Iterator[None]:
        token = self._require_tool_calls.set(True)
        try:
            yield
        finally:
            self._require_tool_calls.reset(token)

    @contextmanager
    def observe_turns(
        self,
        observer: Callable[[ModelTurnResult], None],
    ) -> Iterator[None]:
        """Observe canonical model turns for the enclosing runtime run only."""

        token = self._turn_observer.set(observer)
        try:
            yield
        finally:
            self._turn_observer.reset(token)

    def generate(
        self,
        messages: list[ChatMessage | dict],
        stop_sequences: list[str] | None = None,
        response_format: dict[str, str] | None = None,
        tools_to_call_from: list[Any] | None = None,
        **kwargs: Any,
    ) -> ChatMessage:
        from agentloom.runtime.goal import get_current_goal_provider
        from agentloom.runtime.trace import get_current_local_run_id

        goal_provider = get_current_goal_provider()
        if goal_provider is not None:
            local_run_id = get_current_local_run_id()
            is_planning_request = "<end_plan>" in (stop_sequences or [])
            if is_planning_request and goal_provider.completion_settlement_pending(
                local_run_id=local_run_id,
            ):
                return ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content="Goal is complete. Skip planning and deliver the final answer now.",
                    token_usage=TokenUsage(input_tokens=0, output_tokens=0),
                )
            completion_settlement = goal_provider.assert_request_allowed(
                local_run_id=local_run_id,
                allow_completion_settlement=not is_planning_request,
            )
            if completion_settlement and tools_to_call_from is not None:
                tools_to_call_from = [
                    tool
                    for tool in tools_to_call_from
                    if getattr(tool, "name", None) == "final_answer"
                ]
            goal_provider.mark_started()

        if response_format is not None:
            raise ModelProtocolError("structured response_format is not supported by the tool runtime")
        options = dict(kwargs)
        if stop_sequences:
            options["stop"] = list(stop_sequences)
        if self._require_tool_calls.get() and tools_to_call_from:
            options["tool_choice"] = "required"
        turn = self.binding.turn(
            items=_messages_to_items(messages),
            tools=tuple(_tool_definition(tool) for tool in tools_to_call_from or ()),
            options=options,
        )
        turn_observer = self._turn_observer.get()
        if turn_observer is not None:
            turn_observer(turn)
        tool_calls: list[ChatMessageToolCall] = []
        text: list[str] = []
        available_tool_names = {
            getattr(tool, "name", "")
            for tool in tools_to_call_from or ()
            if getattr(tool, "name", "")
        }
        for item in turn.items:
            if isinstance(item, MessageItem):
                if item.role == "assistant":
                    text.append(item.text)
            elif isinstance(item, FunctionCallItem):
                if available_tool_names and item.name not in available_tool_names:
                    raise ModelProtocolError(
                        f"Tool {item.name!r} not found in registered tools "
                        f"{sorted(available_tool_names)}"
                    )
                try:
                    arguments = json.loads(item.arguments_json)
                except json.JSONDecodeError as exc:
                    raise ModelProtocolError(
                        f"Malformed tool_call for {item.name!r}: arguments must be valid JSON"
                    ) from exc
                if not isinstance(arguments, dict):
                    raise ModelProtocolError(
                        f"Malformed tool_call for {item.name!r}: arguments must be a JSON object"
                    )
                tool_calls.append(
                    ChatMessageToolCall(
                        id=item.call_id,
                        type="function",
                        function=ChatMessageToolCallFunction(
                            name=item.name,
                            arguments=arguments,
                        ),
                    )
                )
            elif isinstance(item, ReasoningItem):
                continue
            else:
                raise ModelProtocolError(
                    f"model response contains invalid output item {type(item).__name__}"
                )
        if self._require_tool_calls.get() and tools_to_call_from and not tool_calls:
            raise ModelProtocolError("model response did not contain a structured tool call")
        raw = {
            MODEL_ITEMS_RAW_KEY: [
                model_item_to_dict(item) for item in turn.items
            ],
            MODEL_RESPONSE_ID_RAW_KEY: turn.response_id,
            TOOL_CALL_RAW_KEY: bool(tool_calls),
        }
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content="".join(text),
            tool_calls=tool_calls or None,
            raw=raw,
            token_usage=TokenUsage(
                input_tokens=turn.usage.input_tokens,
                output_tokens=turn.usage.output_tokens,
            ),
        )

    def parse_tool_calls(self, message: ChatMessage) -> ChatMessage:
        if not message.tool_calls:
            raise ModelProtocolError(
                "Model response must contain native structured tool_calls; "
                "assistant text is not interpreted as a tool call"
            )
        return message
