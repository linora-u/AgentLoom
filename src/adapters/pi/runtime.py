"""Pi implementation of a complete Agent invocation, including the platform Stop gate."""
from __future__ import annotations

from uuid import uuid4
from pathlib import Path
from dataclasses import replace
from threading import Lock
from typing import cast

from agentloom.adapters.pi.metadata import CAPABILITIES, SDK_VERSION, validate_model, validate_options
from agentloom.adapters.pi.protocol import (
    Handshake, HandshakeResult, ModelSelection, Run, RunResult,
    Prepare, PrepareResult, Settle, SettleResult, TerminalRecord,
    PlatformInvoke, PlatformResult, ModelPrepare, ModelPermit,
)
from agentloom.runtime.native_tool_host import NativeReadToolHost
from agentloom.runtime.native_tools import NativeCommitAck
from agentloom.runtime.tool_protocol import ToolCallRecord
from agentloom.adapters.pi.transport import PiTransport
from agentloom.runtime.agent_runtime import (
    AgentRuntimeError, AgentRuntimeRequest, AgentRuntimeResult, RuntimeDefinition, RuntimeEvent, RuntimeUsage,
)
from agentloom.runtime.hooks import HookEvent
from agentloom.runtime.trace import capture_explicit_execution_context
from agentloom.runtime.goal import GoalCompleteError, get_current_goal_provider
from agentloom.runtime.invocation import goal_continuation_prompt
from agentloom.tools.tool_meta import tool_is_concurrency_safe


def _terminal(record: ToolCallRecord) -> TerminalRecord:
    values = record.to_dict()
    values["error"] = record.error
    return TerminalRecord(**values)


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
        if any(tool.operation in {"write", "shell"} or (tool.owner == "runtime" and (
               tool.provider != "pi" or tool.visible_name != "read" or tool.operation != "read" or tool.fixed_arguments))
               for tool in definition.tool_manifest):
            raise AgentRuntimeError("Unsupported Pi tool selection", category="unsupported_capability")
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
        from agentloom.runtime.resources import register_resource
        register_resource(f"pi-runtime:{self.transport.instance_id}", self.close,
                          instance_id=self.transport.instance_id)

    def snapshot(self):
        raise AgentRuntimeError("Pi checkpoint/resume is not enabled", category="unsupported_capability")

    def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult:
        missing = request.requirements.unsupported_by(self.capabilities)
        if missing or request.checkpoint is not None or request.checkpoint_sink is not None:
            raise AgentRuntimeError("Unsupported Pi request capabilities: " + ", ".join(missing or ("checkpoint",)),
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
                        if not tool_is_concurrency_safe(tool.logical_name, execution.agent_config)]
        task = request.task
        max_stops = cast(int, definition.runtime_options.get("max_stop_attempts", 3))
        run_id = request.run_id or uuid4().hex
        cwd = str(Path(definition.project_root or ".").resolve())
        native_entries = tuple(tool for tool in definition.tool_manifest if tool.owner == "runtime")
        platform_entries = {tool.visible_name: tool for tool in definition.tool_manifest if tool.owner != "runtime"}
        native = NativeReadToolHost(tools=native_entries, cwd=cwd) if native_entries else None
        descriptions = {tool.name: tool.description for tool in definition.tool_gateway.definitions}
        wire_tools = [tool if tool.owner == "runtime" else replace(tool, parameters={
            **tool.parameters, "description": descriptions[tool.visible_name]}) for tool in definition.tool_manifest]
        model_calls = set()
        platform_calls = set()
        platform_lock = Lock()
        emitted_commits = set()

        def record_tool(record, entry, identity):
            emit("tool", {"record": record.to_dict(), "owner": entry.owner, "provider": entry.provider,
                          "instance_id": identity.instance_id, "hook_run_id": execution.local_run_id})

        def cancel_callbacks():
            from agentloom.runtime.resources import close_instance_resources, close_run_resources
            if execution.local_run_id == execution.root_run_id:
                close_run_resources()
            else:
                close_instance_resources(self.transport.instance_id)

        def callback(payload):
            if isinstance(payload, ModelPrepare):
                identity = payload.identity
                with platform_lock:
                    if ((identity.application_id, identity.task_id, identity.run_id, identity.instance_id) != (
                            request.application_id or "standalone", request.task_id or "standalone", run_id,
                            self.transport.instance_id) or identity.call_id in model_calls):
                        raise AgentRuntimeError("Invalid Pi model callback identity", category="internal")
                    model_calls.add(identity.call_id)
                state = "work"
                if shared_goal is not None:
                    try:
                        final = shared_goal.assert_request_allowed(local_run_id=execution.local_run_id,
                                                                  allow_completion_settlement=True)
                        shared_goal.mark_started()
                        state = "final" if final else "work"
                    except GoalCompleteError:
                        state = "denied"
                return ModelPermit(method="model_prepare", identity=identity, state=state,
                    agent_context=hook.consume_pending_agent_context() if hook is not None and state != "denied" else [])
            if isinstance(payload, (PlatformInvoke, Prepare)) and shared_goal is not None:
                if shared_goal.snapshot().status == "complete":
                    raise AgentRuntimeError("Goal is complete; further tool work is forbidden", category="tool")
            if isinstance(payload, PlatformInvoke):
                identity = payload.identity
                with platform_lock:
                    if ((identity.application_id, identity.task_id, identity.run_id, identity.instance_id) != (
                            request.application_id, request.task_id, run_id, self.transport.instance_id)
                            or payload.tool_name not in platform_entries or identity.call_id in platform_calls):
                        raise AgentRuntimeError("Invalid Pi platform callback identity or selection", category="internal")
                    platform_calls.add(identity.call_id)
                record = definition.tool_gateway.invoke(call_id=identity.call_id, tool_name=payload.tool_name,
                                                        arguments=payload.arguments)
                record_tool(record, platform_entries[payload.tool_name], identity)
                return PlatformResult(method="platform_invoke", record=_terminal(record))
            if native is not None and isinstance(payload, Prepare):
                prepared = native.prepare(payload.call)
                if prepared.rejection is not None:
                    record_tool(prepared.rejection, payload.call.tool, payload.call.identity)
                return PrepareResult(method="tool_prepare",
                    authorization=native.start_execution(prepared.authorization) if prepared.authorization else None,
                    rejection=_terminal(prepared.rejection) if prepared.rejection else None)
            if native is not None and isinstance(payload, Settle):
                settled = native.settle(payload.outcome)
                if isinstance(settled, NativeCommitAck):
                    with platform_lock:
                        if settled.commit_id not in emitted_commits:
                            entry = next(tool for tool in native_entries if tool.visible_name == settled.record.tool_name)
                            record_tool(settled.record, entry, settled.identity)
                            emitted_commits.add(settled.commit_id)
                return SettleResult(method="tool_settle", identity=payload.outcome.identity,
                    authorization_id=payload.outcome.authorization_id,
                    state="committed" if isinstance(settled, NativeCommitAck) else "uncertain",
                    commit_id=settled.commit_id if isinstance(settled, NativeCommitAck) else None,
                    record=_terminal(settled.record) if isinstance(settled, NativeCommitAck) else None)
            raise AgentRuntimeError("Unexpected Pi tool callback", category="internal")

        try:
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
                    additional_args=dict(request.additional_args))
                response = self.transport.request(wire, run_id=run_id, observe=observe, callback=callback,
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
        finally:
            if native is not None:
                native.close()

    def close(self):
        try:
            self.transport.close()
        finally:
            self.definition.tool_gateway.close()
