"""Render committed execution facts with the existing smolagents-style Rich forms."""

from __future__ import annotations

import json
import time
from typing import Any

from agentloom.execution.logging.levels import AgentLoomLogLevel
from agentloom.execution.observability import RunTrace
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text


class StepPresenter:
    """One run's human-readable view of committed Step and Tool facts."""

    def __init__(self, backend: Any, trace: RunTrace) -> None:
        self.backend = backend
        self.trace = trace
        self._agents: set[str] = set()
        self._active_steps: dict[str, tuple[int, float]] = {}
        self._step_usage: dict[int, tuple[int, int]] = {}
        self._input_total = 0
        self._output_total = 0

    def _complete_step(self, agent_id: str) -> None:
        active = self._active_steps.pop(agent_id, None)
        if active is None:
            return
        number, started = active
        duration = max(0.0, time.monotonic() - started)
        usage = self._step_usage.pop(number, None)
        summary = f"[Step {number}] Duration {duration:.2f} seconds"
        if usage is not None:
            input_tokens, output_tokens = usage
            self._input_total += input_tokens
            self._output_total += output_tokens
            summary += (
                f" | Input tokens: {self._input_total:,} (+{input_tokens})"
                f" | Output tokens: {self._output_total:,} (+{output_tokens})"
            )
        self.backend.log(Text(summary), level=AgentLoomLogLevel.INFO)

    def consume(self, event: dict[str, Any]) -> None:
        kind = event.get("kind")
        agent_id = event.get("agent_id")
        if kind == "agent_start":
            if isinstance(agent_id, str):
                self._agents.add(agent_id)
            task = self.trace.read_text(event["task_ref"])
            self.backend.log(
                Panel(
                    Text("\n" + task + "\n", style="bold"),
                    title="New run - " + str(event["agent_name"]),
                    subtitle=str(event["runtime"]),
                    border_style="#d4b702",
                    subtitle_align="left",
                ),
                level=AgentLoomLogLevel.INFO,
            )
        elif kind == "model_request" and agent_id in self._agents:
            number = event.get("run_step_number")
            if isinstance(number, int):
                active = self._active_steps.get(agent_id)
                if active is not None and active[0] == number:
                    return
                if active is not None:
                    self._complete_step(agent_id)
                self._active_steps[agent_id] = (number, time.monotonic())
                self.backend.log(
                    Rule(f"[bold white]Step {number}", characters="━", style="#d4b702"),
                    level=AgentLoomLogLevel.INFO,
                )
        elif kind == "model_response":
            number = event.get("run_step_number")
            usage = event.get("usage")
            if isinstance(number, int) and isinstance(usage, dict):
                prior_input, prior_output = self._step_usage.get(number, (0, 0))
                self._step_usage[number] = (
                    prior_input + int(usage.get("input_tokens") or 0),
                    prior_output + int(usage.get("output_tokens") or 0),
                )
            if event.get("error"):
                self.backend.log(
                    Text(f"Model error: {event['error']}", style="bold red"),
                    level=AgentLoomLogLevel.ERROR,
                )
            level = getattr(self.backend, "level", None)
            response_ref = event.get("response_ref")
            if level == AgentLoomLogLevel.DEBUG and isinstance(response_ref, str):
                response = self.trace.read_text(response_ref)
                self.backend.log(Text(f"Model response: {response}"),
                                 level=AgentLoomLogLevel.DEBUG)
        elif kind == "tool" and agent_id in self._agents:
            arguments = json.loads(self.trace.read_text(event["input_ref"]))
            self.backend.log(
                Panel(Text(f"Calling tool: '{event['tool_name']}' with arguments: {arguments}")),
                level=AgentLoomLogLevel.INFO,
            )
            result = self.trace.read_text(event["model_ref"])
            self.backend.log(Text(f"Observations: {result}"),
                             level=AgentLoomLogLevel.INFO, soft_wrap=True)
            if event.get("status") != "completed":
                self.backend.log(
                    Text(f"Tool '{event['tool_name']}' {event.get('status')}: {event.get('error')}",
                         style="bold red"),
                    level=AgentLoomLogLevel.ERROR,
                )
        elif kind == "agent_end" and isinstance(agent_id, str):
            self._complete_step(agent_id)
        elif kind == "hook_decision" and event.get("event") == "Stop":
            decision = json.loads(self.trace.read_text(event["decision_ref"]))["result"]
            if decision.get("decision") == "block":
                self.backend.log(
                    Text(f"Stop blocked: {decision.get('reason') or 'continue'}", style="bold yellow"),
                    level=AgentLoomLogLevel.WARNING,
                    soft_wrap=True,
                )
        elif kind == "final_answer":
            answer = self.trace.read_text(event["answer_ref"])
            self.backend.log(Text(f"Final answer: {answer}", style="bold #d4b702"),
                             level=AgentLoomLogLevel.INFO)
