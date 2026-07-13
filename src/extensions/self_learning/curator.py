"""LLM memory curator: cross-run hygiene for the durable memory corpus.

Adapts the hermes curator philosophy to memory: consolidate overlapping or
contradictory entries, compress verbose ones, and propose removals of stale
low-value facts — always as PENDING proposals (``source="curator"``), never
direct mutations. ``replace``/``remove`` proposals are exactly the actions the
auto-apply policy reserves for humans, so nothing changes until an operator
runs ``loom memory apply``.

Manual invocation only (``loom memory curate``): curated proposals wait for
human review. If an automatic cadence is ever added, it must be another
date-deduplicated durable outbox job, never synchronous SessionEnd work.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src.lib.logging import get_logger

from .digest import BLOCKED_TEXT, DigestBuilder
from .ledger import SelfLearningLedger
from .paths import memory_config
from .redaction import redact_text, redact_value, scan_injection_patterns

logger = get_logger(__name__)

_MAX_CURATOR_PROPOSALS = 10   # total per invocation
_MAX_BUCKETS_PER_RUN = 5
_MIN_BUCKET_ITEMS = 3         # smaller buckets have nothing worth consolidating
_PROTECTED_TRUST = 0.7        # never propose changes to well-proven facts...
_PROTECTED_RECENT_DAYS = 7    # ...or removal of freshly updated ones
_CURATE_TIMEOUT_SECONDS = 60  # CLI path, not hook-constrained
_DIGEST_ITEM_PREVIEW_CHARS = 300

CURATOR_SYSTEM_PROMPT = """You are the memory curator for an autonomous agent framework. \
You are given ONE scope bucket of durable memory entries with their metadata \
(trust score, injection/feedback counts, age) plus detected contradiction pairs \
and the bucket's budget usage. Propose the few edits a careful librarian would \
make. An empty proposal list is the common case — only propose changes that \
clearly improve the corpus.

WHAT to propose:
- MERGE near-duplicates or fragmented facts about the same subject: one \
"replace" on the entry to keep (with the consolidated text) plus a separate \
"remove" for each entry it absorbs.
- REWRITE a verbose or narrative entry into one compact declarative fact.
- REMOVE an entry that is stale, superseded, or contradicted by a higher-trust \
entry.

HARD RULES:
- Consolidated text must be derivable from the listed entries — never invent \
facts, numbers, or qualifiers that no entry states.
- Write declarative facts, not imperatives.
- Do not touch entries marked PROTECTED (high trust or recently updated).
- Resolve a contradiction by keeping the higher-trust/newer claim; never merge \
contradictory claims into one hedged sentence.
- At most {max_proposals} proposals total, each content under {max_chars} chars.

Respond ONLY with valid JSON, no other text:
{{"proposals": [{{"action": "replace" | "remove", "target": "<id>", "content": "...", "reason": "...", "evidence_refs": ["memory:<id>"]}}]}}
- "target" is the numeric id of the entry to replace or remove
- "content" is required for replace (the new consolidated/compact text), empty for remove
- "evidence_refs" is required and may contain only unblocked refs in the digest;
  it must include the target's own "memory:<id>" ref
- {{"proposals": []}} when the bucket is already clean"""


def _parse_response_json(response: Any) -> dict[str, Any] | None:
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
    return parsed if isinstance(parsed, dict) else None


def _age_days(updated_at: str) -> float:
    try:
        updated = datetime.fromisoformat(str(updated_at))
    except (TypeError, ValueError):
        return 0.0
    now = datetime.now(updated.tzinfo)
    return max(0.0, (now - updated).total_seconds() / 86400.0)


def _is_protected(item: dict[str, Any]) -> bool:
    trust = float(item.get("trust_score") if item.get("trust_score") is not None else 0.5)
    return trust >= _PROTECTED_TRUST or _age_days(str(item.get("updated_at") or "")) < _PROTECTED_RECENT_DAYS


def build_curation_digest(
    items: list[dict[str, Any]],
    conflict_pairs: list[dict[str, Any]],
    used_chars: int,
    budget_chars: int,
) -> str:
    """Build curator input through the same structured safety boundary."""
    builder = DigestBuilder(max_chars=14000, fragment_max_chars=1000)
    builder.add(
        ref="bucket.budget",
        kind="bucket_budget",
        value={"used_chars": int(used_chars), "budget_chars": int(budget_chars)},
    )
    for item in items:
        trust = float(item.get("trust_score") if item.get("trust_score") is not None else 0.5)
        builder.add(
            ref=f"memory:{item['id']}",
            kind="existing_memory",
            value={
                "id": str(item["id"]),
                "protected": _is_protected(item),
                "trust": round(trust, 2),
                "injected_count": int(item.get("injected_count") or 0),
                "helpful_count": int(item.get("helpful_count") or 0),
                "unhelpful_count": int(item.get("unhelpful_count") or 0),
                "age_days": round(_age_days(str(item.get("updated_at") or "")), 1),
                "chars": len(str(item.get("content") or "")),
                "content": str(item.get("content") or "")[:_DIGEST_ITEM_PREVIEW_CHARS],
            },
        )
    for index, pair in enumerate(conflict_pairs):
        builder.add(
            ref=f"conflict:{index}",
            kind="contradiction_pair",
            value={
                "a_id": str(pair["a_id"]),
                "b_id": str(pair["b_id"]),
                "overlap": pair["score"],
                "a_preview": str(pair["a_preview"])[:100],
                "b_preview": str(pair["b_preview"])[:100],
            },
        )
    return builder.to_json()


def _safe_model_value(value: Any) -> Any:
    """Redact model/provider output before it can enter audit artifacts."""
    redacted = redact_value(value)
    try:
        serialized = json.dumps(redacted, ensure_ascii=False, default=str)
    except Exception:
        serialized = str(redacted)
    if scan_injection_patterns(serialized):
        return {"blocked": True}
    return redacted


def _safe_model_text(value: Any, limit: int) -> str:
    redacted = redact_text(str(value or ""))[:limit]
    return BLOCKED_TEXT if scan_injection_patterns(redacted) else redacted


def _validate_proposal(
    proposal: dict[str, Any],
    items_by_id: dict[int, dict[str, Any]],
    max_item_chars: int,
    valid_evidence_refs: set[str],
) -> str:
    """Return a skip reason, or '' when the proposal passes every gate."""
    action = str(proposal.get("action") or "").strip().lower()
    if action not in {"replace", "remove"}:
        return "unsupported_action"
    try:
        target_id = int(str(proposal.get("target") or "").strip())
    except ValueError:
        return "non_numeric_target"
    item = items_by_id.get(target_id)
    if item is None:
        return "target_outside_bucket"
    evidence = proposal.get("evidence_refs")
    if not isinstance(evidence, list) or not evidence:
        return "missing_evidence_refs"
    evidence_refs = {str(ref).strip() for ref in evidence if str(ref).strip()}
    if not evidence_refs or not evidence_refs <= valid_evidence_refs:
        return "unknown_or_blocked_evidence_ref"
    if f"memory:{target_id}" not in evidence_refs:
        return "target_not_cited"
    if _is_protected(item):
        return "target_protected"
    content = redact_text(str(proposal.get("content") or "")).strip()
    if action == "replace":
        if not content:
            return "replace_without_content"
        if len(content) > max_item_chars:
            return "content_too_long"
        if scan_injection_patterns(content):
            return "injection_pattern"
    return ""


def curate_memory(
    *,
    scope: str | None = None,
    scope_id: str = "",
    model_type: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one curation pass over active memory buckets; proposals only."""
    from .memory_store import MemoryStore

    config = memory_config()
    resolved_model = (model_type or str(config.get("distill_model") or "")).strip()
    if not resolved_model:
        return {
            "ok": False,
            "error": "no_model_configured",
            "hint": "set self_learning.memory.distill_model or pass --model",
        }
    max_item_chars = int(config.get("max_item_chars") or 4000)

    store = MemoryStore()
    actives = [item for item in store.list(scope, scope_id=scope_id, include_pending=False)]
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in actives:
        if item.get("scope_type") == "session":
            continue  # TTL owns session hygiene
        buckets.setdefault((str(item["scope_type"]), str(item["scope_id"])), []).append(item)

    conflict_report = store.conflicts(scope=scope, scope_id=scope_id)
    pairs_by_bucket: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for pair in conflict_report.get("active_conflict_pairs") or []:
        pairs_by_bucket.setdefault((str(pair["scope_type"]), str(pair["scope_id"])), []).append(pair)

    proposals_written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    buckets_reviewed: list[str] = []

    for (scope_type, bucket_id), bucket_items in sorted(buckets.items())[:_MAX_BUCKETS_PER_RUN]:
        if len(bucket_items) < _MIN_BUCKET_ITEMS:
            continue
        used = sum(len(str(item.get("content") or "")) for item in bucket_items)
        budget = store._scope_budget(scope_type)
        digest = build_curation_digest(
            bucket_items, pairs_by_bucket.get((scope_type, bucket_id), []), used, budget
        )
        digest_payload = json.loads(digest)
        valid_evidence_refs = {
            str(fragment["ref"])
            for fragment in digest_payload.get("fragments", [])
            if isinstance(fragment, dict) and not fragment.get("blocked")
        }
        try:
            import litellm

            from src.lib.smolagents.models.model_manager import get_model

            cfg = dict(get_model(resolved_model, framework="litellm"))
            cfg["timeout"] = min(float(cfg.get("timeout") or _CURATE_TIMEOUT_SECONDS), _CURATE_TIMEOUT_SECONDS)
            cfg["num_retries"] = 0
            response = litellm.completion(
                **cfg,
                messages=[
                    {
                        "role": "system",
                        "content": CURATOR_SYSTEM_PROMPT.format(
                            max_proposals=_MAX_CURATOR_PROPOSALS, max_chars=max_item_chars
                        ),
                    },
                    {"role": "user", "content": digest},
                ],
            )
        except Exception as exc:
            safe_error = _safe_model_text(exc, 200)
            logger.warning(
                "Curator model call failed for bucket %s:%s: %s",
                scope_type,
                bucket_id,
                safe_error,
            )
            skipped.append(
                {
                    "bucket": f"{scope_type}:{bucket_id}",
                    "reason": f"model_error: {safe_error}",
                }
            )
            continue
        buckets_reviewed.append(f"{scope_type}:{bucket_id}")
        parsed = _parse_response_json(response)
        if parsed is None or not isinstance(parsed.get("proposals"), list):
            skipped.append({"bucket": f"{scope_type}:{bucket_id}", "reason": "unparseable_response"})
            continue

        items_by_id = {int(item["id"]): item for item in bucket_items}
        write_scope = "project" if scope_type == "project" else "app"
        write_scope_id = "" if scope_type == "project" else bucket_id
        for proposal in parsed["proposals"]:
            if not isinstance(proposal, dict):
                continue
            if len(proposals_written) >= _MAX_CURATOR_PROPOSALS:
                skipped.append({"bucket": f"{scope_type}:{bucket_id}", "reason": "proposal_cap_reached"})
                break
            reason = _validate_proposal(
                proposal,
                items_by_id,
                max_item_chars,
                valid_evidence_refs,
            )
            if reason:
                skipped.append({"proposal": _safe_model_value(proposal), "reason": reason})
                continue
            entry = {
                "action": str(proposal["action"]).strip().lower(),
                "target": str(proposal["target"]).strip(),
                "content": _safe_model_text(proposal.get("content"), max_item_chars).strip(),
                "reason": _safe_model_text(proposal.get("reason"), 200),
                "evidence_refs": list(
                    dict.fromkeys(
                        str(ref).strip()
                        for ref in proposal.get("evidence_refs", [])
                        if str(ref).strip()
                    )
                ),
                "bucket": f"{scope_type}:{bucket_id}",
            }
            if not dry_run:
                try:
                    if entry["action"] == "replace":
                        result = store.replace(
                            write_scope, entry["target"], entry["content"],
                            proposal=True, source="curator", scope_id=write_scope_id,
                        )
                    else:
                        result = store.remove(
                            write_scope, entry["target"],
                            proposal=True, source="curator", scope_id=write_scope_id,
                        )
                except (KeyError, ValueError) as exc:
                    skipped.append(
                        {
                            "proposal": _safe_model_value(proposal),
                            "reason": f"store_rejected: {_safe_model_text(exc, 200)}",
                        }
                    )
                    continue
                entry["proposal_id"] = result.get("id")
            proposals_written.append(entry)

    summary = {
        "ok": True,
        "dry_run": dry_run,
        "model": resolved_model,
        "buckets_reviewed": buckets_reviewed,
        "proposals": proposals_written,
        "skipped": skipped,
        "hint": "Review with `loom memory list` and promote with `loom memory apply <id>`.",
    }
    if not dry_run:
        try:
            SelfLearningLedger().record_review(
                source_run_id="curator_cli",
                hook_event="Curate",
                status="curate",
                output=summary,
            )
        except Exception:
            pass
    return summary
