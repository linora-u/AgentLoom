"""Secret redaction shared by self-learning storage and tools."""

from __future__ import annotations

import json
import re
from typing import Any

_REDACTED = "[REDACTED]"

_PREFIX_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(authorization\s*:\s*basic\s+)[A-Za-z0-9._~+/=-]{12,}"),
)
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|pwd)\b"
    r"(\s*[:=]\s*)(['\"]?)([^'\"\s,;]{6,})(\3)"
)
_BARE_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b(?:ark|volc|bearer)_[A-Za-z0-9_-]{16,}\b"),
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
)


def redact_text(value: Any, *, max_chars: int | None = None) -> str:
    """Return a string with common credential shapes replaced."""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)

    for pattern in _PREFIX_SECRET_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)}{_REDACTED}", text)
    text = _KEY_VALUE_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{match.group(3)}{_REDACTED}{match.group(5)}",
        text,
    )
    for pattern in _BARE_SECRET_PATTERNS:
        text = pattern.sub(_REDACTED, text)

    if max_chars is not None and max_chars >= 0 and len(text) > max_chars:
        return text[:max_chars] + f"\n...[truncated {len(text) - max_chars} chars]"
    return text


def redact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """Redact a JSON-like mapping by serializing and parsing it back."""
    text = redact_text(value)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except Exception:
        return {"value": text}
