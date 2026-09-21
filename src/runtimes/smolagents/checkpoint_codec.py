"""smolagents-owned checkpoint codec for native memory and messages."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from agentloom.runtimes.smolagents.conversation_recovery import (
    prepare_steps_for_resume,
)
from agentloom.runtimes.smolagents.recoverable_errors import (
    is_recoverable_agent_error,
    rebuild_recoverable_agent_error,
)
from agentloom.runtime.agent_runtime import RuntimeCheckpointEnvelope
from agentloom.runtimes.smolagents.error_recovery import RUNTIME_FEEDBACK_RAW_KEY
from agentloom.runtime.model_protocol import (
    MODEL_ITEMS_RAW_KEY,
    MODEL_RESPONSE_ID_RAW_KEY,
    FunctionCallItem,
    FunctionCallOutputItem,
    MessageItem,
    ModelItem,
    ReasoningItem,
    model_item_from_dict,
    model_item_to_dict,
)
from smolagents.memory import (
    ActionStep,
    MemoryStep,
    PlanningStep,
    TaskStep,
    ToolCall,
)
from smolagents.models import ChatMessage
from smolagents.monitoring import Timing, TokenUsage

_STEP_TYPE_KEY = "_step_type"
_TOOL_CALL_RAW_KEY = "agentloom_tool_call"
_TOOL_RESULT_RAW_KEY = "agentloom_tool_result"
CANONICAL_MODEL_ITEMS_KEY = "canonical_model_items"


def _canonical_items_from_message(
    message: ChatMessage | None,
) -> tuple[ModelItem, ...]:
    if message is None or not isinstance(message.raw, Mapping):
        return ()
    raw_items = message.raw.get(MODEL_ITEMS_RAW_KEY)
    if raw_items is None:
        return ()
    if not isinstance(raw_items, (list, tuple)):
        raise ValueError("canonical model items must be a list")
    result: list[ModelItem] = []
    for raw_item in raw_items:
        if isinstance(
            raw_item,
            (
                MessageItem,
                FunctionCallItem,
                FunctionCallOutputItem,
                ReasoningItem,
            ),
        ):
            result.append(raw_item)
            continue
        if not isinstance(raw_item, Mapping):
            raise ValueError("canonical model item must be an object")
        result.append(model_item_from_dict(raw_item))
    return tuple(result)


def _canonical_tool_outputs(step: ActionStep) -> tuple[FunctionCallOutputItem, ...]:
    records = getattr(step, "tool_results", None)
    if not records:
        return ()
    return tuple(
        FunctionCallOutputItem(
            call_id=record.call_id,
            output=record.model_content(),
            status=record.status,
            is_error=record.status != "completed",
            replay_payload={"record": record.to_dict()},
        )
        for record in records
    )


def _runtime_feedback_item(step: ActionStep) -> MessageItem | None:
    if not is_recoverable_agent_error(step.error):
        return None
    from agentloom.runtimes.smolagents.tool_protocol import (
        action_step_to_protocol_messages,
    )

    for message in reversed(action_step_to_protocol_messages(step)):
        raw = message.raw if isinstance(message.raw, Mapping) else {}
        if raw.get(RUNTIME_FEEDBACK_RAW_KEY) is not True:
            continue
        content = message.content
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, Mapping) and item.get("type") == "text"
            )
        else:
            raise ValueError("runtime feedback content must be text")
        return MessageItem(
            role="user",
            text=text,
            replay_payload={RUNTIME_FEEDBACK_RAW_KEY: True},
        )
    raise ValueError("AgentParsingError step lacks runtime feedback message")


def _canonical_entries_from_steps(
    steps: list[MemoryStep],
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for step_index, step in enumerate(steps):
        if isinstance(step, TaskStep):
            items: tuple[ModelItem, ...] = (
                MessageItem(role="user", text=f"New task:\n{step.task}"),
            )
            response_id = None
        elif isinstance(step, PlanningStep):
            model_items = _canonical_items_from_message(
                step.model_output_message
            )
            if step.model_output_message is not None and not model_items:
                raise ValueError(
                    f"PlanningStep {step_index} lacks canonical model items"
                )
            items = (
                *model_items,
                MessageItem(role="user", text="Now proceed and carry out this plan."),
            )
            raw = (
                step.model_output_message.raw
                if step.model_output_message is not None
                and isinstance(step.model_output_message.raw, Mapping)
                else {}
            )
            response_id = raw.get(MODEL_RESPONSE_ID_RAW_KEY)
            if response_id is not None and not isinstance(response_id, str):
                raise ValueError("canonical response_id must be a string")
        elif isinstance(step, ActionStep):
            model_items = _canonical_items_from_message(
                step.model_output_message
            )
            if step.model_output_message is not None and not model_items:
                raise ValueError(
                    f"ActionStep {step_index} lacks canonical model items"
                )
            feedback_item = _runtime_feedback_item(step)
            has_feedback = any(
                isinstance(item, MessageItem)
                and item.replay_payload.get(RUNTIME_FEEDBACK_RAW_KEY) is True
                for item in model_items
            )
            items = (
                *model_items,
                *((feedback_item,) if feedback_item is not None and not has_feedback else ()),
                *_canonical_tool_outputs(step),
            )
            raw = (
                step.model_output_message.raw
                if step.model_output_message is not None
                and isinstance(step.model_output_message.raw, Mapping)
                else {}
            )
            response_id = raw.get(MODEL_RESPONSE_ID_RAW_KEY)
            if response_id is not None and not isinstance(response_id, str):
                raise ValueError("canonical response_id must be a string")
        else:
            raise TypeError(
                f"unsupported smolagents memory step: {type(step).__name__}"
            )
        for item_index, item in enumerate(items):
            entries.append(
                {
                    "step_index": step_index,
                    "item_index": item_index,
                    "response_id": response_id,
                    "item": model_item_to_dict(item),
                }
            )
    return entries


def _deserialize_canonical_entries(
    raw_entries: Any,
    *,
    step_count: int,
) -> list[list[tuple[ModelItem, str | None]]]:
    if not isinstance(raw_entries, list):
        raise ValueError("canonical_model_items must be a list")
    by_step: list[list[tuple[ModelItem, str | None]]] = [
        [] for _ in range(step_count)
    ]
    previous_position: tuple[int, int] | None = None
    for entry in raw_entries:
        if not isinstance(entry, Mapping):
            raise ValueError("canonical model stream entry must be an object")
        step_index = entry.get("step_index")
        item_index = entry.get("item_index")
        if (
            isinstance(step_index, bool)
            or not isinstance(step_index, int)
            or not 0 <= step_index < step_count
            or isinstance(item_index, bool)
            or not isinstance(item_index, int)
            or item_index < 0
        ):
            raise ValueError("canonical model stream position is invalid")
        position = (step_index, item_index)
        if previous_position is not None and position <= previous_position:
            raise ValueError("canonical model stream is not strictly ordered")
        if item_index != len(by_step[step_index]):
            raise ValueError("canonical model stream item indexes are not contiguous")
        response_id = entry.get("response_id")
        if response_id is not None and not isinstance(response_id, str):
            raise ValueError("canonical response_id must be a string")
        raw_item = entry.get("item")
        if not isinstance(raw_item, Mapping):
            raise ValueError("canonical model stream item must be an object")
        try:
            item = model_item_from_dict(raw_item)
        except (TypeError, ValueError) as exc:
            raise ValueError("canonical model stream item is invalid") from exc
        by_step[step_index].append((item, response_id))
        previous_position = position
    return by_step


def _serialize_chat_message(
    message: ChatMessage | None,
) -> dict[str, Any] | None:
    if message is None:
        return None
    tool_calls = [
        {
            "id": call.id,
            "type": call.type,
            "function": {
                "name": call.function.name,
                "arguments": call.function.arguments,
            },
        }
        for call in message.tool_calls or ()
    ]
    raw: dict[str, Any] = {}
    source_raw = message.raw if isinstance(message.raw, dict) else {}
    for key in (
        _TOOL_CALL_RAW_KEY,
        _TOOL_RESULT_RAW_KEY,
    ):
        if key in source_raw:
            raw[key] = deepcopy(source_raw[key])
    role = getattr(message.role, "value", message.role)
    return {
        "role": role,
        "content": deepcopy(message.content),
        "tool_calls": tool_calls or None,
        "raw": raw or None,
        "token_usage": (
            asdict(message.token_usage)
            if message.token_usage is not None
            else None
        ),
    }


def _serialize_memory_step(step: MemoryStep) -> dict[str, Any]:
    if isinstance(step, TaskStep):
        return {"task": step.task, _STEP_TYPE_KEY: "TaskStep"}
    if isinstance(step, PlanningStep):
        return {
            "model_input_messages": None,
            "model_output_message": _serialize_chat_message(
                step.model_output_message
            ),
            "plan": step.plan,
            "timing": step.timing.dict(),
            "token_usage": (
                asdict(step.token_usage)
                if step.token_usage is not None
                else None
            ),
            _STEP_TYPE_KEY: "PlanningStep",
        }
    if isinstance(step, ActionStep):
        value = {
            "step_number": step.step_number,
            "timing": step.timing.dict(),
            "tool_calls": [call.dict() for call in step.tool_calls or ()],
            "error": step.error.dict() if step.error else None,
            "model_output_message": _serialize_chat_message(
                step.model_output_message
            ),
            "model_output": deepcopy(step.model_output),
            "observations": step.observations,
            "action_output": deepcopy(step.action_output),
            "token_usage": (
                asdict(step.token_usage)
                if step.token_usage is not None
                else None
            ),
            "is_final_answer": step.is_final_answer,
            _STEP_TYPE_KEY: "ActionStep",
        }
        tool_results = getattr(step, "tool_results", None)
        if tool_results:
            value["tool_results"] = [
                result.to_dict() for result in tool_results
            ]
        return value
    raise TypeError(
        f"unsupported smolagents memory step: {type(step).__name__}"
    )


class SmolagentsCheckpointCodec:
    """Serialize native smolagents state inside its runtime adapter."""

    @staticmethod
    def serialize_memory_steps(
        steps: list[MemoryStep],
    ) -> list[dict[str, Any]]:
        return [_serialize_memory_step(step) for step in steps]

    @staticmethod
    def deserialize_memory_steps(
        data: list[dict[str, Any]],
    ) -> list[MemoryStep]:
        steps: list[MemoryStep] = []
        for index, value in enumerate(data):
            if not isinstance(value, Mapping):
                raise ValueError(
                    f"smolagents memory step {index} must be an object"
                )
            raw = dict(value)
            step_type = raw.pop(_STEP_TYPE_KEY, None)
            if step_type == "TaskStep":
                steps.append(_rebuild_task_step(raw))
            elif step_type == "ActionStep":
                steps.append(_rebuild_action_step(raw))
            elif step_type == "PlanningStep":
                steps.append(_rebuild_planning_step(raw))
            else:
                raise ValueError(
                    f"unsupported smolagents memory step type: {step_type!r}"
                )
        return steps

    @staticmethod
    def serialize_canonical_model_items(
        steps: list[MemoryStep],
    ) -> list[dict[str, Any]]:
        """Return the only authoritative ordered model-history stream."""

        return _canonical_entries_from_steps(steps)

    @classmethod
    def restore_canonical_model_items(
        cls,
        steps: list[MemoryStep],
        raw_entries: Any,
    ) -> None:
        """Overwrite native model-history projections from canonical items."""

        by_step = _deserialize_canonical_entries(
            raw_entries,
            step_count=len(steps),
        )
        for step_index, (step, entries) in enumerate(
            zip(steps, by_step, strict=True)
        ):
            cls._restore_step_canonical_items(
                step,
                entries,
                step_index=step_index,
            )
        restored = cls.serialize_canonical_model_items(steps)
        expected = [
            {
                "step_index": entry["step_index"],
                "item_index": entry["item_index"],
                "response_id": entry.get("response_id"),
                "item": dict(entry["item"]),
            }
            for entry in raw_entries
        ]
        if restored != expected:
            raise ValueError(
                "restored canonical model stream does not match checkpoint"
            )

    @staticmethod
    def _restore_step_canonical_items(
        step: MemoryStep,
        entries: list[tuple[ModelItem, str | None]],
        *,
        step_index: int,
    ) -> None:
        items = [item for item, _response_id in entries]
        response_ids = {
            response_id
            for _item, response_id in entries
            if response_id is not None
        }
        if len(response_ids) > 1:
            raise ValueError(
                f"canonical step {step_index} has conflicting response IDs"
            )
        response_id = next(iter(response_ids), None)
        if isinstance(step, TaskStep):
            if len(items) != 1 or not isinstance(items[0], MessageItem):
                raise ValueError(
                    f"TaskStep {step_index} must contain one user message"
                )
            item = items[0]
            prefix = "New task:\n"
            if item.role != "user" or not item.text.startswith(prefix):
                raise ValueError(
                    f"TaskStep {step_index} canonical message is invalid"
                )
            step.task = item.text.removeprefix(prefix)
            return
        if isinstance(step, PlanningStep):
            if (
                len(items) < 2
                or not isinstance(items[-1], MessageItem)
                or items[-1]
                != MessageItem(
                    role="user",
                    text="Now proceed and carry out this plan.",
                )
            ):
                raise ValueError(
                    f"PlanningStep {step_index} canonical messages are invalid"
                )
            model_items = items[:-1]
            assistant_text = "".join(
                item.text
                for item in model_items
                if isinstance(item, MessageItem)
                and item.role == "assistant"
            )
            if not assistant_text:
                raise ValueError(
                    f"PlanningStep {step_index} lacks assistant plan text"
                )
            step.plan = assistant_text
            step.model_output_message = ChatMessage(
                role="assistant",
                content=assistant_text,
                raw={
                    MODEL_ITEMS_RAW_KEY: [
                        model_item_to_dict(item) for item in model_items
                    ],
                    MODEL_RESPONSE_ID_RAW_KEY: response_id,
                },
            )
            return
        if not isinstance(step, ActionStep):
            raise ValueError(
                f"unsupported smolagents memory step: {type(step).__name__}"
            )

        feedback_items = [
            item
            for item in items
            if isinstance(item, MessageItem)
            and item.replay_payload.get(RUNTIME_FEEDBACK_RAW_KEY) is True
        ]
        if len(feedback_items) > 1:
            raise ValueError(
                f"ActionStep {step_index} has duplicate runtime feedback items"
            )
        expected_feedback = _runtime_feedback_item(step)
        if (
            (expected_feedback is None) != (not feedback_items)
            or feedback_items
            and feedback_items[0] != expected_feedback
        ):
            raise ValueError(
                f"ActionStep {step_index} runtime feedback is inconsistent"
            )
        model_items = [
            item
            for item in items
            if not isinstance(item, FunctionCallOutputItem)
            and item not in feedback_items
        ]
        output_items = [
            item
            for item in items
            if isinstance(item, FunctionCallOutputItem)
        ]
        saw_output = False
        for item in items:
            if isinstance(item, FunctionCallOutputItem):
                saw_output = True
            elif saw_output:
                raise ValueError(
                    f"ActionStep {step_index} canonical outputs must trail model items"
                )
        calls = [
            item
            for item in model_items
            if isinstance(item, FunctionCallItem)
        ]
        call_ids = [item.call_id for item in calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError(
                f"ActionStep {step_index} has duplicate function call IDs"
            )
        output_call_ids = [item.call_id for item in output_items]
        if (
            len(output_call_ids) != len(set(output_call_ids))
            or not set(output_call_ids) <= set(call_ids)
        ):
            raise ValueError(
                f"ActionStep {step_index} function call outputs are not correlated"
            )
        if (
            calls
            and (step.observations is not None or step.is_final_answer)
            and set(output_call_ids) != set(call_ids)
        ):
            raise ValueError(
                f"ActionStep {step_index} completed calls lack canonical outputs"
            )

        from agentloom.runtime.tool_protocol import ToolCallRecord

        records: list[ToolCallRecord] = []
        for output in output_items:
            raw_record = output.replay_payload.get("record")
            if not isinstance(raw_record, Mapping):
                raise ValueError(
                    f"ActionStep {step_index} tool output lacks canonical record"
                )
            record = ToolCallRecord.from_dict(dict(raw_record))
            if (
                record.call_id != output.call_id
                or record.status != output.status
                or record.model_content() != output.output
            ):
                raise ValueError(
                    f"ActionStep {step_index} canonical tool output is inconsistent"
                )
            records.append(record)

        if model_items:
            raw = {
                MODEL_ITEMS_RAW_KEY: [
                    model_item_to_dict(item) for item in model_items
                ],
                MODEL_RESPONSE_ID_RAW_KEY: response_id,
            }
            step.model_output_message = ChatMessage(
                role="assistant",
                content="".join(
                    item.text
                    for item in model_items
                    if isinstance(item, MessageItem)
                    and item.role == "assistant"
                ),
                raw=raw,
            )
        else:
            step.model_output_message = None
        step.tool_calls = [
            ToolCall(
                name=call.name,
                arguments=deepcopy(json.loads(call.arguments_json)),
                id=call.call_id,
            )
            for call in calls
        ] or None
        step.tool_results = records

    @staticmethod
    def completed_worker_output(
        data: list[dict[str, Any]],
    ) -> tuple[bool, Any]:
        for raw in reversed(data):
            if raw.get(_STEP_TYPE_KEY) != "ActionStep":
                continue
            if (
                raw.get("is_final_answer") is True
                and raw.get("error") is None
                and "action_output" in raw
            ):
                return True, deepcopy(raw["action_output"])
            return False, None
        return False, None

    @staticmethod
    def serialize_messages(
        messages: list[ChatMessage],
    ) -> list[dict[str, Any]]:
        return [message.dict() for message in messages]

    @staticmethod
    def deserialize_messages(
        data: list[dict[str, Any]],
    ) -> list[ChatMessage]:
        return [ChatMessage.from_dict(value) for value in data]

    @classmethod
    def validate_runtime_checkpoint(
        cls,
        raw_checkpoint: Mapping[str, Any],
    ) -> RuntimeCheckpointEnvelope:
        """Validate a canonical smolagents envelope and its native payload."""

        checkpoint = RuntimeCheckpointEnvelope.from_dict(raw_checkpoint)
        checkpoint.require_compatible(
            runtime_id="smolagents",
            state_schema_version=2,
        )
        raw_steps = checkpoint.payload.get("memory_steps")
        if not isinstance(raw_steps, list):
            raise ValueError("smolagents memory_steps must be a list")
        steps = cls.deserialize_memory_steps(raw_steps)
        raw_items = checkpoint.payload.get(CANONICAL_MODEL_ITEMS_KEY)
        cls.restore_canonical_model_items(steps, raw_items)
        prepare_steps_for_resume(steps)
        return checkpoint


def _rebuild_task_step(value: dict[str, Any]) -> TaskStep:
    return TaskStep(task=value.get("task", ""))


def _rebuild_timing(raw: Any) -> Timing:
    if raw is None:
        return Timing(start_time=time.time())
    if isinstance(raw, Timing):
        return raw
    return Timing(
        start_time=raw.get("start_time", time.time()),
        end_time=raw.get("end_time"),
    )


def _rebuild_token_usage(raw: Any) -> TokenUsage | None:
    if raw is None:
        return None
    if isinstance(raw, TokenUsage):
        return raw
    return TokenUsage(
        input_tokens=raw.get("input_tokens", 0),
        output_tokens=raw.get("output_tokens", 0),
    )


def _rebuild_tool_calls(raw: Any) -> list[ToolCall] | None:
    if not raw:
        return None
    calls: list[ToolCall] = []
    for tool_call in raw:
        function = tool_call.get("function", tool_call)
        calls.append(
            ToolCall(
                name=function.get("name", ""),
                arguments=function.get("arguments", {}),
                id=tool_call.get("id", ""),
            )
        )
    return calls


def _rebuild_chat_message(raw: Any) -> ChatMessage | None:
    if raw is None:
        return None
    if isinstance(raw, ChatMessage):
        return raw
    value = dict(raw)
    raw_payload = value.pop("raw", None)
    token_usage = _rebuild_token_usage(value.pop("token_usage", None))
    if isinstance(raw_payload, dict):
        serialized_items = raw_payload.get(MODEL_ITEMS_RAW_KEY)
        if serialized_items is not None:
            if not isinstance(serialized_items, list):
                raise ValueError("canonical model items must be a list")
            raw_payload = dict(raw_payload)
            raw_payload[MODEL_ITEMS_RAW_KEY] = deepcopy(serialized_items)
    return ChatMessage.from_dict(
        value,
        raw=raw_payload,
        token_usage=token_usage,
    )


def _rebuild_action_step(value: dict[str, Any]) -> ActionStep:
    step = ActionStep(
        step_number=value.get("step_number", 0),
        timing=_rebuild_timing(value.get("timing")),
        tool_calls=_rebuild_tool_calls(value.get("tool_calls")),
        error=rebuild_recoverable_agent_error(value.get("error")),
        model_output=value.get("model_output"),
        model_output_message=_rebuild_chat_message(
            value.get("model_output_message")
        ),
        observations=value.get("observations"),
        action_output=value.get("action_output"),
        token_usage=_rebuild_token_usage(value.get("token_usage")),
        is_final_answer=value.get("is_final_answer", False),
    )
    raw_results = value.get("tool_results")
    if raw_results:
        from agentloom.runtime.tool_protocol import ToolCallRecord

        step.tool_results = [
            ToolCallRecord.from_dict(item) for item in raw_results
        ]
    return step


def _rebuild_planning_step(value: dict[str, Any]) -> PlanningStep:
    model_input_messages = []
    raw_input = value.get("model_input_messages")
    if raw_input:
        model_input_messages = [
            ChatMessage.from_dict(message) for message in raw_input
        ]

    model_output_message = _rebuild_chat_message(
        value.get("model_output_message")
    )
    if model_output_message is None:
        model_output_message = ChatMessage(
            role="assistant",
            content=value.get("plan", ""),
        )

    return PlanningStep(
        model_input_messages=model_input_messages,
        model_output_message=model_output_message,
        plan=value.get("plan", ""),
        timing=_rebuild_timing(value.get("timing")),
        token_usage=_rebuild_token_usage(value.get("token_usage")),
    )
