"""Pi bridge protocol handlers for AgentLoom runtime messages."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Any, cast

from agentloom.execution.agent_runtime import (
    AgentRuntimeError,
    AgentRuntimeRequest,
    RuntimeCheckpointEnvelope,
    RuntimeDefinition,
)
from agentloom.execution.goal import GoalCompleteError
from agentloom.execution.native_tool_host import NativeToolHost
from agentloom.execution.native_tools import NativeCallIdentity, NativeCommitAck, ToolManifestEntry
from agentloom.execution.tool_gateway import PreparedToolCall, PreparedToolGateway
from agentloom.execution.tool_protocol import ToolCallRecord, ToolPolicyBlockedError
from agentloom.runtimes.pi.capture import read_capture
from agentloom.runtimes.pi.checkpoint import PiCheckpointStore
from agentloom.runtimes.pi.protocol import (
    Dispatch,
    ModelPermit,
    ModelPrepare,
    PlatformInvoke,
    PlatformPrepare,
    PlatformPrepared,
    PlatformResult,
    Prepare,
    PrepareResult,
    SessionCheckpoint,
    SessionCheckpointResult,
    Settle,
    SettleResult,
    TerminalRecord,
)


def terminal(record: ToolCallRecord) -> TerminalRecord:
    values = record.to_dict()
    values["error"] = record.error
    return TerminalRecord(**values)


class PiCheckpointHandler:
    def __init__(
        self,
        *,
        emit: Callable[[str, Mapping[str, Any]], None],
        set_checkpoint: Callable[[RuntimeCheckpointEnvelope], None],
    ) -> None:
        self.store: PiCheckpointStore | None = None
        self._emit = emit
        self._set_checkpoint = set_checkpoint

    def handle(self, payload: SessionCheckpoint) -> SessionCheckpointResult:
        if self.store is None:
            raise AgentRuntimeError("Pi checkpoint was not requested", category="internal")
        checkpoint = self.store.save(payload.sha256)
        self._set_checkpoint(checkpoint)
        self._emit("checkpoint", {"progress": checkpoint.progress})
        return SessionCheckpointResult(method="session_checkpoint", checkpoint=checkpoint)

    def close(self) -> None:
        if self.store is not None:
            self.store.close()


class PiModelHandler:
    def __init__(
        self,
        *,
        request: AgentRuntimeRequest,
        run_id: str,
        instance_id: str,
        execution: Any,
        hook: Any,
        shared_goal: Any,
    ) -> None:
        self._request = request
        self._run_id = run_id
        self._instance_id = instance_id
        self._execution = execution
        self._hook = hook
        self._shared_goal = shared_goal
        self._model_calls: set[str] = set()
        self._lock = Lock()

    def handle(self, payload: ModelPrepare) -> ModelPermit:
        identity = payload.identity
        with self._lock:
            if (
                (identity.application_id, identity.task_id, identity.run_id, identity.instance_id)
                != (
                    self._request.application_id or "standalone",
                    self._request.task_id or "standalone",
                    self._run_id,
                    self._instance_id,
                )
                or identity.call_id in self._model_calls
            ):
                raise AgentRuntimeError("Invalid Pi model protocol request identity", category="internal")
            self._model_calls.add(identity.call_id)
        state = "work"
        if self._shared_goal is not None:
            try:
                final = self._shared_goal.assert_request_allowed(
                    local_run_id=self._execution.local_run_id,
                    allow_completion_settlement=True,
                )
                self._shared_goal.mark_started()
                state = "final" if final else "work"
            except GoalCompleteError:
                state = "denied"
        return ModelPermit(
            method="model_prepare",
            identity=identity,
            state=state,
            agent_context=self._hook.consume_pending_agent_context()
            if self._hook is not None and state != "denied"
            else [],
        )


class PiToolWorkGate:
    def __init__(self, shared_goal: Any) -> None:
        self._shared_goal = shared_goal

    def require_allowed(self, payload: object) -> None:
        if not isinstance(payload, PlatformPrepare | PlatformInvoke | Prepare | Dispatch):
            return
        if self._shared_goal is not None and self._shared_goal.snapshot().status == "complete":
            raise AgentRuntimeError("Goal is complete; further tool work is forbidden", category="tool")


class PiPlatformToolHandler:
    def __init__(
        self,
        *,
        request: AgentRuntimeRequest,
        definition: RuntimeDefinition,
        run_id: str,
        instance_id: str,
        platform_entries: Mapping[str, ToolManifestEntry],
        checkpoint: PiCheckpointHandler,
        record_tool: Callable[[ToolCallRecord, ToolManifestEntry, NativeCallIdentity], None],
    ) -> None:
        self._request = request
        self._definition = definition
        self._run_id = run_id
        self._instance_id = instance_id
        self._platform_entries = dict(platform_entries)
        self._checkpoint = checkpoint
        self._record_tool = record_tool
        self._platform_calls: set[NativeCallIdentity] = set()
        self._platform_pending: dict[NativeCallIdentity, PreparedToolCall] = {}
        self._lock = Lock()

    def prepare(self, payload: PlatformPrepare) -> PlatformPrepared:
        identity = payload.identity
        with self._lock:
            if (
                (identity.application_id, identity.task_id, identity.run_id, identity.instance_id)
                != (self._request.application_id, self._request.task_id, self._run_id, self._instance_id)
                or payload.tool_name not in self._platform_entries
                or identity in self._platform_calls
            ):
                raise AgentRuntimeError("Invalid Pi platform protocol request identity or selection", category="internal")
            self._platform_calls.add(identity)
        prepared = cast(PreparedToolGateway, self._definition.tool_gateway).prepare(
            call_id=identity.call_id,
            tool_name=payload.tool_name,
            arguments=payload.arguments,
        )
        if isinstance(prepared, ToolCallRecord):
            if prepared.status == "completed":
                raise AgentRuntimeError("Pi preparation cannot report an executed tool", category="internal")
            arguments = dict(prepared.input) if isinstance(prepared.input, dict) else dict(payload.arguments)
        else:
            arguments = dict(prepared.arguments)
        if self._checkpoint.store is not None:
            self._checkpoint.store.prepare_platform(identity, payload.tool_name, arguments)
        if isinstance(prepared, ToolCallRecord):
            if self._checkpoint.store is not None:
                self._checkpoint.store.reject_platform(identity, prepared)
            self._record_tool(prepared, self._platform_entries[payload.tool_name], identity)
            return PlatformPrepared(method="platform_prepare", arguments=arguments, rejection=terminal(prepared))
        with self._lock:
            self._platform_pending[identity] = prepared
        return PlatformPrepared(method="platform_prepare", arguments=arguments)

    def invoke(self, payload: PlatformInvoke) -> PlatformResult:
        identity = payload.identity
        with self._lock:
            pending = self._platform_pending.pop(identity, None)
        if pending is None or pending.tool_name != payload.tool_name or dict(pending.arguments) != payload.arguments:
            raise AgentRuntimeError("Pi platform invocation does not match preparation", category="internal")
        if self._checkpoint.store is not None:
            self._checkpoint.store.start_platform(identity, payload.tool_name, payload.arguments)
        record = cast(PreparedToolGateway, self._definition.tool_gateway).execute_prepared(pending)
        if self._checkpoint.store is not None:
            self._checkpoint.store.commit_platform(identity, record)
        self._record_tool(record, self._platform_entries[payload.tool_name], identity)
        return PlatformResult(
            method="platform_invoke",
            record=terminal(record),
            model_output=record.model_output(),
        )


class PiNativeToolHandler:
    def __init__(
        self,
        *,
        native: NativeToolHost,
        native_entries: tuple[ToolManifestEntry, ...],
        private_directory: Path,
        record_tool: Callable[[ToolCallRecord, ToolManifestEntry, NativeCallIdentity], None],
    ) -> None:
        self._native = native
        self._native_entries = native_entries
        self._private_directory = private_directory
        self._record_tool = record_tool
        self._emitted_commits: set[str] = set()
        self._lock = Lock()

    def prepare(self, payload: Prepare) -> PrepareResult:
        prepared = self._native.prepare(payload.call)
        if prepared.rejection is not None:
            self._record_tool(prepared.rejection, payload.call.tool, payload.call.identity)
        return PrepareResult(
            method="tool_prepare",
            authorization=prepared.authorization,
            rejection=terminal(prepared.rejection) if prepared.rejection else None,
        )

    def dispatch(self, payload: Dispatch) -> PrepareResult:
        try:
            grant = self._native.start_execution(payload.authorization)
        except ToolPolicyBlockedError:
            raw = self._native.receipt(payload.authorization.identity)["dispatch_rejection"]
            rejected = ToolCallRecord.from_dict(raw)
            self._record_tool(rejected, payload.authorization.tool, payload.authorization.identity)
            return PrepareResult(method="tool_dispatch", rejection=terminal(rejected))
        return PrepareResult(method="tool_dispatch", authorization=grant)

    def settle(self, payload: Settle) -> SettleResult:
        if payload.outcome.output is not None:
            raise AgentRuntimeError("Pi results must use the capture artifact", category="internal")
        output, capture = read_capture(
            self._private_directory,
            payload.outcome.authorization_id,
            payload.capture.sha256,
            completed=payload.outcome.status == "completed",
        )
        settled = self._native.settle(replace(payload.outcome, output=output), capture=capture)
        if isinstance(settled, NativeCommitAck):
            with self._lock:
                if settled.commit_id not in self._emitted_commits:
                    entry = next(tool for tool in self._native_entries if tool.visible_name == settled.record.tool_name)
                    self._record_tool(settled.record, entry, settled.identity)
                    self._emitted_commits.add(settled.commit_id)
        return SettleResult(
            method="tool_settle",
            identity=payload.outcome.identity,
            authorization_id=payload.outcome.authorization_id,
            state="committed" if isinstance(settled, NativeCommitAck) else "uncertain",
            commit_id=settled.commit_id if isinstance(settled, NativeCommitAck) else None,
            record=terminal(settled.record) if isinstance(settled, NativeCommitAck) else None,
        )


class PiProtocolCoordinator:
    """Thin router from Pi protocol payloads to specialized handlers."""

    def __init__(
        self,
        *,
        request: AgentRuntimeRequest,
        definition: RuntimeDefinition,
        run_id: str,
        instance_id: str,
        private_directory: Path,
        execution: Any,
        hook: Any,
        shared_goal: Any,
        native: NativeToolHost | None,
        native_entries: tuple[ToolManifestEntry, ...],
        platform_entries: Mapping[str, ToolManifestEntry],
        emit: Callable[[str, Mapping[str, Any]], None],
        set_checkpoint: Callable[[RuntimeCheckpointEnvelope], None],
    ) -> None:
        self.checkpoint = PiCheckpointHandler(emit=emit, set_checkpoint=set_checkpoint)
        self.model = PiModelHandler(
            request=request,
            run_id=run_id,
            instance_id=instance_id,
            execution=execution,
            hook=hook,
            shared_goal=shared_goal,
        )
        self.gate = PiToolWorkGate(shared_goal)

        def record_tool(record: ToolCallRecord, entry: ToolManifestEntry, identity: NativeCallIdentity) -> None:
            emit("tool", {"record": record.to_dict(), "owner": entry.owner, "provider": entry.provider,
                          "instance_id": identity.instance_id, "hook_run_id": execution.local_run_id})

        self.platform = PiPlatformToolHandler(
            request=request,
            definition=definition,
            run_id=run_id,
            instance_id=instance_id,
            platform_entries=platform_entries,
            checkpoint=self.checkpoint,
            record_tool=record_tool,
        )
        self.native = None if native is None else PiNativeToolHandler(
            native=native,
            native_entries=native_entries,
            private_directory=private_directory,
            record_tool=record_tool,
        )

    @property
    def store(self) -> PiCheckpointStore | None:
        return self.checkpoint.store

    @store.setter
    def store(self, value: PiCheckpointStore | None) -> None:
        self.checkpoint.store = value

    def handle(self, payload: object) -> object:
        if isinstance(payload, SessionCheckpoint):
            return self.checkpoint.handle(payload)
        if isinstance(payload, ModelPrepare):
            return self.model.handle(payload)
        self.gate.require_allowed(payload)
        if isinstance(payload, PlatformPrepare):
            return self.platform.prepare(payload)
        if isinstance(payload, PlatformInvoke):
            return self.platform.invoke(payload)
        if self.native is not None and isinstance(payload, Prepare):
            return self.native.prepare(payload)
        if self.native is not None and isinstance(payload, Dispatch):
            return self.native.dispatch(payload)
        if self.native is not None and isinstance(payload, Settle):
            return self.native.settle(payload)
        raise AgentRuntimeError("Unexpected Pi protocol request", category="internal")

    def close(self) -> None:
        self.checkpoint.close()
