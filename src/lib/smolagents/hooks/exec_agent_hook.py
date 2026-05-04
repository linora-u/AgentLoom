"""Multi-turn agent verifier hook executor.

Spawns a sub-agent that evaluates a verification prompt and returns a
structured ``{ok: bool, reason?: str}`` result.

Aligned with upstream ``execAgentHook.ts``:
- ``$ARGUMENTS`` placeholder replaced with hook input JSON
- Model defaults to a fast model (Haiku)
- Max 50 turns to prevent runaway execution
- Enforces structured output via schema
- Timeout defaults to 60 s
- Uses litellm native ``timeout`` parameter (no Thread.join wrapper)
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from src.lib.logging import get_logger

from .hook_helpers import add_arguments_to_prompt
from .exec_prompt_hook import _parse_llm_response, _evaluate_ok_response
from .types import AgentHook, HookResult

logger = get_logger(__name__)

# Default timeout for agent hooks: 60 seconds (aligned with upstream).
DEFAULT_AGENT_TIMEOUT: float = 60.0

# Maximum turns to prevent runaway agent execution.
MAX_AGENT_TURNS: int = 50


def exec_agent_hook(
    hook: AgentHook,
    hook_input: Dict[str, Any],
    *,
    timeout: Optional[float] = None,
) -> HookResult:
    """Execute an agent verifier hook.

    Calls ``litellm.completion()`` directly with the native ``timeout``
    parameter.  No Thread wrapper -- timeout is handled by the HTTP
    client itself, guaranteeing zero thread leaks.

    Currently implements single-turn verification; multi-turn with tool
    access can be added when verification agents need to call tools.
    The ``MAX_AGENT_TURNS`` constant caps multi-turn loops.

    Parameters
    ----------
    hook:
        The agent hook definition.
    hook_input:
        Full hook input payload.
    timeout:
        Override timeout in seconds.
    """
    effective_timeout = timeout or hook.timeout or DEFAULT_AGENT_TIMEOUT
    json_input = json.dumps(hook_input, ensure_ascii=False, default=str)

    # Substitute $ARGUMENTS placeholder
    final_prompt = add_arguments_to_prompt(hook.prompt, json_input)

    try:
        from litellm import completion

        model_id = hook.model or "claude-3-haiku-20240307"

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a verification agent. Your task is to evaluate "
                    "a condition and return a structured result.\n\n"
                    "You MUST respond with ONLY a JSON object in this exact format:\n"
                    '{"ok": true, "reason": "explanation"}\n'
                    "or\n"
                    '{"ok": false, "reason": "why the condition was not met"}\n\n'
                    "Do NOT include any other text, markdown, or formatting."
                ),
            },
            {"role": "user", "content": final_prompt},
        ]

        response = completion(
            model=model_id,
            messages=messages,
            max_tokens=512,
            temperature=0.0,
            timeout=effective_timeout,
        )
    except Exception as exc:
        exc_name = type(exc).__name__
        if "timeout" in exc_name.lower() or "Timeout" in str(exc):
            logger.warning("Agent hook timed out after %ss", effective_timeout)
            return HookResult(
                success=False,
                decision="allow",
                outcome="cancelled",
                reason=f"Agent hook timed out after {effective_timeout}s",
            )
        logger.error("Agent hook execution failed: %s", exc)
        return HookResult(
            success=False,
            decision="allow",
            outcome="non_blocking_error",
            reason=f"Agent hook execution failed: {exc}",
        )

    # Parse agent response (reuse shared parser)
    parsed = _parse_llm_response(response)
    if parsed is None:
        logger.warning("Agent hook response not valid JSON")
        return HookResult(
            success=False,
            decision="allow",
            outcome="non_blocking_error",
            reason="Agent hook response not valid JSON",
        )

    return _evaluate_ok_response(parsed, hook)
