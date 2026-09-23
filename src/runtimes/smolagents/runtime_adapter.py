"""Complete-run adapter for the smolagents runtime."""

from __future__ import annotations

from contextlib import nullcontext
from contextvars import ContextVar
from importlib.metadata import PackageNotFoundError, version
from threading import RLock
from typing import Any

from agentloom.execution.agent_runtime import (
    AgentRuntimeError,
    AgentRuntimeRequest,
    AgentRuntimeResult,
    JSONValue,
    OutputContract,
    RuntimeCapabilities,
    RuntimeCheckpointEnvelope,
    RuntimeEvent,
    RuntimeEventKind,
    RuntimeUsage,
    require_runtime_state,
)
from agentloom.execution.logging import get_logger
from agentloom.execution.model_binding import ModelTurnBinding
from agentloom.execution.model_protocol import ModelProtocolError, ModelTurnResult
from agentloom.execution.tool_gateway import ToolGateway
from agentloom.execution.tool_protocol import ToolCallRecord
from agentloom.runtimes.smolagents.checkpoint_codec import (
    CANONICAL_MODEL_ITEMS_KEY,
    SmolagentsCheckpointCodec,
)
from agentloom.runtimes.smolagents.conversation_recovery import (
    prepare_steps_for_resume,
)
from agentloom.runtimes.smolagents.metadata import CAPABILITIES
from agentloom.runtimes.smolagents.recoverable_errors import (
    is_recoverable_agent_error,
)

try:
    _SMOLAGENTS_VERSION = version("smolagents")
except PackageNotFoundError:  # pragma: no cover - importing this adapter requires smolagents
    _SMOLAGENTS_VERSION = "unknown"

logger = get_logger(__name__)
MODEL_ADAPTER_AUDIT_KEY = "model_adapter_id"


def _exception_chain(error: Exception) -> tuple[Exception, ...]:
    chain: list[Exception] = []
    current: BaseException | None = error
    visited: set[int] = set()
    while isinstance(current, Exception) and id(current) not in visited:
        visited.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _goal_control_error(error: Exception) -> Exception | None:
    from agentloom.execution.goal import GoalCompleteError

    return next(
        (
            item
            for item in _exception_chain(error)
            if isinstance(item, GoalCompleteError)
        ),
        None,
    )


def _provider_cause(error: Exception) -> Exception | None:
    from litellm.exceptions import (
        APIConnectionError,
        APIError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitError,
        ServiceUnavailableError,
        Timeout,
        UnsupportedParamsError,
    )

    provider_types = (
        APIConnectionError,
        APIError,
        AuthenticationError,
        BadRequestError,
        InternalServerError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitError,
        ServiceUnavailableError,
        Timeout,
        UnsupportedParamsError,
        ModelProtocolError,
    )
    return next(
        (
            item
            for item in _exception_chain(error)
            if isinstance(item, provider_types)
        ),
        None,
    )


def _runtime_error(error: Exception) -> AgentRuntimeError:
    if isinstance(error, AgentRuntimeError):
        return error
    provider_cause = _provider_cause(error)
    if provider_cause is not None:
        from agentloom.integrations.litellm.litellm_retry import (
            is_retryable_litellm_error,
        )

        return AgentRuntimeError(
            f"Agent runtime provider failure: {provider_cause}",
            category="provider",
            cause=provider_cause,
            retryable=is_retryable_litellm_error(provider_cause),
        )
    return AgentRuntimeError(
        f"Agent runtime internal failure: {error}",
        category="internal",
        cause=error,
        retryable=False,
    )


def _exhausted_output_correction(native_runtime: Any) -> bool:
    """Return whether max steps followed a rejected structured final answer."""

    steps = getattr(getattr(native_runtime, "memory", None), "steps", ())
    for step in reversed(steps):
        records = getattr(step, "tool_results", None) or ()
        for record in reversed(records):
            if not isinstance(record, ToolCallRecord):
                continue
            if record.tool_name != "final_answer" or record.status == "completed":
                continue
            return (
                record.stage == "output_validation"
                or "output does not satisfy" in record.reason
                or "final_answer requires exactly" in record.reason
            )
    return False


class SmolagentsRuntimeAdapter:
    """Hide smolagents run arguments and result types behind AgentLoom contracts."""

    runtime_id = "smolagents"
    runtime_version = _SMOLAGENTS_VERSION
    # Schema 2 makes the canonical item stream explicit and authoritative.
    # Schema 1 is deliberately not translated or resumed.
    state_schema_version = 2

    def __init__(
        self,
        native_runtime: Any,
        *,
        model_binding: ModelTurnBinding,
        checkpoint_sink: Any | None = None,
        tool_gateway: ToolGateway | None = None,
        output_contract: OutputContract | None = None,
    ) -> None:
        if not isinstance(model_binding, ModelTurnBinding):
            raise TypeError("model_binding must be a ModelTurnBinding")
        from agentloom.runtimes.smolagents.todo import TodoStateProvider

        self._todo_provider = TodoStateProvider()
        self._native_runtime = native_runtime
        self._model_binding = model_binding
        self._tool_gateway = tool_gateway
        self._output_contract = output_contract
        self._default_checkpoint_sink = checkpoint_sink
        self._checkpoint_sink_context: ContextVar[Any | None] = ContextVar(
            f"agentloom_smolagents_checkpoint_sink_{id(self)}",
            default=None,
        )
        self._request_context: ContextVar[AgentRuntimeRequest | None] = ContextVar(
            f"agentloom_smolagents_request_{id(self)}",
            default=None,
        )
        self._event_context: ContextVar[list[RuntimeEvent] | None] = ContextVar(
            f"agentloom_smolagents_events_{id(self)}",
            default=None,
        )
        self._close_lock = RLock()
        self._closed = False
        self._register_checkpoint_bridge()

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return CAPABILITIES

    @staticmethod
    def _is_committed_step(step: Any) -> bool:
        from smolagents.memory import ActionStep

        return not (
            isinstance(step, ActionStep)
            and step.model_output_message is None
            and not is_recoverable_agent_error(step.error)
        )

    @classmethod
    def _committed_steps(cls, steps: list[Any]) -> list[Any]:
        """Exclude ActionSteps that never completed a canonical model turn."""

        return [
            step
            for step in steps
            if cls._is_committed_step(step)
        ]

    def _checkpoint(self, steps: list[Any] | None = None) -> RuntimeCheckpointEnvelope:
        if steps is None:
            memory = getattr(self._native_runtime, "memory", None)
            memory_steps = self._committed_steps(
                list(getattr(memory, "steps", ()) or ())
            )
        else:
            memory_steps = self._committed_steps(list(steps))
        canonical_items = (
            SmolagentsCheckpointCodec.serialize_canonical_model_items(
                memory_steps
            )
        )
        request = self._request_context.get()
        application_context = None
        if request is None:
            from agentloom.execution import get_current_run_context

            application_context = get_current_run_context()
        return RuntimeCheckpointEnvelope(
            runtime_id=self.runtime_id,
            runtime_version=self.runtime_version,
            state_schema_version=self.state_schema_version,
            task_id=(
                request.task_id
                if request is not None
                else getattr(application_context, "task_id", None)
            ),
            run_id=(
                request.run_id
                if request is not None
                else getattr(application_context, "run_id", None)
            ),
            progress=len(memory_steps),
            audit_metadata={
                "native_step_count": len(memory_steps),
                "canonical_item_count": len(canonical_items),
                MODEL_ADAPTER_AUDIT_KEY: self._model_binding.adapter_id,
            },
            payload={
                "memory_steps": SmolagentsCheckpointCodec.serialize_memory_steps(
                    memory_steps
                ),
                CANONICAL_MODEL_ITEMS_KEY: canonical_items,
            },
        )

    def _event(
        self,
        kind: RuntimeEventKind,
        *,
        details: dict[str, JSONValue] | None = None,
    ) -> RuntimeEvent:
        request = self._request_context.get()
        return RuntimeEvent(
            kind=kind,
            application_id=request.application_id if request is not None else None,
            task_id=request.task_id if request is not None else None,
            run_id=request.run_id if request is not None else None,
            details=details or {},
        )

    def _emit_event(
        self,
        kind: RuntimeEventKind,
        *,
        details: dict[str, JSONValue] | None = None,
    ) -> RuntimeEvent:
        event = self._event(kind, details=details)
        events = self._event_context.get()
        if events is not None:
            events.append(event)
        request = self._request_context.get()
        if request is not None and request.event_sink is not None:
            try:
                request.event_sink(event)
            except Exception as exc:
                logger.warning(
                    "Runtime event observer failed for %s: %s",
                    kind,
                    exc,
                )
        return event

    def _emit_step_events(self, step: Any) -> None:
        records = getattr(step, "tool_results", None) or ()
        for record in records:
            if not isinstance(record, ToolCallRecord):
                continue
            self._emit_event(
                "tool",
                details={
                    "call_id": record.call_id,
                    "name": record.tool_name,
                    "status": record.status,
                },
            )

    def _observe_model_turn(self, turn: ModelTurnResult) -> None:
        self._emit_event(
            "model",
            details={
                "phase": "completed",
                "item_count": len(turn.items),
                "response_id": turn.response_id,
            },
        )
        usage = RuntimeUsage(
            input_tokens=turn.usage.input_tokens,
            output_tokens=turn.usage.output_tokens,
            total_tokens=turn.usage.total_tokens,
            cached_input_tokens=turn.usage.cached_input_tokens,
            reasoning_tokens=turn.usage.reasoning_tokens,
            details=turn.usage.details,
        )
        self._emit_event(
            "usage",
            details={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "total_tokens": usage.total_tokens,
                "cached_input_tokens": usage.cached_input_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
            },
        )

    def snapshot(self) -> RuntimeCheckpointEnvelope:
        """Capture the native runtime state as an opaque runtime envelope."""

        return self._checkpoint()

    def restore(self, checkpoint: RuntimeCheckpointEnvelope) -> None:
        """Restore a compatible envelope into the native smolagents runtime."""

        checkpoint_adapter = checkpoint.audit_metadata.get(
            MODEL_ADAPTER_AUDIT_KEY
        )
        if not isinstance(checkpoint_adapter, str) or not checkpoint_adapter:
            raise AgentRuntimeError(
                "Checkpoint is missing required model adapter identity",
                category="configuration",
                retryable=False,
            )
        current_adapter = self._model_binding.adapter_id
        if checkpoint_adapter != current_adapter:
            raise AgentRuntimeError(
                "Checkpoint model adapter "
                f"{checkpoint_adapter!r} is incompatible with "
                f"{current_adapter!r}",
                category="configuration",
                retryable=False,
            )
        checkpoint.require_compatible(
            runtime_id=self.runtime_id,
            state_schema_version=self.state_schema_version,
            runtime_version=self.runtime_version,
        )
        raw_steps = checkpoint.payload.get("memory_steps")
        if not isinstance(raw_steps, list):
            raise ValueError("smolagents memory_steps must be a list")
        steps = SmolagentsCheckpointCodec.deserialize_memory_steps(
            raw_steps
        )
        SmolagentsCheckpointCodec.restore_canonical_model_items(
            steps,
            checkpoint.payload.get(CANONICAL_MODEL_ITEMS_KEY),
        )
        steps, _interruption = prepare_steps_for_resume(steps)
        self._native_runtime.memory.steps = steps

    def _register_checkpoint_bridge(self) -> None:
        native = getattr(self._native_runtime, "_agent", self._native_runtime)
        callbacks = getattr(native, "step_callbacks", None)
        if callbacks is None:
            return

        from smolagents.memory import ActionStep

        def observe_completed_step(completed_step: Any, **kwargs: Any) -> None:
            self._emit_step_events(completed_step)
            checkpoint_sink = (
                self._checkpoint_sink_context.get()
                or self._default_checkpoint_sink
            )
            if checkpoint_sink is None:
                return
            if not self._is_committed_step(completed_step):
                return
            steps = list(native.memory.steps)
            if kwargs.get("agent") is native and (
                not steps or steps[-1] is not completed_step
            ):
                steps.append(completed_step)
            checkpoint = self._checkpoint(steps)
            checkpoint_sink(checkpoint)
            self._emit_event(
                "checkpoint",
                details={"progress": checkpoint.progress},
            )

        callbacks.register(ActionStep, observe_completed_step)

    def run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult:
        from agentloom.runtimes.smolagents.todo import bind_todo_state_provider

        with bind_todo_state_provider(self._todo_provider):
            return self._run(request)

    def _run(self, request: AgentRuntimeRequest) -> AgentRuntimeResult:
        events: list[RuntimeEvent] = []
        request_token = self._request_context.set(request)
        event_token = self._event_context.set(events)
        try:
            unsupported = request.requirements.unsupported_by(
                self.capabilities
            )
            if unsupported:
                raise AgentRuntimeError(
                    "Agent runtime 'smolagents' does not support required "
                    f"capabilities: {', '.join(unsupported)}",
                    category="unsupported_capability",
                    retryable=False,
                )
            self._emit_event(
                "run",
                details={
                    "phase": "started",
                    "resumed": request.checkpoint is not None,
                },
            )
            if request.checkpoint is not None:
                self.restore(request.checkpoint)

            continue_session = request.continue_session or request.checkpoint is not None
            run_kwargs: dict[str, Any] = {
                "task": request.task or "",
                "return_full_result": True,
                "reset": not continue_session,
            }
            if request.task is None:
                run_kwargs["_skip_task_step"] = True
            if continue_session and request.record_task:
                run_kwargs["_skip_task_step_on_reset_false"] = False
            if request.additional_args:
                run_kwargs["additional_args"] = dict(request.additional_args)

            checkpoint_sink = request.checkpoint_sink or self._default_checkpoint_sink
            checkpoint_token = self._checkpoint_sink_context.set(checkpoint_sink)
            native = getattr(self._native_runtime, "_agent", self._native_runtime)
            observe_turns = getattr(getattr(native, "model", None), "observe_turns", None)
            model_observer = (
                observe_turns(self._observe_model_turn)
                if callable(observe_turns)
                else nullcontext()
            )
            try:
                with model_observer:
                    native_result = self._native_runtime.run(**run_kwargs)
            finally:
                self._checkpoint_sink_context.reset(checkpoint_token)
            if (
                self._output_contract is not None
                and getattr(native_result, "state", None) == "max_steps_error"
                and _exhausted_output_correction(native)
            ):
                raise AgentRuntimeError(
                    "Agent exhausted its execution budget with an invalid structured output",
                    category="output_validation",
                    retryable=True,
                )
            require_runtime_state(
                native_result,
                allowed_states={"success", "max_steps_error"},
                error_prefix="Agent run did not complete successfully",
            )
            usage = RuntimeUsage.from_value(
                getattr(native_result, "token_usage", None)
            )
            checkpoint = self._checkpoint()
            if checkpoint_sink is not None:
                checkpoint_sink(checkpoint)
            self._emit_event(
                "checkpoint",
                details={"progress": checkpoint.progress},
            )
            self._emit_event(
                "usage",
                details={
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.total_tokens,
                },
            )
            self._emit_event(
                "terminal",
                details={"state": getattr(native_result, "state", "success")},
            )
            return AgentRuntimeResult(
                state=getattr(native_result, "state", "success"),
                output=getattr(native_result, "output", None),
                usage=usage,
                events=tuple(events),
                checkpoint=checkpoint,
            )
        except Exception as exc:
            control_error = _goal_control_error(exc)
            if control_error is not None:
                if control_error is exc:
                    raise
                raise control_error from exc
            error = _runtime_error(exc)
            self._emit_event(
                "terminal",
                details={
                    "state": "failed",
                    "category": error.category,
                    "retryable": error.retryable,
                },
            )
            raise error from error.cause
        finally:
            self._event_context.reset(event_token)
            self._request_context.reset(request_token)

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

        first_error: Exception | None = None
        native_close = getattr(self._native_runtime, "close", None)
        if callable(native_close):
            try:
                native_close()
            except Exception as exc:
                first_error = exc

        if self._tool_gateway is not None:
            try:
                self._tool_gateway.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc

        if first_error is not None:
            raise first_error
