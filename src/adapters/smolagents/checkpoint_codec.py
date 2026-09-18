"""smolagents-owned checkpoint codec for native memory and messages."""

from __future__ import annotations

import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from agentloom.adapters.smolagents.conversation_recovery import (
    prepare_steps_for_resume,
)
from agentloom.runtime.agent_runtime import RuntimeCheckpointEnvelope
from agentloom.runtime.model_protocol import (
    MODEL_ITEMS_RAW_KEY,
    MODEL_RESPONSE_ID_RAW_KEY,
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
    canonical_items = source_raw.get(MODEL_ITEMS_RAW_KEY)
    if canonical_items is not None:
        raw[MODEL_ITEMS_RAW_KEY] = [
            (
                model_item_to_dict(item)
                if not isinstance(item, Mapping)
                else deepcopy(dict(item))
            )
            for item in canonical_items
        ]
    for key in (
        MODEL_RESPONSE_ID_RAW_KEY,
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
            "code_action": step.code_action,
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
        for value in data:
            raw = dict(value)
            step_type = raw.pop(_STEP_TYPE_KEY, None)
            if step_type == "TaskStep":
                steps.append(_rebuild_task_step(raw))
            elif step_type == "ActionStep":
                steps.append(_rebuild_action_step(raw))
            elif step_type == "PlanningStep":
                steps.append(_rebuild_planning_step(raw))
        return steps

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
    def migrate_legacy_checkpoint(
        cls,
        checkpoint: Mapping[str, Any],
        *,
        runtime_version: str = "legacy",
    ) -> RuntimeCheckpointEnvelope:
        """Validate and wrap one declared legacy smolagents checkpoint."""

        raw_steps = checkpoint.get("memory_steps", [])
        if not isinstance(raw_steps, list):
            raise ValueError("legacy memory_steps must be a list")
        steps = cls.deserialize_memory_steps(raw_steps)
        if raw_steps and not steps:
            raise ValueError("legacy memory_steps contain no supported steps")
        prepare_steps_for_resume(steps)
        return RuntimeCheckpointEnvelope(
            runtime_id="smolagents",
            runtime_version=runtime_version,
            state_schema_version=1,
            payload={
                "memory_steps": deepcopy(raw_steps),
                "step_count": len(raw_steps),
            },
        )

    @classmethod
    def validate_runtime_checkpoint(
        cls,
        raw_checkpoint: Mapping[str, Any],
    ) -> RuntimeCheckpointEnvelope:
        """Validate a canonical smolagents envelope and its native payload."""

        checkpoint = RuntimeCheckpointEnvelope.from_dict(raw_checkpoint)
        checkpoint.require_compatible(
            runtime_id="smolagents",
            state_schema_version=1,
        )
        raw_steps = checkpoint.payload.get("memory_steps", [])
        if not isinstance(raw_steps, list):
            raise ValueError("smolagents memory_steps must be a list")
        steps = cls.deserialize_memory_steps(raw_steps)
        if raw_steps and not steps:
            raise ValueError("smolagents memory_steps contain no supported steps")
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
        model_output=value.get("model_output"),
        model_output_message=_rebuild_chat_message(
            value.get("model_output_message")
        ),
        observations=value.get("observations"),
        code_action=value.get("code_action"),
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
