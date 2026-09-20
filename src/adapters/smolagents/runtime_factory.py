"""Construct the smolagents runtime solely from a neutral RuntimeDefinition."""

from __future__ import annotations

from dataclasses import replace
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
    todo_policy_for_mode,
)
from agentloom.runtime.trace import get_current_hook_run
from agentloom.runtime.tool_gateway import AgentLoomToolGateway, bind_tool, tool_manifest_snapshot
from agentloom.adapters.smolagents.terminal import final_answer_binding
from agentloom.tools.loader import resolve_tool_function
from smolagents import LogLevel

logger = get_logger(__name__)


def _run_scoped_stop_check(
    final_answer: Any,
    memory: Any,
    **kwargs: Any,
) -> bool:
    """Resolve the current Hook Run when final-answer settlement occurs."""

    hook_run = get_current_hook_run(required=True)
    return hook_run.build_stop_check()(final_answer, memory, **kwargs)


def _prompt_metadata(definition: RuntimeDefinition) -> tuple[str | None, str]:
    if definition.project_root is None:
        raise ValueError(
            "Smolagents runtime requires RuntimeDefinition.project_root"
        )
    return definition.prompt_template_path, definition.project_root


class _SmolToolGateway:
    """Add only smol's execution tools, preserving common tool governance."""

    def __init__(self, delegate, todo_mode: str) -> None:
        self.delegate = delegate
        selected = tuple(tool for tool in delegate.definitions if todo_mode != "off" or tool.name != "todo_write")
        names = {tool.name for tool in selected}
        extras = []
        if "final_answer" not in names:
            extras.append(final_answer_binding())
        if todo_mode != "off" and "todo_write" not in names:
            extras.append(bind_tool(resolve_tool_function("todo_write")))
        self.extra = AgentLoomToolGateway(extras)
        self.definitions = (*selected, *self.extra.definitions)
        self._extra_names = {tool.name for tool in self.extra.definitions}
        self._names = {tool.name for tool in self.definitions}

    @property
    def manifest(self):
        return (
            *(entry for entry in tool_manifest_snapshot(self.delegate)
              if entry.visible_name in self._names),
            *self.extra.manifest,
        )

    def invoke(self, *, call_id, tool_name, arguments):
        if tool_name not in self._names:
            raise ValueError(f"Unknown smol tool: {tool_name}")
        gateway = self.extra if tool_name in self._extra_names else self.delegate
        return gateway.invoke(call_id=call_id, tool_name=tool_name, arguments=arguments)

    def close(self):
        try:
            self.extra.close()
        finally:
            self.delegate.close()


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

        if definition.model is None:
            raise ValueError("smolagents requires a Python model binding")
        from agentloom.application.runtime_options import SMOL_DEFAULTS

        options = dict(SMOL_DEFAULTS)
        for name in SMOL_DEFAULTS:
            value = getattr(definition, name)
            if value is not None:
                options[name] = value
        for name, value in definition.runtime_options.items():
            if name not in SMOL_DEFAULTS:
                raise ValueError(f"Unsupported smolagents runtime option: {name}")
            legacy = getattr(definition, name)
            if legacy is not None and legacy != value:
                raise ValueError(f"Conflicting smolagents runtime option: {name}")
            options[name] = value
        definition = replace(definition, **options)
        gateway = _SmolToolGateway(definition.tool_gateway, definition.todo_mode)
        try:
            instructions = "\n\n".join(filter(None, (
                definition.instructions, todo_policy_for_mode(definition.todo_mode),
            )))
            model = SmolagentsModelTurnBridge(binding=definition.model)
            prompt_path, agent_root = _prompt_metadata(definition)
            prompt_templates = load_base_prompt_templates(
                prompt_template_path=prompt_path,
                model_id=definition.model.model_id,
                agent_root=agent_root,
                logger=logger,
            )
            agent_kwargs: dict[str, Any] = {
                "tool_gateway": gateway,
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
                agent_kwargs["instructions"] = instructions
            else:
                _append_to_system_prompt(
                    prompt_templates,
                    instructions,
                )
                agent_kwargs["prompt_templates"] = prompt_templates
            if definition.planning_interval is not None:
                agent_kwargs["planning_interval"] = definition.planning_interval

            native_runtime = ToolCallingAgentV2(**agent_kwargs)
            native_runtime._agent_loom_todo_mode = definition.todo_mode
            native_runtime._max_consecutive_parse_errors = (
                definition.max_consecutive_model_errors
            )
            return SmolagentsRuntimeAdapter(
                native_runtime,
                model_binding=definition.model,
                tool_gateway=gateway,
            )
        except BaseException:
            gateway.close()
            raise
