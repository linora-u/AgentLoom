"""Context Compression Pipeline for Conversation History Management.

This module implements a multi-layer compression pipeline that keeps the
conversation token count within a configurable budget.  Each layer is
progressively more aggressive; the pipeline short-circuits as soon as the
token count falls within limits.

Compression Pipeline (executed in order by ``get_compressed_messages``):
=========================================================================

  Layer 1 – File Read Deduplication  (``_apply_tool_dedup``)
      Detects repeated reads of the same file (read_file,
      read_file, get_file_outline).  All but the latest response
      for each file are replaced with a short placeholder.
      *Idempotent – running twice has no additional effect.*

  Layer 2 – Reversible ContextEngine Compression  (``_apply_context_engine_compression``)
      Stores large tool responses in the active task-scoped ContextStore and
      replaces visible content with a compact preview plus ContextRef.  The
      original content remains retrievable through ``loom_retrieve_context``.
      *Idempotent.*

  Layer 3 – Tool Output Hard Truncation  (``_apply_tool_output_truncation``)
      Caps excessively long TOOL_RESPONSE content to per-tool character
      limits (e.g. shell_tool → 2 000 chars, ripgrep → 3 000 chars).
      Head + tail are kept; the middle is replaced with a truncation notice.
      *Idempotent.*

  Layer 4 – Observation Masking  (``_apply_observation_masking``)
      Inspired by OpenHands' ``ObservationMaskingCondenser``.  The oldest
      ``TRUNCATION_FRAC_TO_REMOVE`` fraction of visible tool responses are
      replaced with a short placeholder, while the corresponding tool-call
      messages are left intact to preserve the conversation structure.
      *Idempotent.*

  Layer 5 – LLM Summarization  (``summarize_conversation``)
      Sends the conversation to a summary model to produce a condensed
      replacement.  Gated behind the ``smart_summary`` flag.
      *NOT idempotent – costs an LLM call.*

  Fallback – Sliding-Window Truncation  (``truncate_until_fits``)
      Repeatedly hides the oldest visible message pairs (user/assistant or
      tool-call/tool-response) and inserts a truncation marker.  When
      message-level truncation can no longer remove pairs, a content-level
      fallback masks the oldest remaining tool response one at a time.

Key design decisions:
  • Each layer checks the token count after execution and returns early
    if the budget is satisfied — no unnecessary downstream work.
  • Two tunable constants: ``TRUNCATION_FRAC_TO_REMOVE`` (default 0.3) and
    ``MAX_TRUNCATION_ROUNDS`` (default 30, safety cap for the fallback loop).
  • The fallback loop terminates when the budget is met, all strategies are
    exhausted, or the safety cap is reached (with a WARNING log).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import re
import time
import uuid
from typing import Iterable, Optional, List, Union

from litellm.utils import token_counter
from smolagents import AgentLogger
from smolagents.models import ChatMessage, MessageRole
from src.lib.config.defaults import DEFAULT_MAX_TOKENS
from src.lib.context_engine.engine import CONTEXT_REF_PREFIX
from src.lib.context_engine.runtime import get_current_context_engine
from src.lib.logging import get_logger

# ===========================================================================
# Global Configuration
# ===========================================================================
# Fraction of visible messages (or tool responses) to process per compression
# pass.  This single constant drives:
#   • Layer 1  – savings threshold to skip deeper layers
#   • Layer 3  – fraction of old tool responses to mask
#   • Fallback – fraction of visible messages to hide per truncation pass
TRUNCATION_FRAC_TO_REMOVE: float = 0.3

# Safety cap for the Fallback truncation loop (``truncate_until_fits``).
# With frac_to_remove=0.3, 10 rounds already reduce visible messages to ~2.8%
# of the original count.  30 rounds is extremely generous; exceeding it means
# the compression strategy has effectively failed.
MAX_TRUNCATION_ROUNDS: int = 30

# ===========================================================================
# Layer 1 Constants – File Read Deduplication
# ===========================================================================
# Placeholder text injected in place of older, duplicated file-read outputs.
FILE_DEDUP_PLACEHOLDER: str = (
    "[Notice: The content of this file has been omitted because it is read again later in the conversation. "
    "Please refer to the latest read operation for the most up-to-date content.]"
)

# Regex patterns used to extract file paths from tool-call text.
# Each key corresponds to a file-reading tool name.
TOOL_DEDUP_PATTERNS: dict[str, re.Pattern] = {
    "read_file": re.compile(
        r'read_file\s*\(\s*(?:f?["\']([^"\'\n]+)["\'])',
        re.IGNORECASE,
    ),
    "get_file_outline": re.compile(
        r'get_file_outline\s*\(\s*(?:f?["\']([^"\'\n]+)["\'])',
        re.IGNORECASE,
    ),
}
FILE_READ_TOOL_NAMES: frozenset[str] = frozenset(TOOL_DEDUP_PATTERNS)

# ===========================================================================
# Layer 2 Constants – Tool Output Hard Truncation
# ===========================================================================
# Per-tool character limits.  ``None`` means the tool is exempt from hard
# truncation (e.g. read_file is handled by Layer 1 deduplication).
# Tools not listed here fall back to the ``"default"`` limit.
TOOL_MAX_RETAIN_CHARS: dict[str, Union[int, None]] = {
    "shell_tool": 2000,
    "glob_search": 1500,
    "grep_search": 3000,
    "read_file": None,
    "get_file_outline": None,
    "python_interpreter": 3000,
    "default": 3000,
}
AST_TOOL_CALL_NAME_ALLOWLIST: frozenset[str] = frozenset(TOOL_MAX_RETAIN_CHARS) | frozenset({
    "get_file_outline",
    "read_file_content",
    "ripgrep_search_directory",
    "list_files_glob",
    "list_directory",
    "write_file",
    "write_markdown_file",
})

# ===========================================================================
# Layer 3 Constants – Observation Masking
# ===========================================================================
# Placeholder text injected in place of masked old tool-response content.
# Inspired by OpenHands ``ObservationMaskingCondenser``.
OBSERVATION_MASKING_PLACEHOLDER: str = (
    "[Observation masked: content omitted to save context. "
    "The tool call above is preserved for conversation continuity.]"
)

# Tool names whose TOOL_RESPONSE should be exempt from observation masking
# (Layer 3) and content-level fallback truncation.  If the preceding TOOL_CALL
# contains any of these names in its text, the response is preserved.
COMPRESSION_EXEMPT_TOOL_NAMES: frozenset[str] = frozenset({
    "load_skill",
})

# Number of recent error-related TOOL_RESPONSE messages to exempt from
# Layer 3 masking and Fallback content-level truncation.  This protects
# the latest error recovery guidance from being compressed away.
RECENT_ERROR_EXEMPT_COUNT: int = 1

# ===========================================================================
# Layer 4 Constants – LLM Summarization
# ===========================================================================
from src.lib.smolagents.models.model_manager import model_manager
from src.lib.smolagents.models.model_types import ModelType

# LLM system prompt and condensation instructions for Layer 4.
SUMMARY_SYSTEM_PROMPT = """You are a helpful AI assistant tasked with summarizing conversations.

CRITICAL: This is a summarization-only request. DO NOT call any tools or functions.
Your ONLY task is to analyze the conversation and produce a text summary.
Respond with text only - no tool calls will be processed.

CRITICAL: This summarization request is a SYSTEM OPERATION, not a user message.
When analyzing "user requests" and "user intent", completely EXCLUDE this summarization message.
The "most recent user request" and "next step" must be based on what the user was doing BEFORE this system message appeared.
The goal is for work to continue seamlessly after condensation - as if it never happened.
"""

CONDENSE_INSTRUCTION = """CRITICAL: This summarization request is a SYSTEM OPERATION, not a user message.
When analyzing "user requests" and "user intent", completely EXCLUDE this summarization message.
The "most recent user request" and "Optional Next Step" must be based on what the user was doing BEFORE this system message appeared.
The goal is for work to continue seamlessly after condensation - as if it never happened.

Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts and ensure you've covered all necessary points. In your analysis process:

1. Chronologically analyze each message and section of the conversation. For each section thoroughly identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details like:
     - file names
     - full code snippets
     - function signatures
     - file edits
   - Errors that you ran into and how you fixed them
   - Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
2. Double-check for technical accuracy and completeness, addressing each required element thoroughly.

Your summary should include the following sections:

1. Primary Request and Intent: Capture all of the user's explicit requests and intents in detail
2. Key Technical Concepts: List all important technical concepts, technologies, and frameworks discussed.
3. Files and Code Sections: Enumerate specific files and code sections examined, modified, or created. Pay special attention to the most recent messages and include full code snippets where applicable and include a summary of why this file read or edit is important.
4. Errors and fixes: List all errors that you ran into, and how you fixed them. Pay special attention to specific user feedback that you received, especially if the user told you to do something differently.
5. Problem Solving: Document problems solved and any ongoing troubleshooting efforts.
6. All user messages: List ALL user messages that are not tool results. These are critical for understanding the users' feedback and changing intent.
7. Pending Tasks: Outline any pending tasks that you have explicitly been asked to work on.
8. Current Work: Describe in detail precisely what was being worked on immediately before this summary request, paying special attention to the most recent messages from both user and assistant. Include file names and code snippets where applicable.
9. Optional Next Step: List the next step that you will take that is related to the most recent work you were doing. IMPORTANT: ensure that this step is DIRECTLY in line with the user's most recent explicit requests, and the task you were working on immediately before this summary request. If your last task was concluded, then only list next steps if they are explicitly in line with the users request. Do not start on tangential requests or really old requests that were already completed without confirming with the user first.

If there is a next step, include direct quotes from the most recent conversation showing exactly what task you were working on and where you left off. This should be verbatim to ensure there's no drift in task interpretation.

Here's an example of how your output should be structured:

<example>
<analysis>
[Your thought process, ensuring all points are covered thoroughly and accurately]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]
   - [...]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary of why this file is important]
      - [Summary of the changes made to this file, if any]
      - [Important Code Snippet]
   - [File Name 2]
      - [Important Code Snippet]
   - [...]

4. Errors and fixes:
   - [Detailed description of error 1]:
      - [How you fixed the error]
      - [User feedback on the error if any]
   - [...]

5. Problem Solving:
   [Description of solved problems and ongoing troubleshooting]

6. All user messages:
   - [Detailed non tool use user message]
   - [...]

7. Pending Tasks:
   - [Task 1]
   - [Task 2]
   - [...]

8. Current Work:
   [Precise description of current work]

9. Optional Next Step:
   [Optional Next step to take]

</summary>
</example>

Please provide your summary based on the conversation so far, following this structure and ensuring precision and thoroughness in your response.

Note: Any <command> blocks from the original task will be automatically appended to your summary wrapped in <system-reminder> tags. You do not need to include them in your summary text.
"""

# Tool responses are useful to summaries, but raw multi-thousand-line outputs
# can make the summarization request fail before compaction can help.
SUMMARY_TOOL_OUTPUT_MAX_CHARS: int = 2000


# ===========================================================================
# Data Classes
# ===========================================================================

@dataclass
class InternalChatMessage:
    """Wraps a ``ChatMessage`` with compression metadata.

    Attributes:
        truncation_parent: If set, this message has been hidden by a truncation
            pass identified by this UUID.
        is_truncation_marker: True for synthetic "[messages hidden]" markers.
        condense_id: If set, this message has been replaced by an LLM summary.
        is_summary: True for synthetic summary messages created by Layer 4.
        ts: Timestamp for ordering.
    """
    message: ChatMessage

    truncation_parent: Optional[str] = None
    is_truncation_marker: bool = False
    truncation_id: Optional[str] = None

    condense_id: Optional[str] = None
    is_summary: bool = False

    ts: Optional[float] = None

    @classmethod
    def from_chat_message(cls, msg: ChatMessage, ts: Optional[float] = None) -> InternalChatMessage:
        return cls(message=msg, ts=ts or time.time())

    def to_chat_message(self) -> ChatMessage:
        return self.message

    def is_visible(self) -> bool:
        return not self.truncation_parent and not self.condense_id


@dataclass
class SummarizeResponse:
    """Return type of ``summarize_conversation`` (Layer 4)."""
    messages: List[InternalChatMessage]
    summary: str
    cost: float = 0.0
    new_context_tokens: Optional[int] = None
    error: Optional[str] = None
    error_details: Optional[str] = None


@dataclass
class TruncationResult:
    """Return type of ``truncate_conversation`` (Fallback)."""
    messages: List[InternalChatMessage]
    truncation_id: str
    messages_removed: int


@dataclass
class ContextBudgetConfig:
    max_tokens: int = DEFAULT_MAX_TOKENS
    target_ratio: float = 0.5
    keep_recent: int = 3
    max_compression_steps: int = 3

    @property
    def target_tokens(self) -> int:
        return int(self.max_tokens * self.target_ratio)

    @classmethod
    def default(cls) -> "ContextBudgetConfig":
        return cls()


@dataclass(frozen=True)
class ToolInvocation:
    """A normalized tool call extracted from text, native metadata, or CodeAct code."""

    name: str
    arguments: Optional[str] = None
    dedup_key: Optional[str] = None


@dataclass(frozen=True)
class ToolResponsePair:
    """Visible tool-call/tool-response pair with original message indexes."""

    call_index: int
    response_index: int
    invocations: tuple[ToolInvocation, ...]


@dataclass(frozen=True)
class VisibleMessageGroup:
    """Visible non-system messages that must be truncated together."""

    indices: tuple[int, ...]


# ===========================================================================
# Utility / Helper Functions
# ===========================================================================

def _extract_content_text(content: object) -> str:
    """Extract plain text from a message content field (str, list-of-dicts, or None)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and "text" in item:
                    parts.append(str(item["text"]))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _role_value(role: object) -> str:
    return role.value if hasattr(role, "value") else str(role)


def _is_tool_call_role(role: object) -> bool:
    return _role_value(role) in ("tool-call", "tool_call")


def _is_tool_response_role(role: object) -> bool:
    return _role_value(role) in ("tool-response", "tool_response")


def _clone_internal_message_with_content(
    internal_msg: InternalChatMessage,
    text: str,
) -> InternalChatMessage:
    return InternalChatMessage(
        message=ChatMessage(
            role=internal_msg.message.role,
            content=[{"type": "text", "text": text}],
            tool_calls=internal_msg.message.tool_calls,
            raw=internal_msg.message.raw,
            token_usage=internal_msg.message.token_usage,
        ),
        truncation_parent=internal_msg.truncation_parent,
        is_truncation_marker=internal_msg.is_truncation_marker,
        truncation_id=internal_msg.truncation_id,
        condense_id=internal_msg.condense_id,
        is_summary=internal_msg.is_summary,
        ts=internal_msg.ts,
    )


def _extract_tool_payload(text: str) -> tuple[Optional[str], object]:
    if not text:
        return None, None

    candidates = [text]
    if "Calling tools:" in text:
        candidates.append(text.split("Calling tools:", 1)[1].strip())

    for candidate in candidates:
        try:
            payload = ast.literal_eval(candidate)
        except Exception:
            continue

        if isinstance(payload, dict):
            function_payload = payload.get("function")
            if isinstance(function_payload, dict):
                name = function_payload.get("name")
                arguments = function_payload.get("arguments")
            else:
                name = payload.get("name") or payload.get("tool")
                arguments = payload.get("arguments")

            if isinstance(name, str):
                return name.strip().lower(), arguments

        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                function_payload = item.get("function")
                if isinstance(function_payload, dict):
                    name = function_payload.get("name")
                    arguments = function_payload.get("arguments")
                else:
                    name = item.get("name") or item.get("tool")
                    arguments = item.get("arguments")
                if isinstance(name, str):
                    return name.strip().lower(), arguments

    return None, None


def _tool_invocation_from_name_args(
    name: object,
    arguments: object = None,
) -> Optional[ToolInvocation]:
    if not isinstance(name, str) or not name.strip():
        return None

    normalized_name = name.strip().lower()
    if isinstance(arguments, str):
        normalized_arguments = arguments
    elif arguments is None:
        normalized_arguments = None
    else:
        try:
            normalized_arguments = json.dumps(arguments, ensure_ascii=True, sort_keys=True)
        except Exception:
            normalized_arguments = str(arguments)

    dedup_key = normalized_arguments if normalized_name in FILE_READ_TOOL_NAMES else None
    return ToolInvocation(
        name=normalized_name,
        arguments=normalized_arguments,
        dedup_key=dedup_key,
    )


def _extract_native_tool_invocations(msg: ChatMessage) -> list[ToolInvocation]:
    invocations: list[ToolInvocation] = []
    for call in getattr(msg, "tool_calls", None) or []:
        if isinstance(call, dict):
            function = call.get("function")
            if isinstance(function, dict):
                name = function.get("name")
                arguments = function.get("arguments")
            else:
                name = call.get("name") or call.get("tool")
                arguments = call.get("arguments")
        else:
            function = getattr(call, "function", None)
            name = getattr(function, "name", None) or getattr(call, "name", None)
            arguments = getattr(function, "arguments", None) or getattr(call, "arguments", None)
        invocation = _tool_invocation_from_name_args(name, arguments)
        if invocation:
            invocations.append(invocation)
    return invocations


def _get_ast_call_name(node: ast.AST) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id.lower()
    if isinstance(node, ast.Attribute):
        return node.attr.lower()
    return None


def _normalize_ast_value(node: ast.AST) -> object:
    try:
        return ast.literal_eval(node)
    except Exception:
        if hasattr(ast, "unparse"):
            return ast.unparse(node)
        return ast.dump(node, include_attributes=False)


def _extract_tool_calls_from_source(source: str) -> List[tuple[str, str]]:
    if not isinstance(source, str) or not source.strip():
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    calls: List[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        tool_name = _get_ast_call_name(node.func)
        if not tool_name:
            continue
        if tool_name not in AST_TOOL_CALL_NAME_ALLOWLIST and not tool_name.endswith(("_tool", "_search")):
            continue

        call_payload = {
            "args": [_normalize_ast_value(arg) for arg in node.args],
            "kwargs": {
                (keyword.arg or "**kwargs"): _normalize_ast_value(keyword.value)
                for keyword in node.keywords
            },
        }
        calls.append((tool_name, json.dumps(call_payload, ensure_ascii=True, sort_keys=True)))

    return calls


def _invocations_from_python_source(source: str) -> list[ToolInvocation]:
    invocations: list[ToolInvocation] = []
    for tool_name, arguments in _extract_tool_calls_from_source(source):
        invocation = _tool_invocation_from_name_args(tool_name, arguments)
        if invocation:
            invocations.append(invocation)
    return invocations


def _extract_tool_invocations_from_text(text: str) -> list[ToolInvocation]:
    primary_tool, nested_source = _extract_tool_payload(text)
    if primary_tool:
        if primary_tool == "python_interpreter" and isinstance(nested_source, str) and nested_source:
            nested = _invocations_from_python_source(nested_source)
            return nested
        invocation = _tool_invocation_from_name_args(primary_tool, nested_source)
        return [invocation] if invocation else []

    invocations = _invocations_from_python_source(text)
    if invocations:
        return invocations

    # Last-resort fallback for non-Python text snippets that still contain
    # familiar read-file calls.
    fallback: list[ToolInvocation] = []
    for tool_name, pattern in TOOL_DEDUP_PATTERNS.items():
        for match in pattern.findall(text):
            if match:
                fallback.append(
                    ToolInvocation(
                        name=tool_name,
                        arguments=match.strip(),
                        dedup_key=match.strip(),
                    )
                )
    return fallback


def _extract_tool_invocations(msg: ChatMessage) -> list[ToolInvocation]:
    """Extract normalized tool invocations from native metadata or message text."""
    native = _extract_native_tool_invocations(msg)
    if native:
        expanded: list[ToolInvocation] = []
        for invocation in native:
            if invocation.name == "python_interpreter" and invocation.arguments:
                nested = _invocations_from_python_source(invocation.arguments)
                expanded.extend(nested)
            else:
                expanded.append(invocation)
        return expanded

    text = _extract_content_text(msg.content)
    if not text:
        return []
    return _extract_tool_invocations_from_text(text)


def _iter_visible_tool_response_pairs(messages: List[InternalChatMessage]) -> list[ToolResponsePair]:
    visible = [(idx, msg) for idx, msg in enumerate(messages) if msg.is_visible()]
    pairs: list[ToolResponsePair] = []
    for visible_idx, (call_idx, call_msg) in enumerate(visible[:-1]):
        if not _is_tool_call_role(call_msg.message.role):
            continue
        response_idx, response_msg = visible[visible_idx + 1]
        if not _is_tool_response_role(response_msg.message.role):
            continue
        pairs.append(
            ToolResponsePair(
                call_index=call_idx,
                response_index=response_idx,
                invocations=tuple(_extract_tool_invocations(call_msg.message)),
            )
        )
    return pairs


def _iter_visible_non_system_groups(messages: List[InternalChatMessage]) -> list[VisibleMessageGroup]:
    """Group visible non-system messages without splitting tool-call/tool-response pairs."""
    visible = [
        (idx, msg)
        for idx, msg in enumerate(messages)
        if msg.is_visible() and msg.message.role != MessageRole.SYSTEM
        and not msg.is_truncation_marker
    ]
    groups: list[VisibleMessageGroup] = []
    visible_idx = 0
    while visible_idx < len(visible):
        idx, msg = visible[visible_idx]
        if (
            _is_tool_call_role(msg.message.role)
            and visible_idx + 1 < len(visible)
            and _is_tool_response_role(visible[visible_idx + 1][1].message.role)
        ):
            groups.append(VisibleMessageGroup((idx, visible[visible_idx + 1][0])))
            visible_idx += 2
            continue
        groups.append(VisibleMessageGroup((idx,)))
        visible_idx += 1
    return groups


def _build_response_pair_map(messages: List[InternalChatMessage]) -> dict[int, ToolResponsePair]:
    return {pair.response_index: pair for pair in _iter_visible_tool_response_pairs(messages)}


def _is_tool_response_exempt(
    messages: List[InternalChatMessage],
    response_index: int,
    pair_by_response: Optional[dict[int, ToolResponsePair]] = None,
) -> bool:
    pair_by_response = pair_by_response if pair_by_response is not None else _build_response_pair_map(messages)
    pair = pair_by_response.get(response_index)
    if pair:
        if any(invocation.name in COMPRESSION_EXEMPT_TOOL_NAMES for invocation in pair.invocations):
            return True
        call_text = _extract_content_text(messages[pair.call_index].message.content)
        return any(tool_name in call_text for tool_name in COMPRESSION_EXEMPT_TOOL_NAMES)

    if response_index > 0 and _is_tool_call_role(messages[response_index - 1].message.role):
        call_text = _extract_content_text(messages[response_index - 1].message.content)
        return any(tool_name in call_text for tool_name in COMPRESSION_EXEMPT_TOOL_NAMES)
    return False


def _recent_error_response_indices(
    messages: List[InternalChatMessage],
    candidate_indices: list[int],
) -> set[int]:
    exempt_indices: set[int] = set()
    exempt_remaining = RECENT_ERROR_EXEMPT_COUNT
    for response_index in reversed(candidate_indices):
        if exempt_remaining <= 0:
            break
        content_text = _extract_content_text(messages[response_index].message.content)
        if content_text.startswith("Error:"):
            exempt_indices.add(response_index)
            exempt_remaining -= 1
    return exempt_indices


def _is_context_ref_response(text: str) -> bool:
    return text.startswith(CONTEXT_REF_PREFIX) or CONTEXT_REF_PREFIX in text[:200]


def _is_placeholder_response(text: str) -> bool:
    return text in (OBSERVATION_MASKING_PLACEHOLDER, FILE_DEDUP_PLACEHOLDER) or _is_context_ref_response(text)


def _is_group_truncatable(
    messages: List[InternalChatMessage],
    group: VisibleMessageGroup,
    pair_by_response: dict[int, ToolResponsePair],
    error_exempt_indices: set[int],
) -> bool:
    for idx in group.indices:
        if messages[idx].is_summary:
            return False
        if not _is_tool_response_role(messages[idx].message.role):
            continue
        if _is_tool_response_exempt(messages, idx, pair_by_response):
            return False
        if idx in error_exempt_indices:
            return False
    return True


def _extract_dedup_keys_from_tool_call(msg: ChatMessage) -> List[tuple[str, str]]:
    """Extract deduplication keys (like file paths) from a TOOL_CALL message.

    In CodeAct mode the TOOL_CALL content looks like:
        Calling tools: [{'function': {'name': 'python_interpreter', 'arguments': '<python code>'}}]
    We scan the entire text for patterns defined in TOOL_DEDUP_PATTERNS.
    Returns: A list of tuples (tool_name: str, dedup_key: str)
    """
    return [
        (invocation.name, invocation.dedup_key)
        for invocation in _extract_tool_invocations(msg)
        if invocation.name in FILE_READ_TOOL_NAMES and invocation.dedup_key
    ]


# ===========================================================================
# Layer 1 – File Read Deduplication
# ===========================================================================

def _apply_tool_dedup(
    messages: List[InternalChatMessage],
    model_id: str,
    logger: Optional[AgentLogger] = None,
) -> tuple:  # (modified_messages, tokens_saved_ratio)
    """Layer 1: File Read Deduplication.

    Scans TOOL_CALL / TOOL_RESPONSE pairs to detect repeated reads of the
    same file (via read_file, read_file, get_file_outline).
    All but the *latest* response for each file are replaced with
    ``FILE_DEDUP_PLACEHOLDER``.

    Returns:
        (new_messages, saved_ratio) — ``saved_ratio`` = chars_saved / total_chars.
    """
    # -----------------------------------------------------------------------
    # Step 1: build (tool_name, dedup_key) -> [tool_response_original_idx]
    # -----------------------------------------------------------------------
    tool_read_pairs: dict[tuple[str, str], list[int]] = {}

    for pair in _iter_visible_tool_response_pairs(messages):
        for invocation in pair.invocations:
            if invocation.name not in FILE_READ_TOOL_NAMES or not invocation.dedup_key:
                continue
            key = invocation.dedup_key.strip()
            if not key:
                continue
            tool_read_pairs.setdefault((invocation.name, key), []).append(pair.response_index)

    # -----------------------------------------------------------------------
    # Step 2: for tools with overlapping targets >1 time, collect (effective_idx, original_text, new_text)
    # -----------------------------------------------------------------------
    # Map from original message idx -> new observation text (only old reads)
    replacements: dict[int, tuple[str, str]] = {}
    engine = get_current_context_engine()

    for (tool_name, key), response_indices in tool_read_pairs.items():
        if len(response_indices) <= 1:
            continue  # only read once, nothing to deduplicate
        # Keep the LAST read; replace all earlier ones
        for response_idx in response_indices[:-1]:
            if response_idx not in replacements:
                original_text = _extract_content_text(messages[response_idx].message.content)
                ref_preview = None
                if engine is not None:
                    ref_preview = engine.compress_tool_result(
                        original_text,
                        tool_name=tool_name,
                        source=f"file_dedup:{key}",
                    )
                replacements[response_idx] = (
                    original_text,
                    ref_preview or FILE_DEDUP_PLACEHOLDER,
                )

    if not replacements:
        return messages, 0.0

    # -----------------------------------------------------------------------
    # Step 3: create new message list with replacements
    # -----------------------------------------------------------------------
    original_chars = 0
    saved_chars = 0
    new_messages = []

    for idx, msg in enumerate(messages):
        if not msg.is_visible():
            new_messages.append(msg)
            continue

        raw_text = _extract_content_text(msg.message.content)
        original_chars += len(raw_text)

        if idx in replacements:
            original_text, new_text = replacements[idx]
            saved_chars += max(0, len(raw_text) - len(new_text))
            new_messages.append(_clone_internal_message_with_content(msg, new_text))
        else:
            new_messages.append(msg)

    saved_ratio = saved_chars / original_chars if original_chars > 0 else 0.0

    if saved_chars > 0:
        log = get_logger(logger, __name__)
        log.info(
            f"[Tool dedup] Replaced {len(replacements)} old tool calls with placeholders. "
            f"Chars saved: {saved_chars:,} / {original_chars:,} ({saved_ratio:.1%})"
        )

    return new_messages, saved_ratio


def _extract_tools_from_code(code: str) -> List[str]:
    """Identify underlying native tool calls from direct code or a tool payload wrapper."""
    tools: list[str] = []
    for invocation in _extract_tool_invocations_from_text(code):
        if invocation.name not in tools:
            tools.append(invocation.name)
    return tools


# ===========================================================================
# Layer 2 – Reversible ContextEngine Compression
# ===========================================================================

def _apply_context_engine_compression(
    messages: List[InternalChatMessage],
    logger: Optional[AgentLogger] = None,
) -> tuple[list[InternalChatMessage], int]:
    """Compress large tool responses into previews backed by retrievable refs."""
    engine = get_current_context_engine()
    if engine is None:
        return messages, 0

    log = get_logger(logger, __name__)
    replacements: dict[int, str] = {}
    total_saved_chars = 0
    pairs = _iter_visible_tool_response_pairs(messages)
    candidate_indices = [pair.response_index for pair in pairs]
    error_exempt_indices = _recent_error_response_indices(messages, candidate_indices)

    for pair in pairs:
        if pair.response_index in error_exempt_indices:
            continue
        if _is_tool_response_exempt(messages, pair.response_index):
            continue
        response_text = _extract_content_text(messages[pair.response_index].message.content)
        if not response_text or _is_placeholder_response(response_text):
            continue

        tool_name = pair.invocations[0].name if pair.invocations else "default"
        preview = engine.compress_tool_result(
            response_text,
            tool_name=tool_name,
            source=f"conversation_tool_response:{pair.response_index}",
        )
        if preview is None:
            continue
        replacements[pair.response_index] = preview
        total_saved_chars += max(0, len(response_text) - len(preview))

    if not replacements:
        return messages, 0

    new_messages = []
    for idx, msg in enumerate(messages):
        if idx in replacements:
            new_messages.append(_clone_internal_message_with_content(msg, replacements[idx]))
        else:
            new_messages.append(msg)

    log.info(
        "[ContextEngine] Replaced %d tool responses with retrievable previews. Chars saved: %d",
        len(replacements),
        total_saved_chars,
    )
    return new_messages, total_saved_chars


# ===========================================================================
# Layer 3 – Tool Output Hard Truncation
# ===========================================================================

def _apply_tool_output_truncation(
    messages: List[InternalChatMessage],
    logger: Optional[AgentLogger] = None,
) -> tuple:
    """Layer 2: Tool Output Hard Truncation.

    For each TOOL_RESPONSE whose content exceeds the per-tool character limit
    defined in ``TOOL_MAX_RETAIN_CHARS``, keeps head + tail and replaces the
    middle with a truncation notice.

    Returns:
        (new_messages, total_chars_saved).
    """
    log = get_logger(logger, __name__)
    replacements: dict[int, str] = {}
    total_saved_chars = 0

    for pair in _iter_visible_tool_response_pairs(messages):
        tools_used = [invocation.name for invocation in pair.invocations] or ["default"]
        quotas: list[tuple[str, int]] = []
        for tool_name in tools_used:
            quota = TOOL_MAX_RETAIN_CHARS.get(tool_name, TOOL_MAX_RETAIN_CHARS["default"])
            if quota is not None:
                quotas.append((tool_name, quota))

        if not quotas:
            continue

        primary, quota = min(quotas, key=lambda item: item[1])
        response_text = _extract_content_text(messages[pair.response_index].message.content)
        if _is_context_ref_response(response_text):
            continue
        if len(response_text) <= quota:
            continue

        half = quota // 2
        head = response_text[:half]
        tail = response_text[-half:] if half > 0 else ""
        omitted = len(response_text) - quota
        trunc_notice = (
            f"\n\n... [Truncated {omitted:,} characters from {primary} output "
            f"due to TOOL_MAX_RETAIN_CHARS ({quota})] ...\n\n"
        )
        replacements[pair.response_index] = head + trunc_notice + tail
        total_saved_chars += omitted

    if not replacements:
        return messages, 0.0
        
    new_messages = []
    for idx, msg in enumerate(messages):
        if idx in replacements:
            new_messages.append(_clone_internal_message_with_content(msg, replacements[idx]))
        else:
            new_messages.append(msg)
            
    if total_saved_chars > 0:
        log.info(f"[Tool Truncation] Truncated {len(replacements)} overly long tool responses. Chars saved: {total_saved_chars:,}")
        
    return new_messages, total_saved_chars


# ===========================================================================
# Layer 4 – Observation Masking
# ===========================================================================

def _apply_observation_masking(
    messages: List[InternalChatMessage],
    frac_to_mask: float = TRUNCATION_FRAC_TO_REMOVE,
    logger: Optional[AgentLogger] = None,
) -> tuple[list[InternalChatMessage], int]:
    """Layer 3: Observation Masking (inspired by OpenHands ObservationMaskingCondenser).

    Replaces the content of the oldest *frac_to_mask* fraction of visible
    TOOL_RESPONSE messages with ``OBSERVATION_MASKING_PLACEHOLDER``.  The
    corresponding TOOL_CALL messages are left intact so the model can still
    see the call chain.

    Returns:
        (new_messages, total_chars_saved).
    """
    log = get_logger(logger, __name__)

    pair_by_response = _build_response_pair_map(messages)
    tool_response_indices: list[int] = []
    for idx, internal_msg in enumerate(messages):
        if not internal_msg.is_visible() or not _is_tool_response_role(internal_msg.message.role):
            continue
        content_text = _extract_content_text(internal_msg.message.content)
        if not content_text or _is_placeholder_response(content_text):
            continue
        if _is_tool_response_exempt(messages, idx, pair_by_response):
            continue
        tool_response_indices.append(idx)

    if not tool_response_indices:
        return messages, 0

    error_exempt_indices = _recent_error_response_indices(messages, tool_response_indices)
    maskable_indices = [i for i in tool_response_indices if i not in error_exempt_indices]

    num_to_mask = int(len(maskable_indices) * frac_to_mask)
    if num_to_mask <= 0:
        return messages, 0

    indices_to_mask = set(maskable_indices[:num_to_mask])

    total_chars_saved = 0
    new_messages = []
    for idx, internal_msg in enumerate(messages):
        if idx in indices_to_mask:
            original_text = _extract_content_text(internal_msg.message.content)
            chars_saved = len(original_text) - len(OBSERVATION_MASKING_PLACEHOLDER)
            if chars_saved > 0:
                total_chars_saved += chars_saved
            new_messages.append(_clone_internal_message_with_content(internal_msg, OBSERVATION_MASKING_PLACEHOLDER))
        else:
            new_messages.append(internal_msg)

    if total_chars_saved > 0:
        log.info(
            f"[Layer 3] Observation masking: masked {num_to_mask} old tool responses. "
            f"Chars saved: {total_chars_saved:,}"
        )

    return new_messages, total_chars_saved


def _extract_message_text(message: ChatMessage) -> str:
    text = _extract_content_text(message.content)

    tool_text = ""
    if message.tool_calls:
        lines = []
        for call in message.tool_calls:
            payload = {
                "tool": call.function.name,
                "arguments": call.function.arguments,
            }
            lines.append(json.dumps(payload, ensure_ascii=True))
        tool_text = "\n".join(lines)

    if tool_text:
        return f"{text}\n{tool_text}" if text else tool_text
    return text


def get_messages_since_last_summary(messages: List[InternalChatMessage]) -> List[InternalChatMessage]:
    last_summary_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].is_summary:
            last_summary_idx = idx
            break

    if last_summary_idx >= 0:
        return [msg for msg in messages[last_summary_idx:] if msg.is_visible()]
    return [msg for msg in messages if msg.is_visible()]


def _count_tokens(messages: Iterable[ChatMessage], model_id: str) -> int:
    payload = []
    for msg in messages:
        text = _extract_message_text(msg)
        
        role_value = msg.role.value if isinstance(msg.role, MessageRole) else str(msg.role)
        if role_value in {"system", "user", "assistant", "tool"}:
            role = role_value
        elif role_value in {"tool-call", "tool-response"}:
            role = "tool"
        else:
            role = "user"
            
        payload.append({
            "role": role,
            "content": text,
        })
    return token_counter(model=model_id, messages=payload)

# ===========================================================================
# Layer 4 – LLM Summarization
# ===========================================================================

def _truncate_text_for_summary(text: str, limit: int = SUMMARY_TOOL_OUTPUT_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return (
        text[:half]
        + f"\n\n... [{omitted:,} characters truncated for summary] ...\n\n"
        + (text[-half:] if half > 0 else "")
    )


def _summary_role_label(role: object) -> str:
    if _is_tool_call_role(role):
        return "Tool call"
    if _is_tool_response_role(role):
        return "Tool result"
    value = _role_value(role)
    return value.replace("_", " ").replace("-", " ").title()


def _serialize_messages_for_summary(messages: List[InternalChatMessage]) -> str:
    """Serialize visible history into bounded text for the real summary model."""
    lines = ["<conversation>"]
    for internal_msg in messages:
        if not internal_msg.is_visible() or internal_msg.message.role == MessageRole.SYSTEM:
            continue
        label = _summary_role_label(internal_msg.message.role)
        text = _extract_message_text(internal_msg.message)
        if _is_tool_response_role(internal_msg.message.role):
            text = _truncate_text_for_summary(text)
        lines.append(f"[{label}]:")
        lines.append(text)
    lines.append("</conversation>")
    return "\n".join(lines)

def summarize_conversation(
    messages: List[InternalChatMessage],
    model_id: str,
    custom_condense_prompt: Optional[str] = None,
    cached_command_blocks: Optional[str] = None,
    cached_skill_load: Optional[str] = None,
) -> SummarizeResponse:
    """Layer 4: LLM Summarization (smart compression).

    Sends the conversation history (since the last summary) to a summary model
    which produces a condensed replacement message.  All pre-summary messages
    are tagged with a ``condense_id`` so they become invisible.

    Gated behind the ``smart_summary`` configuration flag in ``system.yaml``.

    Returns:
        A ``SummarizeResponse`` containing the updated message list or an error.
    """
    log = get_logger(None, __name__)

    # A completed Goal may owe the root exactly one final-delivery request.
    # Smart summary runs under the same ContextVars but is only scaffolding;
    # skip its model call so it cannot consume that ephemeral allowance.
    from src.lib.goal import get_current_goal_provider
    from src.trace import get_current_local_run_id

    goal_provider = get_current_goal_provider()
    if goal_provider is not None and goal_provider.completion_settlement_pending(
        local_run_id=get_current_local_run_id(),
    ):
        return SummarizeResponse(
            messages=messages,
            summary="",
            error="Goal completion settlement pending; smart summary skipped",
        )

    messages_to_summarize = get_messages_since_last_summary(messages)
    summarizable_messages = [
        msg for msg in messages_to_summarize
        if msg.message.role != MessageRole.SYSTEM and msg.is_visible()
    ]

    if len(summarizable_messages) <= 1:
        error = "Not enough messages available for compression"
        return SummarizeResponse(messages=messages, summary="", error=error)

    condense_instructions = custom_condense_prompt.strip() if custom_condense_prompt else CONDENSE_INSTRUCTION
    serialized_history = _serialize_messages_for_summary(messages_to_summarize)

    # CONDENSE + history
    request_messages: List[Union[ChatMessage, dict]] = []

    # CONDENSE
    condense_message = ChatMessage(
        role=MessageRole.USER,
        content=[{
            "type": "text",
            "text": condense_instructions
        }],
        tool_calls=None,
        raw=None,
        token_usage=None,
    )
    request_messages.append(condense_message)

    request_messages.append(
        ChatMessage(
            role=MessageRole.USER,
            content=[{
                "type": "text",
                "text": serialized_history,
            }],
            tool_calls=None,
            raw=None,
            token_usage=None,
        )
    )

    log.info(f"Preparing to compress {len(messages_to_summarize)} messages...")
    log.info("=" * 80)
    log.info("📨 Compression request messages sent to LLM:")
    log.info("=" * 80)
    for i, msg in enumerate(request_messages):
        role = msg.role if hasattr(msg, 'role') else msg.get('role')
        content = msg.content if hasattr(msg, 'content') else msg.get('content')
        content_text = _extract_content_text(content)
        log.info(f"\nMessage #{i}: [{role}]")
        log.info(f"  Content: {content_text}")
    log.info("=" * 80)

    model = model_manager.get_smolagents_model(ModelType.SUMMARY)

    summary_text = ""
    cost = 0.0         # TODO: get cost from model

    try:
        generation_messages: List[Union[ChatMessage, dict]] = [
            ChatMessage(role=MessageRole.SYSTEM, content=SUMMARY_SYSTEM_PROMPT),
        ] + request_messages

        response_msg = model.generate(generation_messages)
        summary_text = _extract_content_text(response_msg.content).strip()

        if not summary_text:
            error = "LLM returned an empty summary"
            return SummarizeResponse(messages=messages, summary="", error=error)

    except Exception as e:
        error_message = f"LLM compression failed: {type(e).__name__}: {str(e)}"
        log.error(error_message)
        return SummarizeResponse(
            messages=messages,
            summary="",
            error=error_message,
            error_details=str(e),
        )

    condense_id = str(uuid.uuid4())

    summary_content_parts = []

    if cached_command_blocks or cached_skill_load:
        reminder_parts = []
        if cached_command_blocks:
            reminder_parts.append(f"## Active Workflows\n{cached_command_blocks}")
        if cached_skill_load:
            reminder_parts.append(f"## Recent Skill Load\n{cached_skill_load}")
        summary_content_parts.append({
            "type": "text",
            "text": f"""<system-reminder>
{chr(10).join(reminder_parts)}
</system-reminder>"""
        })

    summary_content_parts.append({
        "type": "text",
        "text": f"## Conversation Summary\n{summary_text}"
    })

    # TODO: Add environment information

    last_msg_ts = messages[-1].ts if messages else time.time()
    summary_message = InternalChatMessage(
        message=ChatMessage(
            role=MessageRole.USER,
            content=summary_content_parts,
            tool_calls=None,
            raw=None,
            token_usage=None,
        ),
        ts=last_msg_ts + 1,
        is_summary=True,
    )

    log.info(f"Created summary message (condense_id={condense_id[:8]}...)")

    # set condense_id
    new_messages = []
    messages_to_condense = {
        id(msg)
        for msg in messages_to_summarize
        if msg.message.role != MessageRole.SYSTEM
    }
    for msg in messages:
        is_system_msg = msg.message.role == MessageRole.SYSTEM

        if is_system_msg:
            new_messages.append(msg)
        elif id(msg) in messages_to_condense and not msg.condense_id:
            new_msg = InternalChatMessage(
                message=msg.message,
                truncation_parent=msg.truncation_parent,
                is_truncation_marker=msg.is_truncation_marker,
                truncation_id=msg.truncation_id,
                condense_id=condense_id,
                is_summary=msg.is_summary,
                ts=msg.ts,
            )
            new_messages.append(new_msg)
        else:
            new_messages.append(msg)

    new_messages.append(summary_message)

    new_context_tokens = _count_tokens(
        [msg.message for msg in new_messages if msg.is_visible()],
        model_id
    )

    return SummarizeResponse(
        messages=new_messages,
        summary=summary_text,
        cost=0.0,  # TODO: get cost from model
        new_context_tokens=new_context_tokens,
    )


# ===========================================================================
# Message Conversion & Visibility Helpers
# ===========================================================================

def to_internal_messages(messages: List[ChatMessage]) -> List[InternalChatMessage]:
    """Wrap raw ``ChatMessage`` objects into ``InternalChatMessage`` with metadata."""
    return [InternalChatMessage.from_chat_message(msg) for msg in messages]


def get_effective_history(messages: List[InternalChatMessage]) -> List[InternalChatMessage]:
    """Return the visible slice of the conversation history.

    Includes system prompts, the latest summary (if any), and all messages
    after that summary that are not hidden by truncation or condensation.
    """
    system_prompts = []
    for msg in messages:
        if msg.message.role == MessageRole.SYSTEM:
            system_prompts.append(msg)

    last_summary_idx = -1
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].is_summary:
            last_summary_idx = idx
            break

    start_idx = last_summary_idx if last_summary_idx >= 0 else 0

    result = []

    result.extend(system_prompts)

    for msg in messages[start_idx:]:
        if msg.message.role == MessageRole.SYSTEM:
            continue
        if msg.condense_id or msg.truncation_parent:
            continue
        if msg.is_truncation_marker or msg.is_summary:
            result.append(msg)
            continue
        if msg.is_visible():
            result.append(msg)

    return result


def to_api_messages(messages: List[InternalChatMessage]) -> List[ChatMessage]:
    """Convert internal messages to API-ready ``ChatMessage`` list (visible only)."""
    effective_history = get_effective_history(messages)

    result = []
    for internal_msg in effective_history:
        result.append(internal_msg.to_chat_message())

    return result


# ===========================================================================
# Fallback – Sliding-Window Truncation
# ===========================================================================

def truncate_conversation(
    messages: List[InternalChatMessage],
    frac_to_remove: float,
    cached_command_blocks: Optional[str] = None,
    cached_skill_load: Optional[str] = None,
) -> TruncationResult:
    """Fallback: Sliding-Window Truncation.

    Hides the oldest visible non-system message groups by tagging them with a
    ``truncation_parent`` ID, then inserts a synthetic truncation marker at
    the boundary. TOOL_CALL + immediately following TOOL_RESPONSE is treated
    as one atomic group; ordinary messages are single-message groups.

    Returns:
        A ``TruncationResult`` with the updated message list.
    """
    log = get_logger(None, __name__)
    truncation_id = str(uuid.uuid4())

    all_groups = _iter_visible_non_system_groups(messages)
    pair_by_response = _build_response_pair_map(messages)
    visible_response_indices = [
        idx
        for idx, msg in enumerate(messages)
        if msg.is_visible() and _is_tool_response_role(msg.message.role)
    ]
    error_exempt_indices = _recent_error_response_indices(messages, visible_response_indices)
    groups = [
        group for group in all_groups
        if _is_group_truncatable(messages, group, pair_by_response, error_exempt_indices)
    ]
    visible_count = sum(len(group.indices) for group in groups)
    if visible_count <= 0:
        return TruncationResult(
            messages=messages,
            truncation_id=truncation_id,
            messages_removed=0,
        )

    target_messages_to_remove = max(1, int(visible_count * frac_to_remove))
    groups_to_truncate: list[VisibleMessageGroup] = []
    messages_to_remove = 0
    for group in groups:
        groups_to_truncate.append(group)
        messages_to_remove += len(group.indices)
        if messages_to_remove >= target_messages_to_remove:
            break

    if not groups_to_truncate:
        return TruncationResult(
            messages=messages,
            truncation_id=truncation_id,
            messages_removed=0,
        )

    indices_to_truncate = {idx for group in groups_to_truncate for idx in group.indices}
    old_marker_indices = {
        idx
        for idx, msg in enumerate(messages)
        if msg.is_visible() and msg.is_truncation_marker
    }

    tagged_messages = []
    for idx, internal_msg in enumerate(messages):
        if idx in indices_to_truncate or idx in old_marker_indices:
            tagged_msg = InternalChatMessage(
                message=internal_msg.message,
                truncation_parent=truncation_id,
                is_truncation_marker=False,
                truncation_id=None,
                condense_id=internal_msg.condense_id,
                is_summary=internal_msg.is_summary,
                ts=internal_msg.ts,
            )
            tagged_messages.append(tagged_msg)
        else:
            tagged_messages.append(internal_msg)

    if len(groups_to_truncate) < len(groups):
        first_kept_visible_index = groups[len(groups_to_truncate)].indices[0]
    else:
        first_kept_visible_index = len(tagged_messages)

    truncation_content_parts = []

    if cached_command_blocks or cached_skill_load:
        reminder_parts = []
        if cached_command_blocks:
            reminder_parts.append(f"## Active Workflows\n{cached_command_blocks}")
        if cached_skill_load:
            reminder_parts.append(f"## Recent Skill Load\n{cached_skill_load}")
        truncation_content_parts.append({
            "type": "text",
            "text": f"""<system-reminder>
{chr(10).join(reminder_parts)}
</system-reminder>"""
        })

    truncation_content_parts.append({
        "type": "text",
        "text": f"[Sliding window truncation: {messages_to_remove} messages hidden to reduce context]"
    })

    truncation_marker_msg = ChatMessage(
        role=MessageRole.USER,
        content=truncation_content_parts,
        tool_calls=None,
        raw=None,
        token_usage=None,
    )
    truncation_marker = InternalChatMessage(
        message=truncation_marker_msg,
        truncation_parent=None,
        is_truncation_marker=True,
        truncation_id=truncation_id,
        ts=time.time(),
    )

    result_messages = (
        tagged_messages[:first_kept_visible_index]
        + [truncation_marker]
        + tagged_messages[first_kept_visible_index:]
    )

    log.info(
        f"Sliding-window truncation: hid {messages_to_remove} messages "
        f"(truncation_id={truncation_id[:8]}...)"
    )

    return TruncationResult(
        messages=result_messages,
        truncation_id=truncation_id,
        messages_removed=messages_to_remove,
    )


# ===========================================================================
# Public API – ConversationHistoryManager
# ===========================================================================

class ConversationHistoryManager:
    """Manages conversation history and orchestrates the compression pipeline.

    This is the main entry point used by the agent runtime.  Call
    ``sync_from_messages`` to feed in new messages, then
    ``get_compressed_messages`` to obtain an API-ready message list that
    fits within the configured token budget.

    The compression pipeline executed by ``get_compressed_messages``:
        Layer 1 → Layer 2 → Layer 3 → Layer 4 → Fallback
    Each layer short-circuits if the budget is satisfied.
    """

    def __init__(self, max_tokens: int = DEFAULT_MAX_TOKENS, smart_summary: bool = True):
        self._internal_message_history: List[InternalChatMessage] = []
        self._max_tokens = max_tokens
        self._smart_summary = smart_summary
        self._cached_command_blocks: Optional[str] = None
        self._cached_skill_load: Optional[str] = None
        self._cached_sys_prompt: Optional[str] = None
        self._raw_message_count: int = 0  # count of real smolagents messages synced (excludes synthetic entries)

    def truncate_until_fits(
        self,
        model_id: str,
        frac_to_remove: float,
    ) -> None:
        """Fallback loop: repeatedly truncate until the token budget is met.

        Strategy:
            1. Try message-level sliding-window truncation (``truncate_conversation``).
            2. If no more message pairs can be removed, fall back to content-level
               masking — replace the oldest visible TOOL_RESPONSE content with
               ``OBSERVATION_MASKING_PLACEHOLDER``, one at a time.
            3. If neither strategy can make progress, log a WARNING and break.

        The loop also enforces a ``MAX_TRUNCATION_ROUNDS`` safety cap (default 30)
        to prevent excessive iterations in degenerate cases.
        """
        log = get_logger(None, __name__)
        truncate_count = 0
        while True:
            current_tokens = _count_tokens(to_api_messages(self._internal_message_history), model_id)
            if current_tokens <= self._max_tokens:
                break

            truncate_count += 1
            if truncate_count > MAX_TRUNCATION_ROUNDS:
                remaining = len([m for m in self._internal_message_history if m.is_visible()])
                log.warning(
                    f"Truncation loop hit safety cap ({MAX_TRUNCATION_ROUNDS} rounds). "
                    f"current_tokens={current_tokens}, max_tokens={self._max_tokens}, "
                    f"remaining_visible_messages={remaining}"
                )
                break

            if truncate_count > 1:
                log.info(f"Running truncation attempt #{truncate_count}")

            truncate_result = truncate_conversation(
                messages=self._internal_message_history,
                frac_to_remove=frac_to_remove,
                cached_command_blocks=self._cached_command_blocks,
                cached_skill_load=self._cached_skill_load,
            )

            if truncate_result.messages_removed > 0:
                self._internal_message_history = truncate_result.messages
                continue

            # -------------------------------------------------------------------
            # Content-level fallback: when no more messages can be removed,
            # mask the oldest visible tool_response content with a placeholder.
            # -------------------------------------------------------------------
            masked_any = False
            pair_by_response = _build_response_pair_map(self._internal_message_history)
            visible_response_indices = [
                idx
                for idx, msg in enumerate(self._internal_message_history)
                if msg.is_visible() and _is_tool_response_role(msg.message.role)
            ]
            error_exempt_indices = _recent_error_response_indices(self._internal_message_history, visible_response_indices)
            for i, internal_msg in enumerate(self._internal_message_history):
                if not internal_msg.is_visible() or not _is_tool_response_role(internal_msg.message.role):
                    continue
                content_text = _extract_content_text(internal_msg.message.content)
                if not content_text or _is_placeholder_response(content_text):
                    continue
                if _is_tool_response_exempt(self._internal_message_history, i, pair_by_response):
                    continue
                if i in error_exempt_indices:
                    continue
                self._internal_message_history[i] = _clone_internal_message_with_content(
                    internal_msg,
                    OBSERVATION_MASKING_PLACEHOLDER,
                )
                masked_any = True
                log.info("Content-level fallback: masked oldest visible tool_response")
                break  # mask one at a time, then re-check tokens

            if not masked_any:
                remaining = len([m for m in self._internal_message_history if m.is_visible()])
                current_tokens = _count_tokens(
                    to_api_messages(self._internal_message_history), model_id
                )
                log.warning(
                    f"All compression strategies exhausted. "
                    f"current_tokens={current_tokens}, max_tokens={self._max_tokens}, "
                    f"remaining_visible_messages={remaining}"
                )
                break

    def sync_from_messages(self, messages: List[ChatMessage]):
        """Incrementally sync new ``ChatMessage`` objects from the agent runtime.

        On the first call, all messages are ingested.  On subsequent calls,
        only messages beyond the previously-seen count are appended.
        Also caches the system prompt and the original task command block
        (used by truncation markers and summaries to preserve task context).
        """
        if self._cached_sys_prompt is None and messages:
            for msg in messages:
                if msg.role == MessageRole.SYSTEM:
                    self._cached_sys_prompt = _extract_content_text(msg.content)
                    break

        if self._cached_command_blocks is None and messages:
            for msg in messages:
                if msg.role != MessageRole.SYSTEM:
                    content_text = _extract_content_text(msg.content)
                    self._cached_command_blocks = f"""<command="original_task">\n{content_text}\n</command>"""
                    break

        for idx in range(len(messages) - 2, -1, -1):
            current_msg = messages[idx]
            next_msg = messages[idx + 1]
            if current_msg.role != MessageRole.TOOL_CALL or next_msg.role != MessageRole.TOOL_RESPONSE:
                continue
            call_text = _extract_content_text(current_msg.content)
            if not call_text or not any(tool_name in call_text for tool_name in COMPRESSION_EXEMPT_TOOL_NAMES):
                continue
            self._cached_skill_load = _extract_content_text(next_msg.content) or None
            break

        if not self._internal_message_history:
            self._internal_message_history = to_internal_messages(messages)
            self._raw_message_count = len(messages)
        else:
            if len(messages) > self._raw_message_count:
                for msg in messages[self._raw_message_count:]:
                    self._internal_message_history.append(InternalChatMessage.from_chat_message(msg))
                self._raw_message_count = len(messages)

    def get_compressed_messages(
        self,
        model_id: str,
        step: Optional[int] = None,
    ) -> List[ChatMessage]:
        """Run the full compression pipeline and return API-ready messages.

        Pipeline: Layer 1 → Layer 2 → Layer 3 → Layer 4 → Fallback.
        Each layer checks the budget after execution and returns early on success.
        """
        log = get_logger(None, __name__)
        if not model_id:
            return to_api_messages(self._internal_message_history)

        current_tokens = _count_tokens(to_api_messages(self._internal_message_history), model_id)

        step_prefix = f"\\[Step [red]{step}[/red]] " if step is not None else ""
        log.info(f"{step_prefix}Current tokens: [red]{current_tokens:,}[/red] | max limit: [red]{self._max_tokens}[/red]")

        if current_tokens <= self._max_tokens:
            return to_api_messages(self._internal_message_history)

        if self._smart_summary:
            log.info(f"Triggering smart compression: {current_tokens} > {self._max_tokens} tokens")
        else:
            log.info(f"Triggering standard compression: {current_tokens} > {self._max_tokens} tokens")

        # --- Layer 1: File Read Deduplication ---
        deduped_messages, saved_ratio = _apply_tool_dedup(
            self._internal_message_history,
            model_id=model_id,
        )

        if saved_ratio >= TRUNCATION_FRAC_TO_REMOVE:
            # Enough savings from dedup alone — skip compress/truncate
            self._internal_message_history = deduped_messages
            log.info(
                f"[Layer 1] File dedup saved {saved_ratio:.1%} of context "
                f"(threshold={TRUNCATION_FRAC_TO_REMOVE:.0%}). "
                f"Skipping LLM summarization and truncation."
            )
            return to_api_messages(self._internal_message_history)

        # Partial savings: persist the dedup changes anyway (reduces future compression cost)
        if saved_ratio > 0.0:
            self._internal_message_history = deduped_messages
            log.info(
                f"[Layer 1] File dedup saved {saved_ratio:.1%} (below {TRUNCATION_FRAC_TO_REMOVE:.0%} threshold). "
                f"Continuing with Layer 2 Tool Output Truncation."
            )

        # --- Layer 2: Reversible ContextEngine Compression ---
        context_messages, context_saved = _apply_context_engine_compression(
            self._internal_message_history,
        )
        if context_saved > 0:
            self._internal_message_history = context_messages

            current_tokens = _count_tokens(to_api_messages(self._internal_message_history), model_id)
            if current_tokens <= self._max_tokens:
                log.info(
                    f"[Layer 2] ContextEngine reversible compression resolved context limits! "
                    f"({current_tokens} <= {self._max_tokens})"
                )
                return to_api_messages(self._internal_message_history)

        # --- Layer 3: Tool Output Hard Truncation ---
        trunc_messages, trunc_saved = _apply_tool_output_truncation(
            self._internal_message_history,
        )
        if trunc_saved > 0:
            self._internal_message_history = trunc_messages
            
            # Recalculate tokens since we hard truncated strings
            current_tokens = _count_tokens(to_api_messages(self._internal_message_history), model_id)
            if current_tokens <= self._max_tokens:
                log.info(f"[Layer 3] Tool output truncation resolved context limits! ({current_tokens} <= {self._max_tokens})")
                return to_api_messages(self._internal_message_history)

        # --- Layer 4: Observation Masking ---
        masked_messages, mask_saved = _apply_observation_masking(
            self._internal_message_history,
            frac_to_mask=TRUNCATION_FRAC_TO_REMOVE,
        )
        if mask_saved > 0:
            self._internal_message_history = masked_messages

            current_tokens = _count_tokens(to_api_messages(self._internal_message_history), model_id)
            if current_tokens <= self._max_tokens:
                log.info(f"[Layer 4] Observation masking resolved context limits! ({current_tokens} <= {self._max_tokens})")
                return to_api_messages(self._internal_message_history)

        if not self._smart_summary:
            log.info("smart_summary is disabled; skipping LLM summarization and falling back to truncation")
            self.truncate_until_fits(
                model_id=model_id,
                frac_to_remove=TRUNCATION_FRAC_TO_REMOVE,
            )
            return to_api_messages(self._internal_message_history)

        # --- Layer 4: LLM Summarization (requires smart_summary=true) ---
        result = summarize_conversation(
            messages=self._internal_message_history,
            model_id=model_id,
            cached_command_blocks=self._cached_command_blocks,
            cached_skill_load=self._cached_skill_load,
        )

        if result.error:
            log.warning(f"Smart compression failed: {result.error}, falling back to truncation")

            self.truncate_until_fits(
                model_id=model_id,
                frac_to_remove=TRUNCATION_FRAC_TO_REMOVE,
            )
        else:
            log.info("Smart compression succeeded")
            self._internal_message_history = result.messages

            current_tokens = _count_tokens(to_api_messages(self._internal_message_history), model_id)
            log.info(f"Tokens after compression: {current_tokens}, max limit: {self._max_tokens}")

            if current_tokens > self._max_tokens:
                log.warning("Still over limit after compression, performing truncation")
                self.truncate_until_fits(
                    model_id=model_id,
                    frac_to_remove=TRUNCATION_FRAC_TO_REMOVE,
                )

        return to_api_messages(self._internal_message_history)

    def get_internal_messages(self) -> List[InternalChatMessage]:
        return self._internal_message_history


    def clear(self):
        self._internal_message_history = []
        self._cached_command_blocks = None
        self._cached_skill_load = None
        self._cached_sys_prompt = None
        self._raw_message_count = 0
