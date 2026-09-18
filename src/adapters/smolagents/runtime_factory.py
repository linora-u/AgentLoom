"""Construct the smolagents runtime solely from a neutral RuntimeDefinition."""

from __future__ import annotations

from typing import Any

from agentloom.adapters.smolagents.agent_logger import (
    adapt_smolagents_logger_backend,
)
from agentloom.adapters.smolagents.agents import ToolCallingAgentV2
from agentloom.adapters.smolagents.model_turn_bridge import (
    SmolagentsModelTurnBridge,
)
from agentloom.adapters.smolagents.runtime_adapter import (
    SmolagentsRuntimeAdapter,
)
from agentloom.runtime.agent_runtime import RuntimeDefinition
from agentloom.runtime.logging import get_global_logger, get_logger
from agentloom.runtime.prompts.prompt_builder import (
    _append_to_system_prompt,
    load_base_prompt_templates,
)
from agentloom.runtime.trace import get_current_hook_run
from smolagents import LogLevel

_DEFAULT_MAX_CONSECUTIVE_PARSE_ERRORS = 5
_MAX_CONSECUTIVE_PARSE_ERRORS_KEY = "max_consecutive_parse_errors"
_PROMPT_TEMPLATE_PATH_KEY = "smolagents_prompt_template_path"
_AGENT_ROOT_KEY = "agent_root"
logger = get_logger(__name__)


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


def _prompt_metadata(definition: RuntimeDefinition) -> tuple[str | None, str]:
    prompt_path = definition.metadata.get(_PROMPT_TEMPLATE_PATH_KEY)
    if prompt_path is not None and (
        not isinstance(prompt_path, str) or not prompt_path.strip()
    ):
        raise ValueError(
            "runtime definition metadata.smolagents_prompt_template_path "
            "must be a non-empty string path when provided"
        )
    agent_root = definition.metadata.get(_AGENT_ROOT_KEY)
    if not isinstance(agent_root, str) or not agent_root.strip():
        raise ValueError(
            "runtime definition metadata.agent_root must be a non-empty string path"
        )
    return (
        prompt_path.strip() if isinstance(prompt_path, str) else None,
        agent_root.strip(),
    )


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
        prompt_path, agent_root = _prompt_metadata(definition)
        prompt_templates = load_base_prompt_templates(
            prompt_template_path=prompt_path,
            model_id=definition.model.model_id,
            agent_root=agent_root,
            logger=logger,
        )
        agent_kwargs: dict[str, Any] = {
            "tool_gateway": definition.tool_gateway,
            "model": model,
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
            "logger": adapt_smolagents_logger_backend(
                get_global_logger(create_if_missing=False)
            ),
        }
        if prompt_templates is None:
            agent_kwargs["instructions"] = definition.instructions
        else:
            _append_to_system_prompt(
                prompt_templates,
                definition.instructions,
            )
            agent_kwargs["prompt_templates"] = prompt_templates
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
