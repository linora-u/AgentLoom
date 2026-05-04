"""
Generic tool call error recovery: classification, diagnosis, and progressive guidance.

Provides error classification (4 categories), 3-level tool info extraction,
progressive recovery message generation (4 levels), and error message
consolidation for consecutive failures.

All public functions are framework-agnostic — they accept basic types (str,
list, dict) and never import smolagents types.  The smolagents-specific data
assembly lives in ``base_agent._consolidate_error_messages()``.
"""

from __future__ import annotations

import enum
import re
from typing import Any, Optional

from src.lib.logging import get_logger

_LOG = get_logger(__name__)

# ---------------------------------------------------------------------------
#  Error classification
# ---------------------------------------------------------------------------

NOW_LETS_RETRY_PREFIX = "Now let's retry"


class ErrorCategory(enum.Enum):
    """Four mutually-exclusive categories for tool-call parsing errors."""

    FORMAT_NOT_FOUND = "FORMAT_NOT_FOUND"
    JSON_SYNTAX_ERROR = "JSON_SYNTAX_ERROR"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    ARGUMENT_ERROR = "ARGUMENT_ERROR"


# Tag used to encode the category inside ToolCallParseError messages.
_CATEGORY_TAG_PATTERN = re.compile(r"\[CATEGORY:(\w+)\]")

# Patterns for extracting partial tool names from raw LLM output (Level 3).
PARTIAL_TOOL_NAME_PATTERNS = [
    re.compile(r'"name"\s*:\s*"(\w+)"'),                                 # JSON
    re.compile(r"<name>(\w+)</name>"),                                    # XML
    re.compile(r'invoke\s+name="(\w+)"'),                                 # invoke
    re.compile(r"<(?:minimax:)?tool_call>.*?<name>(\w+)</name>", re.DOTALL),  # MiniMax wrapper
    re.compile(r"Calling tool:\s*'(\w+)'"),                               # Verbose
    re.compile(r"<tool_name>(\w+)</tool_name>"),                          # Simple XML
]

# Pattern for extracting tool name from strategy-chain failures (Level 2).
_FAILURE_TOOL_NAME_RE = re.compile(r"tool '(\w+)' not in")


def classify_parse_error(
    failures: list[str] | None,
    partial_tool_name: str | None,
    available_tool_names: list[str] | None,
) -> ErrorCategory:
    """Classify a parse error into one of 4 categories.

    Args:
        failures: Strategy-chain failure reasons (from ToolCallParseError msg).
        partial_tool_name: Extracted tool name (may be None).
        available_tool_names: Registered tool names for membership check.

    Returns:
        The most specific ``ErrorCategory`` that fits.
    """
    try:
        if partial_tool_name and available_tool_names is not None:
            if partial_tool_name in available_tool_names:
                return ErrorCategory.ARGUMENT_ERROR
            return ErrorCategory.UNKNOWN_TOOL

        if failures:
            joined = " ".join(failures).lower()
            # Match actual JSON error indicators, not strategy names like "standard_json"
            json_error_indicators = [
                "jsondecode", "json.decoder", "unterminated", "expecting",
                "invalid escape", "expecting property name",
            ]
            if any(indicator in joined for indicator in json_error_indicators):
                return ErrorCategory.JSON_SYNTAX_ERROR

        return ErrorCategory.FORMAT_NOT_FOUND
    except Exception:
        return ErrorCategory.FORMAT_NOT_FOUND


def extract_tool_info(
    failures: list[str] | None = None,
    raw_text: str | None = None,
    available_tool_names: list[str] | None = None,
) -> Optional[str]:
    """3-level extraction: strategy chain failures -> regex fallback.

    Level 2 (strategy chain): scan *failures* for "tool 'X' not in …".
    Level 3 (raw text regex): scan *raw_text* with PARTIAL_TOOL_NAME_PATTERNS.

    Level 1 (structured data from native path) is handled by the caller, not
    this function.

    Returns:
        Extracted tool name or ``None``.
    """
    try:
        # Level 2: extract from failures list
        if failures:
            for f in failures:
                m = _FAILURE_TOOL_NAME_RE.search(f)
                if m:
                    return m.group(1)

        # Level 3: regex scan on raw LLM output
        if raw_text:
            for pattern in PARTIAL_TOOL_NAME_PATTERNS:
                m = pattern.search(raw_text)
                if m:
                    name = m.group(1)
                    # Optionally validate against available tools
                    if available_tool_names is not None and name in available_tool_names:
                        return name
                    # Return even if not validated — diagnostics only
                    if available_tool_names is None:
                        return name
                    # Keep searching for a matching name
                    continue

            # Second pass: accept any match if nothing validated
            if available_tool_names is not None:
                for pattern in PARTIAL_TOOL_NAME_PATTERNS:
                    m = pattern.search(raw_text)
                    if m:
                        return m.group(1)

        return None
    except Exception:
        return None


def extract_category_from_error(error_message: str) -> Optional[ErrorCategory]:
    """Extract ``[CATEGORY:XXX]`` tag from an error message string."""
    try:
        m = _CATEGORY_TAG_PATTERN.search(error_message)
        if m:
            tag = m.group(1)
            try:
                return ErrorCategory(tag)
            except ValueError:
                return None
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
#  Recovery message generation
# ---------------------------------------------------------------------------

_TOOL_CALL_JSON_EXAMPLE = '{"name": "<tool_name>", "arguments": {"<param>": "<value>"}}'


def format_tool_list(
    tool_names: list[str] | None,
    tool_descriptions: dict[str, str] | None = None,
) -> str:
    """Format available tools into a compact text block.

    Args:
        tool_names: List of registered tool names.
        tool_descriptions: Optional mapping of tool name -> one-line description.

    Returns:
        Formatted string (may be empty if *tool_names* is empty/None).
    """
    try:
        if not tool_names:
            return ""
        lines: list[str] = []
        for name in tool_names:
            if tool_descriptions and name in tool_descriptions:
                lines.append(f"- {name}: {tool_descriptions[name]}")
            else:
                lines.append(f"- {name}")
        return "\n".join(lines)
    except Exception:
        return ""


def build_recovery_message(
    consecutive_errors: int,
    error_category: ErrorCategory | None = None,
    last_output_snippet: str | None = None,
    available_tool_names: list[str] | None = None,
    tool_descriptions: dict[str, str] | None = None,
    partial_tool_name: str | None = None,
) -> str:
    """Build a progressive recovery message based on consecutive error count.

    Levels:
        1 (1st error):  Category-aware format guidance (~150 tokens)
        2 (2nd error):  Enhanced diagnosis (~300 tokens)
        3 (3-4 errors): Approach switch suggestion (~200 tokens, shorter than L2)
        4 (5+ errors):  Minimal reminder (~100 tokens)

    Returns:
        Recovery message text, or empty string if *consecutive_errors* <= 0.
    """
    try:
        if consecutive_errors <= 0:
            return ""
        if consecutive_errors == 1:
            return _level1_format_guidance(
                error_category, available_tool_names, partial_tool_name,
            )
        if consecutive_errors == 2:
            return _level2_enhanced_diagnosis(
                error_category, last_output_snippet, available_tool_names, tool_descriptions
            )
        if consecutive_errors <= 4:
            return _level3_approach_switch(consecutive_errors, available_tool_names)
        return _level4_minimal_reminder(consecutive_errors)
    except Exception:
        # Absolute fallback — never block the main flow
        return f"FORMAT: {_TOOL_CALL_JSON_EXAMPLE}"


def _level1_format_guidance(
    error_category: ErrorCategory | None,
    available_tool_names: list[str] | None,
    partial_tool_name: str | None = None,
) -> str:
    """Level 1: category-aware format guidance with JSON example and tool list.

    Generates category-specific messages so the LLM gets actionable feedback
    on the first error.  Falls back to a generic format reminder when no
    category is available.
    """
    tool_list = ", ".join(available_tool_names) if available_tool_names else "N/A"
    fmt = _TOOL_CALL_JSON_EXAMPLE
    suffix = f"\nAvailable tools: {tool_list}"

    if error_category == ErrorCategory.UNKNOWN_TOOL:
        tool_ref = f"'{partial_tool_name}' " if partial_tool_name else ""
        return (
            f"Tool {tool_ref}does not exist. "
            f"Use one of the available tools.\n"
            f"Correct format:\n{fmt}{suffix}"
        )
    if error_category == ErrorCategory.ARGUMENT_ERROR:
        tool_ref = f"'{partial_tool_name}' " if partial_tool_name else ""
        return (
            f"Tool {tool_ref}received invalid arguments. "
            f"Check parameter names and types.\n"
            f"Correct format:\n{fmt}{suffix}"
        )
    if error_category == ErrorCategory.JSON_SYNTAX_ERROR:
        return (
            f"Your tool call has JSON syntax errors. "
            f"Correct format:\n{fmt}{suffix}"
        )
    if error_category == ErrorCategory.FORMAT_NOT_FOUND:
        return (
            f"Your output did not contain a valid tool call. "
            f"You must respond with a JSON tool call:\n{fmt}{suffix}"
        )
    # None / unknown category — generic fallback
    return (
        f"TOOL FORMAT REMINDER: You must output a JSON tool call:\n"
        f"{fmt}{suffix}"
    )


def _level2_enhanced_diagnosis(
    error_category: ErrorCategory | None,
    last_output_snippet: str | None,
    available_tool_names: list[str] | None,
    tool_descriptions: dict[str, str] | None,
) -> str:
    """Level 2: enhanced diagnosis with error type, correct format only (no wrong examples)."""
    parts: list[str] = []

    # Diagnosis
    cat_name = error_category.value if error_category else "UNKNOWN"
    parts.append(f"DIAGNOSIS: {cat_name}")

    if last_output_snippet:
        snippet = last_output_snippet[:200] + "..." if len(last_output_snippet) > 200 else last_output_snippet
        parts.append(f"Your output contained: {snippet}")

    # Correct format example
    parts.append("CORRECT format:")
    parts.append(_TOOL_CALL_JSON_EXAMPLE)

    # Tool list with descriptions
    if available_tool_names:
        parts.append("Available tools with params:")
        for name in available_tool_names:
            desc = tool_descriptions.get(name, "") if tool_descriptions else ""
            if desc:
                parts.append(f"- {name}: {desc}")
            else:
                parts.append(f"- {name}")

    return "\n".join(parts)


def _level3_approach_switch(
    consecutive_errors: int,
    available_tool_names: list[str] | None,
) -> str:
    """Level 3: approach switch suggestion — intentionally shorter than Level 2."""
    tool_list = ", ".join(available_tool_names) if available_tool_names else "N/A"
    return (
        f"CRITICAL: {consecutive_errors} consecutive format errors. "
        f"You MUST change your approach.\n"
        f"Try a DIFFERENT tool or simplify your request.\n"
        f"Format: {_TOOL_CALL_JSON_EXAMPLE}\n"
        f"Available tools: {tool_list}"
    )


def _level4_minimal_reminder(consecutive_errors: int) -> str:
    """Level 4+: minimal format template, loop indefinitely (no forced final_answer)."""
    return (
        f"FORMAT: {_TOOL_CALL_JSON_EXAMPLE}"
    )


# ---------------------------------------------------------------------------
#  Error message consolidation
# ---------------------------------------------------------------------------

def consolidate_error_messages(
    messages: list[dict[str, Any]],
    consecutive_error_count: int = 0,
    recovery_message: str = "",
    max_full_errors: int = 1,
) -> list[dict[str, Any]]:
    """Consolidate consecutive error messages in a message list.

    Scans *messages* from the end, identifying consecutive TOOL_RESPONSE
    messages that contain "Error:" prefix.  Keeps the latest
    ``max_full_errors`` messages intact (with "Now let's retry…" replaced by
    *recovery_message*); compresses earlier ones to compact summaries.

    Non-consecutive error sequences (separated by successful steps) are
    handled independently.

    Args:
        messages: List of message dicts (``{"role": ..., "content": ...}``).
        consecutive_error_count: How many consecutive errors have been detected
            (used to decide whether to apply replacement).
        recovery_message: Text to replace "Now let's retry…" suffix with.
        max_full_errors: How many recent error messages to keep in full.

    Returns:
        New message list (original is not mutated).
    """
    try:
        if not messages:
            return messages

        messages = list(messages)  # shallow copy

        # Find trailing consecutive error TOOL_RESPONSE indices (from end)
        error_indices: list[int] = []
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            role = _get_role(msg)
            if role in ("tool-response", "tool_response"):
                text = _get_content_text(msg)
                if text and text.startswith("Error:"):
                    error_indices.append(i)
                    continue
            # Non-error message or different role breaks the streak
            break

        if not error_indices:
            return messages

        # error_indices is in reverse order (newest first)
        # Keep the last max_full_errors in full, compress the rest
        to_keep = error_indices[:max_full_errors]
        to_compress = error_indices[max_full_errors:]

        # Replace "Now let's retry…" in the newest error with recovery message
        if to_keep and recovery_message and consecutive_error_count > 0:
            newest_idx = to_keep[0]
            msg = messages[newest_idx]
            text = _get_content_text(msg)
            if text:
                new_text = _replace_retry_suffix(text, recovery_message)
                messages[newest_idx] = _set_content_text(msg, new_text)

        # Compress older error messages to 1-line summaries
        for idx in to_compress:
            msg = messages[idx]
            text = _get_content_text(msg)
            category = _extract_error_category_from_text(text)
            summary = f"[Parse error: {category}]"
            messages[idx] = _set_content_text(msg, summary)

        return messages
    except Exception:
        # Safety: return original messages on any failure
        return messages


def _replace_retry_suffix(text: str, replacement: str) -> str:
    """Replace the 'Now let's retry…' suffix with *replacement*.

    If the suffix is not found, return *text* unchanged (safe fallback).
    """
    idx = text.find(NOW_LETS_RETRY_PREFIX)
    if idx == -1:
        # Suffix not found — append recovery as a separate section
        return text + "\n" + replacement
    return text[:idx] + replacement


def _extract_error_category_from_text(text: str | None) -> str:
    """Try to extract a [CATEGORY:…] tag; fall back to a generic label."""
    if not text:
        return "PARSE_ERROR"
    m = _CATEGORY_TAG_PATTERN.search(text)
    if m:
        return m.group(1)
    return "PARSE_ERROR"


# ---------------------------------------------------------------------------
#  Message helpers (framework-agnostic)
# ---------------------------------------------------------------------------

def _get_role(msg: Any) -> str:
    """Extract role string from a message (ChatMessage or dict)."""
    if isinstance(msg, dict):
        role = msg.get("role", "")
        return role.value if hasattr(role, "value") else str(role)
    role = getattr(msg, "role", "")
    return role.value if hasattr(role, "value") else str(role)


def _get_content_text(msg: Any) -> str:
    """Extract plain text from a message content field."""
    if isinstance(msg, dict):
        content = msg.get("content", "")
    else:
        content = getattr(msg, "content", "")

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return str(content)


def _set_content_text(msg: Any, text: str) -> Any:
    """Return a copy of *msg* with content replaced by *text*.

    Works for both dict messages and ChatMessage objects.
    """
    if isinstance(msg, dict):
        new_msg = dict(msg)
        new_msg["content"] = [{"type": "text", "text": text}]
        return new_msg

    # ChatMessage-like object — create a simple dict representation
    role = msg.role if hasattr(msg, "role") else "tool-response"
    role_str = role.value if hasattr(role, "value") else str(role)
    return {
        "role": role_str,
        "content": [{"type": "text", "text": text}],
    }
