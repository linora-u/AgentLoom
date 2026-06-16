"""Schema-bound tool argument coercion for AgentLoom tool execution."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from src.lib.logging import get_logger
from src.lib.smolagents.models.tool_call_parser import parse_json_with_repair

_LOG = get_logger(__name__)

_RE_INTEGER = re.compile(r"^-?(?:0|[1-9]\d*)$")
_RE_NUMBER = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def _expected_types(schema: dict[str, Any]) -> tuple[str, ...]:
    expected = schema.get("type")
    if isinstance(expected, str):
        return (expected,)
    if isinstance(expected, list):
        return tuple(item for item in expected if isinstance(item, str))
    return ()


def _coerce_scalar(value: Any, expected_type: str) -> Any:
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    if expected_type == "integer" and _RE_INTEGER.fullmatch(stripped):
        try:
            return int(stripped)
        except (ValueError, OverflowError):
            return value

    if expected_type == "number" and _RE_NUMBER.fullmatch(stripped):
        try:
            number = float(stripped)
        except (ValueError, OverflowError):
            return value
        if math.isfinite(number):
            return number
        return value

    if expected_type == "boolean":
        lower = stripped.lower()
        if lower == "true":
            return True
        if lower == "false":
            return False
        return value

    if expected_type in ("array", "object"):
        try:
            parsed = parse_json_with_repair(stripped)
            if isinstance(parsed, str):
                parsed = parse_json_with_repair(parsed)
        except (json.JSONDecodeError, ValueError):
            return value
        if expected_type == "array" and isinstance(parsed, list):
            return parsed
        if expected_type == "object" and isinstance(parsed, dict):
            return parsed

    return value


def coerce_tool_arguments(tool: Any, arguments: Any) -> Any:
    """Coerce only safe string forms required by the tool input schema.

    Allowed conversions:
    - numeric strings to integer/number
    - ``true``/``false`` strings to boolean
    - JSON strings to array/object

    The function mutates dict arguments in place so the subsequent smolagents
    schema validation sees the same argument object that will be executed.
    """

    inputs = getattr(tool, "inputs", None)
    if not isinstance(inputs, dict):
        return arguments

    if isinstance(arguments, dict):
        for key, value in list(arguments.items()):
            schema = inputs.get(key)
            if not isinstance(schema, dict):
                continue
            for expected_type in _expected_types(schema):
                coerced = _coerce_scalar(value, expected_type)
                if coerced is not value:
                    arguments[key] = coerced
                    _LOG.debug("Coerced tool argument %s to %s", key, expected_type)
                    break
        return arguments

    if len(inputs) == 1:
        schema = next(iter(inputs.values()))
        if isinstance(schema, dict):
            for expected_type in _expected_types(schema):
                coerced = _coerce_scalar(arguments, expected_type)
                if coerced is not arguments:
                    return coerced

    return arguments
