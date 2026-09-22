"""Pi implementation of a complete Agent invocation, including the platform Stop gate."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import uuid4

from agentloom.runtimes.pi.checkpoint import PiCheckpointStore
from agentloom.runtimes.pi.metadata import BRIDGE_VERSION, CAPABILITIES, SDK_VERSION, validate_model, validate_options
from agentloom.runtimes.pi.protocol import (
    PI_BRIDGE_PROTOCOL_VERSION,
    Handshake,
    HandshakeResult,
    ModelSelection,
    Run,
    RunResult,
)
from agentloom.runtimes.pi.protocol_handlers import PiProtocolCoordinator
from agentloom.runtimes.pi.recovery import reconcile
from agentloom.runtimes.pi.transport import PiTransport
from agentloom.execution import get_current_run_context
from agentloom.execution.agent_runtime import (
    AgentRuntimeError,
    AgentRuntimeRequest,
    AgentRuntimeResult,
    RuntimeCheckpointEnvelope,
    RuntimeDefinition,
    RuntimeEvent,
    RuntimeUsage,
)
from agentloom.execution.goal import (
    get_current_goal_provider,
    goal_continuation_prompt,
)
from agentloom.execution.hooks import HookEvent
from agentloom.execution.native_tool_host import NativeToolHost
from agentloom.execution.native_tools import NativeCallIdentity
from agentloom.execution.tool_gateway import PreparedToolGateway
from agentloom.execution.trace import capture_explicit_execution_context
from agentloom.tools.tool_meta import tool_is_concurrency_safe


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
        if any((tool.owner != "runtime" and tool.operation in {"write", "shell"}) or (tool.owner == "runtime" and (
               tool.provider != "pi" or (tool.visible_name, tool.operation) not in {("read", "read"), ("write", "write"), ("edit", "write"), ("bash", "shell")} or tool.fixed_arguments))
               for tool in definition.tool_manifest):
            raise AgentRuntimeError("Unsupported Pi tool selection", category="unsupported_capability")
        self.definition = definition
        if any(tool.owner != "runtime" for tool in definition.tool_manifest) and not isinstance(definition.tool_gateway, PreparedToolGateway):
            raise AgentRuntimeError("Pi platform tools require preparation before execution", category="unsupported_capability")
        self._checkpoint: RuntimeCheckpointEnvelope | None = None
        self.transport = PiTransport(definition.instance_id or uuid4().hex)
        try:
            response = self.transport.request(Handshake(
                method="handshake",
                protocol_version=PI_BRIDGE_PROTOCOL_VERSION,
                bridge_version=BRIDGE_VERSION,
                native_tool_contract=1,
            ), timeout=15)
            result = response.payload
            if (not isinstance(result, HandshakeResult)
                    or result.protocol_version != PI_BRIDGE_PROTOCOL_VERSION
                    or result.bridge_version != BRIDGE_VERSION
                    or result.sdk_version != SDK_VERSION
                    or result.capabilities != self.capabilities):
                raise AgentRuntimeError("Pi bridge SDK or capabilities mismatch", category="configuration")
        except BaseException:
            self.transport.close()
            raise
        from agentloom.execution.resources import register_resource
        register_resource(f"pi-runtime:{self.transport.instance_id}", self.close,
                          instance_id=self.transport.instance_id)

    def snapshot(self) -> RuntimeCheckpointEnvelope:
        if self._checkpoint is None:
            raise AgentRuntimeError("Pi has no durable checkpoint yet", category="configuration")
        return self._checkpoint

    def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult:
        missing = request.requirements.unsupported_by(self.capabilities)
        if missing:
            raise AgentRuntimeError("Unsupported Pi request capabilities: " + ", ".join(missing),
                                    category="unsupported_capability")
        events = []
        usage = RuntimeUsage()
        definition = self.definition
        selection = definition.model_selection
        assert selection is not None

        def emit(kind, details):
            event = RuntimeEvent(kind=kind, application_id=request.application_id, task_id=request.task_id,
                                 run_id=request.run_id, details={**details,
                                     "instance_id": self.transport.instance_id, "hook_run_id": execution.local_run_id,
                                     "root_run_id": execution.root_run_id,
                                     "root_agent": execution.local_run_id == execution.root_run_id})
            events.append(event)
            if request.event_sink:
                try:
                    request.event_sink(event)
                except Exception:
                    pass

        def observe(event):
            emit(event.event, event.payload)

        execution = capture_explicit_execution_context()
        hook = execution.hook_run
        shared_goal = get_current_goal_provider()
        goal = shared_goal if execution.local_run_id == execution.root_run_id else None
        serial_tools = [tool.visible_name for tool in definition.tool_manifest
                        if not tool_is_concurrency_safe(tool.visible_name, execution.agent_config)]
        task = request.task
        max_stops = cast(int, definition.runtime_options.get("max_stop_attempts", 3))
        run_id = request.run_id or uuid4().hex
        cwd = str(Path(definition.project_root or ".").resolve())
        native_entries = tuple(tool for tool in definition.tool_manifest if tool.owner == "runtime")
        platform_entries = {tool.visible_name: tool for tool in definition.tool_manifest if tool.owner != "runtime"}
        native = NativeToolHost(tools=native_entries, cwd=cwd) if native_entries else None
        descriptions = {tool.name: tool.description for tool in definition.tool_gateway.definitions}
        wire_tools = [tool if tool.owner == "runtime" else replace(tool, parameters={
            **tool.parameters, "description": descriptions[tool.visible_name]}) for tool in definition.tool_manifest]
        protocol = PiProtocolCoordinator(
            request=request,
            definition=definition,
            run_id=run_id,
            instance_id=self.transport.instance_id,
            private_directory=self.transport.private_directory,
            execution=execution,
            hook=hook,
            shared_goal=shared_goal,
            native=native,
            native_entries=native_entries,
            platform_entries=platform_entries,
            emit=emit,
            set_checkpoint=lambda checkpoint: setattr(self, "_checkpoint", checkpoint),
        )

        def cancel_callbacks():
            from agentloom.execution.resources import close_instance_resources, close_run_resources
            if execution.local_run_id == execution.root_run_id:
                close_run_resources()
            else:
                close_instance_resources(self.transport.instance_id)

        try:
            if request.checkpoint_sink is not None or request.checkpoint is not None or request.requirements.checkpoint_resume:
                runtime_context = get_current_run_context(required=True)
                assert runtime_context is not None
                protocol.store = PiCheckpointStore(runtime_context, definition, self.transport.private_directory,
                                                     self.transport.instance_id, request.checkpoint_sink or (lambda _: None))
                if request.checkpoint is not None:
                    try:
                        plan = reconcile(protocol.store, protocol.store.load(request.checkpoint))
                        if native is not None:
                            for call in plan["bundle"]["calls"]:
                                if call["owner"] == "runtime":
                                    native.restore_observation(NativeCallIdentity(**call["identity"]))
                        protocol.store.private.atomic_write_json("restore.json", plan)
                    except AgentRuntimeError:
                        raise
                    except Exception as exc:
                        raise AgentRuntimeError("Pi checkpoint cannot be safely restored", category="configuration") from exc
            attempt = 0
            stop_blocks = 0
            while True:
                if hook is not None:
                    context = hook.consume_pending_agent_context()
                    if context:
                        task += "\n" + "\n".join(context)
                wire = Run(method="run", application_id=request.application_id or "standalone",
                    task_id=request.task_id or "standalone", task=task, cwd=cwd,
                    instructions=definition.instructions or "", model=ModelSelection(model_type=selection.model_type,
                        model_id=selection.model_id, protocol=selection.protocol, settings=dict(selection.settings),
                        request_headers=dict(selection.request_headers)), tools=wire_tools, serial_tools=serial_tools, runtime_options=dict(definition.runtime_options),
                    continue_session=request.continue_session or attempt > 0, record_task=request.record_task,
                    additional_args=dict(request.additional_args), checkpoint_enabled=protocol.store is not None,
                    checkpoint=request.checkpoint if attempt == 0 else None)
                response = self.transport.request(wire, run_id=run_id, observe=observe, callback=protocol.handle,
                                                  cancel_callbacks=cancel_callbacks)
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
                goal_state = goal.snapshot() if goal is not None else None
                if not decision.should_block() and (goal_state is None or goal_state.status == "complete"):
                    break
                if decision.should_block():
                    stop_blocks += 1
                    if stop_blocks >= max_stops or (goal_state is not None and goal_state.status == "complete"):
                        raise AgentRuntimeError("Pi Stop gate remained blocked", category="tool")
                    task = decision.get_blocked_response()
                elif goal_state is not None:
                    task = goal_continuation_prompt(goal_state)
                attempt += 1
            emit("terminal", {"state": result.state})
            return AgentRuntimeResult(state=result.state, output=result.output, usage=usage, events=tuple(events),
                                      checkpoint=self._checkpoint)
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
        finally:
            if native is not None:
                native.close()
            protocol.close()

    def close(self):
        try:
            self.transport.close()
        finally:
            self.definition.tool_gateway.close()
