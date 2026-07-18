"""Typed public values for the v6 self-learning review state machine."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

_VALID_KINDS = {"fact", "experience"}
_VALID_APPROVALS = {"auto", "manual"}
_VALID_ACTIONS = {"add", "replace", "remove", "promote_project"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_hash(value: dict[str, str]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(value: Any, *, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def normalize_payload(kind: str, value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("payload must be an object")
    if kind == "fact":
        if set(value) != {"text"}:
            raise ValueError("fact payload must contain only text")
        return {"text": _required_text(value.get("text"), field_name="fact text")}
    if kind == "experience":
        fields = ("trigger", "symptom", "action", "verification")
        if set(value) != set(fields):
            raise ValueError("experience payload must contain trigger, symptom, action, and verification")
        return {name: _required_text(value.get(name), field_name=f"experience {name}") for name in fields}
    raise ValueError("kind must be 'fact' or 'experience'")


def normalize_provenance(value: Any) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("provenance must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("each provenance entry must be an object")
        safe = {str(key): item for key, item in sorted(entry.items()) if item is not None and str(item) != ""}
        fingerprint = canonical_json(safe)
        if fingerprint not in seen:
            normalized.append(safe)
            seen.add(fingerprint)
    return tuple(normalized)


@dataclass(frozen=True)
class CandidateInput:
    kind: str
    memory_key: str
    payload: dict[str, str]
    approval: str = "manual"
    action: str = "add"
    provenance: tuple[dict[str, Any], ...] = ()
    source_run_ids: tuple[str, ...] = ()
    auto_eligible: bool = True
    gate_reasons: tuple[str, ...] = ()

    @classmethod
    def from_value(cls, value: CandidateInput | dict[str, Any]) -> CandidateInput:
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("candidate input must be an object")
        kind = str(value.get("kind") or "").strip().casefold()
        if kind not in _VALID_KINDS:
            raise ValueError("kind must be 'fact' or 'experience'")
        memory_key = _required_text(value.get("memory_key"), field_name="memory_key")
        approval = str(value.get("approval") or "manual").strip().casefold()
        if approval not in _VALID_APPROVALS:
            raise ValueError("approval must be 'auto' or 'manual'")
        action = str(value.get("action") or "add").strip().casefold()
        if action not in _VALID_ACTIONS:
            raise ValueError("action must be add, replace, remove, or promote_project")
        source_run_ids = tuple(
            dict.fromkeys(
                _required_text(item, field_name="source run id") for item in value.get("source_run_ids") or ()
            )
        )
        gate_reasons = tuple(
            dict.fromkeys(str(item).strip() for item in value.get("gate_reasons") or () if str(item).strip())
        )
        return cls(
            kind=kind,
            memory_key=memory_key,
            payload=normalize_payload(kind, value.get("payload")),
            approval=approval,
            action=action,
            provenance=normalize_provenance(value.get("provenance")),
            source_run_ids=source_run_ids,
            auto_eligible=bool(value.get("auto_eligible", True)),
            gate_reasons=gate_reasons,
        )


@dataclass(frozen=True)
class EvidenceGateResult:
    eligible_for_auto: bool = False
    quarantine: bool = False
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reasons",
            tuple(dict.fromkeys(str(reason).strip() for reason in self.reasons if str(reason).strip())),
        )


class EvidenceGate(Protocol):
    """Code-owned evidence boundary; model output alone cannot authorize writes."""

    def evaluate(
        self,
        scope_type: str,
        scope_id: str,
        candidate: CandidateInput,
    ) -> EvidenceGateResult: ...


@dataclass(frozen=True)
class CandidateResult:
    candidate_id: str
    revision: int
    kind: str
    memory_key: str
    payload: dict[str, str]
    state: str
    outcome: str
    item_id: int | None = None
    gate_reasons: tuple[str, ...] = ()
    provenance: tuple[dict[str, Any], ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "revision": self.revision,
            "kind": self.kind,
            "memory_key": self.memory_key,
            "payload": dict(self.payload),
            "state": self.state,
            "outcome": self.outcome,
            "item_id": self.item_id,
            "gate_reasons": list(self.gate_reasons),
            "provenance": [dict(entry) for entry in self.provenance],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ReviewBatchResult:
    review_id: str
    scope_type: str
    scope_id: str
    status: str
    dry_run: bool
    candidates: tuple[CandidateResult, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "status": self.status,
            "dry_run": self.dry_run,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


class ReviewConflictError(RuntimeError):
    """A decision used a stale candidate revision or incompatible state."""
