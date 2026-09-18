"""
Serialize / deserialize smolagents ``MemoryStep`` objects for checkpoint persistence.

Handles ``TaskStep``, ``ActionStep``, ``PlanningStep`` and their nested
dataclass fields (``ToolCall``, ``Timing``, ``TokenUsage``, ``ChatMessage``).

Design decisions:
- ``model_input_messages`` is **not** persisted: every LLM step rebuilds
  them from ``write_memory_to_messages()`` so they are redundant.
- ``observations_images`` / ``task_images`` are **skipped**: ``PIL.Image``
  objects cannot be JSON-serialised and very few AgentLoom workflows use them.
- AgentLoom canonical model items and response IDs are explicitly persisted
  from ``ChatMessage.raw``. Arbitrary provider objects are not persisted.
"""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from agentloom.runtime.model_protocol import (
    MODEL_ITEMS_RAW_KEY,
    MODEL_RESPONSE_ID_RAW_KEY,
    model_item_from_dict,
    model_item_to_dict,
)
from smolagents.memory import ActionStep, MemoryStep, PlanningStep, TaskStep, ToolCall
from smolagents.models import ChatMessage
from smolagents.monitoring import Timing, TokenUsage

# ── step-type discriminator key ──────────────────────────────────────────
_STEP_TYPE_KEY = "_step_type"
_TOOL_CALL_RAW_KEY = "agentloom_tool_call"
_TOOL_RESULT_RAW_KEY = "agentloom_tool_result"


def _serialize_chat_message(message: ChatMessage | None) -> dict[str, Any] | None:
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
            model_item_to_dict(item) for item in canonical_items
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
            asdict(message.token_usage) if message.token_usage is not None else None
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
                asdict(step.token_usage) if step.token_usage is not None else None
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
                asdict(step.token_usage) if step.token_usage is not None else None
            ),
            "is_final_answer": step.is_final_answer,
            _STEP_TYPE_KEY: "ActionStep",
        }
        tool_results = getattr(step, "tool_results", None)
        if tool_results:
            value["tool_results"] = [result.to_dict() for result in tool_results]
        return value
    raise TypeError(f"unsupported smolagents memory step: {type(step).__name__}")


# =========================================================================
# Public API
# =========================================================================


class CheckpointSerializer:
    """Stateless serializer: every method is a ``@staticmethod``."""

    # ── serialise (MemoryStep → dict) ────────────────────────────────────

    @staticmethod
    def serialize_memory_steps(steps: list[MemoryStep]) -> list[dict]:
        """Convert a list of ``MemoryStep`` objects to JSON-safe dicts.

        Each dict contains a ``_step_type`` discriminator so that
        ``deserialize_memory_steps`` can reconstruct the correct class.
        """
        result: list[dict] = []
        for step in steps:
            result.append(_serialize_memory_step(step))
        return result

    # ── deserialise (dict → MemoryStep) ──────────────────────────────────

    @staticmethod
    def deserialize_memory_steps(data: list[dict]) -> list[MemoryStep]:
        """Reconstruct a list of ``MemoryStep`` from serialised dicts."""
        steps: list[MemoryStep] = []
        for d in data:
            d = dict(d)                     # shallow copy – don't mutate caller
            step_type = d.pop(_STEP_TYPE_KEY, None)
            if step_type == "TaskStep":
                steps.append(_rebuild_task_step(d))
            elif step_type == "ActionStep":
                steps.append(_rebuild_action_step(d))
            elif step_type == "PlanningStep":
                steps.append(_rebuild_planning_step(d))
            else:
                # Unknown types are silently skipped so that forward-compat
                # checkpoint files don't crash the deserialiser.
                pass
        return steps

    @staticmethod
    def completed_worker_output(data: list[dict]) -> tuple[bool, Any]:
        """Read the committed final answer, independent of stored display text.

        Older checkpoints may have stored a stringified RunResult (possibly
        compressed) in ``result``. The existing ActionStep schema already keeps
        its exact output separately. Never evaluate model code or parse reprs.
        The boolean distinguishes a real ``None`` answer from missing evidence.
        """
        for raw in reversed(data):
            if raw.get(_STEP_TYPE_KEY) != "ActionStep":
                continue
            if raw.get("is_final_answer") is True and raw.get("error") is None and "action_output" in raw:
                return True, deepcopy(raw["action_output"])
            return False, None
        return False, None

    # ── conversation-level serialisation ─────────────────────────────────

    @staticmethod
    def serialize_messages(messages: list[ChatMessage]) -> list[dict]:
        """Serialise a list of ``ChatMessage`` to JSON-safe dicts."""
        return [msg.dict() for msg in messages]

    @staticmethod
    def deserialize_messages(data: list[dict]) -> list[ChatMessage]:
        """Reconstruct ``ChatMessage`` objects from dicts."""
        return [ChatMessage.from_dict(d) for d in data]


# =========================================================================
# Internal rebuilders
# =========================================================================


def _rebuild_task_step(d: dict) -> TaskStep:
    return TaskStep(task=d.get("task", ""))


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
    for tc in raw:
        func = tc.get("function", tc)
        calls.append(ToolCall(
            name=func.get("name", ""),
            arguments=func.get("arguments", {}),
            id=tc.get("id", ""),
        ))
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
            raw_payload[MODEL_ITEMS_RAW_KEY] = tuple(
                model_item_from_dict(item) for item in serialized_items
            )
    return ChatMessage.from_dict(
        value,
        raw=raw_payload,
        token_usage=token_usage,
    )


def _rebuild_action_step(d: dict) -> ActionStep:
    step = ActionStep(
        step_number=d.get("step_number", 0),
        timing=_rebuild_timing(d.get("timing")),
        tool_calls=_rebuild_tool_calls(d.get("tool_calls")),
        model_output=d.get("model_output"),
        model_output_message=_rebuild_chat_message(d.get("model_output_message")),
        observations=d.get("observations"),
        code_action=d.get("code_action"),
        action_output=d.get("action_output"),
        token_usage=_rebuild_token_usage(d.get("token_usage")),
        is_final_answer=d.get("is_final_answer", False),
        # ``error`` – kept as dict/str rather than reconstructing AgentError
        # which has internal-only fields.  Deserialized checkpoints don't
        # replay errors – they only serve as historical context.
    )
    raw_results = d.get("tool_results")
    if raw_results:
        from agentloom.runtime.tool_protocol import ToolCallRecord

        step.tool_results = [ToolCallRecord.from_dict(item) for item in raw_results]
    return step


def _rebuild_planning_step(d: dict) -> PlanningStep:
    model_input_messages = []
    raw_input = d.get("model_input_messages")
    if raw_input:
        model_input_messages = [ChatMessage.from_dict(m) for m in raw_input]

    model_output_message = _rebuild_chat_message(d.get("model_output_message"))
    if model_output_message is None:
        # PlanningStep requires a model_output_message; provide a stub.
        model_output_message = ChatMessage(role="assistant", content=d.get("plan", ""))

    return PlanningStep(
        model_input_messages=model_input_messages,
        model_output_message=model_output_message,
        plan=d.get("plan", ""),
        timing=_rebuild_timing(d.get("timing")),
        token_usage=_rebuild_token_usage(d.get("token_usage")),
    )
