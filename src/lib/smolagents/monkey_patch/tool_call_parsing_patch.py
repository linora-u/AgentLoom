"""
Multi-strategy tool call parsing chain for resilient JSON extraction.

Problem
-------
The upstream ``parse_json_blob()`` only supports strict JSON parsing. When LLMs
output non-standard formats (single quotes, Python dicts, XML tags, free-text
descriptions), the parser fails and wastes an entire step.

Fix
---
Implements a 6-strategy parsing chain that tries formats in priority order:

1. Standard JSON         ``{"name":"x","arguments":{}}``
2. Fixed JSON            ``{'name':'x','arguments':{}}`` (single quotes, trailing commas)
3. ast.literal_eval      Python dict with True/False/None
4. Nested tool_calls     ``[{'function':{'name':'x','arguments':{...}}}]``
5. XML tag extraction    Generic structural parser for XML/bracket formats
6. Regex extraction      ``Calling tool: 'x' with arguments: {...}``

The XML strategy (#5) uses a multi-phase structural parser that handles:
- ``<tool_call><name>X</name><arguments>{...}</arguments></tool_call>``
- ``<PREFIX:TAG>[invoke name="X"><parameter ...></invoke></PREFIX:TAG>``
- ``<invoke name="X"><parameter name="k">v</parameter></invoke>``
- Any namespace prefix and any wrapper tag name (model-agnostic)
- Tag attributes split across lines (``<parameter\\nname="k">``)
- Parameter values containing ``</parameter>`` substrings (markdown, heredoc)
- Parameter values containing JSON arrays ``[{...}]``
- Mixed bracket/angle-bracket format drift

Each strategy validates that the extracted tool name exists in the registered
tool list before accepting the result.

"""

from __future__ import annotations

import ast
import json
import re
import uuid
from typing import Any, Optional, Sequence

from src.lib.logging import get_logger

_LOG = get_logger(__name__)


# ---------------------------------------------------------------------------
#  Data types
# ---------------------------------------------------------------------------

class ParsedToolCall:
    """Result of a successful tool call parse."""

    __slots__ = ("name", "arguments", "strategy", "prefix_text")

    def __init__(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        strategy: str,
        prefix_text: str = "",
    ):
        self.name = name
        self.arguments = arguments or {}
        self.strategy = strategy
        self.prefix_text = prefix_text

    def __repr__(self) -> str:
        return f"ParsedToolCall(name={self.name!r}, strategy={self.strategy!r})"


class ToolCallParseError(Exception):
    """Raised when all parsing strategies fail."""

    def __init__(
        self,
        message: str,
        attempted_strategies: list[str],
        error_category: "ErrorCategory | None" = None,
    ):
        super().__init__(message)
        self.attempted_strategies = attempted_strategies
        self.error_category = error_category


# ---------------------------------------------------------------------------
#  Per-model strategy cache
# ---------------------------------------------------------------------------

_strategy_cache: dict[str, str] = {}  # model_id -> last successful strategy name


def clear_strategy_cache() -> None:
    """Clear the per-model strategy cache (for testing)."""
    _strategy_cache.clear()


def get_strategy_cache() -> dict[str, str]:
    """Return a copy of the current strategy cache (for testing)."""
    return dict(_strategy_cache)


# ---------------------------------------------------------------------------
#  Strategy 1: Standard JSON
# ---------------------------------------------------------------------------

def _strategy_standard_json(
    text: str,
    tool_name_key: str = "name",
    tool_arguments_key: str = "arguments",
) -> Optional[ParsedToolCall]:
    """Parse standard JSON blob from text.

    Finds first '{' and last '}' and attempts json.loads(strict=False).
    When multiple JSON objects exist (},\\n{ pattern), tries to parse the
    first complete JSON object individually.
    """
    first_brace = text.find("{")
    if first_brace == -1:
        return None

    # Find last closing brace
    last_brace = text.rfind("}")
    if last_brace == -1 or last_brace <= first_brace:
        return None

    json_str = text[first_brace : last_brace + 1]

    # Try the full span first
    data = None
    try:
        data = json.loads(json_str, strict=False)
    except (json.JSONDecodeError, ValueError):
        # If full span fails, try to find the first complete JSON object
        # by using json.JSONDecoder to parse just the first object.
        try:
            decoder = json.JSONDecoder(strict=False)
            data, _ = decoder.raw_decode(json_str)
        except (json.JSONDecodeError, ValueError):
            return None

    if not isinstance(data, dict):
        return None

    tool_name = data.get(tool_name_key)
    if not tool_name or not isinstance(tool_name, str):
        return None

    arguments = data.get(tool_arguments_key)
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments, strict=False)
        except (json.JSONDecodeError, ValueError):
            pass

    return ParsedToolCall(
        name=tool_name,
        arguments=arguments if isinstance(arguments, dict) else {},
        strategy="standard_json",
        prefix_text=text[:first_brace].strip(),
    )


# ---------------------------------------------------------------------------
#  Strategy 2: Fixed JSON (single quotes, trailing commas)
# ---------------------------------------------------------------------------

def _fix_json_string(text: str) -> str:
    """Attempt to fix common JSON issues: single quotes, trailing commas."""
    # Replace single quotes with double quotes (naive but effective for tool calls)
    # Be careful not to replace apostrophes in natural text
    fixed = text

    # Replace Python-style True/False/None with JSON equivalents
    fixed = re.sub(r'\bTrue\b', 'true', fixed)
    fixed = re.sub(r'\bFalse\b', 'false', fixed)
    fixed = re.sub(r'\bNone\b', 'null', fixed)

    # Replace single quotes around keys and simple string values
    # This regex handles: {'key': 'value'} -> {"key": "value"}
    fixed = re.sub(r"'([^'\\]*(?:\\.[^'\\]*)*)'", r'"\1"', fixed)

    # Remove trailing commas before closing braces/brackets
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)

    return fixed


def _strategy_fixed_json(
    text: str,
    tool_name_key: str = "name",
    tool_arguments_key: str = "arguments",
) -> Optional[ParsedToolCall]:
    """Parse JSON blob after fixing common formatting issues.

    Handles single quotes, trailing commas, Python True/False/None.
    """
    first_brace = text.find("{")
    if first_brace == -1:
        return None

    last_brace = text.rfind("}")
    if last_brace == -1 or last_brace <= first_brace:
        return None

    json_str = text[first_brace : last_brace + 1]
    fixed_str = _fix_json_string(json_str)

    try:
        data = json.loads(fixed_str, strict=False)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    tool_name = data.get(tool_name_key)
    if not tool_name or not isinstance(tool_name, str):
        return None

    arguments = data.get(tool_arguments_key)
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments, strict=False)
        except (json.JSONDecodeError, ValueError):
            pass

    return ParsedToolCall(
        name=tool_name,
        arguments=arguments if isinstance(arguments, dict) else {},
        strategy="fixed_json",
        prefix_text=text[:first_brace].strip(),
    )


# ---------------------------------------------------------------------------
#  Strategy 3: ast.literal_eval (Python dict)
# ---------------------------------------------------------------------------

def _strategy_ast_literal_eval(
    text: str,
    tool_name_key: str = "name",
    tool_arguments_key: str = "arguments",
) -> Optional[ParsedToolCall]:
    """Parse Python dict syntax using ast.literal_eval.

    Handles {'name': 'tool', 'arguments': {'key': True}} etc.
    """
    first_brace = text.find("{")
    if first_brace == -1:
        return None

    last_brace = text.rfind("}")
    if last_brace == -1 or last_brace <= first_brace:
        return None

    dict_str = text[first_brace : last_brace + 1]

    try:
        data = ast.literal_eval(dict_str)
    except (ValueError, SyntaxError, RecursionError):
        return None

    if not isinstance(data, dict):
        return None

    tool_name = data.get(tool_name_key)
    if not tool_name or not isinstance(tool_name, str):
        return None

    arguments = data.get(tool_arguments_key)

    # Convert Python native types to JSON-compatible dict
    if isinstance(arguments, dict):
        # Recursively convert Python types
        arguments = json.loads(json.dumps(arguments, default=str))

    return ParsedToolCall(
        name=tool_name,
        arguments=arguments if isinstance(arguments, dict) else {},
        strategy="ast_literal_eval",
        prefix_text=text[:first_brace].strip(),
    )


# ---------------------------------------------------------------------------
#  Strategy 4: XML tag extraction — Generic structured parser
# ---------------------------------------------------------------------------
#
# Instead of 7+ hardcoded regex patterns, we use a multi-phase structural
# parser that handles arbitrary XML/bracket tool call formats from any LLM:
#
#   Phase 1: Strip optional outer wrapper (<minimax:tool_call>, <tool_call>)
#   Phase 2: Extract tool name from invoke/call tag (bracket or angle-bracket)
#   Phase 3: Extract arguments via context-aware parameter scanning
#
# This approach handles:
#   - Tag attributes split across lines:  <parameter\n  name="key">
#   - Parameter values containing </parameter> substrings (markdown, heredoc)
#   - Parameter values containing JSON arrays [{...}]
#   - Mixed bracket/angle-bracket format drift ([invoke ... </invoke>)
#   - Any namespace prefix (<minimax:tool_call>, <xxx:tool_call>)


# -- Phase 1 helpers: outer wrapper detection --------------------------------

# Detect and strip outer XML wrapper pairs that enclose the actual tool call.
# Fully generic: matches ANY opening/closing tag pair with an optional namespace
# prefix, as long as the closing tag name matches the opening tag name.
# Uses a backreference to ensure open/close consistency.
# Examples matched:
#   <tool_call>...</tool_call>
#   <minimax:tool_call>...</minimax:tool_call>
#   <deepseek:function_call>...</deepseek:function_call>
#   <qwen:invoke>...</qwen:invoke>
#   <anthropic:tool_use>...</anthropic:tool_use>
#   <any_model:any_tag>...</any_model:any_tag>
_WRAPPER_PATTERN = re.compile(
    r"<((?:[\w]+:)?\w+)>\s*(.*?)\s*</\1>",
    re.DOTALL,
)


def _is_tool_call_wrapper(tag_name: str) -> bool:
    """Check if a matched tag looks like a tool call wrapper.

    A wrapper is an outer element that *encloses* the tool call structure
    (e.g. ``<minimax:tool_call>``).  Tags that *represent* the tool name
    directly (``<tool_name>``, ``<name>``) are NOT wrappers.

    Filters out common non-tool tags (html, div, p, span, etc.) to avoid
    false positives when wrapping patterns match arbitrary XML.
    """
    # Strip namespace prefix for comparison
    base_name = tag_name.split(":")[-1] if ":" in tag_name else tag_name
    lower = base_name.lower()

    # Exclude tags that represent the tool name itself, not a wrapper
    _NON_WRAPPER_TAGS = ("tool_name", "name", "parameter", "arguments", "arg")
    if lower in _NON_WRAPPER_TAGS:
        return False

    # Known tool-call wrapper patterns (substring match for flexibility)
    _TOOL_KEYWORDS = ("tool", "function", "invoke", "call", "action")
    return any(kw in lower for kw in _TOOL_KEYWORDS)


# -- Phase 2 helpers: tool name extraction -----------------------------------

# Bracket-style invoke: [invoke name="tool_name">
_BRACKET_INVOKE_PATTERN = re.compile(
    r'\[(\w+)\s+name\s*=\s*"([\w]+)"\s*>',
    re.DOTALL,
)

# Angle-bracket invoke: <invoke name="tool_name">
_ANGLE_INVOKE_PATTERN = re.compile(
    r'<(\w+)\s+name\s*=\s*"([\w]+)"\s*>',
    re.DOTALL,
)

# Explicit <name>X</name> tag (typically inside <tool_call>)
_NAME_TAG_PATTERN = re.compile(
    r"<name>\s*(\w+)\s*</name>",
    re.DOTALL,
)

# <tool_name>X</tool_name> tag (alternative naming)
_TOOL_NAME_TAG_PATTERN = re.compile(
    r"<tool_name>\s*(\w+)\s*</tool_name>",
    re.DOTALL,
)


# -- Phase 3 helpers: context-aware parameter extraction ---------------------

def _find_matching_close_tag(text: str, tag_name: str, start: int) -> int:
    """Find the position of the matching closing tag for a given open tag.

    Tracks nesting depth so that inner occurrences of the same tag (or
    false positives inside quoted strings / heredoc content) are skipped.

    Args:
        text: Full text to search.
        tag_name: The tag name (e.g. "parameter") without angle brackets.
        start: Position in *text* right after the opening tag's ">".

    Returns:
        Index of the first char of the matching ``</tag_name>`` in *text*,
        or -1 if no match is found.
    """
    open_tag = f"<{tag_name}"
    close_tag = f"</{tag_name}>"
    depth = 1
    pos = start
    text_len = len(text)

    while pos < text_len and depth > 0:
        # Find next opening or closing tag — whichever comes first
        next_open = text.find(open_tag, pos)
        next_close = text.find(close_tag, pos)

        if next_close == -1:
            # No closing tag found at all
            return -1

        if next_open != -1 and next_open < next_close:
            # Found another opening tag first — check it's a real tag (has >)
            gt_pos = text.find(">", next_open + len(open_tag))
            if gt_pos != -1 and gt_pos < next_close:
                depth += 1
            pos = next_open + len(open_tag)
        else:
            # Closing tag comes first (or no more opening tags)
            depth -= 1
            if depth == 0:
                return next_close
            pos = next_close + len(close_tag)

    return -1


def _extract_parameters_robust(text: str) -> list[tuple[str, str]]:
    """Extract <parameter name="key">value</parameter> pairs robustly.

    Unlike regex-based extraction, this handles:
    - Tag attributes split across lines: ``<parameter\\n  name="key">``
    - Parameter values containing ``</parameter>`` substrings
    - Nested XML/HTML fragments inside parameter values

    Args:
        text: Raw content between the invoke/call opening and closing tags.

    Returns:
        List of (param_name, param_value) tuples.
    """
    results: list[tuple[str, str]] = []
    pos = 0
    text_len = len(text)

    while pos < text_len:
        # Find next <parameter (with flexible whitespace after it)
        param_start = text.find("<parameter", pos)
        if param_start == -1:
            break

        # Find the closing ">" of this opening tag
        tag_close = text.find(">", param_start + len("<parameter"))
        if tag_close == -1:
            break

        # Extract the attribute portion between "<parameter" and ">"
        attr_str = text[param_start + len("<parameter"):tag_close]

        # Extract name="..." from attributes (tolerant of whitespace/newlines)
        name_match = re.search(r'name\s*=\s*"([^"]+)"', attr_str, re.DOTALL)
        if not name_match:
            # Malformed tag — skip past it
            pos = tag_close + 1
            continue

        param_name = name_match.group(1).strip()
        value_start = tag_close + 1

        # Find the matching </parameter> using depth tracking
        close_pos = _find_matching_close_tag(text, "parameter", value_start)
        if close_pos == -1:
            # No matching close tag — take everything remaining as value
            param_value = text[value_start:].strip()
            results.append((param_name, param_value))
            break

        param_value = text[value_start:close_pos].strip()
        results.append((param_name, param_value))

        # Move past </parameter>
        pos = close_pos + len("</parameter>")

    return results


def _find_invoke_close(text: str, tag_name: str, start: int) -> int:
    """Find the closing tag for an invoke/call block.

    Handles both bracket-close ``[/tag_name]`` and angle-bracket-close
    ``</tag_name>`` formats (and the mixed-bracket format drift).

    Args:
        text: Text to search.
        tag_name: The tag name (e.g. "invoke").
        start: Position to start searching from.

    Returns:
        Index of the first char of the closing marker, or -1.
    """
    # Try bracket close: [/invoke]
    bracket_close = text.find(f"[/{tag_name}]", start)
    # Try angle-bracket close: </invoke>
    angle_close = text.find(f"</{tag_name}>", start)

    candidates = []
    if bracket_close != -1:
        candidates.append(bracket_close)
    if angle_close != -1:
        candidates.append(angle_close)

    return min(candidates) if candidates else -1


# -- Main structured parser -------------------------------------------------

def _extract_xml_tool_call(text: str) -> Optional[tuple[str, str, dict[str, Any], int]]:
    """Generic XML/bracket tool call extractor.

    Multi-phase structural parser that handles all known LLM XML formats
    without hardcoded patterns.

    Returns:
        Tuple of (tool_name, strategy_detail, arguments, prefix_end_pos)
        or None if no tool call found.
    """
    working_text = text
    prefix_offset = 0  # track how much we stripped for prefix calculation

    # --- Phase 1: Strip outer wrapper if present ---
    # _WRAPPER_PATTERN captures: group(1)=tag_name, group(2)=inner_content
    wrapper_match = _WRAPPER_PATTERN.search(working_text)
    if wrapper_match and _is_tool_call_wrapper(wrapper_match.group(1)):
        prefix_offset = wrapper_match.start()
        working_text = wrapper_match.group(2)
    else:
        wrapper_match = None  # reset so downstream checks work
        prefix_offset = 0

    # --- Phase 2: Extract tool name ---
    tool_name: str | None = None
    tag_name: str | None = None
    body_start: int = 0

    # Strategy A: bracket invoke [invoke name="tool">
    bracket_match = _BRACKET_INVOKE_PATTERN.search(working_text)
    if bracket_match:
        tag_name = bracket_match.group(1)   # e.g. "invoke"
        tool_name = bracket_match.group(2)  # e.g. "read_file"
        body_start = bracket_match.end()

    # Strategy B: angle-bracket invoke <invoke name="tool">
    if tool_name is None:
        angle_match = _ANGLE_INVOKE_PATTERN.search(working_text)
        if angle_match:
            tag_name = angle_match.group(1)
            tool_name = angle_match.group(2)
            body_start = angle_match.end()
            # Update prefix_offset if no wrapper was found
            if not wrapper_match:
                prefix_offset = angle_match.start()

    # Strategy C: <name>tool</name> style (with optional <arguments>)
    if tool_name is None:
        name_match = _NAME_TAG_PATTERN.search(working_text)
        if name_match:
            tool_name = name_match.group(1)
            body_start = name_match.end()
            if not wrapper_match:
                prefix_offset = name_match.start()

    # Strategy D: <tool_name>X</tool_name>
    if tool_name is None:
        tname_match = _TOOL_NAME_TAG_PATTERN.search(working_text)
        if tname_match:
            tool_name = tname_match.group(1)
            body_start = tname_match.end()
            if not wrapper_match:
                prefix_offset = tname_match.start()

    # --- Phase 2b: If wrapper was stripped but no XML tool name found,
    #     check if inner content is a nested list/dict format ---
    if tool_name is None and wrapper_match:
        stripped = working_text.strip()
        if stripped and stripped[0] in ("[", "{"):
            # Delegate to nested_tool_calls strategy on the unwrapped content.
            # This handles <minimax:tool_call>[{...}]</minimax:tool_call>.
            nested_result = _strategy_nested_tool_calls(
                stripped,
                tool_name_key="name",
                tool_arguments_key="arguments",
            )
            if nested_result is not None:
                nested_result.strategy = "xml_tags+nested_delegate"
                nested_result.prefix_text = text[:prefix_offset].strip()
                return (nested_result.name, "xml_tags+nested_delegate",
                        nested_result.arguments, prefix_offset)
        return None

    if tool_name is None:
        return None

    # --- Phase 3: Extract arguments from body ---
    # Determine the body region (everything after the tool name tag)
    if tag_name:
        close_pos = _find_invoke_close(working_text, tag_name, body_start)
        if close_pos != -1:
            body = working_text[body_start:close_pos]
        else:
            body = working_text[body_start:]
    else:
        body = working_text[body_start:]

    arguments = _parse_xml_arguments(body.strip())

    return (tool_name, "xml_tags", arguments, prefix_offset)


def _parse_xml_arguments(raw_args: str) -> dict[str, Any]:
    """Parse arguments from XML body content.

    Tries multiple extraction approaches in priority order:
    1. ``<arguments>{...}</arguments>`` — JSON wrapped in arguments tags
    2. ``<parameter name="k">v</parameter>`` — robust parameter tag extraction
    3. Direct JSON blob ``{...}`` — bare JSON in the body
    """
    raw_args = raw_args.strip()
    if not raw_args:
        return {}

    # Approach 1: <arguments>...</arguments> wrapper
    args_match = re.search(
        r"<arguments>\s*(.*?)\s*</arguments>", raw_args, re.DOTALL
    )
    if args_match:
        inner = args_match.group(1).strip()
        if inner:
            try:
                data = json.loads(inner, strict=False)
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, ValueError):
                pass

    # Approach 2: Robust <parameter> tag extraction (context-aware)
    params = _extract_parameters_robust(raw_args)
    if params:
        result: dict[str, Any] = {}
        for key, value in params:
            value = value.strip()
            # Try to parse as JSON value (handles arrays, objects, numbers, etc.)
            try:
                parsed = json.loads(value, strict=False)
                result[key] = parsed
            except (json.JSONDecodeError, ValueError):
                result[key] = value
        return result

    # Approach 3: Direct JSON blob (no wrapper tags)
    try:
        data = json.loads(raw_args, strict=False)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass

    return {}


# Legacy regex patterns — kept as fallback during transition period.
# The generic parser above handles all these cases and more, but
# we keep the old patterns as a safety net.
_XML_LEGACY_PATTERNS = [
    # <PREFIX:TAG><name>X</name><arguments>{...}</arguments></PREFIX:TAG>
    # Generic: any namespace prefix, any wrapper tag name.
    # Group(1)=tool_name, Group(2)=arguments_body
    re.compile(
        r"<(?:[\w]+:)?\w+>\s*"
        r"<name>\s*(\w+)\s*</name>\s*"
        r"<arguments>\s*(.*?)\s*</arguments>\s*"
        r"</(?:[\w]+:)?\w+>",
        re.DOTALL,
    ),
    # Bracket consistent: <PREFIX:TAG>[invoke name="X">...[/invoke]</PREFIX:TAG>
    # Group(1)=tool_name, Group(2)=body
    re.compile(
        r"<(?:[\w]+:)?\w+>\s*"
        r'\[\w+\s+name="([\w]+)">\s*(.*?)\s*\[/\w+\]\s*'
        r"</(?:[\w]+:)?\w+>",
        re.DOTALL,
    ),
    # Standalone bracket consistent: [TAG name="X">...[/TAG]
    # Group(1)=tool_name, Group(2)=body
    re.compile(
        r'\[\w+\s+name="([\w]+)">\s*(.*)\s*\[/\w+\]',
        re.DOTALL,
    ),
    # <tool_name>X</tool_name> ... <arguments>{...}</arguments>
    # Group(1)=tool_name, Group(2)=arguments_body
    re.compile(
        r"<tool_name>\s*(\w+)\s*</tool_name>\s*"
        r"(?:<arguments>)?\s*(.*?)\s*(?:</arguments>)?$",
        re.DOTALL | re.MULTILINE,
    ),
]


def _strategy_xml_tags(
    text: str,
    tool_name_key: str = "name",
    tool_arguments_key: str = "arguments",
) -> Optional[ParsedToolCall]:
    """Extract tool call from XML-formatted output.

    Uses a generic multi-phase structural parser that handles all known
    LLM XML/bracket formats without hardcoded patterns.  Falls back to
    legacy regex patterns if the structural parser does not match.

    Supported formats include (non-exhaustive):
    - ``<tool_call><name>X</name><arguments>{...}</arguments></tool_call>``
    - ``<PREFIX:TAG>[invoke name="X"><param ...>[/invoke]</PREFIX:TAG>``
    - ``<PREFIX:TAG>[invoke name="X"><param ...></invoke></PREFIX:TAG>``
    - ``<invoke name="X"><parameter name="k">v</parameter></invoke>``
    - ``[invoke name="X"><parameter ...>[/invoke]``
    - ``<tool_name>X</tool_name><arguments>{...}</arguments>``
    - Any namespace prefix and wrapper tag name (fully model-agnostic)
    - Tags with attributes split across lines (``<parameter\\nname="k">``)
    - Parameter values containing ``</parameter>`` substrings
    - Parameter values containing JSON arrays ``[{...}]``
    """
    # --- Primary path: generic structural parser ---
    result = _extract_xml_tool_call(text)
    if result is not None:
        tool_name, strategy, arguments, prefix_end = result
        if tool_name:
            return ParsedToolCall(
                name=tool_name,
                arguments=arguments,
                strategy=strategy,
                prefix_text=text[:prefix_end].strip(),
            )

    # --- Fallback: legacy regex patterns (transitional safety net) ---
    for pattern in _XML_LEGACY_PATTERNS:
        match = pattern.search(text)
        if match:
            tool_name = match.group(1).strip()
            raw_args = match.group(2).strip() if (match.lastindex or 0) >= 2 else ""
            if not tool_name:
                continue
            arguments = _parse_xml_arguments(raw_args)
            return ParsedToolCall(
                name=tool_name,
                arguments=arguments,
                strategy="xml_tags",
                prefix_text=text[:match.start()].strip(),
            )

    return None


# ---------------------------------------------------------------------------
#  Strategy 5: Regex extraction from free text
# ---------------------------------------------------------------------------

_FREE_TEXT_PATTERNS = [
    # "Calling tool: 'tool_name' with arguments: {...}"
    re.compile(
        r"[Cc]alling\s+tool[:\s]+['\"]?(\w+)['\"]?\s+"
        r"with\s+arguments[:\s]+(\{.*\})",
        re.DOTALL,
    ),
    # "Action: tool_name\nAction Input: {...}"
    re.compile(
        r"[Aa]ction[:\s]+(\w+)\s*\n\s*"
        r"[Aa]ction\s+[Ii]nput[:\s]+(\{.*\})",
        re.DOTALL,
    ),
    # "tool_name(arg1=val1, arg2=val2)" - function call syntax
    re.compile(
        r"(\w+)\s*\(\s*((?:\w+\s*=\s*[^,)]+(?:,\s*)?)*)\s*\)",
    ),
    # "Using tool: tool_name\nInput: {...}"
    re.compile(
        r"[Uu]sing\s+tool[:\s]+['\"]?(\w+)['\"]?\s*\n"
        r"\s*[Ii]nput[:\s]+(\{.*\})",
        re.DOTALL,
    ),
]


def _parse_function_call_args(args_str: str) -> dict[str, Any]:
    """Parse 'key=value, key2=value2' format arguments."""
    result = {}
    # Match key=value pairs
    pairs = re.findall(r"(\w+)\s*=\s*([^,]+?)(?:,|$)", args_str)
    for key, value in pairs:
        value = value.strip().strip("'\"")
        # Try to parse as JSON value
        try:
            result[key] = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            result[key] = value
    return result


def _strategy_regex_extraction(
    text: str,
    tool_name_key: str = "name",
    tool_arguments_key: str = "arguments",
) -> Optional[ParsedToolCall]:
    """Extract tool call from free-text descriptions using regex.

    Last resort strategy for when the LLM describes the tool call in
    natural language rather than structured format.
    """
    for i, pattern in enumerate(_FREE_TEXT_PATTERNS):
        match = pattern.search(text)
        if match:
            tool_name = match.group(1).strip()
            raw_args = match.group(2).strip() if (match.lastindex or 0) >= 2 else ""

            if not tool_name:
                continue

            # Try to parse arguments
            arguments: dict[str, Any] = {}
            if raw_args:
                # Try JSON first
                try:
                    parsed = json.loads(raw_args, strict=False)
                    if isinstance(parsed, dict):
                        arguments = parsed
                except (json.JSONDecodeError, ValueError):
                    pass

                # Try fixed JSON
                if not arguments:
                    try:
                        fixed = _fix_json_string(raw_args)
                        parsed = json.loads(fixed, strict=False)
                        if isinstance(parsed, dict):
                            arguments = parsed
                    except (json.JSONDecodeError, ValueError):
                        pass

                # Try ast.literal_eval
                if not arguments:
                    try:
                        parsed = ast.literal_eval(raw_args)
                        if isinstance(parsed, dict):
                            arguments = parsed
                    except (ValueError, SyntaxError):
                        pass

                # Try function call args format
                if not arguments and "=" in raw_args:
                    arguments = _parse_function_call_args(raw_args)

            prefix_end = match.start()

            return ParsedToolCall(
                name=tool_name,
                arguments=arguments,
                strategy="regex_extraction",
                prefix_text=text[:prefix_end].strip(),
            )

    return None


# ---------------------------------------------------------------------------
#  Structural extraction helper (bracket-depth based, size-agnostic)
# ---------------------------------------------------------------------------

def _extract_balanced_patch(text: str, start: int, open_ch: str, close_ch: str) -> str | None:
    """Extract a balanced bracket/brace substring using depth counting.

    Correctly handles nested brackets and quoted strings (both single-
    and double-quoted with backslash escaping).

    Args:
        text: Source string.
        start: Index of the opening character (must equal *open_ch*).
        open_ch: Opening bracket character ('{' or '[').
        close_ch: Closing bracket character ('}' or ']').

    Returns:
        Balanced substring from text[start] through matching close, inclusive.
        None if no balanced match is found.
    """
    if start >= len(text) or text[start] != open_ch:
        return None
    depth = 0
    in_string: str | None = None
    i = start
    length = len(text)
    while i < length:
        ch = text[i]
        if in_string is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
        else:
            if ch in ("'", '"'):
                in_string = ch
            elif ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def _try_structural_extract(raw: str) -> Any:
    """Extract tool call structure using regex + bracket-depth.

    When ast.literal_eval and json.loads both fail (due to large content
    with apostrophes, escape sequences, or malformed dicts), this function
    uses structural parsing to extract just the 'function' object from
    the first tool call dict in the text.

    Returns a list containing one dict (matching the OpenAI tool_call format)
    or a single dict, or None if extraction fails.
    """
    # Look for 'function' key pattern
    func_match = re.search(r"['\"]function['\"]\s*:\s*\{", raw)
    if not func_match:
        return None

    # Find the opening brace of the function value
    brace_start = raw.index("{", func_match.start() + len("'function'"))
    func_dict_str = _extract_balanced_patch(raw, brace_start, "{", "}")
    if not func_dict_str:
        return None

    # Now extract 'name' from within the function dict
    name_match = re.search(r"['\"]name['\"]\s*:\s*['\"](\w+)['\"]", func_dict_str)
    if not name_match:
        return None
    tool_name = name_match.group(1)

    # Extract 'arguments' from within the function dict
    args_match = re.search(r"['\"]arguments['\"]\s*:\s*", func_dict_str)
    arguments = {}
    if args_match:
        val_start = args_match.end()
        # Skip whitespace
        while val_start < len(func_dict_str) and func_dict_str[val_start] in " \t\n\r":
            val_start += 1
        if val_start < len(func_dict_str):
            ch = func_dict_str[val_start]
            if ch == "{":
                balanced = _extract_balanced_patch(func_dict_str, val_start, "{", "}")
                if balanced:
                    # Try parsing the arguments dict
                    try:
                        arguments = json.loads(balanced, strict=False)
                    except Exception:
                        import ast as _ast
                        try:
                            arguments = _ast.literal_eval(balanced)
                            arguments = json.loads(json.dumps(arguments, default=str))
                        except Exception:
                            # For small args, try quote fix
                            if len(balanced) < 2048:
                                try:
                                    fixed = re.sub(r"\bTrue\b", "true", balanced)
                                    fixed = re.sub(r"\bFalse\b", "false", fixed)
                                    fixed = re.sub(r"\bNone\b", "null", fixed)
                                    fixed = re.sub(
                                        r"(?<=[\[{,:\s])'|'(?=[\]},.:\s])", '"', fixed
                                    )
                                    arguments = json.loads(fixed, strict=False)
                                except Exception:
                                    arguments = {}
                            else:
                                arguments = {}
            elif ch in ("'", '"'):
                # Arguments as a JSON string value
                quote = ch
                end = func_dict_str.find(quote, val_start + 1)
                while end != -1 and func_dict_str[end - 1] == "\\":
                    end = func_dict_str.find(quote, end + 1)
                if end != -1:
                    json_str = func_dict_str[val_start + 1 : end]
                    json_str = json_str.replace("\\n", "\n").replace("\\t", "\t")
                    json_str = json_str.replace("\\\\", "\\")
                    try:
                        parsed = json.loads(json_str, strict=False)
                        if isinstance(parsed, dict):
                            arguments = parsed
                    except Exception:
                        pass

    # Extract 'id' if available
    id_match = re.search(r"['\"]id['\"]\s*:\s*['\"]([^'\"]+)['\"]", raw[:func_match.start() + 200])
    call_id = id_match.group(1) if id_match else "structural_" + tool_name

    # Return in the format expected by _extract_from_nested_tool_call
    return [{
        "id": call_id,
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": arguments,
        },
    }]


# ---------------------------------------------------------------------------
#  Strategy 6: Nested tool_calls list format (OpenAI function calling style)
# ---------------------------------------------------------------------------

def _extract_from_nested_tool_call(
    data: Any,
    tool_name_key: str = "name",
    tool_arguments_key: str = "arguments",
) -> Optional[ParsedToolCall]:
    """Extract tool name and arguments from a nested tool_call dict.

    Handles the OpenAI function calling format:
    {'id': '...', 'type': 'function', 'function': {'name': 'tool', 'arguments': {...}}}
    """
    if not isinstance(data, dict):
        return None

    # Direct nested: {'function': {'name': 'x', 'arguments': {...}}}
    func_obj = data.get("function")
    if isinstance(func_obj, dict):
        tool_name = func_obj.get(tool_name_key)
        if tool_name and isinstance(tool_name, str):
            arguments = func_obj.get(tool_arguments_key)
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments, strict=False)
                except (json.JSONDecodeError, ValueError):
                    try:
                        arguments = ast.literal_eval(arguments)
                    except (ValueError, SyntaxError):
                        arguments = {}
            return ParsedToolCall(
                name=tool_name,
                arguments=arguments if isinstance(arguments, dict) else {},
                strategy="nested_tool_calls",
                prefix_text="",
            )
    return None


def _strategy_nested_tool_calls(
    text: str,
    tool_name_key: str = "name",
    tool_arguments_key: str = "arguments",
) -> Optional[ParsedToolCall]:
    """Extract tool call from nested tool_calls list format.

    Handles the format commonly returned by models via Anthropic-compatible
    endpoints when they emit tool calls as structured text instead of native
    API tool_calls:

        Calling tools:
        [{'id': '...', 'type': 'function',
          'function': {'name': 'tool_name', 'arguments': {...}}}]

    Also handles single dict without the list wrapper.
    """

    def _try_parse_structure(raw: str) -> Any:
        """Try multiple parsing approaches for a structure string.

        Models like MiniMax output Python dict syntax with JSON-style escape
        sequences (e.g. ``\\n`` inside single-quoted strings). This is invalid
        for ``ast.literal_eval`` because in Python single-quoted strings
        ``\\n`` is a literal backslash+n, not a newline.

        Strategy: try ``ast.literal_eval`` first (handles True/False/None),
        then fall back to JSON after converting single quotes to double quotes.
        """
        # 1) Direct ast.literal_eval
        try:
            return ast.literal_eval(raw)
        except (ValueError, SyntaxError, RecursionError):
            pass

        # 2) Try json.loads after converting Python dict syntax to JSON.
        #    Only for strings < 2KB — large strings with markdown content
        #    contain apostrophes that corrupt the quote conversion regex.
        if len(raw) < 2048:
            try:
                jsonified = raw
                jsonified = re.sub(r"\bTrue\b", "true", jsonified)
                jsonified = re.sub(r"\bFalse\b", "false", jsonified)
                jsonified = re.sub(r"\bNone\b", "null", jsonified)
                jsonified = re.sub(r"(?<=[\[{,:\s])'|'(?=[\]},:.\s])", '"', jsonified)
                return json.loads(jsonified, strict=False)
            except (json.JSONDecodeError, ValueError):
                pass

        # 3) Structural extraction: find the first dict with 'function' key
        #    using regex + bracket-depth. Robust for any size.
        structural = _try_structural_extract(raw)
        if structural is not None:
            return structural

        return None

    # Try to find a list [...] in the text
    first_bracket = text.find("[")
    if first_bracket != -1:
        last_bracket = text.rfind("]")
        if last_bracket > first_bracket:
            list_str = text[first_bracket : last_bracket + 1]
            data = _try_parse_structure(list_str)
            if isinstance(data, list) and len(data) > 0:
                # Extract from first item in the list
                result = _extract_from_nested_tool_call(
                    data[0],
                    tool_name_key=tool_name_key,
                    tool_arguments_key=tool_arguments_key,
                )
                if result:
                    result.prefix_text = text[:first_bracket].strip()
                    return result

    # Also try parsing a single dict with 'function' key
    first_brace = text.find("{")
    if first_brace == -1:
        return None
    last_brace = text.rfind("}")
    if last_brace == -1 or last_brace <= first_brace:
        return None

    dict_str = text[first_brace : last_brace + 1]
    data = _try_parse_structure(dict_str)
    if isinstance(data, dict):
        result = _extract_from_nested_tool_call(
            data,
            tool_name_key=tool_name_key,
            tool_arguments_key=tool_arguments_key,
        )
        if result:
            result.prefix_text = text[:first_brace].strip()
            return result

    # Last resort: structural extraction directly from the raw text.
    # This handles malformed lists where dicts are missing closing braces
    # (a known MiniMax issue with parallel tool calls).
    structural = _try_structural_extract(text)
    if isinstance(structural, list) and len(structural) > 0:
        result = _extract_from_nested_tool_call(
            structural[0],
            tool_name_key=tool_name_key,
            tool_arguments_key=tool_arguments_key,
        )
        if result:
            result.prefix_text = text[:first_bracket].strip() if first_bracket != -1 else ""
            return result
    elif isinstance(structural, dict):
        result = _extract_from_nested_tool_call(
            structural,
            tool_name_key=tool_name_key,
            tool_arguments_key=tool_arguments_key,
        )
        if result:
            result.prefix_text = text[:first_brace].strip() if first_brace != -1 else ""
            return result

    return None


# ---------------------------------------------------------------------------
#  Main parsing chain
# ---------------------------------------------------------------------------

# Ordered list of strategies, highest priority first
_STRATEGIES = [
    ("standard_json", _strategy_standard_json),
    ("fixed_json", _strategy_fixed_json),
    ("ast_literal_eval", _strategy_ast_literal_eval),
    ("nested_tool_calls", _strategy_nested_tool_calls),
    ("xml_tags", _strategy_xml_tags),
    ("regex_extraction", _strategy_regex_extraction),
]


def parse_tool_call_resilient(
    text: str,
    available_tool_names: Sequence[str] | None = None,
    tool_name_key: str = "name",
    tool_arguments_key: str = "arguments",
    model_id: str | None = None,
) -> ParsedToolCall:
    """Parse a tool call from LLM text output using multi-strategy chain.

    Tries 6 strategies in priority order. If a per-model cached strategy
    exists, it is tried first.  Each successful parse is validated against
    the available tool names (if provided). The first match wins.

    Args:
        text: Raw LLM output text.
        available_tool_names: List of registered tool names for validation.
            If None, skip tool name validation.
        tool_name_key: Key name for the tool name in the JSON structure.
        tool_arguments_key: Key name for the arguments in the JSON structure.
        model_id: Optional model identifier for per-model strategy caching.

    Returns:
        ParsedToolCall with the parsed tool name, arguments, and strategy used.

    Raises:
        ToolCallParseError: When all strategies fail.
    """
    if not text or not text.strip():
        raise ToolCallParseError(
            "Empty or whitespace-only model output.",
            attempted_strategies=[],
        )

    attempted: list[str] = []
    failures: list[str] = []

    def _try_strategy(strategy_name: str, strategy_fn) -> ParsedToolCall | None:
        """Try a single strategy and return result or None."""
        attempted.append(strategy_name)
        try:
            result = strategy_fn(
                text,
                tool_name_key=tool_name_key,
                tool_arguments_key=tool_arguments_key,
            )
        except Exception as exc:
            failures.append(f"{strategy_name}: exception={exc}")
            return None

        if result is None:
            failures.append(f"{strategy_name}: no match")
            return None

        # Validate tool name against registered tools
        if available_tool_names is not None:
            if result.name not in available_tool_names:
                failures.append(
                    f"{strategy_name}: tool '{result.name}' not in "
                    f"registered tools {list(available_tool_names)}"
                )
                return None
        return result

    # Try cached strategy first (if available)
    if model_id and model_id in _strategy_cache:
        cached_name = _strategy_cache[model_id]
        for sname, sfn in _STRATEGIES:
            if sname == cached_name:
                result = _try_strategy(f"{sname}(cached)", sfn)
                if result is not None:
                    _LOG.debug(
                        "Tool call parsed with cached strategy: %s, tool=%s",
                        cached_name, result.name,
                    )
                    return result
                break  # cached strategy failed, fall through to full chain

    # Full strategy chain
    for strategy_name, strategy_fn in _STRATEGIES:
        result = _try_strategy(strategy_name, strategy_fn)
        if result is not None:
            # Update cache on success
            if model_id is not None:
                _strategy_cache[model_id] = strategy_name
            _LOG.debug(
                "Tool call parsed successfully: strategy=%s, tool=%s, "
                "attempted=%s",
                strategy_name,
                result.name,
                attempted,
            )
            return result

    # All strategies failed — classify error and extract partial info
    from src.lib.smolagents.error_recovery import (
        classify_parse_error,
        extract_tool_info,
        ErrorCategory,
    )
    partial_tool_name = extract_tool_info(
        failures=failures,
        raw_text=text,
        available_tool_names=list(available_tool_names) if available_tool_names else None,
    )
    error_category = classify_parse_error(
        failures=failures,
        partial_tool_name=partial_tool_name,
        available_tool_names=list(available_tool_names) if available_tool_names else None,
    )

    # Log detailed strategy diagnostics for debugging (not sent to LLM)
    _LOG.debug(
        "All %d parsing strategies failed. Details: %s",
        len(attempted),
        "; ".join(failures),
    )

    # Build LLM-friendly error message (no internal strategy names).
    # Minimal diagnostic only — LLM-facing guidance is generated by the
    # recovery pipeline in error_recovery.py (L1-L4).
    category_tag = f"[CATEGORY:{error_category.value}]" if error_category else ""

    if error_category == ErrorCategory.UNKNOWN_TOOL and partial_tool_name:
        diagnostic = f" Tool '{partial_tool_name}' not found in registered tools."
    elif partial_tool_name:
        diagnostic = f" Could not parse tool call. partial_tool={partial_tool_name}"
    else:
        diagnostic = " Could not parse tool call."

    # Log full model output for debugging
    _LOG.info(
        "%s%s\nFull model output:\n%s",
        category_tag, diagnostic, text,
    )

    error_msg = (
        f"{category_tag}{diagnostic}\n"
        f"Your output:\n{text}"
    )
    raise ToolCallParseError(
        error_msg,
        attempted_strategies=attempted,
        error_category=error_category,
    )


# ---------------------------------------------------------------------------
#  Monkey-patch integration
# ---------------------------------------------------------------------------

def patch_smolagents_tool_call_parsing() -> None:
    """Monkey-patch smolagents' tool call parsing for resilient multi-strategy parsing.

    Replaces ``parse_json_blob`` in ``smolagents.utils`` and
    ``get_tool_call_from_text`` in ``smolagents.models`` with resilient
    versions that use the multi-strategy parsing chain.
    """
    import smolagents.utils as _utils_mod
    import smolagents.models as _models_mod

    _original_parse_json_blob = _utils_mod.parse_json_blob
    _original_get_tool_call = _models_mod.get_tool_call_from_text

    # Guard against double-patching
    if getattr(_original_parse_json_blob, "_agentloom_patched", False):
        return

    def _resilient_parse_json_blob(json_blob: str) -> tuple[dict, str]:
        """Drop-in replacement for parse_json_blob using multi-strategy chain.

        Maintains the same return signature: (parsed_dict, prefix_text).
        Falls back to original implementation if the multi-strategy chain
        does not produce valid output.  When both fail, re-raises our
        ToolCallParseError (which has richer diagnostics) rather than the
        original's simpler error.
        """
        try:
            result = parse_tool_call_resilient(
                json_blob,
                available_tool_names=None,  # No validation at this level
            )
            data = {
                "name": result.name,
                "arguments": result.arguments,
            }
            return data, result.prefix_text
        except ToolCallParseError as our_error:
            # Try original as fallback for backward compatibility
            try:
                return _original_parse_json_blob(json_blob)
            except Exception:
                # Both failed — re-raise our error (richer diagnostics + [CATEGORY:...] tag)
                raise our_error

    _resilient_parse_json_blob._agentloom_patched = True  # type: ignore[attr-defined]

    def _resilient_get_tool_call_from_text(
        text: str, tool_name_key: str, tool_arguments_key: str
    ):
        """Drop-in replacement for get_tool_call_from_text using multi-strategy chain.

        When both our chain and the original fail, re-raises our ToolCallParseError
        so the [CATEGORY:...] tag and strategy diagnostics propagate.
        """
        from smolagents.models import ChatMessageToolCall, ChatMessageToolCallFunction

        try:
            result = parse_tool_call_resilient(
                text,
                available_tool_names=None,
                tool_name_key=tool_name_key,
                tool_arguments_key=tool_arguments_key,
            )
            return ChatMessageToolCall(
                id=str(uuid.uuid4()),
                type="function",
                function=ChatMessageToolCallFunction(
                    name=result.name,
                    arguments=result.arguments,
                ),
            )
        except ToolCallParseError as our_error:
            try:
                return _original_get_tool_call(text, tool_name_key, tool_arguments_key)
            except Exception:
                raise our_error

    # Apply patches to source modules
    _utils_mod.parse_json_blob = _resilient_parse_json_blob

    # Also patch in models module where get_tool_call_from_text is defined
    _models_mod.get_tool_call_from_text = _resilient_get_tool_call_from_text

    _LOG.info("Patched smolagents tool call parsing with multi-strategy chain")
