"""Extensions of the pinned smolagents Agent and native Tool-call protocol."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from contextvars import copy_context
from typing import Any

from smolagents import (
    AgentAudio,
    AgentGenerationError,
    AgentImage,
    AgentParsingError,
    CodeAgent,
    LogLevel,
    ToolCallingAgent,
)
from smolagents.agents import ToolOutput
from smolagents.memory import ToolCall

from agentloom.adapters.smolagents.models.tool_call_parser import ToolCallParseError, parse_json_with_repair
from agentloom.adapters.smolagents.monkey_patch import install_agentloom_runtime_adapters
from agentloom.adapters.smolagents.tool_argument_coercion import coerce_tool_arguments
from agentloom.adapters.smolagents.tool_protocol import settle_tool_call
from agentloom.runtime.invocation import require_successful_runtime_result
from agentloom.runtime.tool_protocol import ToolCallRecord

# Preserve installation at the first concrete Agent-runtime import, before the
# mixin is loaded, rather than installing patches during definition inspection.
install_agentloom_runtime_adapters()

from agentloom.runtime.loom_mixin import LoomAgentMixin  # noqa: E402


def _normalize_tool_arguments_object(arguments: dict[str, Any] | str) -> dict[str, Any] | str:
    if not isinstance(arguments, str):
        return arguments

    parsed: Any = arguments
    for _ in range(2):
        if not isinstance(parsed, str):
            break
        try:
            parsed = parse_json_with_repair(parsed)
        except Exception:
            return arguments

    return parsed if isinstance(parsed, dict) else arguments


class _SuccessfulRunStateMixin:
    """Preserve the runtime return shape while rejecting failed run states."""

    def run(
        self,
        task: str,
        stream: bool = False,
        reset: bool = True,
        images: Any = None,
        additional_args: dict | None = None,
        max_steps: int | None = None,
        return_full_result: bool | None = None,
        **kwargs,
    ) -> Any:
        if stream:
            return super().run(
                task,
                stream=stream,
                reset=reset,
                images=images,
                additional_args=additional_args,
                max_steps=max_steps,
                return_full_result=return_full_result,
                **kwargs,
            )

        wants_full_result = (
            bool(getattr(self, "return_full_result", False)) if return_full_result is None else return_full_result
        )
        run_result = super().run(
            task,
            stream=False,
            reset=reset,
            images=images,
            additional_args=additional_args,
            max_steps=max_steps,
            return_full_result=True,
            **kwargs,
        )
        require_successful_runtime_result(run_result)
        return run_result if wants_full_result else run_result.output


class CodeAgentV2(_SuccessfulRunStateMixin, LoomAgentMixin, CodeAgent):
    def __init__(
        self,
        *args,
        before_run_callbacks: list | None = None,
        **kwargs,
    ):
        max_tokens = kwargs.pop("max_tokens", None)
        context_window = kwargs.pop("context_window", None)
        max_output_tokens = kwargs.pop("max_output_tokens", None)
        smart_summary = kwargs.pop("smart_summary", True)
        self._init_loom_agent(
            before_run_callbacks,
            max_tokens=max_tokens,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            smart_summary=smart_summary,
        )
        super().__init__(*args, **kwargs)


class ToolCallingAgentV2(_SuccessfulRunStateMixin, LoomAgentMixin, ToolCallingAgent):
    def __init__(
        self,
        *args,
        before_run_callbacks: list | None = None,
        **kwargs,
    ):
        # Remove all code_act specific kwargs before calling ToolCallingAgent.__init__
        # ToolCallingAgent does not support these parameters
        kwargs.pop("executor_type", None)
        kwargs.pop("executor_kwargs", None)

        max_tokens = kwargs.pop("max_tokens", None)
        context_window = kwargs.pop("context_window", None)
        max_output_tokens = kwargs.pop("max_output_tokens", None)
        smart_summary = kwargs.pop("smart_summary", True)
        self._init_loom_agent(
            before_run_callbacks,
            max_tokens=max_tokens,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            smart_summary=smart_summary,
        )
        super().__init__(*args, **kwargs)

    def _step_stream(self, memory_step):
        """Keep each tool-calling action step inside its required-call protocol."""
        require_tool_calls = getattr(self.model, "require_tool_calls", None)
        required_call_context = require_tool_calls() if callable(require_tool_calls) else nullcontext()
        try:
            with required_call_context:
                yield from super()._step_stream(memory_step)
        except AgentGenerationError as exc:
            # LiteLLMModelV2 validates native tool calls at the provider
            # boundary. A bad tool name is model output, not an infrastructure
            # or implementation failure, so feed it back as a recoverable
            # parsing error and let the next ReAct step correct itself.
            cause = exc.__cause__
            if isinstance(cause, ToolCallParseError):
                raise AgentParsingError(
                    f"Error while parsing tool call from model output: {cause}",
                    self.logger,
                ) from cause
            raise

    def process_tool_calls(self, chat_message, memory_step):
        """Settle every Tool call independently and persist its stable provider ID."""

        assert chat_message.tool_calls is not None
        calls = [
            ToolCall(
                name=call.function.name,
                arguments=call.function.arguments,
                id=call.id,
            )
            for call in chat_message.tool_calls
        ]
        memory_step.tool_calls = calls
        yield from calls

        def execute_one(index: int) -> tuple[int, ToolCallRecord, ToolOutput]:
            call = calls[index]
            record = self.execute_tool_call_record(
                call.name,
                call.arguments or {},
                call_id=call.id,
            )
            if record.status != "completed":
                observation = record.model_content()
                is_final_answer = False
                output = observation
            elif type(record.output) in {AgentImage, AgentAudio}:
                media_output = record.output
                observation_name = "image.png" if type(record.output) is AgentImage else "audio.mp3"
                self.state[observation_name] = media_output
                observation = f"Stored '{observation_name}' in memory."
                record = record.with_output(observation)
                is_final_answer = call.name == "final_answer"
                output = media_output
            else:
                observation = str(record.output).strip()
                is_final_answer = call.name == "final_answer"
                output = record.output

            self.logger.log(f"Tool {call.name} [{call.id}] -> {record.status}", level=LogLevel.INFO)
            return (
                index,
                record,
                ToolOutput(
                    id=call.id,
                    output=output,
                    is_final_answer=is_final_answer,
                    observation=observation,
                    tool_call=call,
                ),
            )

        outputs: dict[int, ToolOutput] = {}
        records: dict[int, ToolCallRecord] = {}
        if len(calls) == 1:
            index, record, output = execute_one(0)
            records[index] = record
            outputs[index] = output
            yield output
        else:
            with ThreadPoolExecutor(self.max_tool_threads) as executor:
                futures = []
                for index in range(len(calls)):
                    context = copy_context()
                    futures.append(executor.submit(context.run, execute_one, index))
                for future in as_completed(futures):
                    index, record, output = future.result()
                    records[index] = record
                    outputs[index] = output
                    yield output

        memory_step.tool_results = [records[index] for index in range(len(calls))]
        memory_step.observations = "\n".join(outputs[index].observation for index in range(len(calls)))

    def execute_tool_call_record(
        self,
        tool_name: str,
        arguments: dict[str, str] | str,
        *,
        call_id: str | None = None,
    ) -> ToolCallRecord:
        """Settle one Tool invocation without exception-based terminal-state transport."""

        available_tools = {**self.tools, **self.managed_agents}
        stable_call_id = call_id or uuid.uuid4().hex
        if tool_name not in available_tools:
            return ToolCallRecord.blocked(
                call_id=stable_call_id,
                tool_name=tool_name,
                input=arguments,
                message=f"Unknown tool {tool_name}, should be one of: {', '.join(available_tools)}.",
                kind="invalid_arguments",
                stage="input_validation",
                started_at=time.time(),
                ended_at=time.time(),
            )

        tool = available_tools[tool_name]
        try:
            normalized = _normalize_tool_arguments_object(arguments)
            normalized = self._substitute_state_variables(normalized)
            normalized = coerce_tool_arguments(tool, normalized)
        except Exception as error:
            return ToolCallRecord.blocked(
                call_id=stable_call_id,
                tool_name=tool_name,
                input=arguments,
                message=str(error) or type(error).__name__,
                kind="invalid_arguments",
                stage="input_validation",
                started_at=time.time(),
                ended_at=time.time(),
            )

        return settle_tool_call(
            tool,
            normalized,
            call_id=stable_call_id,
            sanitize_inputs_outputs=tool_name not in self.managed_agents,
        )

    def execute_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, str] | str,
        *,
        call_id: str | None = None,
    ) -> Any:
        """Compatibility interface for callers that need the ordinary Tool value."""

        return self.execute_tool_call_record(
            tool_name,
            arguments,
            call_id=call_id,
        ).direct_result()
