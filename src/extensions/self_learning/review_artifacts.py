"""Human-review artifacts for typed self-learning candidates.

The database remains authoritative.  This module only projects an immutable
batch audit and an editable decision inbox into ``.agentloom/reviews``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .application_scope import safe_application_id

_SAFE_REVIEW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DECISION_BLOCK = re.compile(
    r"<!-- agentloom-decisions:start -->\s*```json\s*(.*?)\s*```\s*"
    r"<!-- agentloom-decisions:end -->",
    flags=re.DOTALL,
)
_DECISION_ACTIONS = {
    "approve",
    "acknowledge",
    "reject",
    "revoke",
    "correct",
    "promote_project",
}


@dataclass(frozen=True)
class ReviewArtifactPaths:
    """Files materialized for one review batch."""

    scope_dir: Path
    batch_dir: Path
    review_json: Path
    report: Path | None
    inbox: Path
    index: Path


class ReviewArtifactConflictError(RuntimeError):
    """An immutable batch id was reused with different content."""


class ReviewArtifactRenderer:
    """Render review engine results without owning review state transitions."""

    def __init__(
        self,
        reviews_root: str | Path,
        *,
        markdown: bool = True,
        review_auto_applied: bool = True,
    ) -> None:
        self.reviews_root = Path(reviews_root).expanduser().resolve()
        self.markdown = bool(markdown)
        self.review_auto_applied = bool(review_auto_applied)

    def render_batch(self, batch: Mapping[str, Any] | Any) -> ReviewArtifactPaths:
        """Render one engine result and return its public artifact paths."""

        payload = _as_mapping(batch)
        review_id = str(payload.get("review_id") or "")
        if not _SAFE_REVIEW_ID.fullmatch(review_id):
            raise ValueError("review_id must be a safe non-empty path component")
        scope_type = str(payload.get("scope_type") or "")
        scope_id = str(payload.get("scope_id") or "")
        scope_dir = self._scope_dir(scope_type, scope_id)
        batch_dir = scope_dir / "batches" / review_id
        inbox = scope_dir / ("INBOX.md" if self.markdown else "INBOX.json")
        existing_decisions = (
            _read_inbox_rows(
                inbox,
                markdown=self.markdown,
                expected_scope=_normalize_scope(scope_type, scope_id),
            )
            if inbox.is_file()
            else []
        )

        normalized = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        _validate_batch_candidates(normalized)
        review_text = (
            json.dumps(
                normalized,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        report_text = _render_report(normalized) if self.markdown else None
        self._create_immutable_batch(batch_dir, review_text, report_text)

        candidates = [
            candidate
            for candidate in normalized.get("candidates", [])
            if isinstance(candidate, dict)
            and (
                candidate.get("state") == "pending_pre_review"
                or (self.review_auto_applied and candidate.get("state") == "active_unreviewed")
            )
        ]
        current_keys = {
            (str(candidate.get("candidate_id") or ""), int(candidate.get("revision") or 0)) for candidate in candidates
        }
        candidates.extend(
            candidate
            for candidate in _find_candidate_details(scope_dir, existing_decisions)
            if (
                str(candidate.get("candidate_id") or ""),
                int(candidate.get("revision") or 0),
            )
            not in current_keys
        )
        scope_dir.mkdir(parents=True, exist_ok=True)
        if candidates or existing_decisions:
            _atomic_write(
                inbox,
                _render_markdown_inbox(
                    scope_type,
                    scope_id,
                    candidates,
                    existing_decisions=existing_decisions,
                )
                if self.markdown
                else _render_json_inbox(
                    scope_type,
                    scope_id,
                    candidates,
                    existing_decisions=existing_decisions,
                ),
            )
        else:
            inbox.unlink(missing_ok=True)
        index = self._render_index()
        return ReviewArtifactPaths(
            scope_dir=scope_dir,
            batch_dir=batch_dir,
            review_json=batch_dir / "review.json",
            report=(batch_dir / "REPORT.md") if self.markdown else None,
            inbox=inbox,
            index=index,
        )

    def read_decisions(self, scope_type: str, scope_id: str = "") -> list[dict[str, Any]]:
        """Read actionable decisions from exactly one scope's editable inbox."""

        normalized_type, normalized_id = _normalize_scope(scope_type, scope_id)
        scope_dir = self._scope_dir(normalized_type, normalized_id)
        inbox = scope_dir / ("INBOX.md" if self.markdown else "INBOX.json")
        rows = _read_inbox_rows(
            inbox,
            markdown=self.markdown,
            expected_scope=(normalized_type, normalized_id),
        )

        decisions: list[dict[str, Any]] = []
        for row in rows:
            action = row["decision"]
            if action == "pending":
                continue
            decision: dict[str, Any] = {
                "candidate_id": row["candidate_id"],
                "revision": row["revision"],
                "action": action,
            }
            if "payload" in row:
                decision["payload"] = row["payload"]
            if "memory_key" in row:
                decision["memory_key"] = str(row["memory_key"])
            decisions.append(decision)
        return decisions

    def render_inbox(
        self,
        scope_type: str,
        scope_id: str,
        candidates: list[dict[str, Any]],
    ) -> Path:
        """Reconcile one scoped INBOX from authoritative engine status."""

        normalized_type, normalized_id = _normalize_scope(scope_type, scope_id)
        normalized_candidates = json.loads(json.dumps(candidates, ensure_ascii=False, default=str))
        _validate_batch_candidates({"candidates": normalized_candidates})
        scope_dir = self._scope_dir(normalized_type, normalized_id)
        inbox = scope_dir / ("INBOX.md" if self.markdown else "INBOX.json")
        existing_decisions = (
            _read_inbox_rows(
                inbox,
                markdown=self.markdown,
                expected_scope=(normalized_type, normalized_id),
            )
            if inbox.is_file()
            else []
        )
        reviewable = [
            candidate
            for candidate in normalized_candidates
            if candidate.get("state") == "pending_pre_review"
            or (self.review_auto_applied and candidate.get("state") == "active_unreviewed")
        ]
        if not reviewable:
            inbox.unlink(missing_ok=True)
            self._render_index()
            return inbox
        content = (
            _render_markdown_inbox(
                normalized_type,
                normalized_id,
                reviewable,
                existing_decisions=existing_decisions,
                preserve_unlisted=False,
            )
            if self.markdown
            else _render_json_inbox(
                normalized_type,
                normalized_id,
                reviewable,
                existing_decisions=existing_decisions,
                preserve_unlisted=False,
            )
        )
        _atomic_write(inbox, content)
        self._render_index()
        return inbox

    def remove_decisions(
        self,
        scope_type: str,
        scope_id: str,
        applied: list[dict[str, Any]],
    ) -> None:
        """Remove rows only after the engine atomically applied them."""

        normalized_type, normalized_id = _normalize_scope(scope_type, scope_id)
        scope_dir = self._scope_dir(normalized_type, normalized_id)
        inbox = scope_dir / ("INBOX.md" if self.markdown else "INBOX.json")
        rows = _read_inbox_rows(
            inbox,
            markdown=self.markdown,
            expected_scope=(normalized_type, normalized_id),
        )
        applied_keys = {(str(row.get("candidate_id") or ""), int(row.get("revision") or 0)) for row in applied}
        remaining = [row for row in rows if (row["candidate_id"], row["revision"]) not in applied_keys]
        if not remaining:
            inbox.unlink(missing_ok=True)
            self._render_index()
            return

        candidates = _find_candidate_details(scope_dir, remaining)
        content = (
            _render_markdown_inbox(
                normalized_type,
                normalized_id,
                candidates,
                existing_decisions=remaining,
            )
            if self.markdown
            else _render_json_inbox(
                normalized_type,
                normalized_id,
                candidates,
                existing_decisions=remaining,
            )
        )
        _atomic_write(inbox, content)
        self._render_index()

    def remove_review(self, review_id: str) -> None:
        """Remove one rolled-back batch's candidates from its scoped INBOX."""

        if not _SAFE_REVIEW_ID.fullmatch(review_id):
            raise ValueError("review_id must be a safe non-empty path component")
        candidates = [self.reviews_root / "project" / "batches" / review_id / "review.json"]
        applications = self.reviews_root / "applications"
        if applications.is_dir():
            candidates.extend(applications.rglob(f"batches/{review_id}/review.json"))
        for review_json in candidates:
            if not review_json.is_file():
                continue
            try:
                batch = json.loads(review_json.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ReviewArtifactConflictError(f"immutable review artifact is invalid: {review_json}") from exc
            if str(batch.get("review_id") or "") != review_id:
                raise ReviewArtifactConflictError(f"immutable review artifact id mismatch: {review_json}")
            scope_type, scope_id = _normalize_scope(
                str(batch.get("scope_type") or ""),
                str(batch.get("scope_id") or ""),
            )
            inbox = self._scope_dir(scope_type, scope_id) / ("INBOX.md" if self.markdown else "INBOX.json")
            if not inbox.is_file():
                continue
            refs = [
                {
                    "candidate_id": str(candidate.get("candidate_id") or ""),
                    "revision": int(candidate.get("revision") or 0),
                }
                for candidate in batch.get("candidates", [])
                if isinstance(candidate, dict)
            ]
            self.remove_decisions(scope_type, scope_id, refs)

    def _scope_dir(self, scope_type: str, scope_id: str) -> Path:
        if scope_type == "project":
            if scope_id not in {"", "project"}:
                raise ValueError("project review scope_id must be empty or 'project'")
            return self.reviews_root / "project"
        if scope_type in {"app", "application"}:
            canonical = safe_application_id(scope_id)
            if not canonical:
                raise ValueError("application review requires a valid application id")
            return self.reviews_root / "applications" / Path(*canonical.split("/"))
        raise ValueError("scope_type must be 'project' or 'application'")

    def _create_immutable_batch(
        self,
        batch_dir: Path,
        review_text: str,
        report_text: str | None,
    ) -> None:
        review_path = batch_dir / "review.json"
        report_path = batch_dir / "REPORT.md"
        if batch_dir.exists():
            same_review = review_path.is_file() and review_path.read_text(encoding="utf-8") == review_text
            same_report = report_text is None or (
                report_path.is_file() and report_path.read_text(encoding="utf-8") == report_text
            )
            if same_review and same_report:
                return
            raise ReviewArtifactConflictError(f"review batch {batch_dir.name!r} already exists with different content")

        batch_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{batch_dir.name}.", dir=batch_dir.parent))
        try:
            (staging / "review.json").write_text(review_text, encoding="utf-8")
            if report_text is not None:
                (staging / "REPORT.md").write_text(report_text, encoding="utf-8")
            try:
                os.replace(staging, batch_dir)
            except OSError:
                if batch_dir.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                    return self._create_immutable_batch(batch_dir, review_text, report_text)
                raise
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def _render_index(self) -> Path:
        self.reviews_root.mkdir(parents=True, exist_ok=True)
        if not self.markdown:
            index = self.reviews_root / "INDEX.json"
            scopes = _discover_scope_links(self.reviews_root, markdown=False)
            _atomic_write(index, json.dumps({"generated": True, "scopes": scopes}, indent=2) + "\n")
            return index

        index = self.reviews_root / "INDEX.md"
        lines = [
            "# AgentLoom review index",
            "",
            "> Generated, read-only aggregation. Apply decisions from a scoped INBOX.",
            "",
        ]
        links = _discover_scope_links(self.reviews_root, markdown=True)
        lines.extend(
            f"- [{entry['label']}]({entry['inbox']}) — "
            f"pending_pre_review={entry['pending_pre_review']}, "
            f"active_unreviewed={entry['active_unreviewed']}"
            for entry in links
        )
        lines.append("")
        _atomic_write(index, "\n".join(lines))
        return index


class ReviewEngineProtocol(Protocol):
    """Narrow persistence seam consumed by the CLI adapter."""

    def status(self, scope_type: str | None = None, scope_id: str = "") -> dict[str, Any]: ...

    def apply_decisions(
        self,
        scope_type: str,
        scope_id: str,
        decisions: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    def rollback(self, review_id: str) -> dict[str, Any]: ...

    def submit_feedback(
        self,
        run_id: str,
        verdict: str,
        item_id: int | None = None,
        *,
        application_id: str = "",
        correction: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class ReviewOrchestratorProtocol(Protocol):
    """Candidate extraction seam; raw run/event logic stays outside the CLI."""

    def run_review(
        self,
        scope_type: str,
        scope_id: str,
        *,
        dry_run: bool = False,
    ) -> Mapping[str, Any] | Any: ...

    def unreviewed_application_ids(self) -> list[str]: ...


def _resolve_application_review_config(application_id: str) -> dict[str, Any]:
    """Build the effective policy for one Application without choosing an Agent.

    The synthetic workflow path triggers the normal Application-level
    ``config/system.yaml`` overlay while deliberately avoiding an arbitrary
    worker YAML. Project policy remains protected by the normal config builder.
    """

    from src.lib.config import C, build_effective_agent_config

    canonical = safe_application_id(application_id)
    app_root = Path(C.agent_root) / "applications" / Path(*canonical.split("/"))
    seed: dict[str, Any] = {
        "name": f"review_{canonical.replace('/', '_')}",
        "application_id": canonical,
    }
    if app_root.is_dir():
        seed["_yaml_file_path"] = str(app_root / "workflows" / ".agentloom-review.yaml")
    return build_effective_agent_config(
        seed,
        source_name=f"Application review CLI ({canonical})",
    )


class ReviewCLIService:
    """Thin orchestration adapter shared by the Click command functions."""

    def __init__(
        self,
        *,
        engine: ReviewEngineProtocol | None = None,
        orchestrator: ReviewOrchestratorProtocol | None = None,
        reviews_root: str | Path | None = None,
        markdown: bool | None = None,
        review_auto_applied: bool | None = None,
        application_config_resolver: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        owns_runtime = engine is None and orchestrator is None
        if engine is None:
            try:
                from .paths import self_learning_db
                from .persistence.evidence_gate import SQLiteEvidenceGate
                from .persistence.review_engine import ReviewEngine
            except ImportError as exc:  # pragma: no cover - integration guard
                raise RuntimeError("ReviewEngine integration is unavailable") from exc
            db_path = self_learning_db()
            engine = ReviewEngine(
                db_path,
                evidence_gate=SQLiteEvidenceGate(db_path),
            )
        if orchestrator is None:
            try:
                from .review_orchestration import ReviewOrchestrator
            except ImportError as exc:  # pragma: no cover - integration guard
                raise RuntimeError("ReviewOrchestrator integration is unavailable") from exc
            orchestrator = ReviewOrchestrator(engine=engine, render_artifacts=False)

        if reviews_root is None or markdown is None or review_auto_applied is None:
            from .paths import review_config, self_learning_root

            artifacts = review_config(scope="application").get("artifacts", {})
            if not isinstance(artifacts, dict):
                artifacts = {}
            if reviews_root is None:
                reviews_root = self_learning_root() / "reviews"
            if markdown is None:
                markdown = bool(artifacts.get("markdown", True))
            if review_auto_applied is None:
                review_auto_applied = bool(artifacts.get("review_auto_applied", True))

        self.engine = engine
        self.orchestrator = orchestrator
        self._owns_runtime = owns_runtime
        self._application_config_resolver = application_config_resolver or _resolve_application_review_config
        self.renderer = ReviewArtifactRenderer(
            reviews_root,
            markdown=bool(markdown),
            review_auto_applied=bool(review_auto_applied),
        )

    def _runtime_for_scope(
        self,
        scope_type: str,
        scope_id: str,
    ) -> tuple[ReviewEngineProtocol, ReviewOrchestratorProtocol]:
        if not self._owns_runtime:
            return self.engine, self.orchestrator

        from .paths import memory_config, self_learning_db
        from .persistence.evidence_gate import SQLiteEvidenceGate
        from .persistence.review_engine import ReviewEngine
        from .review_orchestration import ReviewOrchestrator

        agent_config = self._application_config_resolver(scope_id) if scope_type == "application" else None
        db_path = self_learning_db()
        engine = ReviewEngine(
            db_path,
            evidence_gate=SQLiteEvidenceGate(db_path),
            capacity_policy=memory_config(agent_config),
        )
        orchestrator = ReviewOrchestrator(
            engine=engine,
            agent_config=agent_config,
            render_artifacts=False,
        )
        return engine, orchestrator

    def review_one(
        self,
        scope_type: str,
        scope_id: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        normalized_type, normalized_id = _normalize_scope(scope_type, scope_id)
        engine, orchestrator = self._runtime_for_scope(
            normalized_type,
            normalized_id,
        )
        batch = orchestrator.run_review(
            normalized_type,
            normalized_id,
            dry_run=dry_run,
        )
        result = _as_mapping(batch)
        try:
            rendered = self.renderer.render_batch(batch)
        except BaseException:
            review_id = str(result.get("review_id") or "")
            engine.rollback(review_id)
            try:
                # Preserve immutable batch files if rendering reached them,
                # while removing any editable INBOX references it created.
                self.renderer.remove_review(review_id)
            except Exception:
                pass
            raise
        result["artifacts"] = {
            "review_json": str(rendered.review_json),
            "report": str(rendered.report) if rendered.report is not None else None,
            "inbox": str(rendered.inbox) if rendered.inbox.is_file() else None,
            "index": str(rendered.index),
        }
        return result

    def review_all(self, *, dry_run: bool = False) -> dict[str, Any]:
        application_results = [
            self.review_one("application", application_id, dry_run=dry_run)
            for application_id in sorted(set(self.orchestrator.unreviewed_application_ids()))
        ]
        project_result = self.review_one("project", "project", dry_run=dry_run)
        return {"applications": application_results, "project": project_result}

    def status(self, scope_type: str, scope_id: str = "") -> dict[str, Any]:
        if scope_type == "all":
            return self.engine.status(None, "")
        normalized_type, normalized_id = _normalize_scope(scope_type, scope_id)
        engine, _orchestrator = self._runtime_for_scope(
            normalized_type,
            normalized_id,
        )
        return engine.status(normalized_type, normalized_id)

    def apply(self, scope_type: str, scope_id: str = "") -> dict[str, Any]:
        normalized_type, normalized_id = _normalize_scope(scope_type, scope_id)
        engine, _orchestrator = self._runtime_for_scope(
            normalized_type,
            normalized_id,
        )
        status = engine.status(normalized_type, normalized_id)
        authoritative = status.get("candidates") if isinstance(status, dict) else None
        if isinstance(authoritative, list):
            inbox = self.renderer.render_inbox(
                normalized_type,
                normalized_id,
                authoritative,
            )
            if not inbox.is_file():
                return {"applied": 0, "results": []}
        decisions = self.renderer.read_decisions(normalized_type, normalized_id)
        result = engine.apply_decisions(normalized_type, normalized_id, decisions)
        self.renderer.remove_decisions(normalized_type, normalized_id, decisions)
        return result

    def rollback(self, review_id: str) -> dict[str, Any]:
        result = self.engine.rollback(review_id)
        self.renderer.remove_review(review_id)
        return result

    def submit_feedback(
        self,
        *,
        run_id: str,
        verdict: str,
        item_id: int | None = None,
    ) -> dict[str, Any]:
        return self.engine.submit_feedback(run_id, verdict, item_id)


def _as_mapping(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return dict(converted)
    raise TypeError("review batch must be a mapping or provide to_dict()")


def _validate_batch_candidates(batch: dict[str, Any]) -> None:
    candidates = batch.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("review batch candidates must be a list")
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("each review candidate must be an object")
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        if not candidate_id or any(character in candidate_id for character in "\r\n\x00"):
            raise ValueError("review candidate_id must be a stable non-empty identifier")
        if candidate_id in seen:
            raise ValueError(f"duplicate review candidate_id {candidate_id!r}")
        seen.add(candidate_id)
        revision = candidate.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("review candidate revision must be a positive integer")


def _normalize_scope(scope_type: str, scope_id: str) -> tuple[str, str]:
    if scope_type == "project":
        if scope_id not in {"", "project"}:
            raise ValueError("project review scope_id must be empty or 'project'")
        return "project", "project"
    if scope_type in {"app", "application"}:
        canonical = safe_application_id(scope_id)
        if not canonical:
            raise ValueError("application review requires a valid application id")
        return "application", canonical
    raise ValueError("scope_type must be 'project' or 'application'")


def _render_report(batch: Mapping[str, Any]) -> str:
    lines = [
        f"# Review batch {batch['review_id']}",
        "",
        f"- Scope: `{batch.get('scope_type')}:{batch.get('scope_id')}`",
        f"- Status: `{batch.get('status', '')}`",
        f"- Dry run: `{bool(batch.get('dry_run', False))}`",
        "",
    ]
    candidates = batch.get("candidates", [])
    if not candidates:
        lines.extend(["No candidates.", ""])
        return "\n".join(lines)
    valid = [candidate for candidate in candidates if isinstance(candidate, Mapping)]
    sections = (
        (
            "Pending pre-review",
            [candidate for candidate in valid if candidate.get("state") == "pending_pre_review"],
        ),
        (
            "Auto-applied awaiting acknowledgement",
            [
                candidate
                for candidate in valid
                if candidate.get("state") == "active_unreviewed" and candidate.get("outcome") == "activated"
            ],
        ),
        (
            "Skipped or quarantined",
            [
                candidate
                for candidate in valid
                if candidate.get("state") != "pending_pre_review"
                and not (candidate.get("state") == "active_unreviewed" and candidate.get("outcome") == "activated")
            ],
        ),
    )
    for heading, section_candidates in sections:
        lines.extend([f"## {heading}", ""])
        if not section_candidates:
            lines.extend(["None.", ""])
            continue
        for candidate in section_candidates:
            lines.extend(_report_candidate_lines(candidate))
    return "\n".join(lines)


def _report_candidate_lines(candidate: Mapping[str, Any]) -> list[str]:
    payload = json.dumps(candidate.get("payload", {}), ensure_ascii=False, indent=2)
    indented_payload = "\n".join(f"    {line}" for line in payload.splitlines())
    return [
        f"### `{_inline_code(candidate.get('candidate_id', ''))}`",
        "",
        f"- Revision: `{candidate.get('revision', '')}`",
        f"- Kind: `{_inline_code(candidate.get('kind', ''))}`",
        f"- Key: `{_inline_code(candidate.get('memory_key', ''))}`",
        f"- State: `{_inline_code(candidate.get('state', ''))}`",
        f"- Outcome: `{_inline_code(candidate.get('outcome', ''))}`",
        "",
        indented_payload,
        "",
    ]


def _decision_rows(
    candidates: list[dict[str, Any]],
    existing_decisions: list[dict[str, Any]] | None = None,
    *,
    preserve_unlisted: bool = True,
) -> list[dict[str, Any]]:
    existing = {(row["candidate_id"], row["revision"]): row for row in (existing_decisions or [])}
    rows = []
    current_candidate_ids: set[str] = set()
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        current_candidate_ids.add(candidate_id)
        revision = int(candidate.get("revision") or 0)
        prior = existing.get((candidate_id, revision), {})
        row: dict[str, Any] = {
            "candidate_id": candidate_id,
            "revision": revision,
            "decision": prior.get("decision", "pending"),
        }
        if "payload" in prior:
            row["payload"] = prior["payload"]
        if "memory_key" in prior:
            row["memory_key"] = prior["memory_key"]
        rows.append(row)
    if preserve_unlisted:
        rows.extend(dict(row) for row in (existing_decisions or []) if row["candidate_id"] not in current_candidate_ids)
    return rows


def _render_markdown_inbox(
    scope_type: str,
    scope_id: str,
    candidates: list[dict[str, Any]],
    *,
    existing_decisions: list[dict[str, Any]] | None = None,
    preserve_unlisted: bool = True,
) -> str:
    title = "Project" if scope_type == "project" else f"Application {scope_id}"
    decisions = _decision_rows(
        candidates,
        existing_decisions,
        preserve_unlisted=preserve_unlisted,
    )
    pending = [candidate for candidate in candidates if candidate.get("state") == "pending_pre_review"]
    auto_applied = [candidate for candidate in candidates if candidate.get("state") == "active_unreviewed"]
    candidate_sections = ["## Pending pre-review", ""]
    candidate_sections.extend(_candidate_summary_lines(pending))
    candidate_sections.extend(["", "## Auto-applied awaiting acknowledgement", ""])
    candidate_sections.extend(_candidate_summary_lines(auto_applied))
    return "\n".join(
        [
            f"# {title} review inbox",
            "",
            "Set each `decision` to `approve`, `acknowledge`, `reject`, `revoke`, "
            "`correct`, or `promote_project`, then run the scoped `loom reviews apply` command.",
            "For `correct`, also add a typed `payload` and optionally a `memory_key` to that row.",
            "Candidate id and revision are concurrency guards and must not be changed.",
            "",
            *candidate_sections,
            "",
            "## Decisions",
            "",
            "<!-- agentloom-decisions:start -->",
            "```json",
            json.dumps(
                {"scope_type": scope_type, "scope_id": scope_id, "decisions": decisions}, ensure_ascii=False, indent=2
            ),
            "```",
            "<!-- agentloom-decisions:end -->",
            "",
        ]
    )


def _candidate_summary_lines(candidates: list[dict[str, Any]]) -> list[str]:
    if not candidates:
        return ["None."]
    lines = []
    for candidate in candidates:
        candidate_id = _inline_code(candidate.get("candidate_id", ""))
        revision = int(candidate.get("revision") or 0)
        kind = _inline_code(candidate.get("kind", ""))
        memory_key = _inline_code(candidate.get("memory_key", ""))
        lines.append(f"- `{candidate_id}` revision `{revision}` — `{kind}` / `{memory_key}`")
    return lines


def _inline_code(value: Any) -> str:
    return str(value).replace("`", "\\`").replace("\n", " ").replace("\r", " ")


def _render_json_inbox(
    scope_type: str,
    scope_id: str,
    candidates: list[dict[str, Any]],
    *,
    existing_decisions: list[dict[str, Any]] | None = None,
    preserve_unlisted: bool = True,
) -> str:
    return (
        json.dumps(
            {
                "scope_type": scope_type,
                "scope_id": scope_id,
                "decisions": _decision_rows(
                    candidates,
                    existing_decisions,
                    preserve_unlisted=preserve_unlisted,
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


def _read_inbox_rows(
    inbox: Path,
    *,
    markdown: bool,
    expected_scope: tuple[str, str],
) -> list[dict[str, Any]]:
    raw = inbox.read_text(encoding="utf-8")
    if markdown:
        match = _DECISION_BLOCK.search(raw)
        if match is None:
            raise ValueError("review inbox has no AgentLoom decisions block")
        raw = match.group(1)
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("review inbox decisions must be valid JSON") from exc
    if not isinstance(document, dict):
        raise ValueError("review inbox decisions must be a JSON object")
    document_scope = _normalize_scope(
        str(document.get("scope_type") or ""),
        str(document.get("scope_id") or ""),
    )
    if document_scope != expected_scope:
        raise ValueError("review inbox scope does not match the requested scope")
    raw_rows = document.get("decisions")
    if not isinstance(raw_rows, list):
        raise ValueError("review inbox decisions must be a list")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            raise ValueError("each review decision must be an object")
        candidate_id = str(raw_row.get("candidate_id") or "")
        if not candidate_id:
            raise ValueError("review decision candidate_id is required")
        if candidate_id in seen:
            raise ValueError(f"duplicate review decision for {candidate_id!r}")
        seen.add(candidate_id)
        revision = raw_row.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("review decision revision must be a positive integer")
        decision = str(raw_row.get("decision") or "pending")
        if decision != "pending" and decision not in _DECISION_ACTIONS:
            raise ValueError(f"unsupported review decision {decision!r}")
        row: dict[str, Any] = {
            "candidate_id": candidate_id,
            "revision": revision,
            "decision": decision,
        }
        if "payload" in raw_row:
            row["payload"] = raw_row["payload"]
        if "memory_key" in raw_row:
            row["memory_key"] = str(raw_row["memory_key"])
        rows.append(row)
    return rows


def _find_candidate_details(
    scope_dir: Path,
    decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    wanted = {(row["candidate_id"], row["revision"]) for row in decisions}
    found: dict[tuple[str, int], dict[str, Any]] = {}
    batches = scope_dir / "batches"
    if batches.is_dir():
        for review_json in sorted(batches.glob("*/review.json")):
            try:
                batch = json.loads(review_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for candidate in batch.get("candidates", []):
                if not isinstance(candidate, dict):
                    continue
                try:
                    key = (
                        str(candidate.get("candidate_id") or ""),
                        int(candidate.get("revision") or 0),
                    )
                except (TypeError, ValueError):
                    continue
                if key in wanted:
                    found[key] = candidate
    result = []
    for row in decisions:
        key = (row["candidate_id"], row["revision"])
        result.append(
            found.get(
                key,
                {
                    "candidate_id": row["candidate_id"],
                    "revision": row["revision"],
                    "kind": "",
                    "memory_key": "",
                    "state": "pending_pre_review",
                },
            )
        )
    return result


def _discover_scope_links(root: Path, *, markdown: bool) -> list[dict[str, Any]]:
    inbox_name = "INBOX.md" if markdown else "INBOX.json"
    entries: list[dict[str, Any]] = []
    project_inbox = root / "project" / inbox_name
    if project_inbox.is_file():
        entries.append(
            _scope_index_entry(
                label="project",
                inbox=project_inbox,
                relative_inbox=f"project/{inbox_name}",
                scope_type="project",
                scope_id="project",
                markdown=markdown,
            )
        )
    applications = root / "applications"
    if applications.is_dir():
        for inbox in sorted(applications.rglob(inbox_name)):
            relative = inbox.relative_to(root).as_posix()
            label = inbox.parent.relative_to(applications).as_posix()
            entries.append(
                _scope_index_entry(
                    label=f"application:{label}",
                    inbox=inbox,
                    relative_inbox=relative,
                    scope_type="application",
                    scope_id=label,
                    markdown=markdown,
                )
            )
    return entries


def _scope_index_entry(
    *,
    label: str,
    inbox: Path,
    relative_inbox: str,
    scope_type: str,
    scope_id: str,
    markdown: bool,
) -> dict[str, Any]:
    rows = _read_inbox_rows(
        inbox,
        markdown=markdown,
        expected_scope=_normalize_scope(scope_type, scope_id),
    )
    details = _find_candidate_details(inbox.parent, rows)
    counts = {"pending_pre_review": 0, "active_unreviewed": 0}
    for candidate in details:
        state = str(candidate.get("state") or "pending_pre_review")
        if state in counts:
            counts[state] += 1
    return {
        "label": label,
        "inbox": relative_inbox,
        **counts,
    }


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


__all__ = [
    "ReviewArtifactConflictError",
    "ReviewArtifactPaths",
    "ReviewArtifactRenderer",
    "ReviewCLIService",
    "ReviewEngineProtocol",
    "ReviewOrchestratorProtocol",
]
