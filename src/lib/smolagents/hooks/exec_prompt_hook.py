"""LLM prompt hook executor.

Evaluates a prompt with a small/fast LLM model and parses the
structured ``{ok: bool, reason?: str}`` response.

Aligned with upstream ``execPromptHook.ts``:
- ``$ARGUMENTS`` placeholder replaced with hook input JSON
- Model defaults to a fast/cheap model (e.g. Haiku)
- Response schema: ``{ok: boolean, reason?: string}``
- ``ok=false`` results in blocking outcome
- Timeout defaults to 30 s
- Uses litellm native ``timeout`` parameter (no Thread.join wrapper)
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from src.lib.logging import get_logger

from .hook_helpers import add_arguments_to_prompt
from .types import HookResult, PromptHook

logger = get_logger(__name__)

# Default timeout for prompt hooks: 30 seconds (aligned with upstream).
DEFAULT_PROMPT_TIMEOUT: float = 30.0


def _parse_llm_response(response: Any) -> Optional[Dict[str, Any]]:
    """Extract and parse JSON from an LLM completion response.

    Handles markdown code-block wrapping and whitespace.
    Returns ``None`` on failure.
    """
    try:
        content = response.choices[0].message.content.strip()
    except (AttributeError, IndexError):
        return None

    # Strip markdown code fences
    if content.startswith("```"):
        lines = content.split("\n")
        json_lines = [l for l in lines if not l.startswith("```")]
        content = "\n".join(json_lines).strip()

    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def exec_prompt_hook(
    hook: PromptHook,
    hook_input: Dict[str, Any],
    *,
    timeout: Optional[float] = None,
) -> HookResult:
    """Execute a prompt hook by querying an LLM.

    Calls ``litellm.completion()`` directly with the native ``timeout``
    parameter.  No Thread wrapper -- timeout is handled by the HTTP
    client itself, guaranteeing zero thread leaks.

    Parameters
    ----------
    hook:
        The prompt hook definition.
    hook_input:
        Full hook input payload (serialised to JSON for the prompt).
    timeout:
        Override timeout in seconds.
    """
    effective_timeout = timeout or hook.timeout or DEFAULT_PROMPT_TIMEOUT
    json_input = json.dumps(hook_input, ensure_ascii=False, default=str)

    # Substitute $ARGUMENTS placeholder
    final_prompt = add_arguments_to_prompt(hook.prompt, json_input)

    try:
        from litellm import completion

        model_id = hook.model or "claude-3-haiku-20240307"
        response = completion(
            model=model_id,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a hook validator. Evaluate the following condition "
                        'and respond with a JSON object: {"ok": true/false, "reason": "..."}\n'
                        "Respond ONLY with valid JSON, no other text."
                    ),
                },
                {"role": "user", "content": final_prompt},
            ],
            max_tokens=256,
            temperature=0.0,
            timeout=effective_timeout,
        )
    except Exception as exc:
        # Catches litellm.Timeout, litellm.APIError, network errors, etc.
        exc_name = type(exc).__name__
        if "timeout" in exc_name.lower() or "Timeout" in str(exc):
            logger.warning("Prompt hook timed out after %ss", effective_timeout)
            return HookResult(
                success=False,
                decision="allow",
                outcome="cancelled",
                reason=f"Prompt hook timed out after {effective_timeout}s",
            )
        logger.error("Prompt hook LLM query failed: %s", exc)
        return HookResult(
            success=False,
            decision="allow",
            outcome="non_blocking_error",
            reason=f"Prompt hook LLM query failed: {exc}",
        )

    # Parse LLM response
    parsed = _parse_llm_response(response)
    if parsed is None:
        logger.warning("Prompt hook response not valid JSON")
        return HookResult(
            success=False,
            decision="allow",
            outcome="non_blocking_error",
            reason="Prompt hook response not valid JSON",
        )

    return _evaluate_ok_response(parsed, hook)


def _evaluate_ok_response(
    parsed: Dict[str, Any],
    hook: Any,
) -> HookResult:
    """Evaluate the ``{ok, reason}`` schema common to prompt and agent hooks."""
    ok = parsed.get("ok")
    reason = parsed.get("reason", "")

    if ok is True:
        return HookResult(
            success=True,
            decision="allow",
            outcome="success",
            permission_behavior="allow",
        )

    if ok is False:
        label = getattr(hook, "prompt", "")[:100]
        hook_type = getattr(hook, "type", "prompt")
        return HookResult(
            success=False,
            decision="block",
            outcome="blocking",
            prevent_continuation=True,
            reason=reason or f"{hook_type.capitalize()} hook validation failed",
            permission_behavior="deny",
            blocking_error={
                "blocking_error": reason or f"{hook_type.capitalize()} hook validation failed",
                "command": f"{hook_type}: {label}",
            },
        )

    # Ambiguous response
    logger.warning("Hook returned ambiguous ok=%s", ok)
    return HookResult(
        success=True,
        decision="allow",
        outcome="success",
    )
