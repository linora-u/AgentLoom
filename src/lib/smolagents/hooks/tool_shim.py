"""Tool adapter for the active run-scoped Hook Runtime."""

from __future__ import annotations

import inspect
import math
import os
import uuid
from copy import deepcopy
from functools import wraps
from typing import Any

from smolagents.tools import Tool
from src.lib.checkpoint.file_history_hook import record_active_file_history
from src.lib.logging import get_logger
from src.lib.trusted_memory_evidence import (
    TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY,
    TrustedMemoryEvidenceEnvelope,
    extract_trusted_memory_evidence,
)
from src.trace import get_current_hook_run

from .path_validators import enforce_core_tool_guard
from .type_coercion import coerce_tool_parameters
from .types import (
    Blocked,
    Executed,
    Failed,
    HookContext,
    HookEvent,
    HookResult,
    ToolExecutionOutcome,
)

HOOKS_INJECTED_ATTR = "_hooks_injected"
ORIGINAL_FORWARD_ATTR = "_agentloom_original_forward"
logger = get_logger(__name__)


class _ToolInputContractError(ValueError):
    """The effective tool input does not satisfy the executable contract."""


def _trusted_evidence_payload(tool_instance: Tool, raw_result: Any) -> list[dict[str, str]]:
    try:
        return list(extract_trusted_memory_evidence(tool_instance, raw_result))
    except Exception:
        return []


def clone_tool_for_runtime(tool_instance: Tool) -> Tool:
    """Create an isolated runtime tool through a factory or safe deep clone.

    Stateful tools that cannot be deep-copied must implement
    ``clone_for_runtime() -> Tool``. Silently sharing their mutable fields
    would leak state between cached/concurrent Agent runs.
    """

    factory = getattr(tool_instance, "clone_for_runtime", None)
    if callable(factory):
        cloned = factory()
    else:
        try:
            cloned = deepcopy(tool_instance)
        except Exception as exc:
            raise RuntimeError(
                f"Tool {getattr(tool_instance, 'name', type(tool_instance).__name__)!r} cannot be isolated; "
                "implement clone_for_runtime()"
            ) from exc
    if not isinstance(cloned, Tool) or cloned is tool_instance:
        raise RuntimeError("clone_for_runtime() must return a distinct Tool instance")
    original_forward = getattr(tool_instance, ORIGINAL_FORWARD_ATTR, None)
    if original_forward is not None:
        if inspect.ismethod(original_forward) and original_forward.__self__ is tool_instance:
            original_forward = original_forward.__func__.__get__(cloned, type(cloned))
        cloned.forward = original_forward
    for attribute in (HOOKS_INJECTED_ATTR, ORIGINAL_FORWARD_ATTR):
        if attribute in getattr(cloned, "__dict__", {}):
            delattr(cloned, attribute)
    return cloned


def _try_context_engine_compress(tool_name: str, result: str) -> str | None:
    from src.lib.context_engine.runtime import get_active_context_engine

    engine = get_active_context_engine()
    if engine is None:
        return None
    return engine.compress_tool_result(
        result,
        tool_name=tool_name,
        source=f"tool_result:{tool_name}",
    )


def _get_effective_signature(forward_callable):
    signature = inspect.signature(forward_callable)
    parameters = list(signature.parameters.values())
    if parameters and parameters[0].name == "self":
        return signature.replace(parameters=parameters[1:])
    return signature


def _build_tool_input(forward_callable, args, kwargs) -> dict[str, Any]:
    """Decode Python call syntax into the canonical named input mapping."""

    signature = _get_effective_signature(forward_callable)
    try:
        bound = signature.bind_partial(*args, **kwargs)
    except TypeError as exc:
        raise _ToolInputContractError(f"Invalid tool call syntax: {exc}") from exc

    tool_input: dict[str, Any] = {}
    for name, value in bound.arguments.items():
        parameter = signature.parameters[name]
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            if not isinstance(value, dict):
                raise _ToolInputContractError(f"Invalid variadic keyword input for {name!r}")
            tool_input.update(value)
            continue
        if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
            if value:
                raise _ToolInputContractError("Variadic positional tool arguments are not supported")
            continue
        tool_input[name] = value
    return tool_input


def _schema_type_names(schema: dict[str, Any]) -> tuple[str, ...]:
    raw = schema.get("type")
    if isinstance(raw, str):
        names = (raw,)
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        names = tuple(raw)
    else:
        names = ()
    if schema.get("nullable") is True and "null" not in names:
        names = (*names, "null")
    return names


def _matches_schema_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        if isinstance(value, bool):
            return False
        if isinstance(value, int):
            return True
        return isinstance(value, float) and math.isfinite(value)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _validate_schema_value(value: Any, schema: dict[str, Any], path: str) -> None:
    expected = _schema_type_names(schema)
    if expected and not any(_matches_schema_type(value, name) for name in expected):
        rendered = " | ".join(expected)
        raise _ToolInputContractError(f"Tool input {path!r} must be {rendered}, got {type(value).__name__}")

    if "enum" in schema:
        choices = schema.get("enum")
        if isinstance(choices, list) and not any(type(value) is type(choice) and value == choice for choice in choices):
            raise _ToolInputContractError(f"Tool input {path!r} must be one of {choices!r}")

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        item_schema = schema["items"]
        for index, item in enumerate(value):
            _validate_schema_value(item, item_schema, f"{path}[{index}]")

    if not isinstance(value, dict):
        return

    properties = schema.get("properties")
    if isinstance(properties, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [name for name in required if name not in value]
            if missing:
                raise _ToolInputContractError(
                    f"Tool input {path!r} is missing required field(s): " + ", ".join(sorted(missing))
                )
        for name, child in properties.items():
            if name in value and isinstance(child, dict):
                _validate_schema_value(value[name], child, f"{path}.{name}")

        additional = schema.get("additionalProperties", True)
        unknown = sorted(set(value) - set(properties))
        if additional is False and unknown:
            raise _ToolInputContractError(f"Tool input {path!r} contains undeclared field(s): " + ", ".join(unknown))
        if isinstance(additional, dict):
            for name in unknown:
                _validate_schema_value(value[name], additional, f"{path}.{name}")
    elif isinstance(schema.get("additionalProperties"), dict):
        additional = schema["additionalProperties"]
        for name, child_value in value.items():
            _validate_schema_value(child_value, additional, f"{path}.{name}")


def _strict_decode_tool_input(
    forward_callable,
    tool_input: dict[str, Any],
    tool_inputs_schema: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Coerce once more, reject schema drift, and bind the executable call."""

    decoded = dict(tool_input)
    if isinstance(tool_inputs_schema, dict):
        coerce_tool_parameters(decoded, tool_inputs_schema)
        unknown = sorted(set(decoded) - set(tool_inputs_schema))
        if unknown:
            raise _ToolInputContractError("Tool input field(s) not declared by schema: " + ", ".join(unknown))
        missing = [
            name
            for name, schema in tool_inputs_schema.items()
            if name not in decoded
            and isinstance(schema, dict)
            and schema.get("required", schema.get("nullable") is not True) is True
        ]
        if missing:
            raise _ToolInputContractError("Tool input is missing required field(s): " + ", ".join(sorted(missing)))
        for name, value in decoded.items():
            schema = tool_inputs_schema.get(name)
            if isinstance(schema, dict):
                _validate_schema_value(value, schema, name)

    call_kwargs = _build_call_kwargs_from_input(forward_callable, decoded)
    return decoded, call_kwargs


def _build_call_kwargs_from_input(
    forward_callable,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    signature = _get_effective_signature(forward_callable)
    parameters = signature.parameters
    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    call_kwargs: dict[str, Any] = {}
    for key, value in tool_input.items():
        if key in parameters:
            kind = parameters[key].kind
            if kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.VAR_POSITIONAL,
            ):
                raise _ToolInputContractError(f"Tool input {key!r} cannot be bound as a keyword argument")
            call_kwargs[key] = value
        elif accepts_kwargs:
            call_kwargs[key] = value
        else:
            raise _ToolInputContractError(f"Tool input field {key!r} is not accepted by the callable")
    try:
        signature.bind(**call_kwargs)
    except TypeError as exc:
        raise _ToolInputContractError(f"Tool input does not match callable signature: {exc}") from exc
    return call_kwargs


def _build_runtime_context(
    hook_run,
    *,
    event: HookEvent,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_call_id: str,
    tool_inputs_schema: dict[str, Any] | None,
    tool_response: dict[str, Any] | None = None,
) -> HookContext:
    try:
        from src.trace import capture_explicit_execution_context

        execution = capture_explicit_execution_context()
    except Exception:
        execution = None

    runtime_agent_path = getattr(execution, "runtime_agent_path", None)
    from .runtime import _runtime_workspace_fields

    return HookContext(
        local_run_id=hook_run.local_run_id,
        root_run_id=hook_run.root_run_id,
        cwd=os.getcwd(),
        hook_event_name=event.value,
        tool_name=tool_name,
        tool_input=dict(tool_input),
        tool_call_id=tool_call_id,
        tool_response=tool_response,
        tool_inputs_schema=tool_inputs_schema,
        step_number=hook_run.step_number,
        task_id=getattr(execution, "task_id", None),
        sub_task_id=getattr(execution, "sub_task_id", None),
        agent_name=getattr(execution, "agent_name", None),
        agent_config=deepcopy(hook_run.agent_config),
        runtime_agent_path=runtime_agent_path,
        project_root=hook_run.project_root,
        **_runtime_workspace_fields(runtime_agent_path),
    )


def _observe_final_tool_input(context: HookContext) -> None:
    """Send final input to self-learning as a fail-open observer exactly once."""

    from src.extensions.self_learning.session_recorder import session_recorder_hook

    try:
        session_recorder_hook(context)
    except Exception as exc:
        logger.warning("Final tool-input observer failed: %s", exc)


def _dispatch_tool_failure(
    hook_run,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_call_id: str,
    tool_inputs_schema: dict[str, Any] | None,
    error: Exception,
) -> None:
    try:
        hook_run.dispatch(
            HookEvent.POST_TOOL_USE_FAILURE,
            tool_name,
            tool_input,
            tool_call_id=tool_call_id,
            tool_response={
                "error": str(error),
                "error_type": type(error).__name__,
            },
            tool_inputs_schema=tool_inputs_schema,
        )
        hook_run.flush_user_messages()
    except Exception as exc:
        logger.warning("PostToolUseFailure observer dispatch failed open: %s", exc)


def _record_tool_outcome(hook_run, outcome: ToolExecutionOutcome) -> ToolExecutionOutcome:
    """Persist the typed state before any configurable observer can stall."""

    hook_run.record_tool_outcome(outcome)
    return outcome


def _execute_tool_pipeline(
    tool_instance: Tool,
    original_forward,
    hook_run,
    *,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> ToolExecutionOutcome:
    """Execute one invocation through the complete immutable safety boundary."""

    tool_name = tool_instance.name
    tool_call_id = uuid.uuid4().hex
    tool_inputs_schema = getattr(tool_instance, "inputs", None)
    if not isinstance(tool_inputs_schema, dict):
        tool_inputs_schema = None

    try:
        tool_input = _build_tool_input(original_forward, args, kwargs)
        if tool_inputs_schema is not None:
            coerce_tool_parameters(tool_input, tool_inputs_schema)
    except Exception as exc:
        return _record_tool_outcome(
            hook_run,
            Blocked({}, str(exc), "initial_decode", tool_name=tool_name),
        )

    try:
        pre_result = hook_run.dispatch(
            HookEvent.PRE_TOOL_USE,
            tool_name,
            tool_input,
            tool_call_id=tool_call_id,
            tool_inputs_schema=tool_inputs_schema,
        )
        hook_run.flush_user_messages()
    except Exception as exc:
        return _record_tool_outcome(
            hook_run,
            Blocked(
                tool_input,
                f"PreToolUse failed closed: {exc}",
                "pre_tool_use",
                tool_name=tool_name,
            ),
        )
    candidate_input = (
        dict(pre_result.modified_input) if isinstance(pre_result.modified_input, dict) else dict(tool_input)
    )
    if pre_result.should_block():
        return _record_tool_outcome(
            hook_run,
            Blocked(
                candidate_input,
                pre_result.get_blocked_response(),
                "pre_tool_use",
                tool_name=tool_name,
            ),
        )

    try:
        effective_input, call_kwargs = _strict_decode_tool_input(
            original_forward,
            candidate_input,
            tool_inputs_schema,
        )
    except Exception as exc:
        return _record_tool_outcome(
            hook_run,
            Blocked(candidate_input, str(exc), "final_decode", tool_name=tool_name),
        )

    final_context = _build_runtime_context(
        hook_run,
        event=HookEvent.PRE_TOOL_USE,
        tool_name=tool_name,
        tool_input=effective_input,
        tool_call_id=tool_call_id,
        tool_inputs_schema=tool_inputs_schema,
    )
    try:
        guard_result = enforce_core_tool_guard(final_context)
        if not isinstance(guard_result, HookResult):
            raise TypeError("CoreToolGuard returned an invalid result")
        if guard_result.decision not in {"allow", "block"}:
            raise ValueError("CoreToolGuard may only allow or block")
        if guard_result.modified_input is not None:
            raise ValueError("CoreToolGuard may not transform tool input")
    except Exception as exc:
        return _record_tool_outcome(
            hook_run,
            Blocked(
                effective_input,
                f"Core tool guard failed closed: {exc}",
                "core_tool_guard",
                tool_name=tool_name,
            ),
        )
    if guard_result.should_block():
        return _record_tool_outcome(
            hook_run,
            Blocked(
                effective_input,
                guard_result.get_blocked_response(),
                "core_tool_guard",
                tool_name=tool_name,
            ),
        )

    try:
        record_active_file_history(
            tool_name=tool_name,
            tool_input=effective_input,
            step_number=hook_run.step_number,
        )
    except Exception as exc:
        return _record_tool_outcome(
            hook_run,
            Blocked(
                effective_input,
                f"File history protection failed closed: {exc}",
                "file_history",
                tool_name=tool_name,
            ),
        )

    _observe_final_tool_input(final_context)

    try:
        raw_result = original_forward(**call_kwargs)
    except Exception as tool_error:
        outcome = Failed(effective_input, tool_error, "tool_execution", tool_name=tool_name)
        _record_tool_outcome(hook_run, outcome)
        _dispatch_tool_failure(
            hook_run,
            tool_name=tool_name,
            tool_input=effective_input,
            tool_call_id=tool_call_id,
            tool_inputs_schema=tool_inputs_schema,
            error=tool_error,
        )
        return outcome

    try:
        trusted_evidence = _trusted_evidence_payload(tool_instance, raw_result)
        result = raw_result
        if result is None or (isinstance(result, str) and not result.strip()):
            result = f"({tool_name} completed with no output)"
        if isinstance(result, str):
            compressed = _try_context_engine_compress(tool_name, result)
            if compressed is not None:
                result = compressed
    except Exception as processing_error:
        outcome = Failed(effective_input, processing_error, "result_processing", tool_name=tool_name)
        _record_tool_outcome(hook_run, outcome)
        _dispatch_tool_failure(
            hook_run,
            tool_name=tool_name,
            tool_input=effective_input,
            tool_call_id=tool_call_id,
            tool_inputs_schema=tool_inputs_schema,
            error=processing_error,
        )
        return outcome

    tool_response: dict[str, Any] = {"result": result}
    if trusted_evidence:
        tool_response[TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY] = TrustedMemoryEvidenceEnvelope(trusted_evidence)
    outcome = Executed(effective_input, result, tool_name=tool_name)
    _record_tool_outcome(hook_run, outcome)
    try:
        hook_run.dispatch(
            HookEvent.POST_TOOL_USE,
            tool_name,
            effective_input,
            tool_call_id=tool_call_id,
            tool_response=tool_response,
            tool_inputs_schema=tool_inputs_schema,
        )
        hook_run.flush_user_messages()
    except Exception as exc:
        logger.warning("PostToolUse observer dispatch failed open: %s", exc)
    return outcome


def inject_hooks(tool_instance: Tool) -> Tool:
    """Wrap a tool so every call requires and uses the active Hook Run."""

    if bool(getattr(tool_instance, HOOKS_INJECTED_ATTR, False)):
        return tool_instance
    if not hasattr(tool_instance, "forward"):
        return tool_instance

    original_forward = tool_instance.forward
    setattr(tool_instance, ORIGINAL_FORWARD_ATTR, original_forward)

    @wraps(original_forward)
    def wrapped_forward(*args, **kwargs):
        hook_run = get_current_hook_run(required=True)
        outcome = _execute_tool_pipeline(
            tool_instance,
            original_forward,
            hook_run,
            args=args,
            kwargs=dict(kwargs),
        )
        if isinstance(outcome, Blocked):
            return outcome.model_response()
        if isinstance(outcome, Failed):
            raise outcome.error
        return outcome.value

    tool_instance.forward = wrapped_forward
    setattr(tool_instance, HOOKS_INJECTED_ATTR, True)
    return tool_instance
