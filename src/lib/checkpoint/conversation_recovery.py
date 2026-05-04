"""
Conversation recovery pipeline for checkpoint resume.

Ported from the reference implementation's conversationRecovery.ts +
messages.ts filtering logic, adapted for smolagents MemoryStep objects.

The pipeline cleans up persisted memory steps before feeding them back to
the LLM on resume. It handles:

1. Unresolved tool uses — ActionSteps with tool_calls but no observations
   (interrupted mid-tool execution).
2. Orphaned thinking — ActionSteps with model_output but no tool_calls and
   no action_output (crash during streaming before any action was decided).
3. Empty steps — ActionSteps with no content at all (crash before LLM
   responded).
4. Turn interruption detection — classifies whether the session ended
   cleanly or was interrupted.

Usage::

    from src.lib.checkpoint.conversation_recovery import prepare_steps_for_resume

    steps, interruption = prepare_steps_for_resume(raw_steps)
    # steps: cleaned list of MemoryStep
    # interruption: TurnInterruptionState with .kind in ("none", "interrupted_turn")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.lib.logging import get_logger

if TYPE_CHECKING:
    from smolagents.memory import MemoryStep

_logger = get_logger(__name__)


@dataclass
class TurnInterruptionState:
    """Classification of how the last turn ended.

    Attributes:
        kind: One of:
            - ``"none"``: Session ended cleanly (final answer or completed
              tool call).
            - ``"interrupted_turn"``: Session was interrupted mid-turn
              (e.g. Ctrl-C while tool was running, crash during streaming).
    """
    kind: str  # "none" | "interrupted_turn"


def _is_action_step(step: MemoryStep) -> bool:
    """Return True if *step* is an ActionStep (duck-typed for testability)."""
    return type(step).__name__ == "ActionStep"


def _is_task_step(step: MemoryStep) -> bool:
    """Return True if *step* is a TaskStep."""
    return type(step).__name__ == "TaskStep"


def _is_planning_step(step: MemoryStep) -> bool:
    """Return True if *step* is a PlanningStep."""
    return type(step).__name__ == "PlanningStep"


# ---- Filter functions (ported from reference messages.ts) ----


def filter_unresolved_tool_uses(steps: list[MemoryStep]) -> list[MemoryStep]:
    """Drop ActionSteps where tool_calls exist but observations is empty.

    Ported from reference ``filterUnresolvedToolUses()``.

    In smolagents, an ActionStep with ``tool_calls`` but no ``observations``
    means the tool was dispatched but never completed (e.g. process killed
    between LLM response and tool execution).

    Steps with ``is_final_answer=True`` are always kept — they represent
    completed turns even if they lack observations.

    Non-ActionStep types (TaskStep, PlanningStep) are always kept.
    """
    result: list[MemoryStep] = []
    dropped = 0
    for step in steps:
        if _is_action_step(step):
            has_tool_calls = bool(getattr(step, "tool_calls", None))
            has_observations = bool(getattr(step, "observations", None))
            is_final = bool(getattr(step, "is_final_answer", False))
            if has_tool_calls and not has_observations and not is_final:
                dropped += 1
                continue  # Drop: unresolved tool use
        result.append(step)
    if dropped:
        _logger.info("Filtered %d unresolved tool-use steps", dropped)
    return result


def filter_orphaned_thinking(steps: list[MemoryStep]) -> list[MemoryStep]:
    """Drop ActionSteps with model_output but no tool_calls and no action_output.

    Ported from reference ``filterOrphanedThinkingOnlyMessages()``.

    These are steps where the LLM produced *thinking* text but never
    committed to an action — typically from a crash during streaming
    before the action was decided.

    Steps with ``is_final_answer=True`` are always kept.

    Non-ActionStep types are always kept.
    """
    result: list[MemoryStep] = []
    dropped = 0
    for step in steps:
        if _is_action_step(step):
            has_model_output = bool(getattr(step, "model_output", None))
            has_tool_calls = bool(getattr(step, "tool_calls", None))
            has_action_output = bool(getattr(step, "action_output", None))
            is_final = bool(getattr(step, "is_final_answer", False))
            if has_model_output and not has_tool_calls and not has_action_output and not is_final:
                dropped += 1
                continue  # Drop: orphaned thinking
        result.append(step)
    if dropped:
        _logger.info("Filtered %d orphaned thinking steps", dropped)
    return result


def filter_empty_steps(steps: list[MemoryStep]) -> list[MemoryStep]:
    """Drop ActionSteps with no content at all.

    These occur when the process crashes before the LLM returns any
    response — the ActionStep is created but never populated.

    Non-ActionStep types (TaskStep, PlanningStep) are always kept.
    """
    result: list[MemoryStep] = []
    dropped = 0
    for step in steps:
        if _is_action_step(step):
            has_model_output = bool(getattr(step, "model_output", None))
            has_tool_calls = bool(getattr(step, "tool_calls", None))
            has_observations = bool(getattr(step, "observations", None))
            has_action_output = bool(getattr(step, "action_output", None))
            is_final = bool(getattr(step, "is_final_answer", False))
            if (
                not has_model_output
                and not has_tool_calls
                and not has_observations
                and not has_action_output
                and not is_final
            ):
                dropped += 1
                continue  # Drop: empty step
        result.append(step)
    if dropped:
        _logger.info("Filtered %d empty steps", dropped)
    return result


def detect_turn_interruption(steps: list[MemoryStep]) -> TurnInterruptionState:
    """Analyze the last step to detect whether the session was interrupted.

    Ported from reference ``detectTurnInterruption()``.

    Classification rules (evaluated in order):

    1. Empty list → ``"none"``
    2. Last step is not an ActionStep → ``"none"``
    3. Last step has ``is_final_answer=True`` → ``"none"`` (clean exit)
    4. Last step has ``observations`` → ``"none"`` (completed tool call)
    5. Otherwise → ``"interrupted_turn"`` (tool_calls without observations,
       or model_output without tool_calls — the turn was cut short)
    """
    if not steps:
        return TurnInterruptionState(kind="none")

    last = steps[-1]
    if not _is_action_step(last):
        return TurnInterruptionState(kind="none")

    if getattr(last, "is_final_answer", False):
        return TurnInterruptionState(kind="none")

    if getattr(last, "observations", None):
        return TurnInterruptionState(kind="none")

    return TurnInterruptionState(kind="interrupted_turn")


def prepare_steps_for_resume(
    steps: list[MemoryStep],
) -> tuple[list[MemoryStep], TurnInterruptionState]:
    """Full resume pipeline — clean steps and detect interruption.

    Ported from reference ``deserializeMessagesWithInterruptDetection()``.

    Pipeline stages:

    1. ``filter_unresolved_tool_uses()`` — drop unmatched tool dispatches.
    2. ``filter_orphaned_thinking()`` — drop thinking-only orphans.
    3. ``filter_empty_steps()`` — drop completely empty steps.
    4. ``detect_turn_interruption()`` — classify the final state.

    Returns:
        A tuple of ``(cleaned_steps, interruption_state)``.
    """
    original_count = len(steps)

    # Stage 1: Filter unresolved tool uses
    steps = filter_unresolved_tool_uses(steps)

    # Stage 2: Filter orphaned thinking
    steps = filter_orphaned_thinking(steps)

    # Stage 3: Filter empty steps
    steps = filter_empty_steps(steps)

    # Stage 4: Detect interruption
    interruption = detect_turn_interruption(steps)

    filtered_count = original_count - len(steps)
    if filtered_count > 0:
        _logger.info(
            "Resume pipeline: %d/%d steps kept, %d filtered. Interruption: %s",
            len(steps), original_count, filtered_count, interruption.kind,
        )
    else:
        _logger.debug(
            "Resume pipeline: all %d steps clean. Interruption: %s",
            len(steps), interruption.kind,
        )

    return steps, interruption
