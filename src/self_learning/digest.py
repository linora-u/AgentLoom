"""One safety boundary for every value sent to a self-learning model."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from typing import Any

from .redaction import (
    redact_text,
    redact_value,
    require_safe_identity,
    scan_injection_patterns,
    scan_structured_injection_patterns,
)

BLOCKED_TEXT = "[BLOCKED]"
_DEFAULT_MAX_CHARS = 14000
_DEFAULT_FRAGMENT_MAX_CHARS = 3000
_MAX_REDACTION_PASSES = 10


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=lambda item: str(item))
    return str(value)


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=_json_default,
    )


def _redact_to_fixed_point(text: str, *, max_chars: int | None = None) -> str | None:
    """Return a bounded serialization stable under the text safety boundary.

    Recursive value redaction runs before serialization, but an embedded JSON
    string gains another quote layer when its parent is serialized. Truncating
    that representation can also turn valid nested JSON into a conservative
    malformed-text match. Re-apply the same redactor until the exact bytes to
    be persisted are stable; a cycle or excessive depth fails closed.
    """
    current = text if max_chars is None else text[:max_chars]
    seen = {current}
    for _ in range(_MAX_REDACTION_PASSES):
        candidate = redact_text(current)
        if max_chars is not None:
            candidate = candidate[:max_chars]
        if candidate == current:
            return current
        if candidate in seen:
            return None
        seen.add(candidate)
        current = candidate
    return None


@dataclass(frozen=True)
class DigestFragment:
    ref: str
    kind: str
    text: str
    blocked: bool


class DigestBuilder:
    """Build bounded JSON fragments after redaction and threat scanning.

    The order is intentional: secrets are removed recursively first, then the
    redacted representation is scanned for prompt injection. A finding blocks
    the whole fragment, so partially malicious text can never be spliced into
    model context.
    """

    def __init__(
        self,
        *,
        max_chars: int = _DEFAULT_MAX_CHARS,
        fragment_max_chars: int = _DEFAULT_FRAGMENT_MAX_CHARS,
    ) -> None:
        self.max_chars = max(256, int(max_chars))
        self.fragment_max_chars = max(1, int(fragment_max_chars))
        self._fragments: list[DigestFragment] = []
        self._refs: set[str] = set()

    def add(
        self,
        *,
        ref: str,
        kind: str,
        value: Any,
        max_chars: int | None = None,
    ) -> DigestBuilder:
        ref = require_safe_identity(ref, field="digest fragment ref")
        kind = require_safe_identity(kind, field="digest fragment kind")
        if ref in self._refs:
            raise ValueError(f"duplicate digest fragment ref: {ref}")

        redacted = redact_value(value)
        serialized = _redact_to_fixed_point(_as_text(redacted))
        blocked = (
            serialized is None
            or bool(scan_injection_patterns(serialized))
            or bool(scan_structured_injection_patterns(redacted))
        )
        if blocked:
            text = BLOCKED_TEXT
        else:
            limit = self.fragment_max_chars if max_chars is None else max(0, int(max_chars))
            bounded = _redact_to_fixed_point(serialized, max_chars=limit)
            blocked = (
                bounded is None
                or redact_text(bounded) != bounded
                or bool(scan_injection_patterns(bounded))
            )
            text = BLOCKED_TEXT if blocked else bounded
        self._fragments.append(DigestFragment(ref=ref, kind=kind, text=text, blocked=blocked))
        self._refs.add(ref)
        return self

    def _bounded_fragments(self) -> list[DigestFragment]:
        included: list[DigestFragment] = []
        for fragment in self._fragments:
            candidate = [*included, fragment]
            payload = {"version": 1, "fragments": [asdict(item) for item in candidate]}
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) <= self.max_chars:
                included.append(fragment)
        # A model sees the complete fragment array, so the safety boundary must
        # scan that same view. Two individually harmless strings can otherwise
        # join into one instruction across JSON elements. When the collection
        # is tainted there is no trustworthy way to assign the continuation to
        # only one element, so fail closed for the whole included collection.
        visible_texts = [
            fragment.text
            for fragment in included
            if not fragment.blocked and fragment.text != BLOCKED_TEXT
        ]
        if scan_structured_injection_patterns(visible_texts):
            return [
                replace(fragment, text=BLOCKED_TEXT, blocked=True)
                for fragment in included
            ]
        return included

    @property
    def fragments(self) -> tuple[DigestFragment, ...]:
        return tuple(self._bounded_fragments())

    def to_json(self) -> str:
        payload = {"version": 1, "fragments": [asdict(item) for item in self._bounded_fragments()]}
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
