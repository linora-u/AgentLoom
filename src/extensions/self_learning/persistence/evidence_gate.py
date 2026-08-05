"""Code-owned SQLite evidence gates for automatic self-learning approval.

The review model may summarize evidence, but it never decides whether a
candidate is safe to activate.  This module re-reads the immutable runtime
ledger and proves the required bindings without mutating the database.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..redaction import BLOCKED_TEXT, sanitize_text_fragment_with_taint
from ..review_types import CandidateInput, EvidenceGateResult, payload_hash
from .database import SelfLearningDatabase

_SAFETY_TAINT_KEY = "_safety_tainted"


@dataclass(frozen=True)
class _BoundEvent:
    row_id: int
    event_id: str
    run_id: str
    root_run_id: str
    application_id: str
    tool_name: str
    tool_call_id: str
    event_type: str
    status: str
    input_json: str
    output_json: str
    content_text: str
    metadata_json: str


@dataclass(frozen=True)
class _ExperienceChain:
    root_run_id: str
    action_fingerprint: tuple[str, str]
    trigger_binding: str
    symptom_binding: str
    action_binding: str
    failure_event_id: str
    success_event_id: str
    verifier_event_id: str = ""


def _pending(*reasons: str) -> EvidenceGateResult:
    return EvidenceGateResult(reasons=tuple(reasons))


def _quarantine(*reasons: str) -> EvidenceGateResult:
    return EvidenceGateResult(quarantine=True, reasons=tuple(reasons))


def _eligible() -> EvidenceGateResult:
    return EvidenceGateResult(eligible_for_auto=True)


def _json_value(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _canonical_json_text(value: str) -> str:
    decoded = _json_value(value, None)
    if decoded is None:
        return " ".join(str(value or "").split())
    return json.dumps(
        decoded,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _strict_json_binding(value: str) -> str:
    """Return one unambiguous canonical JSON value or an empty binding.

    Experience auto-approval deliberately accepts copied structured evidence,
    not a model paraphrase. Rejecting non-JSON suffixes and duplicate object
    keys prevents a valid evidence fragment from disguising extra steps.
    """

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = item
        return result

    try:
        decoded = json.loads(str(value), object_pairs_hook=unique_object)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if decoded in (None, "", [], {}):
        return ""
    return json.dumps(
        decoded,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _unsafe(value: Any) -> bool:
    safe, tainted = sanitize_text_fragment_with_taint(value)
    return bool(tainted or safe == BLOCKED_TEXT or BLOCKED_TEXT in str(value or ""))


class SQLiteEvidenceGate:
    """Read-only hard gate injected into :class:`ReviewEngine`.

    Missing evidence and transient SQLite failures remain manually reviewable.
    Provenance tampering, cross-scope binding, and unsafe content are
    quarantined because a human should inspect the source before applying it.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self._database = SelfLearningDatabase(self.db_path)

    def _connect(self) -> sqlite3.Connection:
        # ``mode=ro`` is part of the security boundary: evaluating a model
        # candidate must not create a database or repair evidence in place.
        return self._database.connect(read_only=True)

    @staticmethod
    def _candidate_is_unsafe(candidate: CandidateInput) -> bool:
        values: list[Any] = [candidate.memory_key, candidate.payload]
        values.extend(candidate.provenance)
        values.extend(candidate.source_run_ids)
        return any(_unsafe(value) for value in values)

    @staticmethod
    def _event_columns(conn: sqlite3.Connection) -> set[str]:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(events)").fetchall()}

    @classmethod
    def _event_select(cls, conn: sqlite3.Connection) -> str:
        columns = cls._event_columns(conn)
        required = {
            "id",
            "event_id",
            "run_id",
            "root_run_id",
            "application_id",
            "tool_name",
            "event_type",
            "status",
            "input_json",
            "output_json",
            "content_text",
            "metadata_json",
        }
        if not required.issubset(columns):
            raise sqlite3.OperationalError("events schema is missing evidence columns")
        tool_call_expression = "COALESCE(tool_call_id, '')" if "tool_call_id" in columns else "''"
        return f"""
            SELECT id,event_id,run_id,root_run_id,application_id,tool_name,
                   {tool_call_expression} AS tool_call_id,
                   event_type,status,input_json,output_json,content_text,
                   metadata_json
            FROM events
        """

    @staticmethod
    def _bound_event(row: sqlite3.Row) -> _BoundEvent:
        metadata_json = str(row["metadata_json"] or "{}")
        metadata = _json_value(metadata_json, {})
        stored_tool_call_id = str(row["tool_call_id"] or "")
        if not stored_tool_call_id and isinstance(metadata, Mapping):
            stored_tool_call_id = str(metadata.get("tool_call_id") or "")
        run_id = str(row["run_id"] or "")
        root_run_id = str(row["root_run_id"] or run_id)
        return _BoundEvent(
            row_id=int(row["id"]),
            event_id=str(row["event_id"] or ""),
            run_id=run_id,
            root_run_id=root_run_id,
            application_id=str(row["application_id"] or ""),
            tool_name=str(row["tool_name"] or ""),
            tool_call_id=stored_tool_call_id,
            event_type=str(row["event_type"] or ""),
            status=str(row["status"] or ""),
            input_json=str(row["input_json"] or "{}"),
            output_json=str(row["output_json"] or "{}"),
            content_text=str(row["content_text"] or ""),
            metadata_json=metadata_json,
        )

    @staticmethod
    def _event_is_unsafe(event: _BoundEvent) -> bool:
        metadata = _json_value(event.metadata_json, {})
        if isinstance(metadata, Mapping) and metadata.get(_SAFETY_TAINT_KEY) is True:
            return True
        return any(
            _unsafe(value)
            for value in (
                event.event_id,
                event.root_run_id,
                event.application_id,
                event.tool_name,
                event.tool_call_id,
                event.input_json,
                event.output_json,
                event.content_text,
                event.metadata_json,
            )
        )

    @classmethod
    def _load_event(
        cls,
        conn: sqlite3.Connection,
        event_id: str,
    ) -> _BoundEvent | None:
        row = conn.execute(
            f"{cls._event_select(conn)} WHERE event_id=?",
            (event_id,),
        ).fetchone()
        return cls._bound_event(row) if row is not None else None

    @classmethod
    def _load_root_events(
        cls,
        conn: sqlite3.Connection,
        root_run_id: str,
        application_id: str,
    ) -> list[_BoundEvent]:
        rows = conn.execute(
            f"""
            {cls._event_select(conn)}
            WHERE COALESCE(NULLIF(root_run_id,''),run_id)=?
              AND application_id=?
            ORDER BY id
            """,
            (root_run_id, application_id),
        ).fetchall()
        return [cls._bound_event(row) for row in rows]

    @classmethod
    def _completed_root_application(
        cls,
        conn: sqlite3.Connection,
        root_run_id: str,
    ) -> str | None:
        row = conn.execute(
            "SELECT application_id,status FROM runs WHERE run_id=?",
            (root_run_id,),
        ).fetchone()
        if row is None or str(row["status"] or "").casefold() != "completed":
            return None
        completion = conn.execute(
            """
            SELECT 1 FROM events
            WHERE run_id=?
              AND COALESCE(NULLIF(root_run_id,''),run_id)=?
              AND event_type='run_completed'
              AND status='completed'
            LIMIT 1
            """,
            (root_run_id, root_run_id),
        ).fetchone()
        if completion is None:
            return None
        return str(row["application_id"] or "")

    @classmethod
    def _bind_provenance(
        cls,
        conn: sqlite3.Connection,
        candidate: CandidateInput,
        *,
        expected_application_id: str | None,
    ) -> tuple[list[_BoundEvent], EvidenceGateResult | None]:
        if not candidate.provenance:
            return [], _pending("provenance_missing")
        allowed_roots = set(candidate.source_run_ids)
        events: list[_BoundEvent] = []
        seen: set[str] = set()
        for entry in candidate.provenance:
            root_run_id = str(entry.get("root_run_id") or "")
            event_id = str(entry.get("event_id") or "")
            if not root_run_id or not event_id:
                return [], _pending("provenance_binding_incomplete")
            if allowed_roots and root_run_id not in allowed_roots:
                return [], _quarantine("provenance_source_run_mismatch")
            event = cls._load_event(conn, event_id)
            if event is None:
                return [], _quarantine("provenance_event_missing")
            if event.root_run_id != root_run_id:
                return [], _quarantine("provenance_root_mismatch")
            claimed_application = str(entry.get("application_id") or "")
            if claimed_application and claimed_application != event.application_id:
                return [], _quarantine("provenance_application_mismatch")
            if expected_application_id is not None and event.application_id != expected_application_id:
                return [], _quarantine("provenance_application_mismatch")
            claimed_tool_call_id = str(entry.get("tool_call_id") or "")
            if claimed_tool_call_id and claimed_tool_call_id != event.tool_call_id:
                return [], _quarantine("provenance_tool_call_mismatch")
            root_application = cls._completed_root_application(conn, root_run_id)
            if root_application is None:
                return [], _pending("provenance_root_not_completed")
            if root_application != event.application_id:
                return [], _quarantine("provenance_root_application_mismatch")
            if cls._event_is_unsafe(event):
                return [], _quarantine("unsafe_provenance_event")
            if event.event_id not in seen:
                events.append(event)
                seen.add(event.event_id)
        return events, None

    @staticmethod
    def _trusted_rows(
        conn: sqlite3.Connection,
        event_ids: Sequence[str],
        text: str,
    ) -> list[sqlite3.Row]:
        if not event_ids:
            return []
        placeholders = ",".join("?" for _ in event_ids)
        return conn.execute(
            f"""
            SELECT event_id,root_run_id,tool_name,kind,scope_type,scope_id,
                   source,text
            FROM trusted_review_evidence
            WHERE event_id IN ({placeholders})
              AND kind='durable_fact'
              AND text=?
            """,
            (*event_ids, text),
        ).fetchall()

    @staticmethod
    def _trusted_row_binding_is_valid(
        row: sqlite3.Row,
        events: Mapping[str, _BoundEvent],
    ) -> bool:
        event = events.get(str(row["event_id"] or ""))
        return bool(
            event is not None
            and str(row["root_run_id"] or "") == event.root_run_id
            and str(row["tool_name"] or "") == event.tool_name
            and event.event_type == "tool_result"
            and event.status == "completed"
            and not _unsafe(row["source"])
            and not _unsafe(row["text"])
        )

    @classmethod
    def _evaluate_application_fact(
        cls,
        conn: sqlite3.Connection,
        application_id: str,
        candidate: CandidateInput,
    ) -> EvidenceGateResult:
        events, failure = cls._bind_provenance(
            conn,
            candidate,
            expected_application_id=application_id,
        )
        if failure is not None:
            return failure
        event_map = {event.event_id: event for event in events}
        rows = cls._trusted_rows(
            conn,
            list(event_map),
            candidate.payload["text"],
        )
        for row in rows:
            if not cls._trusted_row_binding_is_valid(row, event_map):
                return _quarantine("trusted_evidence_binding_mismatch")
            if str(row["scope_type"] or "") == "application" and str(row["scope_id"] or "") == application_id:
                return _eligible()
            return _quarantine("trusted_evidence_scope_mismatch")
        return _pending("application_fact_exact_trusted_evidence_missing")

    @staticmethod
    def _matching_application_facts(
        conn: sqlite3.Connection,
        candidate: CandidateInput,
    ) -> set[str]:
        rows = conn.execute(
            """
            SELECT scope_id,payload_json,activation_source
            FROM memory_items
            WHERE scope_type='application'
              AND kind='fact'
              AND memory_key=?
              AND payload_hash=?
              AND state IN ('active_unreviewed','active_confirmed')
            """,
            (candidate.memory_key, payload_hash(candidate.payload)),
        ).fetchall()
        applications: set[str] = set()
        for row in rows:
            if str(row["activation_source"] or "") == "migration":
                continue
            payload = _json_value(row["payload_json"], None)
            if payload == candidate.payload:
                applications.add(str(row["scope_id"] or ""))
        applications.discard("")
        return applications

    @classmethod
    def _evaluate_project_fact(
        cls,
        conn: sqlite3.Connection,
        candidate: CandidateInput,
    ) -> EvidenceGateResult:
        if candidate.provenance:
            events, failure = cls._bind_provenance(
                conn,
                candidate,
                expected_application_id=None,
            )
            if failure is not None:
                return failure
            event_map = {event.event_id: event for event in events}
            rows = cls._trusted_rows(
                conn,
                list(event_map),
                candidate.payload["text"],
            )
            for row in rows:
                if not cls._trusted_row_binding_is_valid(row, event_map):
                    return _quarantine("trusted_evidence_binding_mismatch")
                scope_type = str(row["scope_type"] or "")
                scope_id = str(row["scope_id"] or "")
                if scope_type == "project" and scope_id == "project":
                    return _eligible()
                if scope_type == "application":
                    return _quarantine("application_evidence_cannot_expand_to_project")
                return _quarantine("trusted_evidence_scope_mismatch")
            return _pending("project_fact_direct_evidence_missing")

        applications = cls._matching_application_facts(conn, candidate)
        if len(applications) >= 2:
            return _eligible()
        return _pending("project_fact_requires_two_verified_applications")

    @staticmethod
    def _paired_calls(
        events: Sequence[_BoundEvent],
    ) -> dict[str, list[_BoundEvent]]:
        calls: dict[str, list[_BoundEvent]] = {}
        for event in events:
            if event.event_type == "tool_call" and event.tool_call_id:
                calls.setdefault(event.tool_call_id, []).append(event)
        return calls

    @staticmethod
    def _preceding_call(
        calls: Mapping[str, Sequence[_BoundEvent]],
        event: _BoundEvent,
        *,
        after_row_id: int = -1,
    ) -> _BoundEvent | None:
        eligible = [call for call in calls.get(event.tool_call_id, ()) if after_row_id < call.row_id < event.row_id]
        return eligible[-1] if eligible else None

    @classmethod
    def _verifier_events(
        cls,
        conn: sqlite3.Connection,
        *,
        events: Sequence[_BoundEvent],
        calls: Mapping[str, Sequence[_BoundEvent]],
        success: _BoundEvent,
        application_id: str,
        verification: str,
        anchored_event_ids: set[str],
    ) -> list[_BoundEvent]:
        candidates = [
            event
            for event in events
            if event.row_id > success.row_id
            and event.event_type == "tool_result"
            and event.status == "completed"
            and event.tool_call_id
            and event.event_id in anchored_event_ids
            and cls._preceding_call(
                calls,
                event,
                after_row_id=success.row_id,
            )
            is not None
        ]
        if not candidates:
            return []
        rows = cls._trusted_rows(
            conn,
            [event.event_id for event in candidates],
            verification,
        )
        event_map = {event.event_id: event for event in candidates}
        verified_ids = {
            str(row["event_id"])
            for row in rows
            if cls._trusted_row_binding_is_valid(row, event_map)
            and str(row["scope_type"] or "") == "application"
            and str(row["scope_id"] or "") == application_id
        }
        return [event for event in candidates if event.event_id in verified_ids]

    @classmethod
    def _find_experience_chains(
        cls,
        conn: sqlite3.Connection,
        *,
        root_run_id: str,
        application_id: str,
        verification: str,
        anchored_event_ids: set[str],
    ) -> tuple[list[_ExperienceChain], bool]:
        events = cls._load_root_events(conn, root_run_id, application_id)
        calls = cls._paired_calls(events)
        chains: list[_ExperienceChain] = []
        unsafe_chain = False
        for failure in events:
            if not (failure.event_type == "tool_error" or failure.status.casefold() == "failed"):
                continue
            if not failure.tool_call_id:
                continue
            failed_call = cls._preceding_call(calls, failure)
            if failed_call is None:
                continue
            failed_signature = (
                failed_call.tool_name,
                _canonical_json_text(failed_call.input_json),
            )
            for success in events:
                if not (
                    success.row_id > failure.row_id
                    and success.event_type == "tool_result"
                    and success.status == "completed"
                    and success.tool_call_id
                    and success.tool_call_id != failure.tool_call_id
                    and success.event_id in anchored_event_ids
                ):
                    continue
                changed_call = cls._preceding_call(
                    calls,
                    success,
                    after_row_id=failure.row_id,
                )
                if changed_call is None:
                    continue
                action_fingerprint = (
                    changed_call.tool_name,
                    _canonical_json_text(changed_call.input_json),
                )
                if action_fingerprint == failed_signature:
                    continue
                chain_events = (failed_call, failure, changed_call, success)
                if any(cls._event_is_unsafe(event) for event in chain_events):
                    unsafe_chain = True
                    continue
                verifiers = cls._verifier_events(
                    conn,
                    events=events,
                    calls=calls,
                    success=success,
                    application_id=application_id,
                    verification=verification,
                    anchored_event_ids=anchored_event_ids,
                )
                if verifiers:
                    for verifier in verifiers:
                        if cls._event_is_unsafe(verifier):
                            unsafe_chain = True
                            continue
                        chains.append(
                            _ExperienceChain(
                                root_run_id=root_run_id,
                                action_fingerprint=action_fingerprint,
                                trigger_binding=_strict_json_binding(failed_call.input_json),
                                symptom_binding=_strict_json_binding(failure.output_json),
                                action_binding=_strict_json_binding(changed_call.input_json),
                                failure_event_id=failure.event_id,
                                success_event_id=success.event_id,
                                verifier_event_id=verifier.event_id,
                            )
                        )
                else:
                    chains.append(
                        _ExperienceChain(
                            root_run_id=root_run_id,
                            action_fingerprint=action_fingerprint,
                            trigger_binding=_strict_json_binding(failed_call.input_json),
                            symptom_binding=_strict_json_binding(failure.output_json),
                            action_binding=_strict_json_binding(changed_call.input_json),
                            failure_event_id=failure.event_id,
                            success_event_id=success.event_id,
                        )
                    )
        return chains, unsafe_chain

    @staticmethod
    def _content_bound_experience_chains(
        chains: Sequence[_ExperienceChain],
        candidate: CandidateInput,
    ) -> tuple[list[_ExperienceChain], str]:
        """Bind all candidate claims to their roles in the same chain."""

        trigger = _strict_json_binding(candidate.payload["trigger"])
        symptom = _strict_json_binding(candidate.payload["symptom"])
        action = _strict_json_binding(candidate.payload["action"])

        trigger_bound = [chain for chain in chains if trigger and chain.trigger_binding == trigger]
        if not trigger_bound:
            return [], "experience_trigger_not_bound"
        symptom_bound = [chain for chain in trigger_bound if symptom and chain.symptom_binding == symptom]
        if not symptom_bound:
            return [], "experience_symptom_not_bound"
        action_bound = [chain for chain in symptom_bound if action and chain.action_binding == action]
        if not action_bound:
            return [], "experience_action_not_bound"
        return action_bound, ""

    @classmethod
    def _evaluate_application_experience(
        cls,
        conn: sqlite3.Connection,
        application_id: str,
        candidate: CandidateInput,
    ) -> EvidenceGateResult:
        events, failure = cls._bind_provenance(
            conn,
            candidate,
            expected_application_id=application_id,
        )
        if failure is not None:
            return failure
        anchors_by_root: dict[str, set[str]] = {}
        for event in events:
            anchors_by_root.setdefault(event.root_run_id, set()).add(event.event_id)

        chains: list[_ExperienceChain] = []
        unsafe_chain = False
        for root_run_id, anchors in anchors_by_root.items():
            root_chains, root_unsafe = cls._find_experience_chains(
                conn,
                root_run_id=root_run_id,
                application_id=application_id,
                verification=candidate.payload["verification"],
                anchored_event_ids=anchors,
            )
            chains.extend(root_chains)
            unsafe_chain = unsafe_chain or root_unsafe
        if unsafe_chain:
            return _quarantine("unsafe_experience_evidence")
        if not chains:
            return _pending("experience_stable_tool_call_chain_missing")
        chains, binding_failure = cls._content_bound_experience_chains(
            chains,
            candidate,
        )
        if binding_failure:
            return _pending(binding_failure)
        if any(chain.verifier_event_id for chain in chains):
            return _eligible()

        roots_by_action: dict[tuple[str, str], set[str]] = {}
        for chain in chains:
            roots_by_action.setdefault(chain.action_fingerprint, set()).add(chain.root_run_id)
        if any(len(roots) >= 2 for roots in roots_by_action.values()):
            return _eligible()
        return _pending("experience_requires_verifier_or_two_repeated_roots")

    @classmethod
    def _matching_application_experiences(
        cls,
        conn: sqlite3.Connection,
        candidate: CandidateInput,
    ) -> set[str]:
        rows = conn.execute(
            """
            SELECT scope_id,payload_json,provenance_json,activation_source
            FROM memory_items
            WHERE scope_type='application'
              AND kind='experience'
              AND memory_key=?
              AND payload_hash=?
              AND state IN ('active_unreviewed','active_confirmed')
            ORDER BY scope_id,id
            """,
            (candidate.memory_key, payload_hash(candidate.payload)),
        ).fetchall()
        verified: set[str] = set()
        for row in rows:
            application_id = str(row["scope_id"] or "")
            if not application_id or application_id in verified or str(row["activation_source"] or "") == "migration":
                continue
            payload = _json_value(row["payload_json"], None)
            provenance = _json_value(row["provenance_json"], None)
            if payload != candidate.payload or not isinstance(provenance, list):
                continue
            source_run_ids = tuple(
                dict.fromkeys(
                    str(entry.get("root_run_id") or "")
                    for entry in provenance
                    if isinstance(entry, Mapping) and entry.get("root_run_id")
                )
            )
            try:
                application_candidate = CandidateInput.from_value(
                    {
                        "kind": "experience",
                        "memory_key": candidate.memory_key,
                        "payload": payload,
                        "approval": "auto",
                        "provenance": provenance,
                        "source_run_ids": source_run_ids,
                    }
                )
            except (TypeError, ValueError):
                continue
            result = cls._evaluate_application_experience(
                conn,
                application_id,
                application_candidate,
            )
            if result.eligible_for_auto:
                verified.add(application_id)
        return verified

    @classmethod
    def _evaluate_project_experience(
        cls,
        conn: sqlite3.Connection,
        candidate: CandidateInput,
    ) -> EvidenceGateResult:
        # A model cannot turn raw events from one Application into Project
        # policy. Project experience is derived only from already active,
        # independently re-verified Application experience records.
        if candidate.provenance:
            return _quarantine("project_experience_raw_event_promotion_forbidden")
        applications = cls._matching_application_experiences(conn, candidate)
        if len(applications) >= 2:
            return _eligible()
        return _pending("project_experience_requires_two_verified_applications")

    def evaluate(
        self,
        scope_type: str,
        scope_id: str,
        candidate: CandidateInput,
    ) -> EvidenceGateResult:
        normalized_scope = str(scope_type or "").strip().casefold()
        normalized_id = str(scope_id or "").strip()
        if normalized_scope not in {"project", "application"}:
            return _quarantine("invalid_scope_binding")
        if normalized_scope == "project" and normalized_id != "project":
            return _quarantine("invalid_scope_binding")
        if normalized_scope == "application" and not normalized_id:
            return _quarantine("invalid_scope_binding")
        if self._candidate_is_unsafe(candidate):
            return _quarantine("unsafe_candidate_content")

        try:
            with self._connect() as conn:
                if normalized_scope == "application" and candidate.kind == "fact":
                    return self._evaluate_application_fact(
                        conn,
                        normalized_id,
                        candidate,
                    )
                if normalized_scope == "project" and candidate.kind == "fact":
                    return self._evaluate_project_fact(conn, candidate)
                if normalized_scope == "application" and candidate.kind == "experience":
                    return self._evaluate_application_experience(
                        conn,
                        normalized_id,
                        candidate,
                    )
                if normalized_scope == "project" and candidate.kind == "experience":
                    return self._evaluate_project_experience(conn, candidate)
        except (OSError, sqlite3.Error):
            # Automatic approval must fail closed, while leaving a valid model
            # candidate available for human pre-review and retry.
            return _pending("evidence_store_unavailable")
        return _pending("unsupported_evidence_kind")


__all__ = ["SQLiteEvidenceGate"]
