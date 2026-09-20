"""Pi implementation of a complete Agent invocation, including the platform Stop gate."""
from __future__ import annotations

from uuid import uuid4

from agentloom.adapters.pi.metadata import CAPABILITIES, SDK_VERSION, validate_model, validate_options
from agentloom.adapters.pi.protocol import Handshake, HandshakeResult, ModelSelection, Run, RunResult
from agentloom.adapters.pi.transport import PiTransport
from agentloom.runtime.agent_runtime import (
    AgentRuntimeError, AgentRuntimeRequest, AgentRuntimeResult, RuntimeDefinition, RuntimeEvent, RuntimeUsage,
)
from agentloom.runtime.hooks import HookEvent
from agentloom.runtime.trace import capture_explicit_execution_context


class PiRuntime:
    runtime_id = "pi"
    capabilities = CAPABILITIES

    def __init__(self, definition: RuntimeDefinition):
        if definition.model_selection is None:
            raise AgentRuntimeError("Pi requires a native model selection", category="configuration")
        try:
            validate_model(definition.model_selection)
            validate_options(definition.runtime_options)
        except ValueError as exc:
            raise AgentRuntimeError(str(exc), category="configuration") from None
        if definition.tool_manifest:
            raise AgentRuntimeError("Pi tools are not enabled", category="unsupported_capability")
        self.definition = definition
        self.transport = PiTransport(definition.instance_id or uuid4().hex)
        try:
            response = self.transport.request(Handshake(method="handshake", native_tool_contract=1), timeout=15)
            result = response.payload
            if not isinstance(result, HandshakeResult) or result.sdk_version != SDK_VERSION or result.capabilities != self.capabilities:
                raise AgentRuntimeError("Pi bridge SDK or capabilities mismatch", category="configuration")
        except BaseException:
            self.transport.close()
            raise

    def snapshot(self):
        raise AgentRuntimeError("Pi checkpoint/resume is not enabled", category="unsupported_capability")

    def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult:
        missing = request.requirements.unsupported_by(self.capabilities)
        if missing or request.checkpoint is not None or request.checkpoint_sink is not None or request.additional_args:
            raise AgentRuntimeError("Unsupported Pi request capabilities: " + ", ".join(missing or ("checkpoint/additional_args",)),
                                    category="unsupported_capability")
        events = []
        usage = RuntimeUsage()
        definition = self.definition
        selection = definition.model_selection
        assert selection is not None

        def emit(kind, details):
            event = RuntimeEvent(kind=kind, application_id=request.application_id, task_id=request.task_id,
                                 run_id=request.run_id, details=details)
            events.append(event)
            if request.event_sink:
                try:
                    request.event_sink(event)
                except Exception:
                    pass

        def observe(event):
            emit(event.event, event.payload)

        hook = capture_explicit_execution_context().hook_run
        task = request.task
        max_stops = definition.runtime_options.get("max_stop_attempts", 3)
        run_id = request.run_id or uuid4().hex
        try:
            for attempt in range(max_stops):
                if hook is not None:
                    context = hook.consume_pending_agent_context()
                    if context:
                        task += "\n" + "\n".join(context)
                wire = Run(method="run", application_id=request.application_id or "standalone",
                    task_id=request.task_id or "standalone", task=task, cwd=definition.project_root or ".",
                    instructions=definition.instructions or "", model=ModelSelection(model_type=selection.model_type,
                        model_id=selection.model_id, protocol=selection.protocol, settings=dict(selection.settings),
                        request_headers=dict(selection.request_headers)), tools=[], runtime_options=dict(definition.runtime_options),
                    continue_session=request.continue_session or attempt > 0, record_task=request.record_task)
                response = self.transport.request(wire, run_id=run_id, observe=observe)
                result = response.payload
                assert isinstance(result, RunResult)
                part = RuntimeUsage.from_value(result.usage)
                usage = RuntimeUsage(input_tokens=usage.input_tokens + part.input_tokens,
                    output_tokens=usage.output_tokens + part.output_tokens, total_tokens=usage.total_tokens + part.total_tokens,
                    cached_input_tokens=usage.cached_input_tokens + part.cached_input_tokens)
                if result.error:
                    error = result.error
                    raise AgentRuntimeError(error.message, category="internal" if error.category == "protocol" else error.category,
                                            retryable=error.retryable)
                if result.state != "success" or hook is None:
                    break
                decision = hook.dispatch(HookEvent.STOP, "final_answer", {"final_answer": result.output})
                hook.flush_user_messages()
                if not decision.should_block():
                    break
                if attempt == max_stops - 1:
                    raise AgentRuntimeError("Pi Stop gate remained blocked", category="tool")
                task = decision.get_blocked_response()
            emit("terminal", {"state": result.state})
            return AgentRuntimeResult(state=result.state, output=result.output, usage=usage, events=tuple(events))
        except KeyboardInterrupt:
            try:
                self.transport.cancel()
            except AgentRuntimeError:
                pass
            self.close()
            emit("terminal", {"state": "interrupted"})
            raise
        except AgentRuntimeError as error:
            emit("terminal", {"state": "interrupted" if error.category == "interrupted" else "failed", "category": error.category})
            raise

    def close(self):
        try:
            self.transport.close()
        finally:
            self.definition.tool_gateway.close()
