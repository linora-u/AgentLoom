"""Runtime-neutral Tool discovery, execution, governance, and lifecycle."""

from __future__ import annotations

import inspect
import math
import os
import re
import time
import types
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import (
    Any,
    Literal,
    Protocol,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    runtime_checkable,
)

from agentloom.runtime.logging import get_logger
from agentloom.runtime.model_protocol import ToolDefinition
from agentloom.runtime.native_tools import ToolManifestEntry
from agentloom.runtime.tool_protocol import ToolCallRecord

logger = get_logger(__name__)

ToolForward = Callable[..., Any]
ToolSetup = Callable[[], None]
ToolInitialized = Callable[[], bool]
ToolEvidenceExtractor = Callable[[Any], Iterable[Mapping[str, Any]] | None]
ToolCloneFactory = Callable[[], Any]
ToolOutputNormalizer = Callable[[Any, str | None], Any]
ToolInputValidator = Callable[[dict[str, Any]], None]
ToolResourceCloser = Callable[[], None]


def final_answer_binding() -> ToolBinding:
    """Temporary compatibility import; terminal behavior belongs to smolagents."""
    from agentloom.adapters.smolagents.terminal import final_answer_binding as binding

    return binding()


@dataclass(frozen=True, slots=True)
class ToolBinding:
    """One immutable runtime-neutral Tool declaration and execution binding."""

    definition: ToolDefinition
    forward: ToolForward
    inputs_schema: Mapping[str, Any]
    output_type: str | None = None
    setup: ToolSetup | None = None
    initialized: ToolInitialized | None = None
    evidence_extractor: ToolEvidenceExtractor | None = None
    clone_factory: ToolCloneFactory | None = None
    output_normalizer: ToolOutputNormalizer | None = None
    compression_source: str | None = None
    manifest_entry: ToolManifestEntry | None = None
    input_validator: ToolInputValidator | None = None

    def __post_init__(self) -> None:
        properties = deepcopy(dict(self.inputs_schema))
        object.__setattr__(self, "inputs_schema", MappingProxyType(properties))
        if self.manifest_entry is not None and (
            self.manifest_entry.visible_name != self.definition.name
            or self.manifest_entry.parameters != self.definition.parameters
        ):
            raise ValueError("Tool manifest must match its visible definition")
        for name in (
            "forward",
            "setup",
            "initialized",
            "evidence_extractor",
            "clone_factory",
            "output_normalizer",
            "input_validator",
        ):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise TypeError(f"ToolBinding {name} must be callable or None")


class _ToolInputContractError(ValueError):
    """The effective Tool input does not satisfy its executable contract."""


@dataclass
class _EvidenceCarrier:
    extractor: ToolEvidenceExtractor

    def __post_init__(self) -> None:
        from agentloom.runtime.trusted_memory_evidence import (
            TRUSTED_MEMORY_EVIDENCE_ATTR,
        )

        setattr(self, TRUSTED_MEMORY_EVIDENCE_ATTR, self.extractor)


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    if annotation in {inspect.Signature.empty, Any}:
        return {"type": "any"}
    primitive = {
        str: "string",
        bool: "boolean",
        int: "integer",
        float: "number",
        dict: "object",
        list: "array",
        tuple: "array",
    }
    if annotation in primitive:
        return {"type": primitive[annotation]}

    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal:
        choices = list(args)
        value_types = {
            type(value)
            for value in choices
            if value is not None
        }
        schema = (
            _annotation_schema(next(iter(value_types)))
            if len(value_types) == 1
            else {"type": "any"}
        )
        schema["enum"] = choices
        if None in choices:
            schema["nullable"] = True
        return schema
    if origin in {Union, types.UnionType}:
        nullable = type(None) in args
        children = [
            _annotation_schema(item)
            for item in args
            if item is not type(None)
        ]
        if len(children) == 1:
            schema = children[0]
        else:
            schema = {"anyOf": children}
        if nullable:
            schema["nullable"] = True
        return schema
    if origin in {list, tuple, set, frozenset}:
        schema = {"type": "array"}
        if args:
            schema["items"] = _annotation_schema(args[0])
        return schema
    if origin in {dict, Mapping}:
        schema = {"type": "object"}
        if len(args) == 2:
            schema["additionalProperties"] = _annotation_schema(args[1])
        return schema
    return {"type": "any"}


_GOOGLE_DOCSTRING_SECTIONS = frozenset(
    {"Args:", "Returns:", "Raises:", "Examples:"}
)
_GOOGLE_DOCSTRING_ARG = re.compile(
    r"^(?P<name>[A-Za-z_]\w*)(?:\s*\([^)]*\))?\s*:\s*(?P<description>.*)$"
)


def _parse_google_docstring(
    forward: ToolForward,
) -> tuple[str, dict[str, str]]:
    """Return the callable summary and Google-style argument descriptions."""

    docstring = inspect.getdoc(forward) or ""
    lines = docstring.splitlines()
    first_section = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip() in _GOOGLE_DOCSTRING_SECTIONS
        ),
        len(lines),
    )
    description = "\n".join(lines[:first_section]).strip()

    try:
        args_start = next(
            index for index, line in enumerate(lines) if line.strip() == "Args:"
        )
    except StopIteration:
        return description, {}

    args_indent = len(lines[args_start]) - len(lines[args_start].lstrip())
    descriptions: dict[str, list[str]] = {}
    current_name: str | None = None
    parameter_indent: int | None = None
    for line in lines[args_start + 1 :]:
        stripped = line.strip()
        if stripped in _GOOGLE_DOCSTRING_SECTIONS:
            break
        if not stripped:
            continue

        indent = len(line) - len(line.lstrip())
        if indent <= args_indent:
            current_name = None
            continue

        match = _GOOGLE_DOCSTRING_ARG.match(stripped)
        if match and (
            parameter_indent is None or indent == parameter_indent
        ):
            parameter_indent = indent
            current_name = match.group("name")
            descriptions[current_name] = [match.group("description").strip()]
            continue
        if current_name is not None and (
            parameter_indent is None or indent > parameter_indent
        ):
            descriptions[current_name].append(stripped)

    return (
        description,
        {
            name: " ".join(
                " ".join(part for part in parts if part).split()
            )
            for name, parts in descriptions.items()
        },
    )


def _callable_schema(
    forward: ToolForward,
    *,
    parameter_descriptions: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    signature = _get_effective_signature(forward)
    try:
        resolved_hints = get_type_hints(forward)
    except (NameError, TypeError):
        resolved_hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, parameter in signature.parameters.items():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        schema = _annotation_schema(
            resolved_hints.get(name, parameter.annotation)
        )
        schema.setdefault(
            "description",
            (parameter_descriptions or {}).get(name, ""),
        )
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
            schema["required"] = True
        else:
            schema["required"] = False
            schema["nullable"] = schema.get("nullable", False)
        properties[name] = schema
    return properties, tuple(required)


def _normalize_input_schema(
    inputs_schema: Mapping[str, Any],
    *,
    required: Iterable[str],
) -> dict[str, Any]:
    required_names = set(required)
    normalized: dict[str, Any] = {}
    for name, raw in inputs_schema.items():
        schema = deepcopy(dict(raw)) if isinstance(raw, Mapping) else {}
        schema.setdefault("type", "any")
        schema.setdefault("description", "")
        if "required" not in schema:
            schema["required"] = (
                name in required_names
                if required_names
                else schema.get("nullable") is not True
            )
        normalized[str(name)] = schema
    return normalized


def _clone_tool_like_for_runtime(tool: Any) -> Any:
    """Create one run-owned Tool-like instance without importing its framework."""

    clone_factory = getattr(tool, "clone_for_runtime", None)
    if callable(clone_factory):
        cloned = clone_factory()
    else:
        try:
            cloned = deepcopy(tool)
        except Exception as exc:
            raise RuntimeError(
                f"Tool {getattr(tool, 'name', type(tool).__name__)!r} "
                "cannot be isolated; implement clone_for_runtime()"
            ) from exc

    if cloned is tool:
        raise RuntimeError(
            "clone_for_runtime() must return a distinct Tool-like instance"
        )
    if not callable(getattr(cloned, "forward", None)):
        raise RuntimeError(
            "clone_for_runtime() must return a Tool-like object with forward()"
        )

    return cloned


def bind_tool(
    tool: ToolBinding | Any,
    *,
    compression_source: str | None = None,
    output_normalizer: ToolOutputNormalizer | None = None,
) -> ToolBinding:
    """Snapshot a Tool-like object or plain callable into a neutral binding."""

    if isinstance(tool, ToolBinding):
        return tool

    catalog_spec = getattr(tool, "_agentloom_catalog_spec", None)

    declared_forward = getattr(tool, "forward", None)
    plain_callable = not callable(declared_forward) and callable(tool)
    if not plain_callable:
        if not callable(declared_forward):
            raise TypeError(
                "Tool binding requires a callable or an object with forward()"
            )
        tool = _clone_tool_like_for_runtime(tool)
        declared_forward = getattr(tool, "forward", None)
    forward = declared_forward
    if not callable(forward):
        forward = tool if callable(tool) else None
    if not callable(forward):
        raise TypeError("Tool binding requires a callable or an object with forward()")

    canonical = getattr(tool, "_agentloom_tool_definition", None)
    if isinstance(canonical, ToolDefinition):
        definition = canonical
        parameters = dict(definition.parameters)
        properties = parameters.get("properties")
        if not isinstance(properties, Mapping):
            raise ValueError(
                f"Tool {definition.name!r} parameters must contain object properties"
            )
        required = parameters.get("required")
        required_names = (
            tuple(str(name) for name in required)
            if isinstance(required, list)
            else ()
        )
        inputs_schema = _normalize_input_schema(
            properties,
            required=required_names,
        )
    else:
        name = getattr(tool, "name", None) or getattr(forward, "__name__", None)
        if not isinstance(name, str) or not name:
            raise ValueError("Tool binding requires a non-empty name")
        parameter_descriptions: Mapping[str, str] = {}
        if plain_callable:
            callable_description, parameter_descriptions = (
                _parse_google_docstring(forward)
            )
            description = callable_description
        else:
            description = (
                getattr(tool, "description", None)
                or inspect.getdoc(forward)
                or ""
            )
        raw_inputs = getattr(tool, "inputs", None)
        if isinstance(raw_inputs, Mapping):
            required_names = tuple(
                str(key)
                for key, value in raw_inputs.items()
                if not isinstance(value, Mapping)
                or value.get("nullable") is not True
            )
            inputs_schema = _normalize_input_schema(
                raw_inputs,
                required=required_names,
            )
        else:
            inputs_schema, required_names = _callable_schema(
                forward,
                parameter_descriptions=parameter_descriptions,
            )
        parameters = {
            "type": "object",
            "properties": {
                key: {
                    schema_key: deepcopy(schema_value)
                    for schema_key, schema_value in value.items()
                    if schema_key != "required"
                }
                for key, value in inputs_schema.items()
            },
            "required": list(required_names),
        }
        definition = ToolDefinition(
            name=name,
            description=str(description),
            parameters=parameters,
        )

    setup = getattr(tool, "setup", None)
    if not callable(setup):
        setup = None
    initialized = None
    if hasattr(tool, "is_initialized"):
        def tool_is_initialized(source: Any = tool) -> bool:
            return bool(getattr(source, "is_initialized", False))

        initialized = tool_is_initialized

    from agentloom.runtime.trusted_memory_evidence import (
        TRUSTED_MEMORY_EVIDENCE_ATTR,
    )

    evidence_extractor = getattr(tool, TRUSTED_MEMORY_EVIDENCE_ATTR, None)
    if not callable(evidence_extractor):
        evidence_extractor = getattr(forward, TRUSTED_MEMORY_EVIDENCE_ATTR, None)
    if not callable(evidence_extractor):
        evidence_extractor = None

    clone_factory = getattr(tool, "clone_for_runtime", None)
    if not callable(clone_factory):
        clone_factory = None
    selected_normalizer = output_normalizer
    if selected_normalizer is None:
        candidate = getattr(tool, "_agentloom_output_normalizer", None)
        if callable(candidate):
            selected_normalizer = candidate

    return ToolBinding(
        definition=definition,
        forward=forward,
        inputs_schema=inputs_schema,
        output_type=(
            str(tool.output_type)
            if getattr(tool, "output_type", None) is not None
            else None
        ),
        setup=setup,
        initialized=initialized,
        evidence_extractor=evidence_extractor,
        clone_factory=clone_factory,
        output_normalizer=selected_normalizer,
        compression_source=compression_source,
        manifest_entry=(
            ToolManifestEntry(
                logical_name=catalog_spec.logical_name or catalog_spec.name,
                visible_name=definition.name,
                owner=catalog_spec.owner,
                provider=catalog_spec.provider,
                capability=catalog_spec.capability,
                operation=catalog_spec.operation,
                parameters=definition.parameters,
                path_parameters=catalog_spec.path_params,
                command_parameter=catalog_spec.command_parameter,
                fixed_arguments=getattr(tool, "_agentloom_fixed_values", {}),
            ) if catalog_spec is not None else getattr(tool, "_agentloom_manifest_entry", None)
        ),
    )


def _get_effective_signature(forward_callable: ToolForward) -> inspect.Signature:
    signature = inspect.signature(forward_callable)
    parameters = list(signature.parameters.values())
    if parameters and parameters[0].name == "self":
        return signature.replace(parameters=parameters[1:])
    return signature


def _schema_type_names(schema: Mapping[str, Any]) -> tuple[str, ...]:
    raw = schema.get("type")
    names: tuple[str, ...]
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
    if expected in {"any", ""}:
        return True
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


def _validate_schema_value(
    value: Any,
    schema: Mapping[str, Any],
    path: str,
) -> None:
    expected = _schema_type_names(schema)
    if expected and not any(_matches_schema_type(value, name) for name in expected):
        rendered = " | ".join(expected)
        raise _ToolInputContractError(
            f"Tool input {path!r} must be {rendered}, got {type(value).__name__}"
        )

    choices = schema.get("enum")
    if isinstance(choices, list) and not any(
        type(value) is type(choice) and value == choice
        for choice in choices
    ):
        raise _ToolInputContractError(
            f"Tool input {path!r} must be one of {choices!r}"
        )

    item_schema = schema.get("items")
    if isinstance(value, list) and isinstance(item_schema, Mapping):
        for index, item in enumerate(value):
            _validate_schema_value(item, item_schema, f"{path}[{index}]")

    if not isinstance(value, dict):
        return

    properties = schema.get("properties", {})
    if isinstance(properties, Mapping):
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [name for name in required if name not in value]
            if missing:
                raise _ToolInputContractError(
                    f"Tool input {path!r} is missing required field(s): "
                    + ", ".join(sorted(missing))
                )
        for name, child in properties.items():
            if name in value and isinstance(child, Mapping):
                _validate_schema_value(value[name], child, f"{path}.{name}")

        additional = schema.get("additionalProperties", True)
        unknown = sorted(set(value) - set(properties))
        if additional is False and unknown:
            raise _ToolInputContractError(
                f"Tool input {path!r} contains undeclared field(s): "
                + ", ".join(unknown)
            )
        if isinstance(additional, Mapping):
            for name in unknown:
                _validate_schema_value(value[name], additional, f"{path}.{name}")
    elif isinstance(schema.get("additionalProperties"), Mapping):
        additional = schema["additionalProperties"]
        for name, child_value in value.items():
            _validate_schema_value(child_value, additional, f"{path}.{name}")


def _build_call_kwargs_from_input(
    forward_callable: ToolForward,
    tool_input: dict[str, Any],
) -> dict[str, Any]:
    signature = _get_effective_signature(forward_callable)
    parameters = signature.parameters
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    call_kwargs: dict[str, Any] = {}
    for key, value in tool_input.items():
        if key in parameters:
            kind = parameters[key].kind
            if kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.VAR_POSITIONAL,
            }:
                raise _ToolInputContractError(
                    f"Tool input {key!r} cannot be bound as a keyword argument"
                )
            call_kwargs[key] = value
        elif accepts_kwargs:
            call_kwargs[key] = value
        else:
            raise _ToolInputContractError(
                f"Tool input field {key!r} is not accepted by the callable"
            )
    try:
        signature.bind(**call_kwargs)
    except TypeError as exc:
        raise _ToolInputContractError(
            f"Tool input does not match callable signature: {exc}"
        ) from exc
    return call_kwargs


def _strict_decode_tool_input(
    binding: ToolBinding,
    tool_input: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    from agentloom.runtime.hooks.type_coercion import coerce_tool_parameters

    decoded = deepcopy(tool_input)
    schema = dict(binding.inputs_schema)
    coerce_tool_parameters(decoded, schema)
    if binding.input_validator is not None:
        # Protocol tools may carry references and open object schemas that
        # cannot be represented by the Python callable's property metadata.
        # Validate after Hooks but before guards, history, or side effects.
        binding.input_validator(decoded)
        return decoded, _build_call_kwargs_from_input(binding.forward, decoded)
    unknown = sorted(set(decoded) - set(schema))
    if unknown:
        raise _ToolInputContractError(
            "Tool input field(s) not declared by schema: "
            + ", ".join(unknown)
        )
    missing = [
        name
        for name, entry in schema.items()
        if name not in decoded
        and isinstance(entry, Mapping)
        and entry.get("required", entry.get("nullable") is not True) is True
    ]
    if missing:
        raise _ToolInputContractError(
            "Tool input is missing required field(s): "
            + ", ".join(sorted(missing))
        )
    for name, value in decoded.items():
        entry = schema.get(name)
        if isinstance(entry, Mapping):
            _validate_schema_value(value, entry, name)
    return decoded, _build_call_kwargs_from_input(binding.forward, decoded)


def _build_runtime_context(
    hook_run: Any,
    *,
    event: Any,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_call_id: str,
    tool_inputs_schema: Mapping[str, Any],
    tool_response: dict[str, Any] | None = None,
    cwd: str | None = None,
) -> Any:
    try:
        from agentloom.runtime.trace import capture_explicit_execution_context

        execution = capture_explicit_execution_context()
    except Exception:
        execution = None

    from agentloom.runtime.hooks.runtime import _runtime_workspace_fields
    from agentloom.runtime.hooks.types import HookContext

    runtime_agent_path = getattr(execution, "runtime_agent_path", None)
    return HookContext(
        local_run_id=hook_run.local_run_id,
        root_run_id=hook_run.root_run_id,
        cwd=cwd if cwd is not None else os.getcwd(),
        hook_event_name=event.value,
        tool_name=tool_name,
        tool_input=deepcopy(tool_input),
        tool_call_id=tool_call_id,
        tool_response=tool_response,
        tool_inputs_schema=deepcopy(dict(tool_inputs_schema)),
        step_number=hook_run.step_number,
        task_id=getattr(execution, "task_id", None),
        sub_task_id=getattr(execution, "sub_task_id", None),
        agent_name=getattr(execution, "agent_name", None),
        agent_config=deepcopy(hook_run.agent_config),
        runtime_agent_path=runtime_agent_path,
        project_root=hook_run.project_root,
        **_runtime_workspace_fields(runtime_agent_path),
    )


def _observe_final_tool_input(context: Any) -> None:
    from agentloom.self_learning.session_recorder import session_recorder_hook

    try:
        session_recorder_hook(context)
    except Exception as exc:
        logger.warning("Final tool-input observer failed: %s", exc)


def _dispatch_tool_failure(
    hook_run: Any,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_call_id: str,
    tool_inputs_schema: Mapping[str, Any],
    error: Exception,
) -> None:
    from agentloom.runtime.hooks.types import HookEvent

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
            tool_inputs_schema=deepcopy(dict(tool_inputs_schema)),
        )
        hook_run.flush_user_messages()
    except Exception as exc:
        logger.warning("PostToolUseFailure observer dispatch failed open: %s", exc)


def _compress_tool_result(
    *,
    tool_name: str,
    source: str,
    result: str,
) -> str:
    from agentloom.runtime.context_engine.runtime import get_active_context_engine

    engine = get_active_context_engine()
    if engine is None:
        return result
    return (
        engine.compress_tool_result(
            result,
            tool_name=tool_name,
            source=source,
        )
        or result
    )


@dataclass(frozen=True, slots=True, eq=False)
class PreparedToolCall:
    """An opaque, one-use gateway handle with a detached input snapshot.

    Only the issuing gateway can execute this handle. Reading ``arguments``
    cannot mutate the input that will reach its executor.
    """

    call_id: str
    tool_name: str
    _arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_arguments", deepcopy(dict(self._arguments)))

    @property
    def arguments(self) -> Mapping[str, Any]:
        return MappingProxyType(deepcopy(dict(self._arguments)))


@dataclass(frozen=True, slots=True)
class _PreparedToolExecution:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    call_kwargs: dict[str, Any]
    binding: ToolBinding
    hook_run: Any
    started_at: float


@runtime_checkable
class ToolGateway(Protocol):
    """The only Tool execution seam exposed to an Agent runtime adapter.

    ``definitions`` is an immutable snapshot of the Tools visible to the
    runtime.  ``invoke`` must preserve the caller-supplied, non-empty
    ``call_id`` in its terminal :class:`ToolCallRecord`; implementations do
    not generate or replace provider call IDs.  ``close`` releases any
    Tool-owned resources and must be safe to call more than once.
    ``prepare`` finalizes input without authorizing or executing the tool;
    ``execute_prepared`` consumes that gateway-owned handle exactly once.
    """

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]: ...

    def invoke(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> ToolCallRecord: ...

    def prepare(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> PreparedToolCall | ToolCallRecord: ...

    def execute_prepared(self, prepared: PreparedToolCall) -> ToolCallRecord: ...

    def close(self) -> None: ...


@runtime_checkable
class ToolManifestSource(Protocol):
    """Optional metadata extension; legacy execution gateways stay compatible."""

    @property
    def manifest(self) -> tuple[ToolManifestEntry, ...]: ...


def tool_manifest_snapshot(gateway: ToolGateway) -> tuple[ToolManifestEntry, ...]:
    """Resolve and validate the manifest consumed by every runtime definition."""
    definitions = gateway.definitions
    if isinstance(gateway, ToolManifestSource):
        entries = tuple(gateway.manifest)
    else:
        entries = tuple(
            ToolManifestEntry(
                logical_name=definition.name, visible_name=definition.name,
                owner="external", provider="python", capability=definition.name,
                operation="control", parameters=definition.parameters,
            ) for definition in definitions
        )
    by_name = {entry.visible_name: entry for entry in entries}
    if (
        len(by_name) != len(entries)
        or len(by_name) != len(definitions)
        or set(by_name) != {definition.name for definition in definitions}
    ):
        raise ValueError("Tool manifest must match selected definitions exactly")
    if any(by_name[definition.name].parameters != definition.parameters for definition in definitions):
        raise ValueError("Tool manifest schema must match selected definition")
    return tuple(by_name[definition.name] for definition in definitions)


def _blocked_tool_input(hook_run: Any, **kwargs: Any) -> ToolCallRecord:
    record = ToolCallRecord.blocked(
        input=kwargs.pop("arguments"), ended_at=time.time(),
        kind=kwargs.pop("kind", "invalid_arguments"), **kwargs,
    )
    hook_run.record_tool_outcome(record)
    return record


def _transform_tool_input(
    *, hook_run: Any, call_id: str, tool_name: str,
    arguments: Mapping[str, Any], inputs_schema: Mapping[str, Any],
    decode: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]],
    started_at: float, coerce: bool = True, cwd: str | None = None,
    manifest: ToolManifestEntry | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | ToolCallRecord:
    """Run configurable input repair, then strictly decode its final result."""
    from agentloom.runtime.hooks.types import HookEvent

    if not isinstance(arguments, Mapping):
        return _blocked_tool_input(
            hook_run,
            call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            message="Tool arguments must be an object mapping",
            stage="initial_decode",
            started_at=started_at,
        )

    from agentloom.runtime.hooks.type_coercion import coerce_tool_parameters

    try:
        tool_input = deepcopy(dict(arguments))
        if coerce:
            coerce_tool_parameters(tool_input, deepcopy(dict(inputs_schema)))
    except Exception as exc:
        return _blocked_tool_input(
            hook_run,
            call_id=call_id,
            tool_name=tool_name,
            arguments={},
            message=str(exc),
            stage="initial_decode",
            started_at=started_at,
        )

    schema_copy = deepcopy(dict(inputs_schema))
    try:
        pre_result = hook_run.dispatch(
            HookEvent.PRE_TOOL_USE,
            tool_name,
            tool_input,
            tool_call_id=call_id,
            tool_inputs_schema=schema_copy,
            **({"cwd": cwd} if cwd is not None else {}),
            **({"tool_aliases": (manifest.logical_name,)} if manifest is not None else {}),
        )
        hook_run.flush_user_messages()
    except Exception as exc:
        return _blocked_tool_input(
            hook_run,
            call_id=call_id,
            tool_name=tool_name,
            arguments=tool_input,
            message=f"PreToolUse failed closed: {exc}",
            stage="pre_tool_use",
            kind="policy_blocked",
            started_at=started_at,
        )
    candidate_input = (
        deepcopy(pre_result.modified_input)
        if isinstance(pre_result.modified_input, dict)
        else deepcopy(tool_input)
    )
    if pre_result.should_block():
        return _blocked_tool_input(
            hook_run,
            call_id=call_id,
            tool_name=tool_name,
            arguments=candidate_input,
            message=pre_result.get_blocked_response(),
            stage="pre_tool_use",
            kind="policy_blocked",
            started_at=started_at,
        )

    try:
        effective_input, call_kwargs = decode(candidate_input)
    except Exception as exc:
        return _blocked_tool_input(
            hook_run,
            call_id=call_id,
            tool_name=tool_name,
            arguments=candidate_input,
            message=str(exc),
            stage="final_decode",
            started_at=started_at,
        )

    return effective_input, call_kwargs


def _guard_tool_input(
    *, hook_run: Any, call_id: str, tool_name: str,
    effective_input: dict[str, Any], inputs_schema: Mapping[str, Any],
    started_at: float, cwd: str | None = None,
    manifest: ToolManifestEntry | None = None,
    protect: Callable[[dict[str, Any]], None] | None = None,
) -> ToolCallRecord | None:
    """Authorize final input and capture pre-execution history at dispatch."""
    from agentloom.runtime.hooks.types import HookEvent, HookResult

    final_context = _build_runtime_context(
        hook_run,
        event=HookEvent.PRE_TOOL_USE,
        tool_name=tool_name,
        tool_input=effective_input,
        tool_call_id=call_id,
        tool_inputs_schema=inputs_schema,
        cwd=cwd,
    )
    from agentloom.runtime.hooks.path_validators import enforce_core_tool_guard

    try:
        guard_result = enforce_core_tool_guard(
            final_context, **({"manifest": manifest} if manifest is not None else {})
        )
        if not isinstance(guard_result, HookResult):
            raise TypeError("CoreToolGuard returned an invalid result")
        if guard_result.decision not in {"allow", "block"}:
            raise ValueError("CoreToolGuard may only allow or block")
        if guard_result.modified_input is not None:
            raise ValueError("CoreToolGuard may not transform tool input")
    except Exception as exc:
        return _blocked_tool_input(
            hook_run,
            call_id=call_id,
            tool_name=tool_name,
            arguments=effective_input,
            message=f"Core tool guard failed closed: {exc}",
            stage="core_tool_guard",
            kind="policy_blocked",
            started_at=started_at,
        )
    if guard_result.should_block():
        return _blocked_tool_input(
            hook_run,
            call_id=call_id,
            tool_name=tool_name,
            arguments=effective_input,
            message=guard_result.get_blocked_response(),
            stage="core_tool_guard",
            kind="policy_blocked",
            started_at=started_at,
        )

    if protect is not None:
        try:
            protect(effective_input)
        except Exception as exc:
            return _blocked_tool_input(
                hook_run, call_id=call_id, tool_name=tool_name,
                arguments=effective_input, message=str(exc), stage="operation_policy",
                kind="policy_blocked", started_at=started_at,
            )

    from agentloom.runtime.checkpoint.file_history_hook import (
        record_active_file_history,
    )

    try:
        record_active_file_history(
            tool_name=tool_name,
            tool_input=effective_input,
            step_number=hook_run.step_number,
            **({"manifest": manifest, "cwd": cwd} if manifest is not None else {}),
        )
    except Exception as exc:
        return _blocked_tool_input(
            hook_run,
            call_id=call_id,
            tool_name=tool_name,
            arguments=effective_input,
            message=f"File history protection failed closed: {exc}",
            stage="file_history",
            kind="policy_blocked",
            started_at=started_at,
        )

    _observe_final_tool_input(final_context)

    return None


def _prepare_tool_input(
    *, hook_run: Any, call_id: str, tool_name: str,
    arguments: Mapping[str, Any], inputs_schema: Mapping[str, Any],
    decode: Callable[[dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]],
    started_at: float, coerce: bool = True, cwd: str | None = None,
    manifest: ToolManifestEntry | None = None,
    protect: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | ToolCallRecord:
    """Keep native preparation's transform, guard and history gate together."""
    prepared = _transform_tool_input(
        hook_run=hook_run, call_id=call_id, tool_name=tool_name,
        arguments=arguments, inputs_schema=inputs_schema, decode=decode,
        started_at=started_at, coerce=coerce, cwd=cwd, manifest=manifest,
    )
    if isinstance(prepared, ToolCallRecord):
        return prepared
    rejection = _guard_tool_input(
        hook_run=hook_run, call_id=call_id, tool_name=tool_name,
        effective_input=prepared[0], inputs_schema=inputs_schema,
        started_at=started_at, cwd=cwd, manifest=manifest, protect=protect,
    )
    return rejection if rejection is not None else prepared


class AgentLoomToolGateway:
    """Run-scoped Tool registry and AgentLoom-owned governance pipeline."""

    def __init__(
        self,
        bindings: Iterable[ToolBinding],
        *,
        resource_closers: Iterable[ToolResourceCloser] = (),
    ) -> None:
        by_name: dict[str, ToolBinding] = {}
        for binding in bindings:
            if not isinstance(binding, ToolBinding):
                raise TypeError("AgentLoomToolGateway bindings must be ToolBinding values")
            name = binding.definition.name
            if name in by_name:
                raise ValueError(f"Duplicate Tool definition: {name}")
            by_name[name] = binding
        closers = tuple(resource_closers)
        if any(not callable(closer) for closer in closers):
            raise TypeError("Tool resource closers must be callable")
        self._bindings: Mapping[str, ToolBinding] = MappingProxyType(by_name)
        self._definitions = tuple(
            binding.definition for binding in by_name.values()
        )
        self._resource_closers = closers
        self._close_lock = RLock()
        self._closed = False
        self._prepared: dict[PreparedToolCall, _PreparedToolExecution] = {}
        self._setup_locks = {
            name: RLock()
            for name in by_name
        }

    @classmethod
    def from_tools(
        cls,
        tools: Iterable[ToolBinding | Any],
        *,
        resource_closers: Iterable[ToolResourceCloser] = (),
        output_normalizer: ToolOutputNormalizer | None = None,
    ) -> AgentLoomToolGateway:
        """Bind existing Tool-like objects without importing their framework."""

        return cls(
            (
                bind_tool(
                    tool,
                    output_normalizer=output_normalizer,
                )
                for tool in tools
            ),
            resource_closers=resource_closers,
        )

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    @property
    def manifest(self) -> tuple[ToolManifestEntry, ...]:
        """Only selected tools, with explicit ownership and visible schemas."""
        return tuple(
            binding.manifest_entry or ToolManifestEntry(
                logical_name=binding.definition.name,
                visible_name=binding.definition.name,
                owner="external", provider="python",
                capability=binding.definition.name, operation="control",
                parameters=binding.definition.parameters,
            ) for binding in self._bindings.values()
        )

    def _record(
        self,
        hook_run: Any,
        record: ToolCallRecord,
    ) -> ToolCallRecord:
        hook_run.record_tool_outcome(record)
        return record

    def _blocked(
        self,
        hook_run: Any,
        *,
        call_id: str,
        tool_name: str,
        arguments: Any,
        message: str,
        stage: str,
        kind: str = "invalid_arguments",
        started_at: float,
    ) -> ToolCallRecord:
        return self._record(
            hook_run,
            ToolCallRecord.blocked(
                call_id=call_id,
                tool_name=tool_name,
                input=arguments,
                message=message,
                stage=stage,
                kind=kind,
                started_at=started_at,
                ended_at=time.time(),
            ),
        )

    def invoke(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> ToolCallRecord:
        """Settle one Tool call through the complete governance pipeline."""
        prepared = self.prepare(
            call_id=call_id, tool_name=tool_name, arguments=arguments,
        )
        if isinstance(prepared, ToolCallRecord):
            return prepared
        return self.execute_prepared(prepared)

    def prepare(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> PreparedToolCall | ToolCallRecord:
        """Finalize input once, without guard, history, setup or tool effects."""
        if not isinstance(call_id, str) or not call_id:
            raise ValueError("Tool call_id must be a non-empty string")
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("Tool name must be a non-empty string")
        if self._closed:
            raise RuntimeError("Tool Gateway is closed")

        from agentloom.runtime.trace import get_current_hook_run

        hook_run = get_current_hook_run(required=True)
        started_at = time.time()
        binding = self._bindings.get(tool_name)
        if binding is None:
            available = ", ".join(sorted(self._bindings))
            return self._blocked(
                hook_run,
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                message=(
                    f"Unknown tool {tool_name}, should be one of: {available}."
                ),
                stage="input_validation",
                started_at=started_at,
            )
        prepared = _transform_tool_input(
            hook_run=hook_run, call_id=call_id, tool_name=tool_name,
            arguments=arguments, inputs_schema=binding.inputs_schema,
            decode=lambda value: _strict_decode_tool_input(binding, value),
            started_at=started_at,
        )
        if isinstance(prepared, ToolCallRecord):
            return prepared
        effective_input, call_kwargs = prepared
        with self._close_lock:
            if self._closed:
                return self._blocked(
                    hook_run, call_id=call_id, tool_name=tool_name,
                    arguments=effective_input, message="Tool Gateway closed during preparation",
                    stage="cancellation", started_at=started_at,
                )
            handle = PreparedToolCall(call_id, tool_name, effective_input)
            self._prepared[handle] = _PreparedToolExecution(
                call_id, tool_name, effective_input, deepcopy(call_kwargs),
                binding, hook_run, started_at,
            )
        return handle

    def execute_prepared(self, prepared: PreparedToolCall) -> ToolCallRecord:
        """Consume an owned handle in its original Hook Run before dispatch."""
        from agentloom.runtime.hooks.types import HookEvent
        from agentloom.runtime.trace import get_current_hook_run

        with self._close_lock:
            if self._closed:
                raise RuntimeError("Tool Gateway is closed")
            if not isinstance(prepared, PreparedToolCall) or prepared not in self._prepared:
                raise ValueError("Prepared Tool call is foreign or already consumed")
            execution = self._prepared[prepared]
            if get_current_hook_run(required=True) is not execution.hook_run:
                raise ValueError("Prepared Tool call belongs to a different HookRun")
            del self._prepared[prepared]

        call_id, tool_name = execution.call_id, execution.tool_name
        effective_input, call_kwargs = execution.arguments, execution.call_kwargs
        binding, hook_run = execution.binding, execution.hook_run
        started_at = execution.started_at
        rejection = _guard_tool_input(
            hook_run=hook_run, call_id=call_id, tool_name=tool_name,
            effective_input=effective_input, inputs_schema=binding.inputs_schema,
            started_at=started_at,
        )
        if rejection is not None:
            return rejection
        if self._closed:
            return self._blocked(
                hook_run, call_id=call_id, tool_name=tool_name,
                arguments=effective_input, message="Tool Gateway closed before execution",
                stage="cancellation", started_at=started_at,
            )

        try:
            if binding.setup is not None and (
                binding.initialized is None or not binding.initialized()
            ):
                with self._setup_locks[tool_name]:
                    if binding.initialized is None or not binding.initialized():
                        binding.setup()
            if self._closed:
                from agentloom.runtime.tool_protocol import ToolPolicyBlockedError
                raise ToolPolicyBlockedError("Tool Gateway closed during setup")
            raw_result = binding.forward(**call_kwargs)
        except Exception as tool_error:
            failed = ToolCallRecord.from_exception(
                call_id=call_id,
                tool_name=tool_name,
                input=effective_input,
                error=tool_error,
                stage="tool_execution",
                started_at=started_at,
                ended_at=time.time(),
            )
            self._record(hook_run, failed)
            if failed.status == "error":
                _dispatch_tool_failure(
                    hook_run,
                    tool_name=tool_name,
                    tool_input=effective_input,
                    tool_call_id=call_id,
                    tool_inputs_schema=binding.inputs_schema,
                    error=tool_error,
                )
            return failed

        trusted_evidence: tuple[dict[str, str], ...] = ()
        if binding.evidence_extractor is not None:
            from agentloom.runtime.trusted_memory_evidence import (
                extract_trusted_memory_evidence,
            )

            try:
                trusted_evidence = extract_trusted_memory_evidence(
                    _EvidenceCarrier(binding.evidence_extractor),
                    raw_result,
                )
            except Exception:
                trusted_evidence = ()

        if binding.output_normalizer is not None:
            try:
                raw_result = binding.output_normalizer(
                    raw_result,
                    binding.output_type,
                )
            except Exception as output_error:
                failed = ToolCallRecord.failed(
                    call_id=call_id,
                    tool_name=tool_name,
                    input=effective_input,
                    error=output_error,
                    stage="output_validation",
                    started_at=started_at,
                    ended_at=time.time(),
                )
                self._record(hook_run, failed)
                _dispatch_tool_failure(
                    hook_run,
                    tool_name=tool_name,
                    tool_input=effective_input,
                    tool_call_id=call_id,
                    tool_inputs_schema=binding.inputs_schema,
                    error=output_error,
                )
                return failed

        result = raw_result
        if result is None or (isinstance(result, str) and not result.strip()):
            result = f"({tool_name} completed with no output)"
        if isinstance(result, str):
            try:
                result = _compress_tool_result(
                    tool_name=tool_name,
                    source=(
                        binding.compression_source
                        or f"tool_result:{tool_name}"
                    ),
                    result=result,
                )
            except Exception as processing_error:
                logger.warning(
                    "Context compression failed open for tool %s; "
                    "returning the raw result: %s",
                    tool_name,
                    processing_error,
                )

        from agentloom.runtime.trusted_memory_evidence import (
            TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY,
            TrustedMemoryEvidenceEnvelope,
        )

        tool_response: dict[str, Any] = {"result": result}
        if trusted_evidence:
            tool_response[TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY] = (
                TrustedMemoryEvidenceEnvelope(trusted_evidence)
            )
        completed = ToolCallRecord.completed(
            call_id=call_id,
            tool_name=tool_name,
            input=effective_input,
            output=result,
            started_at=started_at,
            ended_at=time.time(),
        )
        self._record(hook_run, completed)
        try:
            hook_run.dispatch(
                HookEvent.POST_TOOL_USE,
                tool_name,
                effective_input,
                tool_call_id=call_id,
                tool_response=tool_response,
                tool_inputs_schema=deepcopy(dict(binding.inputs_schema)),
            )
            hook_run.flush_user_messages()
        except Exception as exc:
            logger.warning("PostToolUse observer dispatch failed open: %s", exc)
        return completed

    def close(self) -> None:
        """Release run-owned Tool resources once, even after close failures."""

        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._prepared.clear()
        first_error: Exception | None = None
        for closer in reversed(self._resource_closers):
            try:
                closer()
            except Exception as exc:
                logger.warning("Tool resource close failed: %s", exc)
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
