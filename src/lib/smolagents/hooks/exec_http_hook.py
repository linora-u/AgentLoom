"""HTTP POST hook executor.

Sends hook input as JSON to a URL and parses the JSON response.

Aligned with upstream ``execHttpHook.ts``:
- POST hook input JSON to ``hook.url``
- Header environment variable interpolation with allowlist
- CRLF injection prevention in interpolated header values
- Timeout defaults to 600 s (10 minutes)
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

from src.lib.logging import get_logger

from .hook_schemas import SyncHookOutput, parse_hook_output, process_hook_output
from .types import HookResult, HttpHook

logger = get_logger(__name__)

# Default timeout for HTTP hooks: 10 minutes (aligned with upstream).
DEFAULT_HTTP_TIMEOUT: float = 600.0

# Characters stripped from interpolated header values to prevent injection.
_CRLF_PATTERN = re.compile(r"[\r\n\x00]")


def _sanitize_header_value(value: str) -> str:
    """Strip CR, LF, and NUL characters from a header value."""
    return _CRLF_PATTERN.sub("", value)


def _interpolate_env_vars(
    value: str,
    allowed_vars: Optional[list],
) -> str:
    """Replace ``$VAR_NAME`` / ``${VAR_NAME}`` in *value* with env values.

    Only variables listed in *allowed_vars* are interpolated.  Unknown
    variables are replaced with empty strings (aligned with upstream).
    """
    allowed_set = set(allowed_vars or [])

    def _replacer(m: re.Match) -> str:
        var_name = m.group(1) or m.group(2)
        if var_name not in allowed_set:
            return ""
        raw = os.environ.get(var_name, "")
        return _sanitize_header_value(raw)

    # Match $VAR_NAME and ${VAR_NAME}
    return re.sub(r"\$\{(\w+)\}|\$(\w+)", _replacer, value)


def exec_http_hook(
    hook: HttpHook,
    hook_input: Dict[str, Any],
    *,
    timeout: Optional[float] = None,
) -> HookResult:
    """Execute an HTTP POST hook and return the result.

    Parameters
    ----------
    hook:
        The HTTP hook definition.
    hook_input:
        Full hook input payload (serialised to JSON for the POST body).
    timeout:
        Override timeout in seconds.
    """
    effective_timeout = timeout or hook.timeout or DEFAULT_HTTP_TIMEOUT

    # Build request headers
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if hook.headers:
        for key, value in hook.headers.items():
            interpolated = _interpolate_env_vars(value, hook.allowed_env_vars)
            headers[key] = _sanitize_header_value(interpolated)

    # Serialise body
    body = json.dumps(hook_input, ensure_ascii=False, default=str)

    try:
        import httpx

        with httpx.Client(timeout=effective_timeout, follow_redirects=False) as client:
            response = client.post(
                hook.url,
                content=body,
                headers=headers,
            )
    except ImportError:
        # Fallback to requests if httpx not available
        try:
            import requests as req_lib

            response_obj = req_lib.post(
                hook.url,
                data=body,
                headers=headers,
                timeout=effective_timeout,
                allow_redirects=False,
            )
            # Normalize to common interface
            class _Resp:
                status_code = response_obj.status_code
                text = response_obj.text
            response = _Resp()
        except Exception as exc:
            logger.error("HTTP hook request failed: %s", exc)
            return HookResult(
                success=False,
                decision="allow",
                outcome="non_blocking_error",
                reason=f"HTTP hook request failed: {exc}",
            )
    except Exception as exc:
        logger.error("HTTP hook request failed: %s", exc)
        return HookResult(
            success=False,
            decision="allow",
            outcome="non_blocking_error",
            reason=f"HTTP hook request failed: {exc}",
        )

    # Parse response body
    response_text = getattr(response, "text", "")
    status_code = getattr(response, "status_code", 0)

    if not response_text or not response_text.strip():
        # Empty body -> success (aligned with upstream: empty body -> {})
        if 200 <= status_code < 300:
            return HookResult(success=True, decision="allow", outcome="success")
        return HookResult(
            success=False,
            decision="allow",
            outcome="non_blocking_error",
            reason=f"HTTP hook returned status {status_code} with empty body",
        )

    parsed = parse_hook_output(response_text)

    if isinstance(parsed, SyncHookOutput):
        return process_hook_output(
            parsed,
            hook_event=str(hook_input.get("hook_event_name", "")),
            command=f"POST {hook.url}",
            exit_code=0 if 200 <= status_code < 300 else 1,
        )

    # Non-JSON response
    if 200 <= status_code < 300:
        return HookResult(success=True, decision="allow", outcome="success")

    return HookResult(
        success=False,
        decision="allow",
        outcome="non_blocking_error",
        reason=f"HTTP hook returned status {status_code}",
    )
