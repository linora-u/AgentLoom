"""Click CLI contract for scoped self-learning review."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.__main__ import main
from src.extensions.self_learning.review_artifacts import ReviewCLIService


def _commands(output: str) -> set[str]:
    return set(re.findall(r"^  ([a-z][a-z-]+)\s{2,}", output, flags=re.MULTILINE))


def test_main_cli_exposes_scoped_review_command_groups() -> None:
    runner = CliRunner()

    root_help = runner.invoke(main, ["--help"])
    learn_help = runner.invoke(main, ["learn", "--help"])
    reviews_help = runner.invoke(main, ["reviews", "--help"])
    feedback_help = runner.invoke(main, ["feedback", "--help"])

    assert root_help.exit_code == 0, root_help.output
    assert {"learn", "reviews", "feedback"}.issubset(_commands(root_help.output))
    assert _commands(learn_help.output) == {"review"}
    assert _commands(reviews_help.output) == {"apply", "rollback", "status"}
    assert _commands(feedback_help.output) == {"submit"}


def test_review_commands_require_exactly_one_explicit_scope() -> None:
    runner = CliRunner()

    missing = runner.invoke(main, ["learn", "review"])
    conflicting = runner.invoke(
        main,
        ["reviews", "apply", "--application", "checkout", "--project"],
    )

    assert missing.exit_code == 2
    assert "choose exactly one scope" in missing.output.lower()
    assert conflicting.exit_code == 2
    assert "choose exactly one scope" in conflicting.output.lower()


class _ReviewOrchestrator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    def run_review(self, scope_type: str, scope_id: str, *, dry_run: bool = False) -> dict:
        self.calls.append((scope_type, scope_id, dry_run))
        return {
            "review_id": "review-cli-001",
            "scope_type": scope_type,
            "scope_id": scope_id,
            "status": "completed",
            "dry_run": dry_run,
            "candidates": [],
        }

    def unreviewed_application_ids(self) -> list[str]:
        return ["checkout"]


class _PartitionedReviewOrchestrator(_ReviewOrchestrator):
    def unreviewed_application_ids(self) -> list[str]:
        return ["billing", "checkout", "billing"]

    def run_review(self, scope_type: str, scope_id: str, *, dry_run: bool = False) -> dict:
        self.calls.append((scope_type, scope_id, dry_run))
        return {
            "review_id": f"review-{scope_type}-{scope_id}",
            "scope_type": scope_type,
            "scope_id": scope_id,
            "status": "completed",
            "dry_run": dry_run,
            "candidates": [],
        }


class _ReviewEngine:
    pass


def test_learn_review_uses_orchestrator_and_renders_scoped_artifacts(tmp_path: Path) -> None:
    orchestrator = _ReviewOrchestrator()
    service = ReviewCLIService(
        engine=_ReviewEngine(),
        orchestrator=orchestrator,
        reviews_root=tmp_path / ".agentloom" / "reviews",
    )

    result = CliRunner().invoke(
        main,
        ["learn", "review", "--application", "checkout", "--dry-run"],
        obj={"review_service": service},
    )

    assert result.exit_code == 0, result.output
    assert orchestrator.calls == [("application", "checkout", True)]
    assert '"review_id": "review-cli-001"' in result.output
    assert (
        tmp_path / ".agentloom" / "reviews" / "applications" / "checkout" / "batches" / "review-cli-001" / "review.json"
    ).is_file()


def test_cli_artifact_failure_rolls_back_engine_batch_and_allows_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = _ApplyingEngine()
    orchestrator = _ReviewOrchestrator()
    service = ReviewCLIService(
        engine=engine,
        orchestrator=orchestrator,
        reviews_root=tmp_path / ".agentloom" / "reviews",
    )
    original_render = service.renderer.render_batch
    monkeypatch.setattr(
        service.renderer,
        "render_batch",
        lambda _batch: (_ for _ in ()).throw(OSError("CLI artifact disk failure")),
    )

    with pytest.raises(OSError, match="CLI artifact disk failure"):
        service.review_one("application", "checkout")

    assert engine.rollback_calls == ["review-cli-001"]

    monkeypatch.setattr(service.renderer, "render_batch", original_render)
    retried = service.review_one("application", "checkout")

    assert retried["review_id"] == "review-cli-001"
    assert len(orchestrator.calls) == 2


def test_default_cli_runtime_uses_the_selected_application_policy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    selected_config = {
        "self_learning": {
            "enabled": True,
            "review": {
                "enabled": True,
                "application": {
                    "review_model": "application-summary",
                    "trigger": {"mode": "manual", "min_completed_runs": 2},
                    "approval": {"fact": "manual", "experience": "auto"},
                },
                "project": {
                    "review_model": "summary",
                    "trigger": {"mode": "manual", "min_candidates": 5},
                    "approval": {"fact": "manual", "experience": "manual"},
                },
            },
        }
    }
    resolved: list[str] = []
    monkeypatch.setenv("AGENTLOOM_RUNTIME_ROOT", str(tmp_path / ".agentloom"))
    service = ReviewCLIService(
        reviews_root=tmp_path / ".agentloom" / "reviews",
        application_config_resolver=lambda application_id: resolved.append(application_id) or selected_config,
    )

    _engine, application_orchestrator = service._runtime_for_scope(
        "application",
        "checkout",
    )
    _project_engine, project_orchestrator = service._runtime_for_scope(
        "project",
        "project",
    )

    assert resolved == ["checkout"]
    assert application_orchestrator.agent_config is selected_config
    assert project_orchestrator.agent_config is None


def test_legacy_unscoped_memory_approval_commands_are_removed() -> None:
    result = CliRunner().invoke(main, ["memory", "--help"])

    assert result.exit_code == 0
    assert "approve" not in _commands(result.output)
    assert "reject" not in _commands(result.output)


class _ApplyingEngine:
    def __init__(self) -> None:
        self.applied: list[tuple[str, str, list[dict]]] = []
        self.feedback: list[tuple[str, str, int | None]] = []
        self.status_calls: list[tuple[str | None, str]] = []
        self.rollback_calls: list[str] = []

    def apply_decisions(self, scope_type: str, scope_id: str, decisions: list[dict]) -> dict:
        self.applied.append((scope_type, scope_id, decisions))
        return {"applied": len(decisions), "results": decisions}

    def submit_feedback(self, run_id: str, verdict: str, item_id: int | None = None) -> dict:
        self.feedback.append((run_id, verdict, item_id))
        return {"feedback_id": "feedback-1", "verdict": verdict}

    def status(self, scope_type: str | None = None, scope_id: str = "") -> dict:
        self.status_calls.append((scope_type, scope_id))
        return {"counts": {"pending_pre_review": 2}}

    def rollback(self, review_id: str) -> dict:
        self.rollback_calls.append(review_id)
        return {"review_id": review_id, "rolled_back": True, "mutation_count": 1}


class _AuthoritativeApplyingEngine(_ApplyingEngine):
    def status(self, scope_type: str | None = None, scope_id: str = "") -> dict:
        self.status_calls.append((scope_type, scope_id))
        return {
            "candidates": [
                {
                    "candidate_id": "candidate-authoritative",
                    "revision": 2,
                    "kind": "fact",
                    "memory_key": "fact:authoritative",
                    "payload": {"text": "The database owns pending state."},
                    "state": "pending_pre_review",
                    "outcome": "pending",
                }
            ],
            "counts": {"candidates": {"pending_pre_review": 1}},
        }


def test_reviews_apply_reads_only_scoped_inbox_and_removes_successful_rows(
    tmp_path: Path,
) -> None:
    engine = _ApplyingEngine()
    service = ReviewCLIService(
        engine=engine,
        orchestrator=_ReviewOrchestrator(),
        reviews_root=tmp_path / ".agentloom" / "reviews",
    )
    service.renderer.render_batch(
        {
            "review_id": "review-apply-001",
            "scope_type": "project",
            "scope_id": "project",
            "status": "completed",
            "candidates": [
                {
                    "candidate_id": "candidate-apply",
                    "revision": 4,
                    "kind": "fact",
                    "memory_key": "fact:apply",
                    "payload": {"text": "Apply this fact."},
                    "state": "pending_pre_review",
                    "outcome": "pending",
                }
            ],
        }
    )
    inbox = service.renderer.reviews_root / "project" / "INBOX.md"
    inbox.write_text(
        inbox.read_text(encoding="utf-8").replace(
            '"decision": "pending"',
            '"decision": "approve"',
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        main,
        ["reviews", "apply", "--project"],
        obj={"review_service": service},
    )

    assert result.exit_code == 0, result.output
    assert engine.applied == [
        (
            "project",
            "project",
            [
                {
                    "candidate_id": "candidate-apply",
                    "revision": 4,
                    "action": "approve",
                }
            ],
        )
    ]
    assert not inbox.exists()


def test_feedback_submit_passes_a_typed_item_id_to_the_engine(tmp_path: Path) -> None:
    engine = _ApplyingEngine()
    service = ReviewCLIService(
        engine=engine,
        orchestrator=_ReviewOrchestrator(),
        reviews_root=tmp_path / ".agentloom" / "reviews",
    )

    result = CliRunner().invoke(
        main,
        ["feedback", "submit", "run-123", "--verdict", "corrected", "--item", "42"],
        obj={"review_service": service},
    )

    assert result.exit_code == 0, result.output
    assert engine.feedback == [("run-123", "corrected", 42)]
    assert '"feedback_id": "feedback-1"' in result.output


def test_missing_scoped_inbox_is_reported_as_a_cli_error(tmp_path: Path) -> None:
    service = ReviewCLIService(
        engine=_ApplyingEngine(),
        orchestrator=_ReviewOrchestrator(),
        reviews_root=tmp_path / ".agentloom" / "reviews",
    )

    result = CliRunner().invoke(
        main,
        ["reviews", "apply", "--project"],
        obj={"review_service": service},
    )

    assert result.exit_code == 1
    assert "inbox" in result.output.lower()


def test_reviews_status_all_uses_the_read_only_global_engine_query(tmp_path: Path) -> None:
    engine = _ApplyingEngine()
    service = ReviewCLIService(
        engine=engine,
        orchestrator=_ReviewOrchestrator(),
        reviews_root=tmp_path / ".agentloom" / "reviews",
    )

    result = CliRunner().invoke(
        main,
        ["reviews", "status", "--all"],
        obj={"review_service": service},
    )

    assert result.exit_code == 0, result.output
    assert engine.status_calls == [(None, "")]
    assert '"pending_pre_review": 2' in result.output


def test_reviews_rollback_passes_only_the_immutable_batch_id(tmp_path: Path) -> None:
    engine = _ApplyingEngine()
    service = ReviewCLIService(
        engine=engine,
        orchestrator=_ReviewOrchestrator(),
        reviews_root=tmp_path / ".agentloom" / "reviews",
    )
    artifacts = service.renderer.render_batch(
        {
            "review_id": "review-123",
            "scope_type": "project",
            "scope_id": "project",
            "status": "completed",
            "candidates": [
                {
                    "candidate_id": "candidate-rollback",
                    "revision": 1,
                    "kind": "fact",
                    "memory_key": "fact:rollback",
                    "payload": {"text": "Rollback this item."},
                    "state": "active_unreviewed",
                    "outcome": "activated",
                }
            ],
        }
    )

    result = CliRunner().invoke(
        main,
        ["reviews", "rollback", "review-123"],
        obj={"review_service": service},
    )

    assert result.exit_code == 0, result.output
    assert engine.rollback_calls == ["review-123"]
    assert '"rolled_back": true' in result.output
    assert artifacts.review_json.is_file()
    assert not artifacts.inbox.exists()


def test_all_unreviewed_partitions_applications_before_project(tmp_path: Path) -> None:
    orchestrator = _PartitionedReviewOrchestrator()
    service = ReviewCLIService(
        engine=_ApplyingEngine(),
        orchestrator=orchestrator,
        reviews_root=tmp_path / ".agentloom" / "reviews",
    )

    result = CliRunner().invoke(
        main,
        ["learn", "review", "--all-unreviewed", "--dry-run"],
        obj={"review_service": service},
    )

    assert result.exit_code == 0, result.output
    assert orchestrator.calls == [
        ("application", "billing", True),
        ("application", "checkout", True),
        ("project", "project", True),
    ]


def test_apply_reconciles_the_inbox_from_authoritative_engine_status(tmp_path: Path) -> None:
    engine = _AuthoritativeApplyingEngine()
    service = ReviewCLIService(
        engine=engine,
        orchestrator=_ReviewOrchestrator(),
        reviews_root=tmp_path / ".agentloom" / "reviews",
        markdown=False,
    )
    rendered = service.renderer.render_batch(
        {
            "review_id": "review-authoritative",
            "scope_type": "project",
            "scope_id": "project",
            "status": "completed",
            "candidates": engine.status("project", "project")["candidates"],
        }
    )
    document = json.loads(rendered.inbox.read_text(encoding="utf-8"))
    document["decisions"] = []
    rendered.inbox.write_text(json.dumps(document), encoding="utf-8")
    engine.status_calls.clear()

    result = CliRunner().invoke(
        main,
        ["reviews", "apply", "--project"],
        obj={"review_service": service},
    )

    assert result.exit_code == 0, result.output
    assert engine.status_calls == [("project", "project")]
    restored = json.loads(rendered.inbox.read_text(encoding="utf-8"))
    assert restored["decisions"] == [
        {
            "candidate_id": "candidate-authoritative",
            "revision": 2,
            "decision": "pending",
        }
    ]
