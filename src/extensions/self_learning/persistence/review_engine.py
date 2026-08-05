"""Transactional v6 review state machine for typed self-learning memory."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from ..application_scope import safe_application_id
from ..paths import memory_config
from ..redaction import (
    require_safe_identity,
    sanitize_text_fragment_with_taint,
    sanitize_value_fragments_with_taint,
)
from ..review_types import (
    CandidateInput,
    CandidateResult,
    EvidenceGate,
    EvidenceGateResult,
    ReviewBatchResult,
    ReviewConflictError,
    normalize_payload,
    normalize_provenance,
    payload_hash,
)
from .database import SelfLearningDatabase, serialized_write_transaction
from .ledger import SelfLearningLedger

_ACTIVE_STATES = {"active_unreviewed", "active_confirmed"}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_loads(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


class ReviewEngine:
    """Persist review outcomes and apply only code-authorized add mutations."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        evidence_gate: EvidenceGate | None = None,
        capacity_policy: Mapping[str, Any] | None = None,
    ) -> None:
        self._ledger = SelfLearningLedger(db_path)
        self.db_path = self._ledger.db_path
        self._database = SelfLearningDatabase(self.db_path)
        self._evidence_gate = evidence_gate
        policy = dict(capacity_policy or memory_config())
        budgets = policy.get("scope_budgets")
        self._scope_budgets = {
            "project": int((budgets or {}).get("project") or 0),
            "application": int((budgets or {}).get("application") or 0),
        }
        self._max_item_chars = int(policy.get("max_item_chars") or 0)

    def _connect(self) -> sqlite3.Connection:
        return self._database.connect(foreign_keys=True)

    @staticmethod
    def _validate_scope(scope_type: str, scope_id: str) -> tuple[str, str]:
        normalized = str(scope_type or "").strip().casefold()
        if normalized not in {"project", "application"}:
            raise ValueError("scope_type must be 'project' or 'application'")
        if normalized == "project":
            if str(scope_id or "").strip() not in {"", "project"}:
                raise ValueError("project scope_id must be 'project'")
            return "project", "project"
        return "application", safe_application_id(require_safe_identity(scope_id, field="application scope id"))

    @staticmethod
    def _normalize_human_payload(kind: str, value: Any) -> dict[str, str]:
        normalized = normalize_payload(kind, value)
        for text in normalized.values():
            safe_text, tainted = sanitize_text_fragment_with_taint(text)
            if tainted or safe_text != text:
                raise ValueError("human correction payload contains sensitive or blocked text")
        return normalized

    @staticmethod
    def _normalize_source_runs(
        source_runs: Sequence[dict[str, Any] | tuple[Any, ...]] | None,
    ) -> tuple[tuple[str, str], ...]:
        normalized: list[tuple[str, str]] = []
        for source_run in source_runs or ():
            if isinstance(source_run, dict):
                root_run_id = source_run.get("root_run_id") or source_run.get("run_id")
                application_id = source_run.get("application_id") or ""
            elif isinstance(source_run, tuple) and 1 <= len(source_run) <= 2:
                root_run_id = source_run[0]
                application_id = source_run[1] if len(source_run) == 2 else ""
            else:
                raise ValueError("source_runs entries must be objects or 1-2 item tuples")
            normalized.append(
                (
                    require_safe_identity(root_run_id, field="source root run id"),
                    require_safe_identity(
                        application_id,
                        field="source application id",
                        allow_empty=True,
                    ),
                )
            )
        return tuple(dict.fromkeys(normalized))

    def _gate(
        self,
        scope_type: str,
        scope_id: str,
        candidate: CandidateInput,
    ) -> EvidenceGateResult:
        if "unsafe_candidate_payload" in candidate.gate_reasons:
            return EvidenceGateResult(
                quarantine=True,
                reasons=candidate.gate_reasons,
            )
        if self._evidence_gate is None:
            return EvidenceGateResult(reasons=("evidence_gate_unconfigured",))
        result = self._evidence_gate.evaluate(scope_type, scope_id, candidate)
        if not isinstance(result, EvidenceGateResult):
            raise TypeError("evidence gate must return EvidenceGateResult")
        if candidate.approval != "auto":
            return EvidenceGateResult(
                quarantine=result.quarantine,
                reasons=result.reasons,
            )
        if not candidate.auto_eligible:
            return EvidenceGateResult(
                eligible_for_auto=False,
                quarantine=result.quarantine,
                reasons=(*result.reasons, "candidate_marked_ineligible"),
            )
        if candidate.gate_reasons:
            return EvidenceGateResult(
                eligible_for_auto=False,
                quarantine=result.quarantine,
                reasons=(*result.reasons, *candidate.gate_reasons),
            )
        return result

    def _require_activation_evidence(
        self,
        candidate: sqlite3.Row,
    ) -> None:
        if self._evidence_gate is None:
            raise ReviewConflictError("candidate cannot activate without a configured evidence gate")
        value = CandidateInput.from_value(
            {
                "kind": str(candidate["kind"]),
                "memory_key": str(candidate["memory_key"]),
                "payload": _json_loads(candidate["payload_json"], {}),
                "approval": "auto",
                "action": str(candidate["proposed_action"]),
                "provenance": _json_loads(candidate["provenance_json"], []),
                "source_run_ids": _json_loads(
                    candidate["source_run_ids_json"],
                    [],
                ),
            }
        )
        result = self._evidence_gate.evaluate(
            str(candidate["scope_type"]),
            str(candidate["scope_id"]),
            value,
        )
        if not isinstance(result, EvidenceGateResult):
            raise TypeError("evidence gate must return EvidenceGateResult")
        if result.quarantine or not result.eligible_for_auto:
            reasons = ", ".join(result.reasons) or "verified evidence missing"
            raise ReviewConflictError(f"candidate does not pass the code evidence gate: {reasons}")

    @staticmethod
    def _is_v5_pending_migration_candidate(candidate: sqlite3.Row) -> bool:
        return str(candidate["candidate_id"]).startswith("migration_v5_pending_")

    def _require_manual_migration_evidence(
        self,
        candidate: sqlite3.Row,
    ) -> None:
        """Authorize one intact v5 pending row through the manual-only gate.

        A pre-v6 pending write is not runtime evidence and can never qualify for
        auto approval. It is, however, immutable migration provenance that a
        human may approve when every binding created by the schema migration is
        still intact and the payload still passes the current safety boundary.
        """

        def reject(reason: str) -> None:
            raise ReviewConflictError(f"candidate does not pass the migration evidence gate: {reason}")

        candidate_id = str(candidate["candidate_id"])
        prefix = "migration_v5_pending_"
        legacy_pending_id = candidate_id.removeprefix(prefix)
        if not candidate_id.startswith(prefix) or not legacy_pending_id.isdigit():
            reject("invalid migration candidate identity")
        if (
            str(candidate["approval"]) != "manual"
            or str(candidate["state"]) != "pending_pre_review"
            or str(candidate["outcome"]) != "pending"
            or str(candidate["reason"]) != "migrated_from_v5_without_model"
            or str(candidate["kind"]) != "fact"
            or int(candidate["revision"]) != 1
            or candidate["resolved_at"] is not None
        ):
            reject("candidate is not an untouched manual migration candidate")

        scope_type = str(candidate["scope_type"])
        scope_id = str(candidate["scope_id"])
        expected_review_id = "migration_v5_" + hashlib.sha256(f"{scope_type}:{scope_id}".encode()).hexdigest()[:16]
        if str(candidate["review_id"]) != expected_review_id:
            reject("review batch is not bound to the candidate scope")

        gate_reasons = _json_loads(candidate["gate_reasons_json"], [])
        if not isinstance(gate_reasons, list) or "migrated_v5_pending" not in gate_reasons:
            reject("migration gate marker is missing")
        blocking_reasons = {
            "legacy_application_scope_unresolved",
            "legacy_payload_unreconstructable",
            "legacy_action_invalid",
            "unsafe_candidate_payload",
        }
        if blocking_reasons.intersection(str(value) for value in gate_reasons):
            reject("candidate was quarantined during migration")

        provenance = _json_loads(candidate["provenance_json"], [])
        if not isinstance(provenance, list) or len(provenance) != 1 or not isinstance(provenance[0], Mapping):
            reject("dedicated migration provenance is missing")
        evidence = provenance[0]
        if (
            evidence.get("migration_schema") != 5
            or evidence.get("migration_evidence") != "v5_pending_write"
            or str(evidence.get("legacy_pending_id") or "") != legacy_pending_id
            or str(evidence.get("canonical_scope_type") or "") != scope_type
            or str(evidence.get("canonical_scope_id") or "") != scope_id
            or str(evidence.get("proposed_action") or "") != str(candidate["proposed_action"])
            or str(evidence.get("memory_key") or "") != str(candidate["memory_key"])
        ):
            reject("migration provenance does not match the candidate")

        try:
            require_safe_identity(candidate["memory_key"], field="memory key")
            raw_payload = _json_loads(candidate["payload_json"], None)
            safe_payload = self._normalize_human_payload(
                str(candidate["kind"]),
                raw_payload,
            )
        except (TypeError, ValueError) as exc:
            reject(f"candidate payload is unsafe: {exc}")
        if raw_payload != safe_payload:
            reject("candidate payload is not canonical")
        bound_payload_hash = payload_hash(safe_payload)
        if (
            str(candidate["payload_hash"]) != bound_payload_hash
            or str(evidence.get("payload_hash") or "") != bound_payload_hash
        ):
            reject("candidate payload hash does not match migration provenance")

        source_run_id = str(evidence.get("source_run_id") or "")
        source_run_ids = _json_loads(candidate["source_run_ids_json"], None)
        expected_source_run_ids = [source_run_id] if source_run_id else []
        if str(evidence.get("root_run_id") or "") != source_run_id or source_run_ids != expected_source_run_ids:
            reject("source run binding does not match migration provenance")
        evidence_target = evidence.get("target_item_id")
        candidate_target = candidate["target_item_id"]
        if int(evidence_target or 0) != int(candidate_target or 0) or (evidence_target is None) != (
            candidate_target is None
        ):
            reject("target item binding does not match migration provenance")

    @staticmethod
    def _prepare_candidate(candidate: CandidateInput) -> CandidateInput:
        memory_key = require_safe_identity(candidate.memory_key, field="memory key")
        payload: dict[str, str] = {}
        unsafe = False
        for name, value in candidate.payload.items():
            safe_value, tainted = sanitize_text_fragment_with_taint(value)
            payload[name] = safe_value
            unsafe = unsafe or tainted or safe_value != value

        safe_provenance_value, provenance_tainted = sanitize_value_fragments_with_taint(list(candidate.provenance))
        provenance = normalize_provenance(safe_provenance_value if isinstance(safe_provenance_value, list) else [])
        unsafe = unsafe or provenance_tainted or provenance != candidate.provenance

        source_run_ids = tuple(
            require_safe_identity(value, field="candidate source run id") for value in candidate.source_run_ids
        )
        safe_gate_reasons: list[str] = []
        for reason in candidate.gate_reasons:
            safe_reason, tainted = sanitize_text_fragment_with_taint(reason)
            unsafe = unsafe or tainted or safe_reason != reason
            if safe_reason:
                safe_gate_reasons.append(safe_reason)
        if unsafe:
            safe_gate_reasons.append("unsafe_candidate_payload")
        return CandidateInput(
            kind=candidate.kind,
            memory_key=memory_key,
            payload=normalize_payload(candidate.kind, payload),
            approval=candidate.approval,
            action=candidate.action,
            provenance=provenance,
            source_run_ids=source_run_ids,
            auto_eligible=candidate.auto_eligible,
            gate_reasons=tuple(dict.fromkeys(safe_gate_reasons)),
        )

    @staticmethod
    def _merge_provenance(
        current: Sequence[dict[str, Any]],
        incoming: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in (*current, *incoming):
            fingerprint = _json_dumps(entry)
            if fingerprint not in seen:
                merged.append(dict(entry))
                seen.add(fingerprint)
        return merged

    @staticmethod
    def _payload_chars(value: Any) -> int:
        payload = _json_loads(value, {}) if not isinstance(value, dict) else value
        if not isinstance(payload, dict):
            return 0
        return sum(len(str(item)) for item in payload.values())

    def _capacity_available(
        self,
        conn: sqlite3.Connection,
        *,
        scope_type: str,
        scope_id: str,
        payload: dict[str, str],
        exclude_id: int | None = None,
    ) -> bool:
        incoming = self._payload_chars(payload)
        if self._max_item_chars > 0 and incoming > self._max_item_chars:
            return False
        budget = int(self._scope_budgets.get(scope_type) or 0)
        if budget <= 0:
            return True
        sql = """
            SELECT payload_json FROM memory_items
            WHERE scope_type=? AND scope_id=?
              AND state IN ('active_unreviewed','active_confirmed')
        """
        params: list[Any] = [scope_type, scope_id]
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        used = sum(self._payload_chars(row["payload_json"]) for row in conn.execute(sql, params).fetchall())
        return used + incoming <= budget

    @staticmethod
    def _memory_snapshot(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        return {
            key: value.get(key)
            for key in (
                "id",
                "scope_type",
                "scope_id",
                "kind",
                "memory_key",
                "payload_json",
                "payload_hash",
                "state",
                "activation_source",
                "provenance_json",
                "revision",
                "source_review_id",
                "supersedes_id",
                "created_at",
                "updated_at",
            )
        }

    @staticmethod
    def _record_mutation(
        conn: sqlite3.Connection,
        *,
        review_id: str,
        candidate_id: str | None,
        item_id: int,
        operation: str,
        before: dict[str, Any] | None,
        after: dict[str, Any],
        now: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO review_mutations(
                mutation_id, review_id, candidate_id, memory_item_id,
                operation, before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"mutation_{uuid.uuid4().hex}",
                review_id,
                candidate_id,
                item_id,
                operation,
                _json_dumps(before) if before is not None else None,
                _json_dumps(after),
                now,
            ),
        )

    def review(
        self,
        scope_type: str,
        scope_id: str,
        candidate_inputs: Sequence[CandidateInput | dict[str, Any]],
        dry_run: bool = False,
        *,
        source_runs: Sequence[dict[str, Any] | tuple[Any, ...]] | None = None,
    ) -> ReviewBatchResult:
        scope_type, scope_id = self._validate_scope(scope_type, scope_id)
        candidates = tuple(self._prepare_candidate(CandidateInput.from_value(value)) for value in candidate_inputs)
        normalized_runs = self._normalize_source_runs(source_runs)
        review_id = f"review_{uuid.uuid4().hex}"
        now = _now_iso()
        results: list[CandidateResult] = []

        with serialized_write_transaction(self.db_path, self._connect) as conn:
            # Hold the writer reservation while the code-owned gate reads its
            # immutable evidence. A corroborating memory item therefore cannot
            # be retracted between authorization and activation.
            gate_results = tuple(self._gate(scope_type, scope_id, candidate) for candidate in candidates)
            conn.execute(
                """
                INSERT INTO review_batches(
                    review_id, scope_type, scope_id, status, dry_run,
                    result_json, created_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, '{}', ?, ?)
                """,
                (
                    review_id,
                    scope_type,
                    scope_id,
                    "dry_run" if dry_run else "completed",
                    int(bool(dry_run)),
                    now,
                    now,
                ),
            )
            if not dry_run:
                for root_run_id, application_id in normalized_runs:
                    conn.execute(
                        """
                        INSERT INTO review_batch_runs(
                            review_id, root_run_id, application_id
                        ) VALUES (?, ?, ?)
                        """,
                        (review_id, root_run_id, application_id),
                    )

            for candidate, gate in zip(candidates, gate_results, strict=True):
                candidate_id = f"candidate_{uuid.uuid4().hex}"
                digest = payload_hash(candidate.payload)
                gate_reasons = tuple(dict.fromkeys((*gate.reasons, *candidate.gate_reasons)))
                state = "pending_pre_review"
                outcome = "pending"
                reason = "manual_approval_required"
                item_id: int | None = None
                target_item = conn.execute(
                    """
                    SELECT * FROM memory_items
                    WHERE scope_type=? AND scope_id=? AND kind=? AND memory_key=?
                      AND state IN ('active_unreviewed', 'active_confirmed')
                    """,
                    (scope_type, scope_id, candidate.kind, candidate.memory_key),
                ).fetchone()
                capacity_allowed = target_item is not None or self._capacity_available(
                    conn,
                    scope_type=scope_type,
                    scope_id=scope_id,
                    payload=candidate.payload,
                )
                if not capacity_allowed:
                    gate_reasons = tuple(dict.fromkeys((*gate_reasons, "memory_scope_capacity_exceeded")))

                if dry_run:
                    state = "dry_run"
                    outcome = "dry_run"
                    reason = "dry_run_no_mutation"
                elif gate.quarantine:
                    state = "quarantined"
                    outcome = "quarantined"
                    reason = "evidence_gate_quarantined"
                elif target_item is not None and str(target_item["payload_hash"]) == digest:
                    item_id = int(target_item["id"])
                    before = self._memory_snapshot(target_item)
                    existing_provenance = _json_loads(
                        target_item["provenance_json"],
                        [],
                    )
                    merged = self._merge_provenance(
                        existing_provenance if isinstance(existing_provenance, list) else [],
                        candidate.provenance,
                    )
                    conn.execute(
                        "UPDATE memory_items SET provenance_json=?, updated_at=? WHERE id=?",
                        (_json_dumps(merged), now, item_id),
                    )
                    updated = conn.execute(
                        "SELECT * FROM memory_items WHERE id=?",
                        (item_id,),
                    ).fetchone()
                    self._record_mutation(
                        conn,
                        review_id=review_id,
                        candidate_id=candidate_id,
                        item_id=item_id,
                        operation="provenance",
                        before=before,
                        after=self._memory_snapshot(updated),
                        now=now,
                    )
                    state = str(updated["state"])
                    outcome = "duplicate"
                    reason = "exact_payload_duplicate"
                elif target_item is not None:
                    item_id = int(target_item["id"])
                    outcome = "conflict"
                    reason = "active_key_has_different_payload"
                elif (
                    candidate.approval == "auto"
                    and candidate.action == "add"
                    and gate.eligible_for_auto
                    and capacity_allowed
                ):
                    memory_revision = int(
                        conn.execute(
                            """
                            SELECT COALESCE(MAX(revision), 0) + 1
                            FROM memory_items
                            WHERE scope_type=? AND scope_id=? AND kind=? AND memory_key=?
                            """,
                            (
                                scope_type,
                                scope_id,
                                candidate.kind,
                                candidate.memory_key,
                            ),
                        ).fetchone()[0]
                    )
                    cursor = conn.execute(
                        """
                        INSERT INTO memory_items(
                            scope_type, scope_id, kind, memory_key, payload_json,
                            payload_hash, state, activation_source,
                            provenance_json, revision, source_review_id,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'active_unreviewed', 'auto',
                            ?, ?, ?, ?, ?)
                        """,
                        (
                            scope_type,
                            scope_id,
                            candidate.kind,
                            candidate.memory_key,
                            _json_dumps(candidate.payload),
                            digest,
                            _json_dumps(candidate.provenance),
                            memory_revision,
                            review_id,
                            now,
                            now,
                        ),
                    )
                    item_id = int(cursor.lastrowid)
                    inserted = conn.execute(
                        "SELECT * FROM memory_items WHERE id=?",
                        (item_id,),
                    ).fetchone()
                    self._record_mutation(
                        conn,
                        review_id=review_id,
                        candidate_id=candidate_id,
                        item_id=item_id,
                        operation="insert",
                        before=None,
                        after=self._memory_snapshot(inserted),
                        now=now,
                    )
                    state = "active_unreviewed"
                    outcome = "activated"
                    reason = "auto_add_verified"
                elif candidate.approval == "auto":
                    reason = (
                        "memory_scope_capacity_exceeded"
                        if not capacity_allowed
                        else "auto_approval_requires_verified_evidence"
                    )

                conn.execute(
                    """
                    INSERT INTO review_candidates(
                        candidate_id, review_id, scope_type, scope_id, kind,
                        memory_key, payload_json, payload_hash, proposed_action,
                        approval, state, outcome, revision, target_item_id,
                        provenance_json, source_run_ids_json, gate_reasons_json,
                        reason, created_at, resolved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        review_id,
                        scope_type,
                        scope_id,
                        candidate.kind,
                        candidate.memory_key,
                        _json_dumps(candidate.payload),
                        digest,
                        candidate.action,
                        candidate.approval,
                        state,
                        outcome,
                        item_id,
                        _json_dumps(candidate.provenance),
                        _json_dumps(candidate.source_run_ids),
                        _json_dumps(gate_reasons),
                        reason,
                        now,
                        now if state not in {"pending_pre_review", "active_unreviewed"} else None,
                    ),
                )
                results.append(
                    CandidateResult(
                        candidate_id=candidate_id,
                        revision=1,
                        kind=candidate.kind,
                        memory_key=candidate.memory_key,
                        payload=dict(candidate.payload),
                        state=state,
                        outcome=outcome,
                        item_id=item_id,
                        gate_reasons=gate_reasons,
                        provenance=candidate.provenance,
                        reason=reason,
                    )
                )

            result = ReviewBatchResult(
                review_id=review_id,
                scope_type=scope_type,
                scope_id=scope_id,
                status="dry_run" if dry_run else "completed",
                dry_run=bool(dry_run),
                candidates=tuple(results),
            )
            conn.execute(
                "UPDATE review_batches SET result_json=? WHERE review_id=?",
                (_json_dumps(result.to_dict()), review_id),
            )
        return result

    def status(
        self,
        scope_type: str | None = None,
        scope_id: str = "",
    ) -> dict[str, Any]:
        normalized_scope: tuple[str, str] | None = None
        if scope_type is not None:
            normalized_scope = self._validate_scope(scope_type, scope_id)
        clauses: list[str] = []
        params: list[Any] = []
        if normalized_scope is not None:
            clauses = ["scope_type=?", "scope_id=?"]
            params.extend(normalized_scope)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            memory_rows = conn.execute(
                f"SELECT * FROM memory_items{where} ORDER BY id",
                params,
            ).fetchall()
            candidate_rows = conn.execute(
                f"SELECT * FROM review_candidates{where} ORDER BY created_at, candidate_id",
                params,
            ).fetchall()
            batch_rows = conn.execute(
                f"SELECT * FROM review_batches{where} ORDER BY created_at, review_id",
                params,
            ).fetchall()
            batch_run_rows = conn.execute(
                """
                SELECT consumed.review_id, consumed.root_run_id, consumed.application_id
                FROM review_batch_runs AS consumed
                JOIN review_batches AS batch ON batch.review_id=consumed.review_id
                """
                + (" WHERE batch.scope_type=? AND batch.scope_id=?" if normalized_scope is not None else "")
                + " ORDER BY consumed.review_id, consumed.root_run_id, consumed.application_id",
                params,
            ).fetchall()

        memory_items = []
        for row in memory_rows:
            item = dict(row)
            item["payload"] = _json_loads(item.pop("payload_json"), {})
            item["provenance"] = _json_loads(item.pop("provenance_json"), [])
            memory_items.append(item)
        candidates = []
        for row in candidate_rows:
            item = dict(row)
            item["payload"] = _json_loads(item.pop("payload_json"), {})
            item["provenance"] = _json_loads(item.pop("provenance_json"), [])
            item["source_run_ids"] = _json_loads(item.pop("source_run_ids_json"), [])
            item["gate_reasons"] = _json_loads(item.pop("gate_reasons_json"), [])
            candidates.append(item)
        runs_by_batch: dict[str, list[dict[str, str]]] = {}
        for row in batch_run_rows:
            runs_by_batch.setdefault(str(row["review_id"]), []).append(
                {
                    "application_id": str(row["application_id"]),
                    "root_run_id": str(row["root_run_id"]),
                }
            )
        batches = []
        for row in batch_rows:
            batch = dict(row)
            batch["source_runs"] = runs_by_batch.get(str(row["review_id"]), [])
            batches.append(batch)
        return {
            "scope_type": normalized_scope[0] if normalized_scope else None,
            "scope_id": normalized_scope[1] if normalized_scope else "",
            "counts": {
                "memory": dict(Counter(item["state"] for item in memory_items)),
                "candidates": dict(Counter(item["state"] for item in candidates)),
                "batches": dict(Counter(str(row["status"]) for row in batch_rows)),
            },
            "memory_items": memory_items,
            "candidates": candidates,
            "batches": batches,
        }

    @staticmethod
    def _active_item_for_candidate(
        conn: sqlite3.Connection,
        candidate: sqlite3.Row,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM memory_items
            WHERE scope_type=? AND scope_id=? AND kind=? AND memory_key=?
              AND state IN ('active_unreviewed', 'active_confirmed')
            """,
            (
                candidate["scope_type"],
                candidate["scope_id"],
                candidate["kind"],
                candidate["memory_key"],
            ),
        ).fetchone()

    @staticmethod
    def _next_memory_revision(
        conn: sqlite3.Connection,
        candidate: sqlite3.Row,
        *,
        scope_type: str | None = None,
        scope_id: str | None = None,
    ) -> int:
        return int(
            conn.execute(
                """
                SELECT COALESCE(MAX(revision), 0) + 1 FROM memory_items
                WHERE scope_type=? AND scope_id=? AND kind=? AND memory_key=?
                """,
                (
                    scope_type or str(candidate["scope_type"]),
                    scope_id or str(candidate["scope_id"]),
                    candidate["kind"],
                    candidate["memory_key"],
                ),
            ).fetchone()[0]
        )

    def _set_memory_state(
        self,
        conn: sqlite3.Connection,
        *,
        review_id: str,
        candidate_id: str,
        row: sqlite3.Row,
        state: str,
        now: str,
    ) -> sqlite3.Row:
        before = self._memory_snapshot(row)
        conn.execute(
            "UPDATE memory_items SET state=?, updated_at=? WHERE id=?",
            (state, now, int(row["id"])),
        )
        updated = conn.execute(
            "SELECT * FROM memory_items WHERE id=?",
            (int(row["id"]),),
        ).fetchone()
        self._record_mutation(
            conn,
            review_id=review_id,
            candidate_id=candidate_id,
            item_id=int(row["id"]),
            operation="state",
            before=before,
            after=self._memory_snapshot(updated),
            now=now,
        )
        return updated

    def _insert_manual_memory(
        self,
        conn: sqlite3.Connection,
        *,
        candidate: sqlite3.Row,
        review_id: str,
        candidate_id: str,
        now: str,
        scope_type: str | None = None,
        scope_id: str | None = None,
        supersedes_id: int | None = None,
    ) -> sqlite3.Row:
        target_scope_type = scope_type or str(candidate["scope_type"])
        target_scope_id = scope_id or str(candidate["scope_id"])
        candidate_payload = _json_loads(candidate["payload_json"], {})
        if not isinstance(candidate_payload, dict) or not self._capacity_available(
            conn,
            scope_type=target_scope_type,
            scope_id=target_scope_id,
            payload=candidate_payload,
            exclude_id=supersedes_id,
        ):
            raise ReviewConflictError("memory scope capacity exceeded")
        revision = self._next_memory_revision(
            conn,
            candidate,
            scope_type=target_scope_type,
            scope_id=target_scope_id,
        )
        cursor = conn.execute(
            """
            INSERT INTO memory_items(
                scope_type, scope_id, kind, memory_key, payload_json,
                payload_hash, state, activation_source, provenance_json,
                revision, source_review_id, supersedes_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active_confirmed', 'manual', ?, ?, ?, ?, ?, ?)
            """,
            (
                target_scope_type,
                target_scope_id,
                candidate["kind"],
                candidate["memory_key"],
                candidate["payload_json"],
                candidate["payload_hash"],
                candidate["provenance_json"],
                revision,
                review_id,
                supersedes_id,
                now,
                now,
            ),
        )
        inserted = conn.execute(
            "SELECT * FROM memory_items WHERE id=?",
            (int(cursor.lastrowid),),
        ).fetchone()
        self._record_mutation(
            conn,
            review_id=review_id,
            candidate_id=candidate_id,
            item_id=int(inserted["id"]),
            operation="insert",
            before=None,
            after=self._memory_snapshot(inserted),
            now=now,
        )
        return inserted

    @staticmethod
    def _validate_decision_state(candidate: sqlite3.Row, action: str) -> None:
        state = str(candidate["state"])
        allowed = {
            "pending_pre_review": {"approve", "reject", "promote_project"},
            "quarantined": {"reject"},
            "active_unreviewed": {"acknowledge", "revoke", "correct", "promote_project"},
            "active_confirmed": {"revoke", "correct", "promote_project"},
        }
        if action not in allowed.get(state, set()):
            raise ReviewConflictError(f"decision {action!r} is incompatible with candidate state {state!r}")

    def apply_decisions(
        self,
        scope_type: str,
        scope_id: str,
        decisions: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        scope_type, scope_id = self._validate_scope(scope_type, scope_id)
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for decision in decisions:
            if not isinstance(decision, dict):
                raise ValueError("each review decision must be an object")
            candidate_id = require_safe_identity(
                decision.get("candidate_id"),
                field="candidate id",
            )
            if candidate_id in seen_ids:
                raise ValueError(f"duplicate decision for candidate {candidate_id}")
            seen_ids.add(candidate_id)
            try:
                revision = int(decision.get("revision"))
            except (TypeError, ValueError) as exc:
                raise ValueError("decision revision must be an integer") from exc
            action = str(decision.get("action") or "").strip().casefold()
            if action not in {
                "approve",
                "reject",
                "acknowledge",
                "revoke",
                "correct",
                "promote_project",
            }:
                raise ValueError("unsupported review decision action")
            normalized.append(
                {
                    "candidate_id": candidate_id,
                    "revision": revision,
                    "action": action,
                    "payload": decision.get("payload"),
                    "memory_key": decision.get("memory_key"),
                }
            )

        now = _now_iso()
        results: list[dict[str, Any]] = []
        with serialized_write_transaction(self.db_path, self._connect) as conn:
            rows: list[tuple[dict[str, Any], sqlite3.Row]] = []
            for decision in normalized:
                row = conn.execute(
                    "SELECT * FROM review_candidates WHERE candidate_id=?",
                    (decision["candidate_id"],),
                ).fetchone()
                if row is None:
                    raise ReviewConflictError(f"candidate not found: {decision['candidate_id']}")
                if (str(row["scope_type"]), str(row["scope_id"])) != (
                    scope_type,
                    scope_id,
                ):
                    raise ReviewConflictError("candidate belongs to a different scope")
                if int(row["revision"]) != decision["revision"]:
                    raise ReviewConflictError(f"candidate revision mismatch for {decision['candidate_id']}")
                self._validate_decision_state(row, decision["action"])
                if decision["action"] == "correct":
                    decision["payload"] = self._normalize_human_payload(
                        str(row["kind"]),
                        decision["payload"],
                    )
                    if decision.get("memory_key") is not None:
                        decision["memory_key"] = require_safe_identity(
                            decision["memory_key"],
                            field="corrected memory key",
                        )
                if decision["action"] == "promote_project":
                    self._require_activation_evidence(row)
                elif decision["action"] == "approve":
                    if self._is_v5_pending_migration_candidate(row):
                        self._require_manual_migration_evidence(row)
                    elif str(row["proposed_action"]) != "remove":
                        self._require_activation_evidence(row)
                rows.append((decision, row))

            for decision, candidate in rows:
                candidate_id = str(candidate["candidate_id"])
                review_id = str(candidate["review_id"])
                action = str(decision["action"])
                active = self._active_item_for_candidate(conn, candidate)
                next_state = str(candidate["state"])
                outcome = action
                item_id = int(active["id"]) if active is not None else None
                extra: dict[str, Any] = {}

                if action == "approve":
                    proposed_action = str(candidate["proposed_action"])
                    correction_target = None
                    if proposed_action == "remove" and active is None:
                        raise ReviewConflictError("manual target is no longer active")
                    if proposed_action == "replace" and active is None:
                        target_id = int(candidate["target_item_id"] or 0)
                        correction_target = conn.execute(
                            "SELECT * FROM memory_items WHERE id=?",
                            (target_id,),
                        ).fetchone()
                        if (
                            correction_target is None
                            or str(correction_target["scope_type"]) != scope_type
                            or str(correction_target["scope_id"]) != scope_id
                            or str(correction_target["kind"]) != str(candidate["kind"])
                            or str(correction_target["state"]) != "retracted"
                        ):
                            raise ReviewConflictError("manual target is no longer active")
                    if proposed_action == "remove":
                        self._set_memory_state(
                            conn,
                            review_id=review_id,
                            candidate_id=candidate_id,
                            row=active,
                            state="retracted",
                            now=now,
                        )
                    else:
                        supersedes_id = int(correction_target["id"]) if correction_target is not None else None
                        if active is not None:
                            supersedes_id = int(active["id"])
                            self._set_memory_state(
                                conn,
                                review_id=review_id,
                                candidate_id=candidate_id,
                                row=active,
                                state="retracted",
                                now=now,
                            )
                        inserted = self._insert_manual_memory(
                            conn,
                            candidate=candidate,
                            review_id=review_id,
                            candidate_id=candidate_id,
                            now=now,
                            supersedes_id=supersedes_id,
                        )
                        item_id = int(inserted["id"])
                    next_state = "active_confirmed"
                    outcome = "approved"
                elif action == "reject":
                    next_state = "rejected"
                    outcome = "rejected"
                elif action == "acknowledge":
                    if active is None or str(active["state"]) != "active_unreviewed":
                        raise ReviewConflictError("auto-applied item is no longer unreviewed")
                    updated = self._set_memory_state(
                        conn,
                        review_id=review_id,
                        candidate_id=candidate_id,
                        row=active,
                        state="active_confirmed",
                        now=now,
                    )
                    item_id = int(updated["id"])
                    next_state = "active_confirmed"
                    outcome = "acknowledged"
                elif action == "revoke":
                    if active is None:
                        raise ReviewConflictError("memory item is no longer active")
                    self._set_memory_state(
                        conn,
                        review_id=review_id,
                        candidate_id=candidate_id,
                        row=active,
                        state="retracted",
                        now=now,
                    )
                    next_state = "retracted"
                    outcome = "revoked"
                elif action == "correct":
                    if active is None:
                        raise ReviewConflictError("memory item is no longer active")
                    self._set_memory_state(
                        conn,
                        review_id=review_id,
                        candidate_id=candidate_id,
                        row=active,
                        state="retracted",
                        now=now,
                    )
                    correction_id = f"candidate_{uuid.uuid4().hex}"
                    correction_payload = decision["payload"]
                    correction_key = str(decision.get("memory_key") or candidate["memory_key"])
                    conn.execute(
                        """
                        INSERT INTO review_candidates(
                            candidate_id, review_id, scope_type, scope_id, kind,
                            memory_key, payload_json, payload_hash, proposed_action,
                            approval, state, outcome, revision, target_item_id,
                            provenance_json, source_run_ids_json, gate_reasons_json,
                            reason, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'replace', 'manual',
                            'pending_pre_review', 'pending', 1, ?, ?, ?, '[]',
                            'human_correction_requires_approval', ?)
                        """,
                        (
                            correction_id,
                            review_id,
                            scope_type,
                            scope_id,
                            candidate["kind"],
                            correction_key,
                            _json_dumps(correction_payload),
                            payload_hash(correction_payload),
                            int(active["id"]),
                            candidate["provenance_json"],
                            candidate["source_run_ids_json"],
                            now,
                        ),
                    )
                    next_state = "retracted"
                    outcome = "corrected"
                    extra["correction_candidate_id"] = correction_id
                elif action == "promote_project":
                    if scope_type != "application":
                        raise ReviewConflictError("project promotion requires an application candidate")
                    if active is None:
                        active = self._insert_manual_memory(
                            conn,
                            candidate=candidate,
                            review_id=review_id,
                            candidate_id=candidate_id,
                            now=now,
                        )
                    project = conn.execute(
                        """
                        SELECT * FROM memory_items
                        WHERE scope_type='project' AND scope_id='project'
                          AND kind=? AND memory_key=?
                          AND state IN ('active_unreviewed', 'active_confirmed')
                        """,
                        (candidate["kind"], candidate["memory_key"]),
                    ).fetchone()
                    if project is not None and str(project["payload_hash"]) != str(candidate["payload_hash"]):
                        raise ReviewConflictError("project memory key has conflicting payload")
                    if project is None:
                        project = self._insert_manual_memory(
                            conn,
                            candidate=candidate,
                            review_id=review_id,
                            candidate_id=candidate_id,
                            now=now,
                            scope_type="project",
                            scope_id="project",
                        )
                    else:
                        before = self._memory_snapshot(project)
                        merged = self._merge_provenance(
                            _json_loads(project["provenance_json"], []),
                            _json_loads(candidate["provenance_json"], []),
                        )
                        conn.execute(
                            "UPDATE memory_items SET provenance_json=?,updated_at=? WHERE id=?",
                            (_json_dumps(merged), now, int(project["id"])),
                        )
                        updated_project = conn.execute(
                            "SELECT * FROM memory_items WHERE id=?",
                            (int(project["id"]),),
                        ).fetchone()
                        self._record_mutation(
                            conn,
                            review_id=review_id,
                            candidate_id=candidate_id,
                            item_id=int(project["id"]),
                            operation="provenance",
                            before=before,
                            after=self._memory_snapshot(updated_project),
                            now=now,
                        )
                        project = updated_project
                    self._set_memory_state(
                        conn,
                        review_id=review_id,
                        candidate_id=candidate_id,
                        row=active,
                        state="shadowed",
                        now=now,
                    )
                    item_id = int(project["id"])
                    next_state = "active_confirmed"
                    outcome = "promoted"

                next_revision = int(candidate["revision"]) + 1
                conn.execute(
                    """
                    UPDATE review_candidates
                    SET state=?, outcome=?, target_item_id=?, revision=?, resolved_at=?
                    WHERE candidate_id=?
                    """,
                    (next_state, outcome, item_id, next_revision, now, candidate_id),
                )
                results.append(
                    {
                        "candidate_id": candidate_id,
                        "revision": next_revision,
                        "state": next_state,
                        "outcome": outcome,
                        "item_id": item_id,
                        **extra,
                    }
                )
        return {"applied": len(results), "results": results}

    @staticmethod
    def _restore_memory_snapshot(
        conn: sqlite3.Connection,
        snapshot: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            UPDATE memory_items SET
                scope_type=?, scope_id=?, kind=?, memory_key=?, payload_json=?,
                payload_hash=?, state=?, activation_source=?, provenance_json=?,
                revision=?, source_review_id=?, supersedes_id=?,
                created_at=?, updated_at=?
            WHERE id=?
            """,
            (
                snapshot["scope_type"],
                snapshot["scope_id"],
                snapshot["kind"],
                snapshot["memory_key"],
                snapshot["payload_json"],
                snapshot["payload_hash"],
                snapshot["state"],
                snapshot["activation_source"],
                snapshot["provenance_json"],
                snapshot["revision"],
                snapshot.get("source_review_id"),
                snapshot.get("supersedes_id"),
                snapshot["created_at"],
                snapshot["updated_at"],
                snapshot["id"],
            ),
        )

    @classmethod
    def _require_current_mutation_snapshot(
        cls,
        conn: sqlite3.Connection,
        mutation: sqlite3.Row,
    ) -> None:
        """Refuse to roll back a mutation whose output is no longer current.

        The caller holds the serialized ``BEGIN IMMEDIATE`` writer
        transaction, so this comparison and the following restoration form one
        compare-and-swap operation.  Comparing the complete persisted snapshot
        (including provenance, revision, source review, and timestamp) protects
        both later review batches and direct administrator changes.
        """

        after = _json_loads(mutation["after_json"], None)
        if not isinstance(after, dict):
            raise RuntimeError("mutation is missing its post-mutation snapshot")
        item_id = int(mutation["memory_item_id"])
        current = conn.execute(
            "SELECT * FROM memory_items WHERE id=?",
            (item_id,),
        ).fetchone()
        if current is None or cls._memory_snapshot(current) != after:
            raise ReviewConflictError(f"memory item {item_id} changed after review batch; rollback refused")

    def rollback(self, review_id: str) -> dict[str, Any]:
        review_id = require_safe_identity(review_id, field="review id")
        now = _now_iso()
        with serialized_write_transaction(self.db_path, self._connect) as conn:
            batch = conn.execute(
                "SELECT * FROM review_batches WHERE review_id=?",
                (review_id,),
            ).fetchone()
            if batch is None:
                raise KeyError(f"review batch not found: {review_id}")
            if str(batch["status"]) == "rolled_back":
                return {
                    "review_id": review_id,
                    "rolled_back": False,
                    "mutation_count": 0,
                }
            mutations = conn.execute(
                """
                SELECT rowid AS mutation_order, * FROM review_mutations
                WHERE review_id=? AND rolled_back_at IS NULL
                ORDER BY mutation_order DESC
                """,
                (review_id,),
            ).fetchall()
            for mutation in mutations:
                self._require_current_mutation_snapshot(conn, mutation)
                operation = str(mutation["operation"])
                item_id = int(mutation["memory_item_id"])
                if operation == "insert":
                    conn.execute(
                        "UPDATE memory_items SET state='retracted',updated_at=? WHERE id=?",
                        (now, item_id),
                    )
                else:
                    before = _json_loads(mutation["before_json"], None)
                    if not isinstance(before, dict):
                        raise RuntimeError("mutation is missing its rollback snapshot")
                    self._restore_memory_snapshot(conn, before)
                conn.execute(
                    "UPDATE review_mutations SET rolled_back_at=? WHERE mutation_id=?",
                    (now, mutation["mutation_id"]),
                )
            conn.execute(
                """
                UPDATE review_candidates
                SET state='retracted', outcome='rolled_back', revision=revision+1,
                    resolved_at=?
                WHERE review_id=? AND state NOT IN ('quarantined', 'rejected', 'dry_run')
                """,
                (now, review_id),
            )
            conn.execute(
                "UPDATE review_batches SET status='rolled_back',finished_at=? WHERE review_id=?",
                (now, review_id),
            )
            return {
                "review_id": review_id,
                "rolled_back": True,
                "mutation_count": len(mutations),
            }

    def submit_feedback(
        self,
        run_id: str,
        verdict: str,
        item_id: int | None = None,
        *,
        application_id: str = "",
        correction: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        run_id = require_safe_identity(run_id, field="feedback run id")
        application_id = require_safe_identity(
            application_id,
            field="feedback application id",
            allow_empty=True,
        )
        verdict = str(verdict or "").strip().casefold()
        if verdict not in {"accepted", "rejected", "corrected"}:
            raise ValueError("feedback verdict must be accepted, rejected, or corrected")
        feedback_id = f"feedback_{uuid.uuid4().hex}"
        review_id = f"feedback_review_{uuid.uuid4().hex}" if item_id is not None else ""
        now = _now_iso()
        state = ""
        correction_candidate_id: str | None = None

        with serialized_write_transaction(self.db_path, self._connect) as conn:
            item = None
            normalized_correction: dict[str, str] | None = None
            if item_id is not None:
                try:
                    normalized_item_id = int(item_id)
                except (TypeError, ValueError) as exc:
                    raise ValueError("feedback item_id must be an integer") from exc
                item = conn.execute(
                    "SELECT * FROM memory_items WHERE id=?",
                    (normalized_item_id,),
                ).fetchone()
                if item is None:
                    raise KeyError(f"memory item not found: {normalized_item_id}")
                if (
                    application_id
                    and str(item["scope_type"]) == "application"
                    and str(item["scope_id"]) != application_id
                ):
                    raise ReviewConflictError("feedback item belongs to another application")
                if str(item["state"]) not in _ACTIVE_STATES:
                    raise ReviewConflictError("feedback item is not active")
                if verdict == "corrected" and correction is not None:
                    normalized_correction = self._normalize_human_payload(
                        str(item["kind"]),
                        correction,
                    )
                conn.execute(
                    """
                    INSERT INTO review_batches(
                        review_id,scope_type,scope_id,status,dry_run,
                        result_json,created_at,finished_at
                    ) VALUES(?,?,?,'completed',0,'{}',?,?)
                    """,
                    (
                        review_id,
                        item["scope_type"],
                        item["scope_id"],
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO review_batch_runs(review_id,root_run_id,application_id)
                    VALUES(?,?,?)
                    """,
                    (review_id, run_id, application_id),
                )
                target_state = "active_confirmed" if verdict == "accepted" else "retracted"
                updated = self._set_memory_state(
                    conn,
                    review_id=review_id,
                    candidate_id=feedback_id,
                    row=item,
                    state=target_state,
                    now=now,
                )
                state = str(updated["state"])
                conn.execute(
                    """
                    UPDATE review_candidates
                    SET state=?, outcome=?, revision=revision+1, resolved_at=?
                    WHERE target_item_id=?
                      AND state IN ('active_unreviewed','active_confirmed')
                    """,
                    (
                        target_state,
                        "acknowledged" if verdict == "accepted" else verdict,
                        now,
                        normalized_item_id,
                    ),
                )
                if normalized_correction is not None:
                    correction_candidate_id = f"candidate_{uuid.uuid4().hex}"
                    provenance = self._merge_provenance(
                        _json_loads(item["provenance_json"], []),
                        (
                            {
                                "feedback_id": feedback_id,
                                "root_run_id": run_id,
                                "verdict": "corrected",
                            },
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO review_candidates(
                            candidate_id,review_id,scope_type,scope_id,kind,
                            memory_key,payload_json,payload_hash,proposed_action,
                            approval,state,outcome,revision,target_item_id,
                            provenance_json,source_run_ids_json,gate_reasons_json,
                            reason,created_at
                        ) VALUES(?,?,?,?,?,?,?,?, 'replace','manual',
                            'pending_pre_review','pending',1,?,?,?,'[]',
                            'feedback_correction_requires_approval',?)
                        """,
                        (
                            correction_candidate_id,
                            review_id,
                            item["scope_type"],
                            item["scope_id"],
                            item["kind"],
                            item["memory_key"],
                            _json_dumps(normalized_correction),
                            payload_hash(normalized_correction),
                            normalized_item_id,
                            _json_dumps(provenance),
                            _json_dumps([run_id]),
                            now,
                        ),
                    )
            elif correction is not None:
                raise ValueError("correction requires item_id")

            conn.execute(
                """
                INSERT INTO run_feedback(
                    feedback_id,run_id,verdict,item_id,application_id,
                    correction_json,created_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    feedback_id,
                    run_id,
                    verdict,
                    item_id,
                    application_id,
                    _json_dumps(correction) if correction is not None else None,
                    now,
                ),
            )

        result = {
            "feedback_id": feedback_id,
            "run_id": run_id,
            "verdict": verdict,
            "item_id": item_id,
            "state": state,
        }
        if review_id:
            result["review_id"] = review_id
        if correction_candidate_id:
            result["correction_candidate_id"] = correction_candidate_id
        return result
