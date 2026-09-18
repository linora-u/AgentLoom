"""Extensions of the pinned smolagents Agent and native Tool-call protocol."""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from contextvars import copy_context
from typing import Any

from agentloom.adapters.smolagents.monkey_patch import install_agentloom_runtime_adapters
from agentloom.adapters.smolagents.tool_proxy import build_smolagents_tool_proxies
from agentloom.runtime.agent_runtime import require_runtime_state
from agentloom.runtime.model_protocol import ModelProtocolError
from agentloom.runtime.tool_gateway import ToolGateway
from agentloom.runtime.tool_protocol import ToolCallRecord
from smolagents import (
    AgentAudio,
    AgentGenerationError,
    AgentImage,
    AgentParsingError,
    LogLevel,
    ToolCallingAgent,
)
from smolagents.agents import ToolOutput
from smolagents.memory import ToolCall

# Preserve installation at the first concrete Agent-runtime import, before the
# mixin is loaded, rather than installing patches during definition inspection.
install_agentloom_runtime_adapters()

from agentloom.adapters.smolagents.loom_mixin import LoomAgentMixin  # noqa: E402


def _decode_provider_tool_arguments(arguments: Any) -> dict[str, Any]:
    """Normalize the provider's native JSON arguments field to one object."""

    decoded = arguments
    for _ in range(2):
        if not isinstance(decoded, str):
            break
        try:
            decoded = json.loads(decoded)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(
                "Provider Tool arguments must contain a valid JSON object"
            ) from exc
    if not isinstance(decoded, dict):
        raise ValueError("Provider Tool arguments must resolve to an object mapping")
    return decoded


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
        require_runtime_state(
            run_result,
            allowed_states={"success"},
            error_prefix="Agent run did not complete successfully",
        )
        return run_result if wants_full_result else run_result.output


class ToolCallingAgentV2(_SuccessfulRunStateMixin, LoomAgentMixin, ToolCallingAgent):
    def __init__(
        self,
        *args,
        tool_gateway: ToolGateway,
        before_run_callbacks: list | None = None,
        **kwargs,
    ):
        if args:
            raise TypeError(
                "ToolCallingAgentV2 requires keyword construction with tool_gateway"
            )
        self.tool_gateway = tool_gateway
        kwargs["tools"] = build_smolagents_tool_proxies(tool_gateway)
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
            # Protocol-shape failures are model output, not infrastructure
            # failures. Feed them back as recoverable parsing errors so the
            # next ReAct step can correct itself.
            cause = exc.__cause__
            if isinstance(cause, ModelProtocolError):
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
        arguments: dict[str, Any],
        *,
        call_id: str,
    ) -> ToolCallRecord:
        """Route one provider Tool call through AgentLoom's only executor."""

        if not isinstance(call_id, str) or not call_id:
            raise ValueError("Provider Tool call_id must be a non-empty string")
        decoded_arguments = _decode_provider_tool_arguments(arguments)
        substituted_arguments = self._substitute_state_variables(decoded_arguments)
        if not isinstance(substituted_arguments, dict):
            raise ValueError("Provider Tool arguments must resolve to an object mapping")
        return self.tool_gateway.invoke(
            call_id=call_id,
            tool_name=tool_name,
            arguments=substituted_arguments,
        )
