"""Hook helper utilities: pattern matching, deduplication, argument substitution,
and shared process-group management.

Aligned with upstream ``hookHelpers.ts``.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from typing import Any, Dict, Optional, Union

from src.lib.logging import get_logger

from .types import CommandHook, HookCommand, HttpHook, PromptHook, AgentHook

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Process group management (shared by exec_command_hook + async_hook_registry)
# ---------------------------------------------------------------------------

def kill_hook_process_group(
    proc: subprocess.Popen,
    grace_sec: float = 0.5,
) -> None:
    """Kill an entire process group with SIGTERM -> SIGKILL escalation.

    Uses ``os.killpg`` to terminate all descendants spawned under the
    same session created by ``os.setsid`` in ``exec_command_hook``.
    Falls back to ``proc.kill()`` if the process group is unavailable.

    This pattern is aligned with ``src/tools/shell/tree_kill.py`` and
    the Timer-based escalation in ``src/tools/search/grep_tool/``.
    """
    if proc.poll() is not None:
        return  # Already exited

    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            proc.wait(timeout=grace_sec)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
    except (ProcessLookupError, OSError):
        # Process group gone or not accessible — try direct kill
        try:
            proc.kill()
            proc.wait(timeout=1)
        except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
            pass

# Pre-compiled pattern for detecting "simple" matchers that should use
# exact or pipe-separated matching instead of regex.
_SIMPLE_PATTERN = re.compile(r"^[a-zA-Z0-9_|]+$")


# ---------------------------------------------------------------------------
# Pattern matching (aligned with upstream matchesPattern())
# ---------------------------------------------------------------------------

def matches_pattern(query: str, matcher: Optional[str]) -> bool:
    """Three-level pattern matching aligned with upstream ``matchesPattern()``.

    Level 1 -- wildcard:
        ``None``, ``""``, or ``"*"`` matches everything.

    Level 2 -- exact / pipe-separated:
        Pure alphanumeric + pipe strings (e.g. ``"Write|Edit"``) are split
        on ``|`` and compared via exact equality.

    Level 3 -- regex:
        Anything containing regex meta-characters is compiled as a regex
        and tested with ``re.search`` (partial match, aligned with
        upstream ``regex.test()``).

    Returns ``False`` on invalid regex instead of raising.
    """
    if not matcher or matcher == "*":
        return True

    if _SIMPLE_PATTERN.fullmatch(matcher):
        if "|" in matcher:
            return query in [p.strip() for p in matcher.split("|")]
        return query == matcher

    try:
        return bool(re.search(matcher, query))
    except re.error:
        logger.debug("Invalid regex pattern '%s', returning False", matcher)
        return False


# ---------------------------------------------------------------------------
# Deduplication key (aligned with upstream hookDedupKey logic)
# ---------------------------------------------------------------------------

def hook_dedup_key(
    hook: HookCommand,
    source: str = "",
    if_condition: Optional[str] = None,
) -> str:
    """Build a deduplication key for a hook command.

    Aligned with upstream dedup strategy: hooks with the same source
    context + command payload + if-condition are considered duplicates.

    Parameters
    ----------
    hook:
        The hook command dataclass.
    source:
        Source identifier (e.g. skill name, plugin root, or empty for
        settings-level hooks).
    if_condition:
        The ``if`` condition string from the hook config.
    """
    cond = if_condition or ""

    if isinstance(hook, CommandHook):
        payload = f"command\0{hook.shell}\0{hook.command}"
    elif isinstance(hook, PromptHook):
        payload = f"prompt\0{hook.prompt}"
    elif isinstance(hook, HttpHook):
        payload = f"http\0{hook.url}"
    elif isinstance(hook, AgentHook):
        payload = f"agent\0{hook.prompt}"
    else:
        # Fallback for unknown types -- use repr
        payload = repr(hook)

    return f"{source}\0{payload}\0{cond}"


# ---------------------------------------------------------------------------
# Argument substitution (aligned with upstream addArgumentsToPrompt())
# ---------------------------------------------------------------------------

def add_arguments_to_prompt(prompt: str, json_input: str) -> str:
    """Replace ``$ARGUMENTS`` placeholder in *prompt* with *json_input*.

    Also supports indexed placeholders ``$ARGUMENTS[0]``, ``$0``, etc.
    If no placeholder is found, the JSON input is appended to the prompt.
    """
    if "$ARGUMENTS" in prompt:
        return prompt.replace("$ARGUMENTS", json_input)

    # Shorthand: $0, $1, ...
    if re.search(r"\$\d+", prompt):
        # For simplicity, replace $0 with the full input
        return re.sub(r"\$0\b", json_input, prompt)

    # No placeholder found -- append
    return f"{prompt}\n\n{json_input}"


# ---------------------------------------------------------------------------
# Hook command factory (dict -> dataclass)
# ---------------------------------------------------------------------------

def parse_hook_command(raw: Dict[str, Any]) -> Optional[HookCommand]:
    """Parse a raw dict (from YAML/JSON config) into a typed HookCommand.

    Returns ``None`` if the ``type`` field is unrecognised.
    """
    hook_type = raw.get("type", "command")

    # Map ``if`` key (Python reserved word) to ``if_condition`` field
    raw_copy = dict(raw)
    if "if" in raw_copy:
        raw_copy["if_condition"] = raw_copy.pop("if")
    # Map ``async`` key (Python reserved word) to ``async_mode`` field
    if "async" in raw_copy:
        raw_copy["async_mode"] = raw_copy.pop("async")
    if "asyncRewake" in raw_copy:
        raw_copy["async_rewake"] = raw_copy.pop("asyncRewake")
    # Map camelCase to snake_case for remaining fields
    if "statusMessage" in raw_copy:
        raw_copy["status_message"] = raw_copy.pop("statusMessage")
    if "allowedEnvVars" in raw_copy:
        raw_copy["allowed_env_vars"] = raw_copy.pop("allowedEnvVars")

    if hook_type == "command":
        return CommandHook(**{k: v for k, v in raw_copy.items() if hasattr(CommandHook, k) or k in CommandHook.__dataclass_fields__})
    elif hook_type == "prompt":
        return PromptHook(**{k: v for k, v in raw_copy.items() if k in PromptHook.__dataclass_fields__})
    elif hook_type == "http":
        return HttpHook(**{k: v for k, v in raw_copy.items() if k in HttpHook.__dataclass_fields__})
    elif hook_type == "agent":
        return AgentHook(**{k: v for k, v in raw_copy.items() if k in AgentHook.__dataclass_fields__})
    else:
        logger.warning("Unknown hook type '%s', skipping", hook_type)
        return None
