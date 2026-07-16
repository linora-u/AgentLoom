"""Tool-bound provenance for completed-run memory review.

This module deliberately lives above the smolagents and self-learning
packages.  Both sides share its marker types without importing either runtime
package during schema bootstrap.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

TRUSTED_MEMORY_EVIDENCE_ATTR = (
    "_agentloom_trusted_memory_evidence_extractor_v1"
)
TRUSTED_MEMORY_EVIDENCE_RESPONSE_KEY = (
    "_agentloom_trusted_memory_evidence_v1"
)
TRUSTED_MEMORY_EVIDENCE_KIND = "durable_fact"
TRUSTED_MEMORY_EVIDENCE_SCOPES = frozenset({"project", "application"})

_MAX_EVIDENCE_ITEMS = 20
_MAX_EVIDENCE_SOURCE_CHARS = 160
_MAX_EVIDENCE_TEXT_CHARS = 4_000


class InvalidTrustedMemoryEvidence(ValueError):
    """Raised without payload content when an extractor violates the contract."""


class TrustedMemoryEvidenceEnvelope(list[dict[str, str]]):
    """JSON-compatible in-process marker that cannot survive JSONL import."""


def trusted_memory_evidence(
    extractor: Callable[[Any], Iterable[Mapping[str, Any]] | None],
) -> Callable[[Any], Any]:
    """Bind a code-owned evidence extractor to a function or Tool instance.

    The extractor must explicitly classify every mapping as ``durable_fact``
    and provide an explicit ``scope`` plus non-empty ``source`` and ``text``
    strings. Every text must be a literal substring of the raw result. Invalid
    output fails closed; scope is never inferred from prose or model output.
    """

    if not callable(extractor):
        raise TypeError("trusted memory evidence extractor must be callable")

    def decorate(tool_callable: Any) -> Any:
        setattr(tool_callable, TRUSTED_MEMORY_EVIDENCE_ATTR, extractor)
        return tool_callable

    return decorate


def _raw_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
    except Exception as exc:
        raise InvalidTrustedMemoryEvidence(
            "tool result could not be serialized"
        ) from exc


def extract_trusted_memory_evidence(
    tool_instance: Any,
    raw_result: Any,
) -> tuple[dict[str, str], ...]:
    """Validate and return one tool's code-declared evidence envelope."""

    extractor = getattr(tool_instance, TRUSTED_MEMORY_EVIDENCE_ATTR, None)
    if not callable(extractor):
        return ()

    try:
        extracted = extractor(raw_result)
    except Exception as exc:
        raise InvalidTrustedMemoryEvidence("evidence extractor failed") from exc
    if extracted is None:
        return ()
    if isinstance(extracted, (str, bytes, Mapping)):
        raise InvalidTrustedMemoryEvidence(
            "evidence extractor must return an iterable of mappings"
        )

    raw_text = _raw_result_text(raw_result)
    validated: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    try:
        iterator = iter(extracted)
    except TypeError as exc:
        raise InvalidTrustedMemoryEvidence(
            "evidence extractor returned a non-iterable"
        ) from exc

    for entry in iterator:
        if len(validated) >= _MAX_EVIDENCE_ITEMS:
            raise InvalidTrustedMemoryEvidence("too many evidence entries")
        if not isinstance(entry, Mapping):
            raise InvalidTrustedMemoryEvidence("evidence entry must be a mapping")
        source = entry.get("source")
        text = entry.get("text")
        kind = entry.get("kind")
        scope = entry.get("scope")
        if kind != TRUSTED_MEMORY_EVIDENCE_KIND:
            raise InvalidTrustedMemoryEvidence(
                "evidence kind must be durable_fact"
            )
        if scope not in TRUSTED_MEMORY_EVIDENCE_SCOPES:
            raise InvalidTrustedMemoryEvidence(
                "evidence scope must be project or application"
            )
        if not isinstance(source, str) or not source.strip():
            raise InvalidTrustedMemoryEvidence("evidence source must be non-empty")
        if not isinstance(text, str) or not text:
            raise InvalidTrustedMemoryEvidence("evidence text must be non-empty")
        source = source.strip()
        if len(source) > _MAX_EVIDENCE_SOURCE_CHARS:
            raise InvalidTrustedMemoryEvidence("evidence source is too long")
        if len(text) > _MAX_EVIDENCE_TEXT_CHARS:
            raise InvalidTrustedMemoryEvidence("evidence text is too long")
        if text not in raw_text:
            raise InvalidTrustedMemoryEvidence(
                "evidence text is absent from the raw tool result"
            )
        key = (scope, source, text)
        if key in seen:
            continue
        seen.add(key)
        validated.append(
            {
                "kind": TRUSTED_MEMORY_EVIDENCE_KIND,
                "scope": scope,
                "source": source,
                "text": text,
            }
        )
    return tuple(validated)
