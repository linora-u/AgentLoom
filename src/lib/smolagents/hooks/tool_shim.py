import inspect
import json
from functools import wraps
from typing import Any

from smolagents.tools import Tool
from src.trace.task_context import get_current_hook_manager
from src.lib.logging import get_logger
from .hook_manager import HookManager
from .type_coercion import coerce_tool_parameters
from .types import HookEvent

logger = get_logger(__name__)
HOOKS_INJECTED_ATTR = "_hooks_injected"


def _try_context_engine_compress(tool_name: str, result: str) -> str | None:
    """Compress a tool result through the active task-scoped ContextEngine."""
    from src.lib.context_engine.runtime import get_active_context_engine

    engine = get_active_context_engine()
    if engine is None:
        return None
    return engine.compress_tool_result(
        result,
        tool_name=tool_name,
        source=f"tool_result:{tool_name}",
    )


def _resolve_hook_manager() -> HookManager:
    current_manager = get_current_hook_manager()
    if current_manager is not None:
        return current_manager
    return HookManager.get_instance()


def _get_effective_signature(forward_callable):
    """
    Return a signature suitable for runtime argument binding.

    Some dynamically generated tools expose a synthetic `self` parameter in
    `__signature__` even though their runtime callable is effectively static.
    Remove that synthetic leading `self` for binding.
    """
    signature = inspect.signature(forward_callable)
    parameters = list(signature.parameters.values())
    if parameters and parameters[0].name == "self":
        signature = signature.replace(parameters=parameters[1:])
    return signature


def _build_tool_input(forward_callable, args, kwargs) -> dict[str, Any]:
    """Build a normalized hook input payload from mixed args/kwargs."""
    tool_input: dict[str, Any] = dict(kwargs)
    try:
        signature = _get_effective_signature(forward_callable)
        bound = signature.bind_partial(*args, **kwargs)
        tool_input.update(bound.arguments)
    except Exception:
        if args:
            tool_input["_args"] = args

    query_value = tool_input.get("query")
    if isinstance(query_value, str):
        try:
            query_data = json.loads(query_value)
            if isinstance(query_data, dict):
                merged_input = dict(tool_input)
                merged_input.pop("query", None)
                merged_input.update(query_data)
                tool_input = merged_input
        except (json.JSONDecodeError, ValueError):
            pass

    return tool_input


def _build_call_kwargs_from_input(forward_callable, tool_input: dict[str, Any]) -> dict[str, Any]:
    """
    Convert normalized input dict back to callable kwargs.

    Raises a binding exception if modified input cannot be applied.
    """
    signature = _get_effective_signature(forward_callable)
    parameters = signature.parameters
    accepts_var_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())

    call_kwargs: dict[str, Any] = {}
    for key, value in tool_input.items():
        if key in parameters:
            param = parameters[key]
            if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL):
                continue
            call_kwargs[key] = value
        elif accepts_var_kwargs:
            call_kwargs[key] = value

    signature.bind_partial(**call_kwargs)
    return call_kwargs


def _log_failed_hook_result(event_name: str, tool_name: str, result: Any) -> None:
    telemetry = getattr(result, "telemetry", {}) or {}
    details: list[str] = []
    skill_name = telemetry.get("skill_name")
    invalid_contract = telemetry.get("invalid_contract")
    hook_event_name = telemetry.get("hook_event_name") or event_name

    if skill_name:
        details.append(f"skill={skill_name}")
    if invalid_contract:
        details.append(f"invalid_contract={invalid_contract}")

    details_text = f" ({', '.join(details)})" if details else ""
    logger.warning(
        "Ignoring failed %s hook for tool %s%s: %s",
        hook_event_name,
        tool_name,
        details_text,
        getattr(result, "reason", None) or "hook returned success=False",
    )


def inject_hooks(tool_instance: Tool) -> Tool:
    """
    Inject hook execution logic into a Tool instance.
    Modifies the tool.forward method to trigger ToolStart and ToolEnd hooks.
    
    Args:
        tool_instance: The Tool instance to modify.
        
    Returns:
        The modified Tool instance.
    """
    if bool(getattr(tool_instance, HOOKS_INJECTED_ATTR, False)):
        return tool_instance

    if not hasattr(tool_instance, 'forward'):
        return tool_instance

    original_forward = tool_instance.forward
    tool_name = tool_instance.name
    tool_inputs_schema = getattr(tool_instance, 'inputs', None)

    @wraps(original_forward)
    def wrapped_forward(*args, **kwargs):
        try:
            hook_manager = _resolve_hook_manager()
        except Exception as hook_manager_error:
            logger.warning("Hook manager unavailable for tool %s: %s", tool_name, hook_manager_error)
            return original_forward(*args, **kwargs)

        tool_input = _build_tool_input(original_forward, args, kwargs)

        # --- Type coercion: convert LLM string values to declared types ---
        if tool_inputs_schema:
            coerce_tool_parameters(tool_input, tool_inputs_schema)

        call_args = args
        call_kwargs = kwargs
        effective_tool_input = tool_input

        try:
            pre_result = hook_manager.trigger_hooks(
                HookEvent.PRE_TOOL_USE,
                tool_name,
                tool_input,
                tool_response=None,
                tool_inputs_schema=tool_inputs_schema,
            )
            hook_manager.flush_user_messages()

            if pre_result.should_block():
                return pre_result.get_blocked_response()
            if not pre_result.success:
                _log_failed_hook_result(HookEvent.PRE_TOOL_USE.value, tool_name, pre_result)
            elif pre_result.decision == "modify" and isinstance(pre_result.modified_input, dict):
                candidate_input = dict(tool_input)
                candidate_input.update(pre_result.modified_input)
                try:
                    call_kwargs = _build_call_kwargs_from_input(original_forward, candidate_input)
                    call_args = ()
                    effective_tool_input = candidate_input
                except Exception as bind_error:
                    logger.warning(
                        "Failed to apply modified_input for tool %s: %s. Falling back to original call arguments.",
                        tool_name,
                        bind_error,
                    )

        except Exception as pre_hook_error:
            logger.warning("Pre-execution hook error for tool %s: %s", tool_name, pre_hook_error)

        try:
            result = original_forward(*call_args, **call_kwargs)

            # Empty-result protection: empty tool_result content can trigger
            # LLM stop sequences causing the model to end its turn with zero
            # output.  Inject a short marker so it always has something to
            # react to.
            if result is None or (isinstance(result, str) and not result.strip()):
                result = f"({tool_name} completed with no output)"

            # Reversible CCR stores original tool output in the task-scoped
            # ContextStore and returns a visible ContextRef preview.
            if isinstance(result, str):
                compressed_result = _try_context_engine_compress(tool_name, result)
                if compressed_result is not None:
                    result = compressed_result

            try:
                post_result = hook_manager.trigger_hooks(
                    HookEvent.POST_TOOL_USE,
                    tool_name,
                    effective_tool_input,
                    tool_response={"result": result} if result is not None else {},
                    tool_inputs_schema=tool_inputs_schema,
                )
                hook_manager.flush_user_messages()
                if post_result.should_block():
                    return post_result.get_blocked_response()
                if not post_result.success:
                    _log_failed_hook_result(HookEvent.POST_TOOL_USE.value, tool_name, post_result)
                    return result
                return post_result.merge_with_tool_result(result)

            except Exception as post_hook_error:
                logger.warning("Post-execution hook error for tool %s: %s", tool_name, post_hook_error)
                return result

        except Exception as tool_error:
            try:
                hook_manager.trigger_hooks(
                    HookEvent.POST_TOOL_USE_FAILURE,
                    tool_name,
                    effective_tool_input,
                    tool_response={"error": str(tool_error), "error_type": type(tool_error).__name__},
                    tool_inputs_schema=tool_inputs_schema,
                )
                hook_manager.flush_user_messages()
            except Exception as post_error_hook_error:
                logger.warning("Post-error hook error for tool %s: %s", tool_name, post_error_hook_error)

            raise

    tool_instance.forward = wrapped_forward
    setattr(tool_instance, HOOKS_INJECTED_ATTR, True)
    return tool_instance
