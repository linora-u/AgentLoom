"""Complete-run adapter for the smolagents runtime."""

from __future__ import annotations

from contextvars import ContextVar
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from agentloom.runtime.agent_runtime import (
    AgentRuntimeRequest,
    AgentRuntimeResult,
    RuntimeCapabilities,
    RuntimeCheckpointEnvelope,
    require_runtime_state,
)
from agentloom.runtime.checkpoint.conversation_recovery import (
    prepare_steps_for_resume,
)
from agentloom.runtime.checkpoint.serializer import CheckpointSerializer

try:
    _SMOLAGENTS_VERSION = version("smolagents")
except PackageNotFoundError:  # pragma: no cover - importing this adapter requires smolagents
    _SMOLAGENTS_VERSION = "unknown"


class SmolagentsRuntimeAdapter:
    """Hide smolagents run arguments and result types behind AgentLoom contracts."""

    runtime_id = "smolagents"
    runtime_version = _SMOLAGENTS_VERSION
    state_schema_version = 1

    def __init__(
        self,
        native_runtime: Any,
        *,
        checkpoint_sink: Any | None = None,
    ) -> None:
        self._native_runtime = native_runtime
        self._default_checkpoint_sink = checkpoint_sink
        self._checkpoint_sink_context: ContextVar[Any | None] = ContextVar(
            f"agentloom_smolagents_checkpoint_sink_{id(self)}",
            default=None,
        )
        self._register_checkpoint_bridge()

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            structured_tools=True,
            parallel_tools=True,
            checkpoint_resume=True,
            subagents=True,
        )

    @property
    def native_runtime(self) -> Any:
        """Return the native runtime for adapter-owned compatibility bridges."""

        return self._native_runtime

    def _checkpoint(self, steps: list[Any] | None = None) -> RuntimeCheckpointEnvelope:
        if steps is None:
            memory = getattr(self._native_runtime, "memory", None)
            memory_steps = list(getattr(memory, "steps", ()) or ())
        else:
            memory_steps = list(steps)
        return RuntimeCheckpointEnvelope(
            runtime_id=self.runtime_id,
            runtime_version=self.runtime_version,
            state_schema_version=self.state_schema_version,
            payload={
                "memory_steps": CheckpointSerializer.serialize_memory_steps(memory_steps)
            },
        )

    def _register_checkpoint_bridge(self) -> None:
        native = getattr(self._native_runtime, "_agent", self._native_runtime)
        callbacks = getattr(native, "step_callbacks", None)
        if callbacks is None:
            return

        from smolagents.memory import ActionStep

        def push_checkpoint(completed_step: Any, **kwargs: Any) -> None:
            checkpoint_sink = (
                self._checkpoint_sink_context.get()
                or self._default_checkpoint_sink
            )
            if checkpoint_sink is None:
                return
            steps = list(native.memory.steps)
            if kwargs.get("agent") is native and (
                not steps or steps[-1] is not completed_step
            ):
                steps.append(completed_step)
            checkpoint_sink(self._checkpoint(steps))

        callbacks.register(ActionStep, push_checkpoint)

    def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult:
        if request.checkpoint is not None:
            request.checkpoint.require_compatible(
                runtime_id=self.runtime_id,
                state_schema_version=self.state_schema_version,
            )
            raw_steps = request.checkpoint.payload.get("memory_steps", [])
            steps = CheckpointSerializer.deserialize_memory_steps(list(raw_steps))
            steps, _interruption = prepare_steps_for_resume(steps)
            self._native_runtime.memory.steps = steps

        run_kwargs: dict[str, Any] = {
            "task": request.task,
            "return_full_result": True,
            "reset": not request.continue_session,
        }
        if request.continue_session and request.record_task:
            run_kwargs["_skip_task_step_on_reset_false"] = False
        if request.additional_args:
            run_kwargs["additional_args"] = dict(request.additional_args)

        checkpoint_sink = request.checkpoint_sink or self._default_checkpoint_sink
        token = self._checkpoint_sink_context.set(checkpoint_sink)
        try:
            native_result = self._native_runtime.run(**run_kwargs)
        finally:
            self._checkpoint_sink_context.reset(token)
        require_runtime_state(
            native_result,
            allowed_states={"success", "max_steps_error"},
            error_prefix="Agent run did not complete successfully",
        )
        checkpoint = self._checkpoint()
        if checkpoint_sink is not None:
            checkpoint_sink(checkpoint)
        return AgentRuntimeResult(
            state=getattr(native_result, "state", "success"),
            output=getattr(native_result, "output", None),
            usage=getattr(native_result, "token_usage", None),
            checkpoint=checkpoint,
        )

    def close(self) -> None:
        close = getattr(self._native_runtime, "close", None)
        if callable(close):
            close()
