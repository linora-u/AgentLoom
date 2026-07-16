"""Tests for ``src.lib.checkpoint.serializer.CheckpointSerializer``."""

from __future__ import annotations

import time

import pytest
from smolagents.memory import ActionStep, PlanningStep, TaskStep, ToolCall
from smolagents.models import ChatMessage, MessageRole
from smolagents.monitoring import Timing, TokenUsage

from src.lib.checkpoint.serializer import CheckpointSerializer

# ── helpers ──────────────────────────────────────────────────────────────

def _make_timing(dur: float = 1.0) -> Timing:
    t = time.time()
    return Timing(start_time=t, end_time=t + dur)


def _make_token_usage(inp: int = 100, out: int = 50) -> TokenUsage:
    return TokenUsage(input_tokens=inp, output_tokens=out)


# ── ActionStep round-trip ────────────────────────────────────────────────


class TestSerializeActionStep:

    def test_roundtrip_basic(self):
        step = ActionStep(step_number=1, timing=_make_timing(), observations="hello world")
        data = CheckpointSerializer.serialize_memory_steps([step])
        assert len(data) == 1
        assert data[0]["_step_type"] == "ActionStep"

        rebuilt = CheckpointSerializer.deserialize_memory_steps(data)
        assert len(rebuilt) == 1
        assert isinstance(rebuilt[0], ActionStep)
        assert rebuilt[0].observations == "hello world"
        assert rebuilt[0].step_number == 1

    def test_roundtrip_with_tool_calls(self):
        tc = ToolCall(name="shell_tool", arguments={"cmd": "ls"}, id="call_abc")
        step = ActionStep(
            step_number=2,
            timing=_make_timing(),
            tool_calls=[tc],
            observations="file1.py\nfile2.py",
        )
        data = CheckpointSerializer.serialize_memory_steps([step])
        rebuilt = CheckpointSerializer.deserialize_memory_steps(data)
        r = rebuilt[0]
        assert r.tool_calls is not None
        assert len(r.tool_calls) == 1
        assert r.tool_calls[0].name == "shell_tool"
        assert r.tool_calls[0].arguments == {"cmd": "ls"}
        assert r.tool_calls[0].id == "call_abc"

    def test_roundtrip_with_token_usage(self):
        step = ActionStep(
            step_number=3,
            timing=_make_timing(),
            token_usage=_make_token_usage(200, 80),
        )
        data = CheckpointSerializer.serialize_memory_steps([step])
        rebuilt = CheckpointSerializer.deserialize_memory_steps(data)
        assert rebuilt[0].token_usage is not None
        assert rebuilt[0].token_usage.input_tokens == 200
        assert rebuilt[0].token_usage.output_tokens == 80

    def test_roundtrip_with_model_output(self):
        step = ActionStep(
            step_number=1,
            timing=_make_timing(),
            model_output="I will call shell_tool to list files.",
            observations="output here",
        )
        data = CheckpointSerializer.serialize_memory_steps([step])
        rebuilt = CheckpointSerializer.deserialize_memory_steps(data)
        assert rebuilt[0].model_output == "I will call shell_tool to list files."

    def test_skip_model_input_messages(self):
        """model_input_messages should be stripped during serialisation."""
        step = ActionStep(
            step_number=1,
            timing=_make_timing(),
            model_input_messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            observations="done",
        )
        data = CheckpointSerializer.serialize_memory_steps([step])
        assert "model_input_messages" not in data[0]

    def test_skip_observations_images(self):
        """observations_images should be stripped (PIL.Image can't serialise)."""
        from unittest.mock import MagicMock
        fake_img = MagicMock()
        fake_img.tobytes.return_value = b"fake"
        step = ActionStep(step_number=1, timing=_make_timing())
        step.observations_images = [fake_img]
        data = CheckpointSerializer.serialize_memory_steps([step])
        assert "observations_images" not in data[0]


# ── TaskStep ─────────────────────────────────────────────────────────────


class TestSerializeTaskStep:

    def test_roundtrip(self):
        step = TaskStep(task="Analyse the codebase")
        data = CheckpointSerializer.serialize_memory_steps([step])
        assert data[0]["_step_type"] == "TaskStep"
        rebuilt = CheckpointSerializer.deserialize_memory_steps(data)
        assert isinstance(rebuilt[0], TaskStep)
        assert rebuilt[0].task == "Analyse the codebase"

    def test_skip_task_images(self):
        step = TaskStep(task="foo")
        step.task_images = ["fake_image"]  # type: ignore[assignment]
        data = CheckpointSerializer.serialize_memory_steps([step])
        assert "task_images" not in data[0]


# ── PlanningStep ─────────────────────────────────────────────────────────


class TestSerializePlanningStep:

    def test_roundtrip(self):
        msg = ChatMessage(role=MessageRole.ASSISTANT, content="My plan.")
        step = PlanningStep(
            model_input_messages=[],
            model_output_message=msg,
            plan="1. Scan directory\n2. Analyse files",
            timing=_make_timing(),
        )
        data = CheckpointSerializer.serialize_memory_steps([step])
        assert data[0]["_step_type"] == "PlanningStep"
        rebuilt = CheckpointSerializer.deserialize_memory_steps(data)
        assert isinstance(rebuilt[0], PlanningStep)
        assert rebuilt[0].plan == "1. Scan directory\n2. Analyse files"


# ── Mixed steps ──────────────────────────────────────────────────────────


class TestSerializeMixedSteps:

    def test_full_conversation(self):
        steps = [
            TaskStep(task="Review code quality"),
            ActionStep(
                step_number=1,
                timing=_make_timing(),
                model_output="Starting scan...",
                tool_calls=[ToolCall(name="shell_tool", arguments={"cmd": "ls"}, id="c1")],
                observations="file1.py",
            ),
            PlanningStep(
                model_input_messages=[],
                model_output_message=ChatMessage(role=MessageRole.ASSISTANT, content="plan"),
                plan="Step 2: deep review",
                timing=_make_timing(),
            ),
            ActionStep(
                step_number=2,
                timing=_make_timing(),
                observations="All checks passed",
                is_final_answer=True,
            ),
        ]

        data = CheckpointSerializer.serialize_memory_steps(steps)
        assert len(data) == 4

        rebuilt = CheckpointSerializer.deserialize_memory_steps(data)
        assert len(rebuilt) == 4
        assert isinstance(rebuilt[0], TaskStep)
        assert isinstance(rebuilt[1], ActionStep)
        assert isinstance(rebuilt[2], PlanningStep)
        assert isinstance(rebuilt[3], ActionStep)
        assert rebuilt[3].is_final_answer is True


# ── ChatMessage serialisation ────────────────────────────────────────────


class TestSerializeMessages:

    def test_roundtrip(self):
        msgs = [
            ChatMessage(role=MessageRole.USER, content="hello"),
            ChatMessage(role=MessageRole.ASSISTANT, content="world"),
        ]
        data = CheckpointSerializer.serialize_messages(msgs)
        assert len(data) == 2
        rebuilt = CheckpointSerializer.deserialize_messages(data)
        assert rebuilt[0].role == MessageRole.USER
        assert rebuilt[1].content == "world"
