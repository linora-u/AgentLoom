"""
Tests for src.lib.checkpoint.conversation_recovery.

Covers:
- filter_unresolved_tool_uses: normal, boundary, edge cases
- filter_orphaned_thinking: normal, boundary, edge cases
- filter_empty_steps: normal, boundary, edge cases
- detect_turn_interruption: all classification branches
- prepare_steps_for_resume: full pipeline integration tests
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from src.lib.checkpoint.conversation_recovery import (
    TurnInterruptionState,
    detect_turn_interruption,
    filter_empty_steps,
    filter_orphaned_thinking,
    filter_unresolved_tool_uses,
    prepare_steps_for_resume,
)

# ---------------------------------------------------------------------------
# Lightweight step stubs — avoid heavy smolagents import for unit tests.
# Duck-typed to match ActionStep / TaskStep / PlanningStep attributes.
# ---------------------------------------------------------------------------


@dataclass
class _FakeActionStep:
    """Minimal ActionStep stub."""
    tool_calls: Optional[list[dict[str, Any]]] = None
    observations: Optional[str] = None
    model_output: Optional[str] = None
    action_output: Optional[str] = None
    is_final_answer: bool = False
    step_number: int = 0

    # The recovery module uses type(step).__name__ for duck-typed checks.
    def __class_getitem__(cls, _):
        return cls


# Make type(instance).__name__ return "ActionStep"
_FakeActionStep.__name__ = "ActionStep"  # type: ignore[attr-defined]


@dataclass
class _FakeTaskStep:
    """Minimal TaskStep stub."""
    task: str = "do something"
    step_number: int = 0


_FakeTaskStep.__name__ = "TaskStep"  # type: ignore[attr-defined]


@dataclass
class _FakePlanningStep:
    """Minimal PlanningStep stub."""
    plan: str = "plan text"
    model_input_messages: list = field(default_factory=list)
    model_output_message: dict = field(default_factory=dict)
    step_number: int = 0


_FakePlanningStep.__name__ = "PlanningStep"  # type: ignore[attr-defined]


# ===================================================================
# filter_unresolved_tool_uses
# ===================================================================


class TestFilterUnresolvedToolUses:
    """Tests for filter_unresolved_tool_uses()."""

    def test_keeps_step_with_tool_calls_and_observations(self):
        """Normal: ActionStep with tool_calls + observations is kept."""
        step = _FakeActionStep(
            tool_calls=[{"name": "read_file", "id": "1"}],
            observations="file content here",
        )
        result = filter_unresolved_tool_uses([step])
        assert len(result) == 1
        assert result[0] is step

    def test_drops_step_with_tool_calls_no_observations(self):
        """Normal: ActionStep with tool_calls but no observations is dropped."""
        step = _FakeActionStep(
            tool_calls=[{"name": "read_file", "id": "1"}],
            observations=None,
        )
        result = filter_unresolved_tool_uses([step])
        assert len(result) == 0

    def test_keeps_final_answer_even_without_observations(self):
        """Normal: is_final_answer=True is always kept."""
        step = _FakeActionStep(
            tool_calls=[{"name": "final_answer", "id": "1"}],
            observations=None,
            is_final_answer=True,
        )
        result = filter_unresolved_tool_uses([step])
        assert len(result) == 1

    def test_empty_list_returns_empty(self):
        """Boundary: Empty input returns empty output."""
        result = filter_unresolved_tool_uses([])
        assert result == []

    def test_mixed_list_filters_only_unresolved(self):
        """Boundary: Mixed list — only unresolved steps are dropped."""
        good1 = _FakeActionStep(tool_calls=[{"name": "a"}], observations="ok")
        bad1 = _FakeActionStep(tool_calls=[{"name": "b"}], observations=None)
        good2 = _FakeActionStep(tool_calls=[{"name": "c"}], observations="ok")
        bad2 = _FakeActionStep(tool_calls=[{"name": "d"}], observations=None)
        good3 = _FakeActionStep(model_output="thinking", tool_calls=None)

        result = filter_unresolved_tool_uses([good1, bad1, good2, bad2, good3])
        assert len(result) == 3
        assert result == [good1, good2, good3]

    def test_task_step_always_kept(self):
        """Boundary: TaskStep (not ActionStep) is always kept."""
        task = _FakeTaskStep(task="initial task")
        result = filter_unresolved_tool_uses([task])
        assert len(result) == 1
        assert result[0] is task

    def test_planning_step_always_kept(self):
        """Boundary: PlanningStep is always kept."""
        plan = _FakePlanningStep(plan="step 1")
        result = filter_unresolved_tool_uses([plan])
        assert len(result) == 1

    def test_no_tool_calls_keeps_step(self):
        """Edge: ActionStep with no tool_calls at all is kept."""
        step = _FakeActionStep(model_output="just thinking", tool_calls=None)
        result = filter_unresolved_tool_uses([step])
        assert len(result) == 1

    def test_empty_tool_calls_list_keeps_step(self):
        """Edge: Empty tool_calls list is falsy — step is kept."""
        step = _FakeActionStep(tool_calls=[], observations=None)
        result = filter_unresolved_tool_uses([step])
        assert len(result) == 1

    def test_empty_observations_string_is_falsy(self):
        """Edge: Empty string observations is falsy — step is dropped if it has tool_calls."""
        step = _FakeActionStep(
            tool_calls=[{"name": "edit_file"}],
            observations="",
        )
        result = filter_unresolved_tool_uses([step])
        assert len(result) == 0


# ===================================================================
# filter_orphaned_thinking
# ===================================================================


class TestFilterOrphanedThinking:
    """Tests for filter_orphaned_thinking()."""

    def test_keeps_step_with_model_output_and_tool_calls(self):
        """Normal: model_output + tool_calls means action was taken."""
        step = _FakeActionStep(
            model_output="I'll read the file",
            tool_calls=[{"name": "read_file"}],
        )
        result = filter_orphaned_thinking([step])
        assert len(result) == 1

    def test_drops_orphaned_thinking_only(self):
        """Normal: model_output but no tool_calls/action_output is orphaned."""
        step = _FakeActionStep(
            model_output="Let me think about this...",
            tool_calls=None,
            action_output=None,
        )
        result = filter_orphaned_thinking([step])
        assert len(result) == 0

    def test_keeps_step_with_action_output(self):
        """Normal: model_output + action_output means code was executed."""
        step = _FakeActionStep(
            model_output="result = 42",
            action_output="42",
            tool_calls=None,
        )
        result = filter_orphaned_thinking([step])
        assert len(result) == 1

    def test_keeps_final_answer_thinking_only(self):
        """Boundary: is_final_answer=True + model_output only is kept."""
        step = _FakeActionStep(
            model_output="The answer is done.",
            is_final_answer=True,
        )
        result = filter_orphaned_thinking([step])
        assert len(result) == 1

    def test_task_step_always_kept(self):
        """Boundary: TaskStep is never filtered."""
        task = _FakeTaskStep()
        result = filter_orphaned_thinking([task])
        assert len(result) == 1

    def test_mixed_types(self):
        """Boundary: Mixed step types — only orphaned ActionSteps dropped."""
        task = _FakeTaskStep()
        good = _FakeActionStep(model_output="ok", tool_calls=[{"name": "x"}])
        orphan = _FakeActionStep(model_output="thinking...")
        plan = _FakePlanningStep()

        result = filter_orphaned_thinking([task, good, orphan, plan])
        assert len(result) == 3
        assert orphan not in result

    def test_no_model_output_not_dropped(self):
        """Edge: ActionStep with no model_output at all is not orphaned."""
        step = _FakeActionStep(model_output=None, tool_calls=None)
        result = filter_orphaned_thinking([step])
        # Not dropped by orphaned thinking (has no model_output)
        assert len(result) == 1


# ===================================================================
# filter_empty_steps
# ===================================================================


class TestFilterEmptySteps:
    """Tests for filter_empty_steps()."""

    def test_drops_completely_empty_action_step(self):
        """Normal: ActionStep with nothing is dropped."""
        step = _FakeActionStep()
        result = filter_empty_steps([step])
        assert len(result) == 0

    def test_keeps_step_with_model_output(self):
        """Normal: ActionStep with model_output is not empty."""
        step = _FakeActionStep(model_output="thinking")
        result = filter_empty_steps([step])
        assert len(result) == 1

    def test_keeps_step_with_tool_calls(self):
        """Normal: ActionStep with tool_calls is not empty."""
        step = _FakeActionStep(tool_calls=[{"name": "x"}])
        result = filter_empty_steps([step])
        assert len(result) == 1

    def test_keeps_step_with_observations(self):
        """Normal: ActionStep with observations is not empty."""
        step = _FakeActionStep(observations="some output")
        result = filter_empty_steps([step])
        assert len(result) == 1

    def test_keeps_step_with_action_output(self):
        """Normal: ActionStep with action_output is not empty."""
        step = _FakeActionStep(action_output="42")
        result = filter_empty_steps([step])
        assert len(result) == 1

    def test_keeps_final_answer_even_if_empty(self):
        """Boundary: is_final_answer=True is always kept."""
        step = _FakeActionStep(is_final_answer=True)
        result = filter_empty_steps([step])
        assert len(result) == 1

    def test_planning_step_always_kept(self):
        """Boundary: PlanningStep with no content is not filtered."""
        plan = _FakePlanningStep()
        result = filter_empty_steps([plan])
        assert len(result) == 1

    def test_task_step_always_kept(self):
        """Boundary: TaskStep is always kept."""
        task = _FakeTaskStep()
        result = filter_empty_steps([task])
        assert len(result) == 1

    def test_empty_list(self):
        """Boundary: Empty input returns empty output."""
        result = filter_empty_steps([])
        assert result == []


# ===================================================================
# detect_turn_interruption
# ===================================================================


class TestDetectTurnInterruption:
    """Tests for detect_turn_interruption()."""

    def test_empty_list_returns_none(self):
        """Boundary: Empty step list → kind='none'."""
        state = detect_turn_interruption([])
        assert state.kind == "none"

    def test_last_step_is_final_answer(self):
        """Normal: Last step with is_final_answer=True → kind='none'."""
        step = _FakeActionStep(is_final_answer=True, model_output="done")
        state = detect_turn_interruption([step])
        assert state.kind == "none"

    def test_last_step_has_observations(self):
        """Normal: Last step with observations → kind='none' (completed tool call)."""
        step = _FakeActionStep(
            tool_calls=[{"name": "read_file"}],
            observations="file content",
        )
        state = detect_turn_interruption([step])
        assert state.kind == "none"

    def test_last_step_has_tool_calls_no_observations(self):
        """Normal: Last step has tool_calls but no observations → interrupted_turn."""
        step = _FakeActionStep(
            tool_calls=[{"name": "edit_file"}],
            observations=None,
        )
        state = detect_turn_interruption([step])
        assert state.kind == "interrupted_turn"

    def test_last_step_has_model_output_only(self):
        """Normal: Last step has model_output but nothing else → interrupted_turn."""
        step = _FakeActionStep(model_output="I will...")
        state = detect_turn_interruption([step])
        assert state.kind == "interrupted_turn"

    def test_last_step_is_task_step(self):
        """Boundary: Last step is TaskStep → kind='none'."""
        task = _FakeTaskStep()
        state = detect_turn_interruption([task])
        assert state.kind == "none"

    def test_last_step_is_planning_step(self):
        """Boundary: Last step is PlanningStep → kind='none'."""
        plan = _FakePlanningStep()
        state = detect_turn_interruption([plan])
        assert state.kind == "none"

    def test_multiple_steps_only_last_matters(self):
        """Normal: Only the last step determines interruption state."""
        good = _FakeActionStep(
            tool_calls=[{"name": "x"}], observations="ok"
        )
        interrupted = _FakeActionStep(
            tool_calls=[{"name": "y"}], observations=None
        )
        state = detect_turn_interruption([good, interrupted])
        assert state.kind == "interrupted_turn"

    def test_multiple_steps_last_is_clean(self):
        """Normal: Last step is clean despite earlier issues."""
        bad = _FakeActionStep(tool_calls=[{"name": "x"}], observations=None)
        clean = _FakeActionStep(
            tool_calls=[{"name": "y"}], observations="done"
        )
        state = detect_turn_interruption([bad, clean])
        assert state.kind == "none"

    def test_completely_empty_action_step(self):
        """Edge: Completely empty ActionStep → interrupted_turn."""
        step = _FakeActionStep()
        state = detect_turn_interruption([step])
        assert state.kind == "interrupted_turn"


# ===================================================================
# prepare_steps_for_resume (integration)
# ===================================================================


class TestPrepareStepsForResume:
    """Integration tests for the full pipeline."""

    def test_full_pipeline_mixed_bad_steps(self):
        """Normal: 10 steps with 2 unresolved + 1 orphaned + 1 empty → 6 clean steps."""
        task = _FakeTaskStep(task="analyze code")
        good1 = _FakeActionStep(tool_calls=[{"name": "a"}], observations="ok", step_number=1)
        unresolved1 = _FakeActionStep(tool_calls=[{"name": "b"}], step_number=2)
        good2 = _FakeActionStep(tool_calls=[{"name": "c"}], observations="data", step_number=3)
        orphan = _FakeActionStep(model_output="thinking...", step_number=4)
        good3 = _FakeActionStep(tool_calls=[{"name": "d"}], observations="result", step_number=5)
        unresolved2 = _FakeActionStep(tool_calls=[{"name": "e"}], step_number=6)
        empty = _FakeActionStep(step_number=7)
        good4 = _FakeActionStep(tool_calls=[{"name": "f"}], observations="done", step_number=8)
        plan = _FakePlanningStep(plan="next steps")

        all_steps = [task, good1, unresolved1, good2, orphan, good3, unresolved2, empty, good4, plan]
        cleaned, interruption = prepare_steps_for_resume(all_steps)

        # task + good1 + good2 + good3 + good4 + plan = 6 steps
        assert len(cleaned) == 6
        assert task in cleaned
        assert good1 in cleaned
        assert good2 in cleaned
        assert good3 in cleaned
        assert good4 in cleaned
        assert plan in cleaned
        # Last step is PlanningStep → interruption = none
        assert interruption.kind == "none"

    def test_all_clean_with_final_answer(self):
        """Normal: All clean steps, last has final_answer → interruption=none."""
        task = _FakeTaskStep()
        step1 = _FakeActionStep(tool_calls=[{"name": "a"}], observations="ok")
        final = _FakeActionStep(is_final_answer=True, model_output="The answer is 42")

        cleaned, interruption = prepare_steps_for_resume([task, step1, final])
        assert len(cleaned) == 3
        assert interruption.kind == "none"

    def test_all_clean_last_has_tool_calls_only(self):
        """Normal: All clean but last has tool_calls only → interrupted_turn."""
        task = _FakeTaskStep()
        step1 = _FakeActionStep(tool_calls=[{"name": "a"}], observations="ok")
        incomplete = _FakeActionStep(tool_calls=[{"name": "b"}])

        cleaned, interruption = prepare_steps_for_resume([task, step1, incomplete])
        # incomplete is dropped by filter_unresolved_tool_uses
        assert len(cleaned) == 2
        # After filtering, last step is step1 which has observations → none
        assert interruption.kind == "none"

    def test_single_task_step(self):
        """Boundary: Single TaskStep → returned as-is, interruption=none."""
        task = _FakeTaskStep()
        cleaned, interruption = prepare_steps_for_resume([task])
        assert len(cleaned) == 1
        assert cleaned[0] is task
        assert interruption.kind == "none"

    def test_empty_input(self):
        """Boundary: Empty input → empty output, interruption=none."""
        cleaned, interruption = prepare_steps_for_resume([])
        assert cleaned == []
        assert interruption.kind == "none"

    def test_pipeline_order_matters(self):
        """Verify filtering order: unresolved first, then orphaned, then empty.

        An ActionStep with tool_calls but no observations would be caught
        by filter_unresolved_tool_uses before filter_empty_steps gets to it.
        """
        # This step has tool_calls → caught by unresolved filter
        step = _FakeActionStep(tool_calls=[{"name": "x"}])
        cleaned, _ = prepare_steps_for_resume([step])
        assert len(cleaned) == 0

    def test_returns_correct_type(self):
        """Verify return types are correct."""
        steps = [_FakeTaskStep()]
        cleaned, interruption = prepare_steps_for_resume(steps)
        assert isinstance(cleaned, list)
        assert isinstance(interruption, TurnInterruptionState)
        assert interruption.kind in ("none", "interrupted_turn")

    def test_interrupted_turn_after_pipeline(self):
        """Normal: After filtering, last step indicates interrupted_turn."""
        task = _FakeTaskStep()
        good = _FakeActionStep(tool_calls=[{"name": "a"}], observations="ok")
        # This step has model_output only — won't be dropped by any filter
        # (it has model_output so not empty, has no tool_calls so not unresolved)
        # BUT it is orphaned thinking → will be dropped
        thinking = _FakeActionStep(model_output="I will now...")

        cleaned, interruption = prepare_steps_for_resume([task, good, thinking])
        # thinking is dropped by orphaned filter
        assert len(cleaned) == 2
        # After filtering, last is good (has observations) → none
        assert interruption.kind == "none"

    def test_last_step_is_action_with_only_model_output_and_action_output(self):
        """Edge: Step with model_output + action_output is kept and detected correctly."""
        step = _FakeActionStep(model_output="result = 42", action_output="42")
        cleaned, interruption = prepare_steps_for_resume([step])
        assert len(cleaned) == 1
        # Has no observations → interrupted_turn
        assert interruption.kind == "interrupted_turn"
