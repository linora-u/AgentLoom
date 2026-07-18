"""Run-scoped, sequential hook dispatch."""

from __future__ import annotations

import inspect
import json
import os
import time
from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Any

from src.lib.logging import get_logger

from .hook_helpers import matches_pattern
from .types import (
    GATE_EVENTS,
    Blocked,
    Executed,
    Failed,
    HookContext,
    HookEvent,
    HookHandler,
    HookResult,
    ToolExecutionOutcome,
)

logger = get_logger(__name__)
_MAX_PENDING_ITEMS = 1000
_MAX_TOOL_TRACE_ITEM_BYTES = 16 * 1024
_MAX_TOOL_TRACE_TOTAL_BYTES = 256 * 1024
_MAX_TOOL_TRACE_TEXT_CHARS = 4096
_MAX_PENDING_EFFECT_ITEM_BYTES = 16 * 1024
_MAX_PENDING_EFFECT_TOTAL_BYTES = 256 * 1024


class _TracedToolFailure(RuntimeError):
    """Payload-free failure summary retained by a HookRun trace."""


def _bounded_trace_text(value: str) -> str:
    if len(value) <= _MAX_TOOL_TRACE_TEXT_CHARS:
        return value
    return value[:_MAX_TOOL_TRACE_TEXT_CHARS] + "…[truncated]"


def _bounded_pending_text(value: str) -> tuple[str, int, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= _MAX_PENDING_EFFECT_ITEM_BYTES:
        return value, len(encoded), False
    marker = "\n…[truncated]"
    budget = _MAX_PENDING_EFFECT_ITEM_BYTES - len(marker.encode("utf-8"))
    truncated = encoded[:budget].decode("utf-8", errors="ignore") + marker
    return truncated, len(truncated.encode("utf-8")), True


def _bounded_trace_input(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Copy small JSON inputs and summarize anything too large or exotic."""

    try:
        copied = deepcopy(tool_input)
        encoded = json.dumps(copied, ensure_ascii=False, sort_keys=True, default=repr).encode("utf-8")
    except Exception:
        return {"_trace_input": "unavailable"}
    if len(encoded) <= _MAX_TOOL_TRACE_ITEM_BYTES:
        return copied
    keys = [_bounded_trace_text(str(key)) for key in list(copied)[:50]]
    return {
        "_trace_input": "truncated",
        "original_bytes": len(encoded),
        "keys": keys,
    }


def _tool_outcome_size(outcome: ToolExecutionOutcome) -> int:
    payload = {
        "outcome": outcome.outcome,
        "tool_name": outcome.tool_name,
        "stage": getattr(outcome, "stage", ""),
        "reason": getattr(outcome, "reason", ""),
        "error": str(getattr(outcome, "error", "")),
        "tool_input": outcome.tool_input,
    }
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=repr).encode("utf-8"))


def wrap_in_system_reminder(content: str) -> str:
    stripped = content.strip()
    if not stripped:
        return ""
    if stripped.startswith("<system-reminder>"):
        return stripped
    return f"<system-reminder>\n{stripped}\n</system-reminder>"


@dataclass(frozen=True, slots=True)
class HookPlan:
    """Immutable Hook Handler sequence compiled for one Agent."""

    handlers: tuple[HookHandler, ...] = ()
    fingerprint: str = ""

    def __init__(
        self,
        handlers: Iterable[HookHandler] = (),
        *,
        fingerprint: str = "",
    ) -> None:
        object.__setattr__(self, "handlers", tuple(handlers))
        object.__setattr__(self, "fingerprint", str(fingerprint))

    def matching(self, event: HookEvent, tool_name: str) -> tuple[HookHandler, ...]:
        return tuple(
            handler
            for handler in self.handlers
            if handler.event is event
            and (
                handler.shell_spec.matches(tool_name)
                if handler.shell_spec is not None
                else matches_pattern(tool_name, handler.pattern)
            )
        )


class HookRun:
    """Mutable effects and metrics owned by one Agent invocation."""

    def __init__(
        self,
        plan: HookPlan,
        *,
        local_run_id: str,
        root_run_id: str,
        user_message_sink: Callable[[str], None] | None = None,
        parent: HookRun | None = None,
        agent_config: dict[str, Any] | None = None,
        project_root: str | None = None,
    ) -> None:
        if not local_run_id or not root_run_id:
            raise ValueError("HookRun requires local_run_id and root_run_id")
        self.plan = plan
        self.local_run_id = local_run_id
        self.root_run_id = root_run_id
        self.parent = parent
        self.agent_config = deepcopy(agent_config) if isinstance(agent_config, dict) else None
        self.project_root = str(project_root) if project_root is not None else None
        self.step_number = 0
        self._pending_agent_context: list[str] = []
        self._pending_user_messages: list[str] = []
        self._pending_effect_bytes: dict[str, list[int]] = {"context": [], "message": []}
        self._pending_effect_total_bytes: dict[str, int] = {"context": 0, "message": 0}
        self._tool_outcomes: list[ToolExecutionOutcome] = []
        self._tool_outcome_bytes: list[int] = []
        self._tool_outcome_total_bytes = 0
        self._user_message_sink = user_message_sink
        self._metrics: dict[str, list[float]] = {}
        self._lock = RLock()

    def set_user_message_sink(self, sink: Callable[[str], None] | None) -> None:
        self._user_message_sink = sink

    def _queue(self, queue: list[str], text: str | None, label: str) -> None:
        if text is None or not str(text).strip():
            return
        bounded, size, truncated = _bounded_pending_text(str(text).strip())
        with self._lock:
            sizes = self._pending_effect_bytes[label]
            dropped = False
            while queue and (
                len(queue) >= _MAX_PENDING_ITEMS
                or self._pending_effect_total_bytes[label] + size > _MAX_PENDING_EFFECT_TOTAL_BYTES
            ):
                queue.pop(0)
                self._pending_effect_total_bytes[label] -= sizes.pop(0)
                dropped = True
            if truncated:
                logger.warning("Pending hook %s item exceeded byte budget and was truncated", label)
            if dropped:
                logger.warning("Pending hook %s queue overflow, dropping oldest", label)
            queue.append(bounded)
            sizes.append(size)
            self._pending_effect_total_bytes[label] += size

    def consume_pending_agent_context(self) -> list[str]:
        with self._lock:
            values = list(self._pending_agent_context)
            self._pending_agent_context.clear()
            self._pending_effect_bytes["context"].clear()
            self._pending_effect_total_bytes["context"] = 0
        return values

    def queue_agent_context(self, text: str | None) -> None:
        self._queue(self._pending_agent_context, text, "context")

    def queue_user_message(self, text: str | None) -> None:
        self._queue(self._pending_user_messages, text, "message")

    def consume_pending_user_messages(self) -> list[str]:
        with self._lock:
            values = list(self._pending_user_messages)
            self._pending_user_messages.clear()
            self._pending_effect_bytes["message"].clear()
            self._pending_effect_total_bytes["message"] = 0
        return values

    @staticmethod
    def _copy_tool_outcome(
        outcome: ToolExecutionOutcome,
        *,
        for_storage: bool,
    ) -> ToolExecutionOutcome:
        """Copy trace input so callers cannot mutate run-owned history."""

        tool_input = _bounded_trace_input(outcome.tool_input) if for_storage else deepcopy(outcome.tool_input)
        tool_name = _bounded_trace_text(outcome.tool_name)
        if isinstance(outcome, Executed):
            # Trace the state transition, not a potentially unbounded tool
            # payload that is already owned by model memory/context storage.
            return Executed(tool_input, None, tool_name=tool_name)
        if isinstance(outcome, Blocked):
            return Blocked(
                tool_input,
                _bounded_trace_text(outcome.reason),
                _bounded_trace_text(outcome.stage),
                tool_name=tool_name,
            )
        if isinstance(outcome, Failed):
            error = outcome.error
            if not isinstance(error, _TracedToolFailure):
                error = _TracedToolFailure(_bounded_trace_text(f"{type(error).__name__}: {error}"))
            return Failed(tool_input, error, _bounded_trace_text(outcome.stage), tool_name=tool_name)
        raise TypeError(f"Unsupported tool execution outcome: {type(outcome).__name__}")

    def record_tool_outcome(self, outcome: ToolExecutionOutcome) -> None:
        """Retain the typed boundary outcome in this run's bounded trace."""

        copied = self._copy_tool_outcome(outcome, for_storage=True)
        size = _tool_outcome_size(copied)
        with self._lock:
            dropped = False
            while self._tool_outcomes and (
                len(self._tool_outcomes) >= _MAX_PENDING_ITEMS
                or self._tool_outcome_total_bytes + size > _MAX_TOOL_TRACE_TOTAL_BYTES
            ):
                self._tool_outcomes.pop(0)
                self._tool_outcome_total_bytes -= self._tool_outcome_bytes.pop(0)
                dropped = True
            if dropped:
                logger.warning("Tool outcome trace overflow, dropping oldest")
            self._tool_outcomes.append(copied)
            self._tool_outcome_bytes.append(size)
            self._tool_outcome_total_bytes += size

    def tool_outcomes_snapshot(self) -> tuple[ToolExecutionOutcome, ...]:
        """Return an isolated snapshot without consuming the run trace."""

        with self._lock:
            return tuple(self._copy_tool_outcome(outcome, for_storage=False) for outcome in self._tool_outcomes)

    def flush_user_messages(self) -> list[str]:
        messages = self.consume_pending_user_messages()
        sink = self._user_message_sink
        for message in messages:
            if sink is None:
                logger.info("Hook user message: %s", message)
                continue
            try:
                sink(message)
            except Exception as exc:
                logger.warning("Failed to deliver hook user message: %s", exc)
        return messages

    def get_hook_metrics(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {
                name: {
                    "calls": len(values),
                    "total_time": sum(values),
                    "avg_time": sum(values) / len(values),
                    "max_time": max(values),
                    "min_time": min(values),
                }
                for name, values in self._metrics.items()
                if values
            }

    @staticmethod
    def _append_text(existing: str | None, extra: str | None) -> str | None:
        if extra is None or not str(extra).strip():
            return existing
        if existing is None or not str(existing).strip():
            return str(extra).strip()
        return f"{existing}\n{str(extra).strip()}"

    @staticmethod
    def _invoke(handler: HookHandler, context: HookContext) -> HookResult | None:
        signature = inspect.signature(handler.callback)
        if "context" in signature.parameters:
            return handler.callback(context=context)  # type: ignore[call-arg]
        if signature.parameters:
            return handler.callback(context)
        raise TypeError(f"Hook Handler {handler.source!r} must accept HookContext")

    @staticmethod
    def _contract_error(event: HookEvent, handler: HookHandler, result: Any) -> str | None:
        if result is not None and not isinstance(result, HookResult):
            return f"{handler.source} returned {type(result).__name__}, expected HookResult or None"
        if result is None:
            return None
        if event is HookEvent.PRE_TOOL_USE:
            if result.decision == "modify" and not isinstance(result.modified_input, dict):
                return f"{handler.source} returned modify without modified_input"
            if result.decision != "modify" and result.modified_input is not None:
                return f"{handler.source} returned modified_input without decision=modify"
            return None
        if event is HookEvent.STOP:
            if result.decision == "modify" or result.modified_input is not None:
                return f"{handler.source} cannot modify Stop"
            return None
        if result.decision != "allow" or result.modified_input is not None:
            return f"{handler.source} cannot block or modify observer event {event.value}"
        return None

    def _record_metric(self, source: str, duration: float) -> None:
        with self._lock:
            values = self._metrics.setdefault(source, [])
            values.append(duration)
            if len(values) > 1000:
                del values[:-1000]

    def dispatch(
        self,
        event: HookEvent,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        tool_response: dict[str, Any] | None = None,
        tool_inputs_schema: dict[str, Any] | None = None,
    ) -> HookResult:
        """Run matching handlers in order under event-specific semantics."""

        try:
            from src.trace import capture_explicit_execution_context

            execution = capture_explicit_execution_context()
        except Exception:
            execution = None

        current_input = deepcopy(tool_input)
        final = HookResult()
        errors: list[dict[str, str]] = []
        for handler in self.plan.matching(event, tool_name):
            context = HookContext(
                local_run_id=self.local_run_id,
                root_run_id=self.root_run_id,
                cwd=os.getcwd(),
                hook_event_name=event.value,
                tool_name=tool_name,
                tool_input=deepcopy(current_input),
                tool_response=deepcopy(tool_response),
                tool_inputs_schema=deepcopy(tool_inputs_schema),
                step_number=self.step_number,
                task_id=getattr(execution, "task_id", None),
                sub_task_id=getattr(execution, "sub_task_id", None),
                agent_name=getattr(execution, "agent_name", None),
                agent_config=deepcopy(self.agent_config),
                runtime_agent_path=getattr(execution, "runtime_agent_path", None),
                project_root=self.project_root,
            )
            started = time.monotonic()
            try:
                result = self._invoke(handler, context)
                contract_error = self._contract_error(event, handler, result)
                if contract_error:
                    raise ValueError(contract_error)
            except Exception as exc:
                message = f"{handler.source}: {exc}"
                errors.append({"source": handler.source, "reason": str(exc)})
                logger.warning("Hook Handler failed for %s: %s", event.value, message)
                self._record_metric(handler.source, time.monotonic() - started)
                if event in GATE_EVENTS:
                    final.decision = "block"
                    final.reason = message
                    break
                continue

            self._record_metric(handler.source, time.monotonic() - started)
            if result is None:
                continue
            self._queue(self._pending_agent_context, result.agent_context, "context")
            self._queue(self._pending_user_messages, result.user_message, "message")
            final.agent_context = self._append_text(final.agent_context, result.agent_context)
            final.user_message = self._append_text(final.user_message, result.user_message)
            final.reason = self._append_text(final.reason, result.reason)
            final.telemetry.update(deepcopy(result.telemetry))

            if event is HookEvent.PRE_TOOL_USE and result.decision == "modify":
                assert result.modified_input is not None
                current_input.update(deepcopy(result.modified_input))
                final.decision = "modify"
                final.modified_input = deepcopy(current_input)
            elif result.decision == "block":
                final.decision = "block"
                break

        if errors:
            final.telemetry["hook_errors"] = errors
        return final

    def build_stop_check(self) -> Callable[[Any, Any], bool]:
        def _check(final_answer: Any, memory: Any, **_kwargs: Any) -> bool:
            memory_steps = getattr(memory, "steps", None)
            response = {"memory_steps": len(memory_steps)} if isinstance(memory_steps, list) else None
            result = self.dispatch(
                HookEvent.STOP,
                "final_answer",
                {"final_answer": final_answer},
                tool_response=response,
            )
            self.flush_user_messages()
            if result.should_block():
                raise AssertionError(result.get_blocked_response())
            return True

        return _check
