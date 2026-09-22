"""
Smol model-feedback recovery and message consolidation.

The feedback wording and message markers are part of smol execution policy.
Helpers accept basic types so checkpoint/model-protocol replay can reuse them.
"""

from __future__ import annotations

import enum
from typing import Any

from agentloom.execution.logging import get_logger

_LOG = get_logger(__name__)

# ---------------------------------------------------------------------------
#  Error classification
# ---------------------------------------------------------------------------

NOW_LETS_RETRY_PREFIX = "Now let's retry"
RUNTIME_FEEDBACK_RAW_KEY = "agentloom_runtime_feedback"


class ErrorCategory(enum.Enum):
    """Native tool-call protocol errors recoverable by another model turn."""

    NATIVE_TOOL_CALL_REQUIRED = "NATIVE_TOOL_CALL_REQUIRED"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    ARGUMENT_ERROR = "ARGUMENT_ERROR"


def extract_category_from_error(error_message: str) -> ErrorCategory | None:
    """Classify a native tool-call validation error without parsing model text."""

    normalized = (error_message or "").lower()
    if "not found in registered tools" in normalized:
        return ErrorCategory.UNKNOWN_TOOL
    if "arguments must be a json object" in normalized:
        return ErrorCategory.ARGUMENT_ERROR
    if "native structured tool_calls" in normalized or "no tool call" in normalized:
        return ErrorCategory.NATIVE_TOOL_CALL_REQUIRED
    return None


# ---------------------------------------------------------------------------
#  Recovery message generation
# ---------------------------------------------------------------------------

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
        1 (1st error): category-aware native-call guidance
        2 (2nd error): diagnosis and available tools
        3+ (later errors): concise native-call reminder

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
        return _level3_native_call_reminder(consecutive_errors, available_tool_names)
    except Exception:
        return "Call one available tool using the provider's native structured tool-call mechanism."


def _level1_format_guidance(
    error_category: ErrorCategory | None,
    available_tool_names: list[str] | None,
    partial_tool_name: str | None = None,
) -> str:
    """Level 1: category-aware guidance for provider-native tool calls."""
    tool_list = ", ".join(available_tool_names) if available_tool_names else "N/A"
    suffix = f"\nAvailable tools: {tool_list}"

    if error_category == ErrorCategory.UNKNOWN_TOOL:
        tool_ref = f"'{partial_tool_name}' " if partial_tool_name else ""
        return (
            f"Tool {tool_ref}does not exist. "
            f"Call one of the available tools using the provider's native "
            f"structured tool-call mechanism.{suffix}"
        )
    if error_category == ErrorCategory.ARGUMENT_ERROR:
        tool_ref = f"'{partial_tool_name}' " if partial_tool_name else ""
        return (
            f"Tool {tool_ref}received invalid arguments. "
            f"Check its schema, then issue a native structured tool call.{suffix}"
        )
    if error_category == ErrorCategory.NATIVE_TOOL_CALL_REQUIRED:
        return (
            "Your response did not contain a provider-native structured tool call. "
            f"Do not describe or serialize a call in assistant text.{suffix}"
        )
    return (
        "Call one available tool using the provider's native structured "
        f"tool-call mechanism.{suffix}"
    )


def _level2_enhanced_diagnosis(
    error_category: ErrorCategory | None,
    last_output_snippet: str | None,
    available_tool_names: list[str] | None,
    tool_descriptions: dict[str, str] | None,
) -> str:
    """Level 2: concise diagnosis without any text-call encoding example."""
    parts: list[str] = []

    # Diagnosis
    cat_name = error_category.value if error_category else "UNKNOWN"
    parts.append(f"DIAGNOSIS: {cat_name}")

    if last_output_snippet:
        snippet = last_output_snippet[:200] + "..." if len(last_output_snippet) > 200 else last_output_snippet
        parts.append(f"Your output contained: {snippet}")

    parts.append(
        "Issue the next call through the provider's native structured "
        "tool-call mechanism; do not write JSON, XML, or prose that describes a call."
    )

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


def _level3_native_call_reminder(
    consecutive_errors: int,
    available_tool_names: list[str] | None,
) -> str:
    """Level 3+: short native-call reminder."""
    tool_list = ", ".join(available_tool_names) if available_tool_names else "N/A"
    return (
        f"{consecutive_errors} consecutive native tool-call errors. "
        "Use the provider's structured tool-call mechanism, not assistant text.\n"
        f"Available tools: {tool_list}"
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

        for idx in error_indices:
            messages[idx] = _mark_runtime_feedback(messages[idx])

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
    """Classify a retained native tool-call error for compact history."""

    category = extract_category_from_error(text or "")
    return category.value if category is not None else "NATIVE_TOOL_CALL_ERROR"


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
        "raw": dict(getattr(msg, "raw", None) or {}),
    }


def _mark_runtime_feedback(msg: Any) -> Any:
    """Mark a smolagents parsing-error message without changing provider text."""

    if isinstance(msg, dict):
        marked = dict(msg)
        raw = marked.get("raw")
        marked["raw"] = dict(raw) if isinstance(raw, dict) else {}
        marked["raw"][RUNTIME_FEEDBACK_RAW_KEY] = True
        return marked

    role = msg.role if hasattr(msg, "role") else "tool-response"
    role_str = role.value if hasattr(role, "value") else str(role)
    raw = getattr(msg, "raw", None)
    marked_raw = dict(raw) if isinstance(raw, dict) else {}
    marked_raw[RUNTIME_FEEDBACK_RAW_KEY] = True
    return {
        "role": role_str,
        "content": getattr(msg, "content", ""),
        "raw": marked_raw,
    }
