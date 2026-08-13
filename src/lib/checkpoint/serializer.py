"""
Serialize / deserialize smolagents ``MemoryStep`` objects for checkpoint persistence.

Handles ``TaskStep``, ``ActionStep``, ``PlanningStep`` and their nested
dataclass fields (``ToolCall``, ``Timing``, ``TokenUsage``, ``ChatMessage``).

Design decisions:
- ``model_input_messages`` is **not** persisted: every LLM step rebuilds
  them from ``write_memory_to_messages()`` so they are redundant.
- ``observations_images`` / ``task_images`` are **skipped**: ``PIL.Image``
  objects cannot be JSON-serialised and very few AgentLoom workflows use them.
- ``raw`` on ``ChatMessage`` is excluded by the upstream ``dict()`` helper.
"""

from __future__ import annotations

import time
from typing import Any

from smolagents.memory import ActionStep, MemoryStep, PlanningStep, TaskStep, ToolCall
from smolagents.models import ChatMessage
from smolagents.monitoring import Timing, TokenUsage

# ── step-type discriminator key ──────────────────────────────────────────
_STEP_TYPE_KEY = "_step_type"


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
            d = step.dict()
            d[_STEP_TYPE_KEY] = type(step).__name__

            # Drop non-serialisable / redundant fields.
            d.pop("observations_images", None)
            d.pop("task_images", None)
            d.pop("model_input_messages", None)
            tool_results = getattr(step, "tool_results", None)
            if tool_results:
                d["tool_results"] = [result.to_dict() for result in tool_results]
            result.append(d)
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
    return ChatMessage.from_dict(raw)


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
        from src.lib.smolagents.tool_protocol import ToolCallRecord

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
