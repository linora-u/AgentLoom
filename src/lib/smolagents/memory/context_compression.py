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

  Layer 2 – Tool Output Hard Truncation  (``_apply_tool_output_truncation``)
      Caps excessively long TOOL_RESPONSE content to per-tool character
      limits (e.g. shell_tool → 2 000 chars, ripgrep → 3 000 chars).
      Head + tail are kept; the middle is replaced with a truncation notice.
      *Idempotent.*

  Layer 3 – Observation Masking  (``_apply_observation_masking``)
      Inspired by OpenHands' ``ObservationMaskingCondenser``.  The oldest
      ``TRUNCATION_FRAC_TO_REMOVE`` fraction of visible tool responses are
      replaced with a short placeholder, while the corresponding tool-call
      messages are left intact to preserve the conversation structure.
      *Idempotent.*

  Layer 4 – LLM Summarization  (``summarize_conversation``)
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
    "python_interpreter": 3000,
    "default": 3000,
}

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


def _extract_tool_payload(text: str) -> tuple[Optional[str], Optional[str]]:
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
                return name.strip().lower(), arguments if isinstance(arguments, str) else None

        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                function_payload = item.get("function")
                if not isinstance(function_payload, dict):
                    continue
                name = function_payload.get("name")
                arguments = function_payload.get("arguments")
                if isinstance(name, str):
                    return name.strip().lower(), arguments if isinstance(arguments, str) else None

    return None, None


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

        call_payload = {
            "args": [_normalize_ast_value(arg) for arg in node.args],
            "kwargs": {
                (keyword.arg or "**kwargs"): _normalize_ast_value(keyword.value)
                for keyword in node.keywords
            },
        }
        calls.append((tool_name, json.dumps(call_payload, ensure_ascii=True, sort_keys=True)))

    return calls


def _extract_dedup_keys_from_tool_call(msg: ChatMessage) -> List[tuple[str, str]]:
    """Extract deduplication keys (like file paths) from a TOOL_CALL message.

    In CodeAct mode the TOOL_CALL content looks like:
        Calling tools: [{'function': {'name': 'python_interpreter', 'arguments': '<python code>'}}]
    We scan the entire text for patterns defined in TOOL_DEDUP_PATTERNS.
    Returns: A list of tuples (tool_name: str, dedup_key: str)
    """
    text = _extract_content_text(msg.content)
    if not text:
        return []

    primary_tool, nested_source = _extract_tool_payload(text)
    source = nested_source if primary_tool == "python_interpreter" and nested_source else text

    results = [
        (tool_name, dedup_key)
        for tool_name, dedup_key in _extract_tool_calls_from_source(source)
        if tool_name in FILE_READ_TOOL_NAMES
    ]
    if results:
        return results

    for tool_name, pattern in TOOL_DEDUP_PATTERNS.items():
        matches = pattern.findall(text)
        for match in matches:
            if match:
                results.append((tool_name, match.strip()))

    return results


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
    # Only operate on visible (non-compressed, non-truncated) messages
    effective = [m for m in messages if m.is_visible()]

    # -----------------------------------------------------------------------
    # Step 1: build (tool_name, dedup_key) -> [(tool_call_idx, tool_resp_idx)] mapping
    #   indices into `effective` list
    # -----------------------------------------------------------------------
    # (tool_name, dedup_key) -> list of (tool_call_effective_idx, tool_resp_effective_idx)
    tool_read_pairs: dict = {}  # tuple[str, str] -> List[Tuple[int, int]]

    i = 0
    while i < len(effective):
        msg = effective[i]
        role_value = msg.message.role.value if hasattr(msg.message.role, 'value') else str(msg.message.role)

        if role_value in ("tool-call", "tool_call"):
            # Look for the corresponding TOOL_RESPONSE immediately following
            tool_keys = _extract_dedup_keys_from_tool_call(msg.message)
            if tool_keys and i + 1 < len(effective):
                next_role = effective[i + 1].message.role
                next_role_val = next_role.value if hasattr(next_role, 'value') else str(next_role)
                if next_role_val in ("tool-response", "tool_response"):
                    for tool_name, key in tool_keys:
                        # Normalize path: strip quotes / f-string artifacts
                        key = key.strip()
                        if not key:
                            continue
                        pairs = tool_read_pairs.setdefault((tool_name, key), [])
                        pairs.append((i, i + 1))
        i += 1

    # -----------------------------------------------------------------------
    # Step 2: for tools with overlapping targets >1 time, collect (effective_idx, original_text, new_text)
    # -----------------------------------------------------------------------
    # Map from effective_idx -> new Observation text (only old reads)
    replacements: dict = {}  # int -> str (new content text)

    for (tool_name, key), pairs in tool_read_pairs.items():
        if len(pairs) <= 1:
            continue  # only read once, nothing to deduplicate
        # Keep the LAST read; replace all earlier ones
        for (tc_idx, tr_idx) in pairs[:-1]:
            if tr_idx not in replacements:
                original_text = _extract_content_text(effective[tr_idx].message.content)
                replacements[tr_idx] = (original_text, FILE_DEDUP_PLACEHOLDER)

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

        eff_idx = -1
        try:
            eff_idx = effective.index(msg)
        except ValueError:
            pass # msg not in effective (e.g. it's a system message or already compressed/truncated)

        if eff_idx in replacements:
            original_text, new_text = replacements[eff_idx]
            saved_chars += (len(raw_text) - len(new_text))

            new_chat_msg = ChatMessage(
                role=msg.message.role,
                content=[{"type": "text", "text": new_text}],
            )
            new_internal = InternalChatMessage(
                message=new_chat_msg,
                truncation_parent=msg.truncation_parent,
                is_truncation_marker=msg.is_truncation_marker,
                truncation_id=msg.truncation_id,
                condense_id=msg.condense_id,
                is_summary=msg.is_summary,
                ts=msg.ts,
            )
            new_messages.append(new_internal)
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
    primary_tool, nested_source = _extract_tool_payload(code)

    if primary_tool and primary_tool != "python_interpreter":
        return [primary_tool]

    source = nested_source if primary_tool == "python_interpreter" and nested_source else code
    tools = []
    for tool_name, _ in _extract_tool_calls_from_source(source):
        if tool_name not in FILE_READ_TOOL_NAMES and tool_name not in TOOL_MAX_RETAIN_CHARS:
            continue
        if tool_name not in tools:
            tools.append(tool_name)
    return tools


# ===========================================================================
# Layer 2 – Tool Output Hard Truncation
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
    effective = [m for m in messages if m.is_visible()]
    
    replacements = {}
    total_saved_chars = 0
    
    i = 0
    while i < len(effective):
        msg = effective[i]
        role_value = msg.message.role.value if hasattr(msg.message.role, 'value') else str(msg.message.role)
        
        if role_value in ("tool-call", "tool_call"):
            if i + 1 < len(effective):
                next_msg = effective[i + 1]
                next_role_val = next_msg.message.role.value if hasattr(next_msg.message.role, 'value') else str(next_msg.message.role)
                
                if next_role_val in ("tool-response", "tool_response"):
                    # Find out which tool was called
                    tools_used = []
                    call_text = _extract_content_text(msg.message.content)
                    
                    # Direct check in text for standard codeact formatting
                    # Example: {'name': 'python_interpreter', 'arguments': '...'}
                    m = re.search(r"\'name\':\s*\'([^\']+)\'", call_text)
                    if m:
                        primary_tool = m.group(1)
                        if primary_tool == "python_interpreter":
                            # Try to dig deeper into the actual argument code
                            tools_used.extend(_extract_tools_from_code(call_text))
                        else:
                            tools_used.append(primary_tool)
                            
                    if not tools_used:
                        tools_used = ["default"]
                        
                    # Find min quota among invoked tools (ignoring None)
                    quotas = []
                    for t in tools_used:
                        # If the tool is a deduplication tool, and it doesn't have an explicit
                        # truncation limit, we skip truncating its overall content, trusting Layer 1.
                        if t in TOOL_DEDUP_PATTERNS and t not in TOOL_MAX_RETAIN_CHARS:
                            continue
                            
                        quota = TOOL_MAX_RETAIN_CHARS.get(t, TOOL_MAX_RETAIN_CHARS["default"])
                        if quota is not None:
                            quotas.append(quota)
                    
                    if quotas:
                        quota = min(quotas)
                        resp_text = _extract_content_text(next_msg.message.content)
                        if len(resp_text) > quota:
                            half = quota // 2
                            head = resp_text[:half]
                            tail = resp_text[-half:]
                            omitted = len(resp_text) - quota
                            
                            primary = tools_used[0] if tools_used else "tool"
                            trunc_notice = f"\n\n... [Truncated {omitted:,} characters from {primary} output due to TOOL_MAX_RETAIN_CHARS ({quota})] ...\n\n"
                            
                            new_text = head + trunc_notice + tail
                            replacements[i + 1] = new_text
                            total_saved_chars += omitted
        i += 1

    if not replacements:
        return messages, 0.0
        
    new_messages = []
    
    for idx, msg in enumerate(messages):
        eff_idx = -1
        try:
            eff_idx = effective.index(msg)
        except ValueError:
            pass # msg not in effective (e.g. it's a system message or already compressed/truncated)

        if eff_idx in replacements:
            new_text = replacements[eff_idx]
            new_chat_msg = ChatMessage(
                role=msg.message.role,
                content=[{"type": "text", "text": new_text}],
            )
            new_internal = InternalChatMessage(
                message=new_chat_msg,
                truncation_parent=msg.truncation_parent,
                is_truncation_marker=msg.is_truncation_marker,
                truncation_id=msg.truncation_id,
                condense_id=msg.condense_id,
                is_summary=msg.is_summary,
                ts=msg.ts,
            )
            new_messages.append(new_internal)
        else:
            new_messages.append(msg)
            
    if total_saved_chars > 0:
        log.info(f"[Tool Truncation] Truncated {len(replacements)} overly long tool responses. Chars saved: {total_saved_chars:,}")
        
    return new_messages, total_saved_chars


# ===========================================================================
# Layer 3 – Observation Masking
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

    # Collect indices of visible TOOL_RESPONSE messages
    tool_response_indices: list[int] = []
    for idx, internal_msg in enumerate(messages):
        if (
            internal_msg.is_visible()
            and internal_msg.message.role == MessageRole.TOOL_RESPONSE
        ):
            content_text = _extract_content_text(internal_msg.message.content)
            # Skip already-masked or placeholder responses
            if content_text and content_text not in (
                OBSERVATION_MASKING_PLACEHOLDER,
                FILE_DEDUP_PLACEHOLDER,
            ):
                # [Mechanism A] Exempt skill-loading tool responses from masking.
                # Check if the preceding message is a TOOL_CALL containing an exempt tool.
                if idx > 0:
                    prev_msg = messages[idx - 1]
                    prev_role = prev_msg.message.role.value if hasattr(prev_msg.message.role, 'value') else str(prev_msg.message.role)
                    if prev_role in ("tool-call", "tool_call"):
                        prev_text = _extract_content_text(prev_msg.message.content)
                        if prev_text and any(t in prev_text for t in COMPRESSION_EXEMPT_TOOL_NAMES):
                            continue
                tool_response_indices.append(idx)

    if not tool_response_indices:
        return messages, 0

    # [Mechanism B] Exempt the most recent N error-related TOOL_RESPONSE messages
    # from masking, so the latest error recovery guidance is preserved.
    try:
        error_exempt_indices: set[int] = set()
        exempt_remaining = RECENT_ERROR_EXEMPT_COUNT
        for tr_idx in reversed(tool_response_indices):
            if exempt_remaining <= 0:
                break
            content_text = _extract_content_text(messages[tr_idx].message.content)
            if content_text and content_text.startswith("Error:"):
                error_exempt_indices.add(tr_idx)
                exempt_remaining -= 1
        maskable_indices = [i for i in tool_response_indices if i not in error_exempt_indices]
    except Exception:
        # Safety: fall back to no exemption on any error
        maskable_indices = tool_response_indices

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
            masked_msg = InternalChatMessage(
                message=ChatMessage(
                    role=internal_msg.message.role,
                    content=[{"type": "text", "text": OBSERVATION_MASKING_PLACEHOLDER}],
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
            new_messages.append(masked_msg)
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
        return messages[last_summary_idx + 1:]
    else:
        return messages


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

    messages_to_summarize = get_messages_since_last_summary(messages)

    if len(messages_to_summarize) <= 1:
        error = "Not enough messages available for compression"
        return SummarizeResponse(messages=messages, summary="", error=error)

    recent_summary_exists = any(msg.is_summary for msg in messages_to_summarize)
    if recent_summary_exists and len(messages_to_summarize) <= 2:
        error = "Recently compressed; no need to compress again yet"
        return SummarizeResponse(messages=messages, summary="", error=error)

    condense_instructions = custom_condense_prompt.strip() if custom_condense_prompt else CONDENSE_INSTRUCTION

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

    for internal_msg in messages_to_summarize:
        if internal_msg.message.role != MessageRole.SYSTEM:
            request_messages.append(internal_msg.message)

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
    for msg in messages:
        is_system_msg = msg.message.role == MessageRole.SYSTEM

        if is_system_msg:
            new_messages.append(msg)
        elif not msg.condense_id:
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

    Hides the oldest ``frac_to_remove`` fraction of visible non-system
    messages by tagging them with a ``truncation_parent`` ID, then inserts
    a synthetic truncation marker at the boundary.

    Steps:
        1. Collect visible non-system messages.
        2. Compute how many to hide (even-aligned to preserve message pairs).
        3. Tag those messages with ``truncation_parent``.
        4. Insert a truncation marker carrying any cached command blocks.

    Returns:
        A ``TruncationResult`` with the updated message list.
    """
    log = get_logger(None, __name__)
    truncation_id = str(uuid.uuid4())

    visible_non_system_indices = []
    for idx, internal_msg in enumerate(messages):
        if internal_msg.is_visible() and internal_msg.message.role != MessageRole.SYSTEM:
            visible_non_system_indices.append(idx)

    visible_count = len(visible_non_system_indices)
    if visible_count <= 0:
        return TruncationResult(
            messages=messages,
            truncation_id=truncation_id,
            messages_removed=0,
        )

    raw_messages_to_remove = int(visible_count * frac_to_remove)
    # Even-align to preserve user/assistant (or tool-call/tool-response) pairs.
    # (the minimum meaningful pair removal), provided enough visible messages exist.
    messages_to_remove = raw_messages_to_remove - (raw_messages_to_remove % 2)
    if messages_to_remove == 0 and raw_messages_to_remove >= 1 and visible_count >= 2:
        messages_to_remove = 2

    if messages_to_remove <= 0:
        return TruncationResult(
            messages=messages,
            truncation_id=truncation_id,
            messages_removed=0,
        )

    indices_to_truncate = set(visible_non_system_indices[:messages_to_remove])

    tagged_messages = []
    for idx, internal_msg in enumerate(messages):
        if idx in indices_to_truncate:
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

    # Locate where to insert the truncation marker
    if messages_to_remove < len(visible_non_system_indices):
        first_kept_visible_index = visible_non_system_indices[messages_to_remove]
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

            # [Mechanism B] Build set of indices to exempt (recent error messages)
            _error_exempt_fb: set[int] = set()
            try:
                _exempt_left = RECENT_ERROR_EXEMPT_COUNT
                for _ri in range(len(self._internal_message_history) - 1, -1, -1):
                    if _exempt_left <= 0:
                        break
                    _rm = self._internal_message_history[_ri]
                    if _rm.is_visible() and _rm.message.role == MessageRole.TOOL_RESPONSE:
                        _rt = _extract_content_text(_rm.message.content)
                        if _rt and _rt.startswith("Error:"):
                            _error_exempt_fb.add(_ri)
                            _exempt_left -= 1
            except Exception:
                _error_exempt_fb = set()

            for i, internal_msg in enumerate(self._internal_message_history):
                if (
                    internal_msg.is_visible()
                    and internal_msg.message.role == MessageRole.TOOL_RESPONSE
                ):
                    content_text = _extract_content_text(internal_msg.message.content)
                    if content_text and content_text != OBSERVATION_MASKING_PLACEHOLDER:
                        # [Mechanism A] Exempt skill-loading tool responses.
                        if i > 0:
                            prev_msg = self._internal_message_history[i - 1]
                            prev_role = prev_msg.message.role.value if hasattr(prev_msg.message.role, 'value') else str(prev_msg.message.role)
                            if prev_role in ("tool-call", "tool_call"):
                                prev_text = _extract_content_text(prev_msg.message.content)
                                if prev_text and any(t in prev_text for t in COMPRESSION_EXEMPT_TOOL_NAMES):
                                    continue
                        # [Mechanism B] Exempt recent error messages.
                        if i in _error_exempt_fb:
                            continue
                        internal_msg.message.content = [
                            {"type": "text", "text": OBSERVATION_MASKING_PLACEHOLDER}
                        ]
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

        # --- Layer 2: Tool Output Hard Truncation ---
        trunc_messages, trunc_saved = _apply_tool_output_truncation(
            self._internal_message_history,
        )
        if trunc_saved > 0:
            self._internal_message_history = trunc_messages
            
            # Recalculate tokens since we hard truncated strings
            current_tokens = _count_tokens(to_api_messages(self._internal_message_history), model_id)
            if current_tokens <= self._max_tokens:
                log.info(f"[Layer 2] Tool output truncation resolved context limits! ({current_tokens} <= {self._max_tokens})")
                return to_api_messages(self._internal_message_history)

        # --- Layer 3: Observation Masking ---
        masked_messages, mask_saved = _apply_observation_masking(
            self._internal_message_history,
            frac_to_mask=TRUNCATION_FRAC_TO_REMOVE,
        )
        if mask_saved > 0:
            self._internal_message_history = masked_messages

            current_tokens = _count_tokens(to_api_messages(self._internal_message_history), model_id)
            if current_tokens <= self._max_tokens:
                log.info(f"[Layer 3] Observation masking resolved context limits! ({current_tokens} <= {self._max_tokens})")
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
