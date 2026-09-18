"""Construct the smolagents runtime solely from a neutral RuntimeDefinition."""

from __future__ import annotations

from typing import Any

from agentloom.adapters.smolagents.agents import ToolCallingAgentV2
from agentloom.adapters.smolagents.model_turn_bridge import (
    SmolagentsModelTurnBridge,
)
from agentloom.adapters.smolagents.runtime_adapter import (
    SmolagentsRuntimeAdapter,
)
from agentloom.runtime.agent_runtime import RuntimeDefinition
from agentloom.runtime.logging import get_global_logger
from agentloom.runtime.trace import get_current_hook_run
from smolagents import LogLevel

_DEFAULT_MAX_CONSECUTIVE_PARSE_ERRORS = 5
_MAX_CONSECUTIVE_PARSE_ERRORS_KEY = "max_consecutive_parse_errors"


def _run_scoped_stop_check(
    final_answer: Any,
    memory: Any,
    **kwargs: Any,
) -> bool:
    """Resolve the current Hook Run when final-answer settlement occurs."""

    hook_run = get_current_hook_run(required=True)
    return hook_run.build_stop_check()(final_answer, memory, **kwargs)


def _max_consecutive_parse_errors(definition: RuntimeDefinition) -> int:
    raw_value = definition.metadata.get(
        _MAX_CONSECUTIVE_PARSE_ERRORS_KEY,
        _DEFAULT_MAX_CONSECUTIVE_PARSE_ERRORS,
    )
    if (
        isinstance(raw_value, bool)
        or not isinstance(raw_value, int)
        or raw_value < 1
    ):
        raise ValueError(
            "runtime definition metadata.max_consecutive_parse_errors "
            "must be a positive integer"
        )
    return raw_value


class SmolagentsRuntimeFactory:
    """Build one complete smolagents runtime from a neutral definition."""

    runtime_id = "smolagents"

    def __call__(
        self,
        definition: RuntimeDefinition,
    ) -> SmolagentsRuntimeAdapter:
        if not isinstance(definition, RuntimeDefinition):
            raise TypeError("definition must be a RuntimeDefinition")
        if definition.runtime_id != self.runtime_id:
            raise ValueError(
                "SmolagentsRuntimeFactory requires "
                f"runtime_id={self.runtime_id!r}, got {definition.runtime_id!r}"
            )

        model = SmolagentsModelTurnBridge(binding=definition.model)
        agent_kwargs: dict[str, Any] = {
            "tool_gateway": definition.tool_gateway,
            "model": model,
            "instructions": definition.instructions,
            "max_steps": definition.max_steps,
            "max_tokens": definition.model.max_tokens,
            "context_window": definition.model.context_window,
            "max_output_tokens": definition.model.max_output_tokens,
            "smart_summary": definition.smart_summary,
            "stream_outputs": False,
            "verbosity_level": LogLevel.INFO,
            "name": definition.name,
            "description": definition.description,
            "final_answer_checks": [_run_scoped_stop_check],
            "logger": get_global_logger(create_if_missing=False),
        }
        if definition.planning_interval is not None:
            agent_kwargs["planning_interval"] = definition.planning_interval

        native_runtime = ToolCallingAgentV2(**agent_kwargs)
        native_runtime._agent_loom_todo_mode = definition.todo_mode
        native_runtime._max_consecutive_parse_errors = (
            _max_consecutive_parse_errors(definition)
        )
        return SmolagentsRuntimeAdapter(
            native_runtime,
            tool_gateway=definition.tool_gateway,
        )
