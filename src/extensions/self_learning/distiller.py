"""LLM-based semantic distillation of a finished run into memory proposals.

At session end the run's digest (task, outcome, session notes, repeated
failures, key events) is handed to a cheap configured model which returns a
handful of durable, declarative facts. Anything that goes wrong — no model,
bad JSON, transport error — returns ``None`` so the caller can fall back to
the deterministic distillation path; the reviewer hook must never block a run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.lib.logging import get_logger

from .digest import BLOCKED_TEXT, DigestBuilder
from .event_schema import safe_run_id
from .ledger import SelfLearningLedger
from .redaction import redact_text, require_safe_identity, scan_injection_patterns

logger = get_logger(__name__)

_DIGEST_MAX_CHARS = 14000
_DIGEST_MAX_EVENTS = 40
_EVENT_PREVIEW_CHARS = 300
_EXISTING_MEMORY_PREVIEW_CHARS = 3000
_MAX_PROPOSALS = 5
_MAX_PROPOSAL_CHARS = 600
_ERROR_PREVIEW_CHARS = 200
_MIN_REPEATED_FAILURES = 2
# The call runs in a durable outbox worker. Keep the provider timeout bounded
# so the job can renew its lease and reach the queue's explicit retry policy.
_COMPLETION_TIMEOUT_SECONDS = 60

DISTILL_SYSTEM_PROMPT = """You are the memory curator for an autonomous agent framework. \
A run just finished; from its digest, extract the few durable facts a FUTURE run would \
otherwise have to re-learn. Quality over quantity — most runs yield zero or one.

SAVE (priority order): corrections of earlier wrong assumptions > environment facts \
(services, paths, credentials locations, data-source conventions) > procedures that \
demonstrably worked.

WRITE declarative facts, not imperatives. "The export API paginates at 100 rows" ✓ — \
"Always paginate the export API" ✗. Imperative phrasing gets re-read as a standing \
order in every future run.

DO NOT CAPTURE (these become persistent self-imposed constraints that bite later runs):
- environment-dependent failures or negative claims about tools ("X is broken", "Y doesn't work")
- transient errors that a retry fixed — capture the retry pattern instead, if anything
- one-off task narratives or results ("processed 37 files", "the answer was ...")
- anything stale within a week: run ids, dates, counts, progress markers
- secrets, tokens, or credentials of any kind
If a failure came from setup or environment state, capture the FIX, not the failure.

SESSION NOTE CONTRACT (mandatory):
- Evaluate every unblocked `session_note` fragment independently. If its text
  begins exactly with `learned:`, treat the text after that marker as an explicit
  persistence candidate.
- When that candidate is safe, declarative, durable under SAVE/DO NOT CAPTURE,
  and not already in EXISTING memory, you MUST emit one proposal for that note.
  Preserve the durable fact faithfully, and cite that fragment's exact `ref` in
  `evidence_refs` (for example, `session_note:41`). Do not require a second source.
- The `learned:` marker never overrides secret, injection, durability, or duplicate
  checks. If a learned note describes a transient failure plus a reusable fix,
  retain the reusable fix rather than a failure-only claim.
- If a session note begins with `progress:`, it is transient progress and you
  MUST NOT emit a proposal for it. A progress or rejected note must not cause you
  to omit a different eligible learned note from the same digest.

`run.final_answer` may be the only evidence source. When an unblocked final-answer
fragment explicitly labels a declarative value as `durable_observation`, apply the
same SAVE/DO NOT CAPTURE rules; if it is safe and genuinely durable, copy that value
verbatim into one proposal and cite `run.final_answer`. Do not require a session note
or a second source merely to create the pending proposal.

The digest includes the target scopes' EXISTING memory. Never re-propose a fact that is \
already there. If a new fact supersedes an existing entry, set "replaces" to that \
entry's id (the number in brackets); otherwise leave "replaces" empty.

Every proposal MUST cite one or more `ref` values from the supplied digest in
`evidence_refs`. Never cite a blocked fragment. A replacement target must be an
id present in an `existing_memory` fragment.

Respond ONLY with valid JSON, no other text:
{"proposals": [{"scope": "app" | "project", "content": "...", "replaces": "", "evidence_refs": ["..."]}]}
- scope "app" = specific to this application; "project" = true for every application
- at most 5 proposals, each a standalone fact under 600 characters
- {"proposals": []} only when no eligible durable candidate remains after evaluating
  every fragment (this is the common case)"""


@dataclass(frozen=True)
class _RunDigest:
    text: str
    evidence_refs: set[str]
    replace_targets: set[str]


def _safe_log_text(value: Any) -> str:
    text = redact_text(str(value or ""), max_chars=300)
    return BLOCKED_TEXT if scan_injection_patterns(text) else text


def _digest_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _prepared_payload(digest: _RunDigest) -> dict[str, Any]:
    return {
        "text": digest.text,
        "evidence_refs": sorted(digest.evidence_refs),
        "replace_targets": sorted(digest.replace_targets),
        "sha256": _digest_sha256(digest.text),
    }


def _load_prepared_digest(value: Any) -> _RunDigest | None:
    """Validate a persisted digest without consulting mutable run state."""
    if not isinstance(value, dict):
        return None
    if set(value) != {"text", "evidence_refs", "replace_targets", "sha256"}:
        return None
    text = value.get("text")
    expected_sha = value.get("sha256")
    if not isinstance(text, str) or not isinstance(expected_sha, str):
        return None
    if not hmac.compare_digest(_digest_sha256(text), expected_sha):
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"version", "fragments"}:
        return None
    if type(payload.get("version")) is not int or payload["version"] != 1:
        return None
    fragments = payload.get("fragments")
    if not isinstance(fragments, list):
        return None

    evidence_refs: set[str] = set()
    replace_targets: set[str] = set()
    seen_refs: set[str] = set()
    canonical_fragments: list[dict[str, Any]] = []
    for fragment in fragments:
        if not isinstance(fragment, dict) or set(fragment) != {"ref", "kind", "text", "blocked"}:
            return None
        ref = fragment.get("ref")
        kind = fragment.get("kind")
        fragment_text = fragment.get("text")
        blocked = fragment.get("blocked")
        if not isinstance(ref, str) or not isinstance(kind, str):
            return None
        try:
            ref = require_safe_identity(ref, field="digest fragment ref")
            kind = require_safe_identity(kind, field="digest fragment kind")
        except ValueError:
            return None
        if ref in seen_refs or not isinstance(fragment_text, str) or not isinstance(blocked, bool):
            return None
        seen_refs.add(ref)
        canonical_fragments.append(
            {
                "ref": ref,
                "kind": kind,
                "text": fragment_text,
                "blocked": blocked,
            }
        )
        if blocked:
            if fragment_text != BLOCKED_TEXT:
                return None
            continue
        # A job payload is persistent state, not trusted model context. Reject
        # rather than silently mutate if its supposedly-safe text no longer
        # passes the current safety boundary.
        if redact_text(fragment_text) != fragment_text or scan_injection_patterns(fragment_text):
            return None
        evidence_refs.add(ref)
        if kind == "existing_memory":
            try:
                existing = json.loads(fragment_text)
            except (json.JSONDecodeError, ValueError):
                return None
            if (
                not isinstance(existing, dict)
                or not str(existing.get("id") or "").strip()
                or existing.get("status") not in {"active", "pending"}
            ):
                return None
            if existing["status"] == "active":
                replace_targets.add(str(existing["id"]).strip())

    supplied_refs = value.get("evidence_refs")
    supplied_targets = value.get("replace_targets")
    if not isinstance(supplied_refs, list) or not isinstance(supplied_targets, list):
        return None
    if not all(isinstance(ref, str) for ref in supplied_refs):
        return None
    if not all(isinstance(target, str) for target in supplied_targets):
        return None
    try:
        canonical_refs = [
            require_safe_identity(ref, field="digest evidence ref")
            for ref in supplied_refs
        ]
        canonical_targets = [
            require_safe_identity(target, field="digest replace target")
            for target in supplied_targets
        ]
    except ValueError:
        return None
    if len(canonical_refs) != len(set(canonical_refs)) or set(canonical_refs) != evidence_refs:
        return None
    if len(canonical_targets) != len(set(canonical_targets)) or set(canonical_targets) != replace_targets:
        return None
    canonical_text = json.dumps(
        {"version": 1, "fragments": canonical_fragments},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _RunDigest(
        text=canonical_text,
        evidence_refs=evidence_refs,
        replace_targets=replace_targets,
    )


_SEMANTIC_PLAN_VERSION = 1
_SEMANTIC_PLAN_MODES = {
    "llm",
    "deterministic",
    "deterministic_fallback",
    "no_signal",
    "disabled",
}


def _semantic_plan_sha256(value: dict[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "sha256"}
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _prepared_session_notes_from_digest(
    digest: _RunDigest | None,
) -> list[dict[str, Any]]:
    """Extract notes from a digest already validated at the caller boundary."""
    if digest is None:
        return []
    payload = json.loads(digest.text)
    notes: list[dict[str, Any]] = []
    for fragment in payload.get("fragments") or []:
        if not isinstance(fragment, dict) or fragment.get("kind") != "session_note":
            continue
        ref = str(fragment.get("ref") or "")
        prefix, separator, raw_id = ref.partition(":")
        if prefix != "session_note" or not separator or not raw_id.isdigit():
            continue
        notes.append(
            {
                "id": int(raw_id),
                "ref": ref,
                "text": str(fragment.get("text") or ""),
                "blocked": bool(fragment.get("blocked")),
            }
        )
    return notes


def build_semantic_plan(
    *,
    prepared_digest: dict[str, Any] | None,
    application_id: str,
    mode: str,
    proposals: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Freeze validated model output or deterministic note facts before writes."""
    mode = str(mode or "").strip()
    if mode not in _SEMANTIC_PLAN_MODES:
        raise ValueError(f"unsupported semantic plan mode: {mode}")
    digest = _load_prepared_digest(prepared_digest)
    if prepared_digest is not None and digest is None:
        raise ValueError("semantic plan requires a valid prepared digest")
    # Loading a prepared digest performs the full redact/injection/schema
    # validation. Re-running that same validation in the note helper doubles
    # the dominant worker cost without creating another trust boundary.
    notes = _prepared_session_notes_from_digest(digest)
    has_app_scope = bool(application_id and application_id != "default")

    planned: list[dict[str, Any]] = []
    if mode in {"deterministic", "deterministic_fallback"}:
        scope = "app" if has_app_scope else "project"
        for note in notes:
            if note["blocked"] or not note["text"]:
                continue
            planned.append(
                {
                    "scope": scope,
                    "content": note["text"],
                    "replaces": "",
                    "evidence_refs": [note["ref"]],
                    "source_note_id": int(note["id"]),
                }
            )
    elif mode == "llm":
        if digest is None:
            raise ValueError("LLM semantic plans require a prepared digest")
        for proposal in proposals or []:
            if not isinstance(proposal, dict):
                raise ValueError("semantic plan proposals must be objects")
            content = redact_text(str(proposal.get("content") or "")).strip()
            scope = str(proposal.get("scope") or "project").strip().lower()
            if scope == "app" and not has_app_scope:
                scope = "project"
            replaces = str(proposal.get("replaces") or "").strip()
            evidence = proposal.get("evidence_refs")
            evidence_refs = list(
                dict.fromkeys(
                    str(ref).strip()
                    for ref in (evidence if isinstance(evidence, list) else [])
                    if str(ref).strip()
                )
            )
            if (
                not content
                or len(content) > _MAX_PROPOSAL_CHARS
                or scope not in {"app", "project"}
                or scan_injection_patterns(content)
                or not evidence_refs
                or any(ref not in digest.evidence_refs for ref in evidence_refs)
                or (replaces and replaces not in digest.replace_targets)
            ):
                raise ValueError("semantic plan contains an invalid model proposal")
            planned.append(
                {
                    "scope": scope,
                    "content": content,
                    "replaces": replaces,
                    "evidence_refs": evidence_refs,
                }
            )

    plan: dict[str, Any] = {
        "version": _SEMANTIC_PLAN_VERSION,
        "mode": mode,
        "proposals": planned,
        "archive_note_ids": (
            sorted({int(note["id"]) for note in notes})
            if mode in {"llm", "deterministic", "deterministic_fallback"}
            else []
        ),
    }
    plan["sha256"] = _semantic_plan_sha256(plan)
    return plan


def load_semantic_plan(
    value: Any,
    *,
    prepared_digest: dict[str, Any] | None,
    application_id: str,
) -> dict[str, Any] | None:
    """Validate a persisted plan against the exact frozen digest whitelists."""
    if not isinstance(value, dict):
        return None
    if value.get("version") != _SEMANTIC_PLAN_VERSION:
        return None
    mode = str(value.get("mode") or "")
    if mode not in _SEMANTIC_PLAN_MODES:
        return None
    expected_sha = str(value.get("sha256") or "")
    if not expected_sha or not hmac.compare_digest(
        expected_sha,
        _semantic_plan_sha256(value),
    ):
        return None
    proposals = value.get("proposals")
    archive_note_ids = value.get("archive_note_ids")
    if not isinstance(proposals, list) or not isinstance(archive_note_ids, list):
        return None
    try:
        rebuilt = build_semantic_plan(
            prepared_digest=prepared_digest,
            application_id=application_id,
            mode=mode,
            proposals=proposals if mode == "llm" else None,
        )
    except (TypeError, ValueError):
        return None
    if mode in {"deterministic", "deterministic_fallback"}:
        return rebuilt if rebuilt == value else None
    if rebuilt["proposals"] != proposals:
        return None
    if rebuilt["archive_note_ids"] != archive_note_ids:
        return None
    return rebuilt if rebuilt == value else None


def _error_signature(error_text: str) -> str:
    collapsed = re.sub(r"[0-9]+", "N", str(error_text))
    collapsed = re.sub(r"\s+", " ", collapsed).strip()
    return collapsed[:120]


def _repeated_failures(ledger: SelfLearningLedger, run_id: str) -> list[dict[str, Any]]:
    with ledger._connect() as conn:
        rows = conn.execute(
            """SELECT tool_name, output_json, content_text FROM events
            WHERE root_run_id = ?
                AND event_type = 'tool_error'""",
            (run_id,),
        ).fetchall()
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        tool_name = str(row["tool_name"] or "unknown_tool")
        error_text = ""
        try:
            output = json.loads(row["output_json"] or "{}")
            if isinstance(output, dict):
                error_text = str(output.get("error") or "")
        except json.JSONDecodeError:
            pass
        if not error_text:
            error_text = str(row["content_text"] or "")[:_ERROR_PREVIEW_CHARS]
        group = groups.setdefault((tool_name, _error_signature(error_text)), {"count": 0, "example": error_text})
        group["count"] += 1
    failures: list[dict[str, Any]] = []
    for (tool_name, _sig), group in sorted(groups.items(), key=lambda pair: -pair[1]["count"]):
        if group["count"] >= _MIN_REPEATED_FAILURES:
            failures.append(
                {
                    "tool_name": tool_name,
                    "count": int(group["count"]),
                    "example": str(group["example"])[:_ERROR_PREVIEW_CHARS],
                }
            )
    return failures


def _event_content_for_digest(value: Any) -> Any:
    """Keep canonical JSON events structured before redaction and bounding."""
    text = str(value or "")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return " ".join(text.split())
    return parsed if isinstance(parsed, (dict, list)) else " ".join(text.split())


def _build_run_digest(
    run_id: str,
    application_id: str = "",
    *,
    fallback_task: str = "",
    fallback_final_answer: str = "",
    db_path: str | Path | None = None,
) -> _RunDigest | None:
    """Build the model payload and its two explicit whitelists."""
    run_id = safe_run_id(run_id)
    if not run_id:
        return None
    ledger = SelfLearningLedger(db_path)
    from .memory_store import MemoryStore

    with ledger._connect() as conn:
        run_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        event_rows = conn.execute(
            """
            SELECT event_id, event_type, tool_name, content_text FROM events
            WHERE root_run_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (run_id, _DIGEST_MAX_EVENTS),
        ).fetchall()
    store = MemoryStore(db_path)
    notes = store.active_session_notes(run_id)
    failures = _repeated_failures(ledger, run_id)
    task_text = (str(run_row["task_text"] or "") if run_row else "") or str(fallback_task or "")
    final_answer = (str(run_row["final_answer"] or "") if run_row else "") or str(
        fallback_final_answer or ""
    )
    if not notes and not failures and not final_answer:
        return None

    builder = DigestBuilder(max_chars=_DIGEST_MAX_CHARS)
    if run_row:
        builder.add(ref="run.status", kind="run_status", value=str(run_row["status"] or ""), max_chars=80)
    if task_text:
        builder.add(ref="run.task", kind="task", value=task_text, max_chars=1500)
    if final_answer:
        builder.add(ref="run.final_answer", kind="final_answer", value=final_answer, max_chars=1500)
    for note in notes:
        builder.add(
            ref=f"session_note:{note['id']}",
            kind="session_note",
            value=note.get("content"),
            max_chars=400,
        )
    for index, failure in enumerate(failures):
        builder.add(
            ref=f"repeated_failure:{index}",
            kind="repeated_failure",
            value=(
                f"{failure['tool_name']} failed {failure['count']}x: "
                f"{failure['example']}"
            ),
            max_chars=_ERROR_PREVIEW_CHARS + 100,
        )

    target_by_ref: dict[str, str] = {}
    existing_chars = 0
    for scope, scope_id in (("project", ""), ("app", application_id)):
        if scope == "app" and (not application_id or application_id == "default"):
            continue
        try:
            items = store.list_digest_preview(
                scope,
                scope_id=scope_id,
                max_preview_chars=(
                    _EXISTING_MEMORY_PREVIEW_CHARS - existing_chars
                ),
                per_item_chars=200,
            )
        except Exception:
            continue
        for item in items:
            content = str(item.get("content") or "")
            if existing_chars >= _EXISTING_MEMORY_PREVIEW_CHARS:
                break
            preview = content[: min(200, _EXISTING_MEMORY_PREVIEW_CHARS - existing_chars)]
            existing_chars += len(preview)
            ref = f"existing_memory:{item['id']}"
            builder.add(
                ref=ref,
                kind="existing_memory",
                value={
                    "id": str(item["id"]),
                    "scope": scope,
                    "status": "pending" if item.get("status") == "pending" else "active",
                    "content": preview,
                },
                max_chars=350,
            )
            if item.get("status") == "active":
                target_by_ref[ref] = str(item["id"])

    for row in reversed(event_rows):
        event_id = str(row["event_id"] or "")
        if not event_id:
            continue
        builder.add(
            ref=f"event:{event_id}",
            kind="event",
            value={
                "event_type": str(row["event_type"] or ""),
                "tool_name": str(row["tool_name"] or ""),
                "content": _event_content_for_digest(row["content_text"]),
            },
            max_chars=_EVENT_PREVIEW_CHARS + 100,
        )

    evidence_refs = builder.evidence_refs
    replace_targets = {
        target for ref, target in target_by_ref.items() if ref in evidence_refs
    }
    return _RunDigest(
        text=builder.to_json(),
        evidence_refs=evidence_refs,
        replace_targets=replace_targets,
    )


def prepare_run_digest(
    run_id: str,
    application_id: str = "",
    *,
    fallback_task: str = "",
    fallback_final_answer: str = "",
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Freeze a run digest for durable outbox retries.

    The returned mapping is JSON-serializable and self-verifying. Once a job
    stores it, every retry must pass it back to ``distill_with_model`` so a
    changed ledger cannot change the model input or its evidence whitelist.
    """
    digest = _build_run_digest(
        run_id,
        application_id,
        fallback_task=fallback_task,
        fallback_final_answer=fallback_final_answer,
        db_path=db_path,
    )
    return _prepared_payload(digest) if digest is not None else None


def build_run_digest(
    run_id: str,
    application_id: str = "",
    *,
    fallback_task: str = "",
    fallback_final_answer: str = "",
    db_path: str | Path | None = None,
) -> str | None:
    """Compact JSON digest of one run; ``None`` when there is no signal.

    No session notes, no repeated failures, and no final answer means nothing
    worth a model call — the cost guard for batch deployments.

    ``fallback_task``/``fallback_final_answer`` preserve values from the
    atomic SessionEnd finalizer in the durable job payload. Every fragment
    crosses ``DigestBuilder``'s redact-then-scan boundary.
    """
    built = _build_run_digest(
        run_id,
        application_id,
        fallback_task=fallback_task,
        fallback_final_answer=fallback_final_answer,
        db_path=db_path,
    )
    return built.text if built is not None else None


def _parse_proposals(
    response: Any,
    *,
    valid_evidence_refs: set[str],
    valid_replace_targets: set[str],
) -> list[dict[str, Any]] | None:
    try:
        content = response["choices"][0]["message"]["content"].strip()
    except (TypeError, KeyError, IndexError, AttributeError):
        return None
    if content.startswith("```"):
        content = "\n".join(line for line in content.split("\n") if not line.startswith("```")).strip()
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("proposals"), list):
        return None
    raw_entries = parsed["proposals"][:_MAX_PROPOSALS]
    proposals: list[dict[str, Any]] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        raw_content = str(entry.get("content") or "").strip()
        scope = str(entry.get("scope") or "app").strip().lower()
        if not raw_content or len(raw_content) > _MAX_PROPOSAL_CHARS or scope not in {"app", "project"}:
            continue
        content_text = redact_text(raw_content).strip()
        if scan_injection_patterns(content_text):
            logger.warning("Distilled proposal dropped: matched injection pattern")
            continue
        evidence = entry.get("evidence_refs")
        if not isinstance(evidence, list) or not evidence:
            logger.warning("Distilled proposal dropped: missing evidence references")
            continue
        evidence_refs = list(dict.fromkeys(str(ref).strip() for ref in evidence if str(ref).strip()))
        if not evidence_refs or any(ref not in valid_evidence_refs for ref in evidence_refs):
            logger.warning("Distilled proposal dropped: unknown or blocked evidence reference")
            continue
        replaces = str(entry.get("replaces") or "").strip()
        if replaces and replaces not in valid_replace_targets:
            logger.warning("Distilled proposal dropped: replace target outside digest")
            continue
        proposals.append(
            {
                "scope": scope,
                "content": content_text,
                "replaces": replaces,
                "evidence_refs": evidence_refs,
            }
        )
    if raw_entries and not proposals:
        logger.warning(
            "Distillation response contained proposals, but every proposal failed validation"
        )
        return None
    return proposals


def distill_with_model(
    run_id: str,
    *,
    application_id: str = "",
    model_type: str,
    fallback_task: str = "",
    fallback_final_answer: str = "",
    prepared_digest: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
) -> list[dict[str, Any]] | None:
    """One completion call per run end; ``None`` means fall back to deterministic."""
    if prepared_digest is not None:
        digest = _load_prepared_digest(prepared_digest)
        if digest is None:
            logger.warning("Prepared memory digest failed integrity or safety validation")
            return None
    else:
        digest = _build_run_digest(
            run_id, application_id,
            fallback_task=fallback_task, fallback_final_answer=fallback_final_answer,
            db_path=db_path,
        )
    if digest is None:
        return []
    try:
        # Importing model_manager patches litellm.completion with the retry
        # wrapper that strips the retry-only keys present in the config dict.
        import litellm

        from src.lib.smolagents.models.model_manager import get_model

        cfg = dict(get_model(model_type, framework="litellm"))
        cfg["timeout"] = min(float(cfg.get("timeout") or _COMPLETION_TIMEOUT_SECONDS), _COMPLETION_TIMEOUT_SECONDS)
        cfg["num_retries"] = 0  # best-effort: a failed call falls back to deterministic distillation
        response = litellm.completion(
            **cfg,
            messages=[
                {"role": "system", "content": DISTILL_SYSTEM_PROMPT},
                {"role": "user", "content": digest.text},
            ],
        )
    except Exception as exc:
        logger.warning(
            "LLM distillation failed, falling back to deterministic: %s",
            _safe_log_text(exc),
        )
        return None
    proposals = _parse_proposals(
        response,
        valid_evidence_refs=digest.evidence_refs,
        valid_replace_targets=digest.replace_targets,
    )
    if proposals is None:
        logger.warning("LLM distillation response was not valid proposal JSON; falling back")
        return None
    return proposals
