"""
LLM semantic type coercion for tool parameters.

Automatically converts LLM-generated string values to the correct Python
types based on the tool's input schema.  This handles the common case where
an LLM outputs ``"true"`` instead of ``true`` or ``"42"`` instead of ``42``.

Rules:
    - schema ``"boolean"`` + value ``"true"/"false"`` (case-insensitive) → bool
    - schema ``"integer"`` + value matching ``/^-?\\d+$/`` → int
    - schema ``"number"``  + value matching ``/^-?\\d+(\\.\\d+)?$/`` → float
      (rejects ``"Infinity"`` / ``"NaN"``)
    - schema ``"array"``   + value is a JSON-encoded string → list
    - schema ``"object"``  + value is a JSON-encoded string → dict
    - everything else: unchanged (let downstream validation handle it)
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_RE_INTEGER = re.compile(r"^-?\d+$")
_RE_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def coerce_tool_parameters(
    tool_input: Dict[str, Any],
    tool_inputs_schema: Optional[Dict[str, Dict[str, Any]]],
) -> Dict[str, Any]:
    """Coerce string values in *tool_input* to match schema-declared types.

    Only converts when the value is a ``str`` and the schema declares a
    numeric or boolean type.  Values that are already the correct type, or
    that don't match the safe conversion patterns, are left untouched.

    Args:
        tool_input: Mutable dict of parameter name → value.
        tool_inputs_schema: The tool's ``inputs`` dict, e.g.
            ``{"flag": {"type": "boolean"}, "count": {"type": "integer"}}``.

    Returns:
        The same dict (mutated in-place for efficiency) with coerced values.
    """
    if not tool_inputs_schema or not isinstance(tool_inputs_schema, dict):
        return tool_input

    for key, value in tool_input.items():
        if not isinstance(value, str):
            continue
        schema_entry = tool_inputs_schema.get(key)
        if not isinstance(schema_entry, dict):
            continue
        expected_type = schema_entry.get("type")
        if not expected_type:
            continue

        coerced = _coerce_single(value, expected_type)
        if coerced is not _SENTINEL:
            tool_input[key] = coerced

    return tool_input


# Sentinel to distinguish "no conversion" from a valid None
_SENTINEL = object()


def _coerce_single(value: str, expected_type: str) -> Any:
    """Try to coerce a string *value* to *expected_type*.

    Returns ``_SENTINEL`` if no conversion should be applied.
    """
    if expected_type == "boolean":
        lower = value.strip().lower()
        if lower == "true":
            return True
        if lower == "false":
            return False
        return _SENTINEL

    if expected_type == "integer":
        if _RE_INTEGER.match(value.strip()):
            try:
                return int(value.strip())
            except (ValueError, OverflowError):
                return _SENTINEL
        return _SENTINEL

    if expected_type == "number":
        stripped = value.strip()
        if _RE_NUMBER.match(stripped):
            try:
                n = float(stripped)
                if not (n != n or n == float("inf") or n == float("-inf")):  # reject NaN/Inf
                    return n
            except (ValueError, OverflowError):
                pass
        return _SENTINEL

    # LLMs sometimes emit JSON arrays/objects as strings, e.g.
    # sections: '[{"heading": "A", ...}]' instead of sections: [{"heading": "A", ...}]
    if expected_type == "array":
        stripped = value.strip()
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        return _SENTINEL

    if expected_type == "object":
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
        return _SENTINEL

    return _SENTINEL
