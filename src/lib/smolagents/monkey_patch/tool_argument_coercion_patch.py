"""
Monkey-patch: Coerce stringified JSON arguments before type validation.

Problem
-------
Some LLMs serialize tool arguments as strings instead of native types when
making tool calls. For example::

    sections: '[{"heading": "Intro", "body": "..."}]'  # string
    # Expected:
    sections: [{"heading": "Intro", "body": "..."}]    # native list

    step: "1"  # string
    # Expected:
    step: 1    # native int

The smolagents framework validates argument types *before* calling the tool,
so our tool-level coercion never gets a chance to run. The framework raises::

    TypeError: Argument sections has type 'string' but should be 'array'

Fix
---
Monkey-patch ``validate_tool_arguments`` to coerce string arguments before
validation when the expected schema type is a safe scalar/container type.
"""

import json
import re

from src.lib.logging import get_logger

_LOG = get_logger(__name__)

_PATCHED = False
_RE_INTEGER = re.compile(r"^-?\d+$")
_RE_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def _coerce_stringified_json(tool, arguments):
    """Attempt to coerce string arguments to their expected types.

    Modifies ``arguments`` dict in-place when a string value can be
    parsed as JSON and the result matches the expected schema type.
    Also handles reverse coercion: array/object → string when expected.
    """
    if not isinstance(arguments, dict):
        return arguments

    for key, value in list(arguments.items()):
        if key not in tool.inputs:
            continue

        expected_type = tool.inputs[key].get("type")

        if isinstance(value, str) and expected_type == "integer":
            stripped = value.strip()
            if _RE_INTEGER.match(stripped):
                try:
                    arguments[key] = int(stripped)
                    _LOG.debug("Coerced string argument '%s' to int", key)
                except (ValueError, OverflowError):
                    pass

        elif isinstance(value, str) and expected_type == "number":
            stripped = value.strip()
            if _RE_NUMBER.match(stripped):
                try:
                    parsed_number = float(stripped)
                except (ValueError, OverflowError):
                    parsed_number = None
                if parsed_number is not None and parsed_number not in (float("inf"), float("-inf")):
                    arguments[key] = parsed_number
                    _LOG.debug("Coerced string argument '%s' to float", key)

        elif isinstance(value, str) and expected_type == "boolean":
            lower = value.strip().lower()
            if lower == "true":
                arguments[key] = True
                _LOG.debug("Coerced string argument '%s' to bool", key)
            elif lower == "false":
                arguments[key] = False
                _LOG.debug("Coerced string argument '%s' to bool", key)

        # Forward: string → array/object
        elif isinstance(value, str) and expected_type in ("array", "object"):
            parsed = None

            # Attempt 1: direct parse
            try:
                result = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                result = None

            if result is not None:
                if isinstance(result, (list, dict)):
                    # Direct parse succeeded with correct type
                    parsed = result
                elif isinstance(result, str):
                    # Attempt 2: double-serialized (LLM wraps JSON in extra string layer)
                    try:
                        parsed = json.loads(result)
                    except (json.JSONDecodeError, ValueError):
                        pass

            # If both attempts fail, skip — let validate_tool_arguments report the error
            if parsed is None:
                continue

            if expected_type == "array" and isinstance(parsed, list):
                arguments[key] = parsed
                _LOG.debug(
                    "Coerced string argument '%s' to list (len=%d)",
                    key, len(parsed),
                )
            elif expected_type == "object" and isinstance(parsed, dict):
                arguments[key] = parsed
                _LOG.debug(
                    "Coerced string argument '%s' to dict (keys=%s)",
                    key, list(parsed.keys()),
                )

        # Reverse: array/object → string
        elif expected_type == "string" and isinstance(value, (list, dict)):
            try:
                arguments[key] = json.dumps(value, ensure_ascii=False)
                _LOG.debug(
                    "Coerced %s argument '%s' to JSON string",
                    type(value).__name__, key,
                )
            except (TypeError, ValueError):
                pass

    return arguments


def patch_tool_argument_coercion():
    """Install the argument coercion monkey-patch.

    Wraps ``smolagents.tools.validate_tool_arguments`` so that string
    values are coerced to native types before the original validation
    runs.

    Safe to call multiple times (idempotent).
    """
    global _PATCHED
    if _PATCHED:
        return

    import smolagents.tools as _tools_mod

    _original_validate = _tools_mod.validate_tool_arguments

    def _patched_validate(tool, arguments):
        arguments = _coerce_stringified_json(tool, arguments)
        return _original_validate(tool, arguments)

    _tools_mod.validate_tool_arguments = _patched_validate

    # Also patch the reference in agents module (already imported)
    try:
        import smolagents.agents as _agents_mod
        _agents_mod.validate_tool_arguments = _patched_validate
    except (ImportError, AttributeError):
        pass

    _PATCHED = True
    _LOG.debug("Patched validate_tool_arguments with JSON string coercion")
