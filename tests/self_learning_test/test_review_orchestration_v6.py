from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.extensions.self_learning.review_types import (
    CandidateResult,
    ReviewBatchResult,
)


def _config() -> dict:
    return {
        "self_learning": {
            "review": {
                "enabled": True,
                "application": {
                    "review_model": "summary",
                    "trigger": {"mode": "after_run", "min_completed_runs": 5},
                    "approval": {"fact": "auto", "experience": "manual"},
                },
                "project": {
                    "review_model": "summary",
                    "trigger": {"mode": "batch", "min_candidates": 5},
                    "approval": {"fact": "manual", "experience": "manual"},
                },
            }
        }
    }


class _Engine:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls: list[dict] = []

    def review(
        self,
        scope_type: str,
        scope_id: str,
        candidates,
        *,
        dry_run: bool = False,
        source_runs=(),
    ) -> ReviewBatchResult:
        candidates = tuple(candidates)
        self.calls.append(
            {
                "scope_type": scope_type,
                "scope_id": scope_id,
                "candidates": candidates,
                "dry_run": dry_run,
                "source_runs": tuple(source_runs),
            }
        )
        candidate = candidates[0]
        return ReviewBatchResult(
            review_id="review-1",
            scope_type=scope_type,
            scope_id=scope_id,
            status="dry_run" if dry_run else "completed",
            dry_run=dry_run,
            candidates=(
                CandidateResult(
                    candidate_id="candidate-1",
                    revision=1,
                    kind=candidate.kind,
                    memory_key=candidate.memory_key,
                    payload=candidate.payload,
                    state="pending_pre_review",
                    outcome="pending",
                    provenance=candidate.provenance,
                ),
            ),
        )


class _StructuredModel:
    model_id = "fake/summary"

    def __init__(self) -> None:
        self.calls = []

    def generate(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        assert not kwargs.get("tools_to_call_from")
        return SimpleNamespace(
            content=json.dumps(
                {
                    "candidates": [
                        {
                            "scope": "project",
                            "kind": "fact",
                            "memory_key": "api:page-size",
                            "payload": {"text": "The page size is 100 rows."},
                            "approval": "manual",
                            "action": "remove",
                            "provenance": [
                                {
                                    "root_run_id": "root-1",
                                    "event_id": "event-1",
                                    "tool_call_id": "call-1",
                                }
                            ],
                        }
                    ]
                }
            )
        )


def test_review_model_only_returns_candidates_and_cannot_choose_scope_policy_or_mutation(
    tmp_path: Path,
) -> None:
    from src.extensions.self_learning.review_orchestration import ReviewOrchestrator

    engine = _Engine(tmp_path / "self_learning.db")
    model = _StructuredModel()
    orchestrator = ReviewOrchestrator(
        engine=engine,
        agent_config=_config(),
        model_resolver=lambda _name: model,
        render_artifacts=False,
    )
    orchestrator.collect = lambda _scope, _scope_id: {
        "source_runs": [{"root_run_id": "root-1", "application_id": "app-a"}],
        "allowed_provenance": [
            {
                "root_run_id": "root-1",
                "event_id": "event-1",
                "tool_call_id": "call-1",
            }
        ],
        "context": [{"kind": "trusted_evidence", "text": "The page size is 100 rows."}],
    }

    result = orchestrator.run_review("application", "app-a")

    assert result.review_id == "review-1"
    assert len(model.calls) == 1
    call = engine.calls[0]
    assert (call["scope_type"], call["scope_id"]) == ("application", "app-a")
    assert call["source_runs"] == (("root-1", "app-a"),)
    candidate = call["candidates"][0]
    assert candidate.approval == "auto"
    assert candidate.action == "add"
    assert candidate.provenance == (
        {
            "event_id": "event-1",
            "root_run_id": "root-1",
            "tool_call_id": "call-1",
        },
    )


def test_large_context_uses_valid_json_and_binds_only_complete_prompted_runs(
    tmp_path: Path,
) -> None:
    from src.extensions.self_learning.review_orchestration import ReviewOrchestrator

    engine = _Engine(tmp_path / "self_learning.db")
    prompted: list[dict] = []

    class _PromptAwareModel:
        def generate(self, messages, **_kwargs):
            payload = json.loads(messages[-1].content)
            prompted.append(payload)
            provenance = payload["allowed_provenance"][0]
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "candidates": [
                            {
                                "kind": "fact",
                                "memory_key": "prompt:bounded",
                                "payload": {"text": "Only prompted runs are consumed."},
                                "provenance": [provenance],
                            }
                        ]
                    }
                )
            )

    orchestrator = ReviewOrchestrator(
        engine=engine,
        agent_config=_config(),
        model_resolver=lambda _name: _PromptAwareModel(),
        render_artifacts=False,
    )
    orchestrator.collect = lambda _scope, _scope_id: {
        "source_runs": [
            {"root_run_id": "root-shared", "application_id": "app-a"},
            {"root_run_id": "root-shared", "application_id": "app-b"},
        ],
        "allowed_provenance": [
            {
                "root_run_id": "root-shared",
                "application_id": "app-a",
                "event_id": "event-1",
            },
            {
                "root_run_id": "root-shared",
                "application_id": "app-b",
                "event_id": "event-2",
            },
        ],
        "context": [
            {
                "provenance": {
                    "root_run_id": "root-shared",
                    "application_id": "app-a",
                    "event_id": "event-1",
                },
                "observed_result": "a" * 28_000,
            },
            {
                "provenance": {
                    "root_run_id": "root-shared",
                    "application_id": "app-b",
                    "event_id": "event-2",
                },
                "observed_result": "b" * 28_000,
            },
        ],
    }

    orchestrator.run_review("project", "project")

    assert len(prompted) == 1
    assert prompted[0]["source_runs"] == [{"application_id": "app-a", "root_run_id": "root-shared"}]
    assert prompted[0]["context"][0]["observed_result"] == "a" * 28_000
    assert len(prompted[0]["context"]) == 1
    assert engine.calls[0]["source_runs"] == (("root-shared", "app-a"),)
    assert engine.calls[0]["candidates"][0].source_run_ids == ("root-shared",)


def test_single_oversized_run_is_retryable_without_model_or_engine_call(
    tmp_path: Path,
) -> None:
    from src.extensions.self_learning.review_orchestration import ReviewOrchestrator

    engine = _Engine(tmp_path / "self_learning.db")
    model = _StructuredModel()
    orchestrator = ReviewOrchestrator(
        engine=engine,
        agent_config=_config(),
        model_resolver=lambda _name: model,
        render_artifacts=False,
    )
    provenance = {
        "root_run_id": "root-too-large",
        "application_id": "app-a",
        "event_id": "event-too-large",
    }
    orchestrator.collect = lambda _scope, _scope_id: {
        "source_runs": [{"root_run_id": "root-too-large", "application_id": "app-a"}],
        "allowed_provenance": [provenance],
        "context": [{"provenance": provenance, "observed_result": "x" * 60_000}],
    }

    with pytest.raises(ValueError, match="complete review context unit exceeds"):
        orchestrator.run_review("application", "app-a")

    assert model.calls == []
    assert engine.calls == []


def test_ambiguous_same_root_entry_is_neither_prompted_nor_consumed(
    tmp_path: Path,
) -> None:
    from src.extensions.self_learning.review_orchestration import ReviewOrchestrator

    engine = _Engine(tmp_path / "self_learning.db")
    prompted: list[dict] = []

    class _CaptureModel:
        def generate(self, messages, **_kwargs):
            prompted.append(json.loads(messages[-1].content))
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "candidates": [
                            {
                                "kind": "fact",
                                "memory_key": "prompt:bound-only",
                                "payload": {"text": "Only bound evidence is eligible."},
                                "provenance": [prompted[-1]["allowed_provenance"][0]],
                            }
                        ]
                    }
                )
            )

    orchestrator = ReviewOrchestrator(
        engine=engine,
        agent_config=_config(),
        model_resolver=lambda _name: _CaptureModel(),
        render_artifacts=False,
    )
    orchestrator.collect = lambda _scope, _scope_id: {
        "source_runs": [
            {"root_run_id": "root-shared", "application_id": "app-a"},
            {"root_run_id": "root-shared", "application_id": "app-b"},
        ],
        "allowed_provenance": [
            {
                "root_run_id": "root-shared",
                "application_id": "app-a",
                "event_id": "event-a",
            },
            {"root_run_id": "root-shared", "event_id": "ambiguous-event"},
        ],
        "context": [
            {
                "provenance": {
                    "root_run_id": "root-shared",
                    "application_id": "app-a",
                    "event_id": "event-a",
                },
                "observed_result": "bound-to-app-a",
            },
            {
                "provenance": {"root_run_id": "root-shared"},
                "observed_result": "must-not-reach-the-model",
            },
        ],
    }

    orchestrator.run_review("project", "project")

    assert prompted[0]["source_runs"] == [{"application_id": "app-a", "root_run_id": "root-shared"}]
    assert prompted[0]["allowed_provenance"] == [
        {
            "application_id": "app-a",
            "event_id": "event-a",
            "root_run_id": "root-shared",
        }
    ]
    assert prompted[0]["context"][0]["observed_result"] == "bound-to-app-a"
    assert len(prompted[0]["context"]) == 1
    assert engine.calls[0]["source_runs"] == (("root-shared", "app-a"),)


def test_only_ambiguous_same_root_entries_fail_before_model_or_consumption(
    tmp_path: Path,
) -> None:
    from src.extensions.self_learning.review_orchestration import ReviewOrchestrator

    engine = _Engine(tmp_path / "self_learning.db")
    model = _StructuredModel()
    orchestrator = ReviewOrchestrator(
        engine=engine,
        agent_config=_config(),
        model_resolver=lambda _name: model,
        render_artifacts=False,
    )
    orchestrator.collect = lambda _scope, _scope_id: {
        "source_runs": [
            {"root_run_id": "root-shared", "application_id": "app-a"},
            {"root_run_id": "root-shared", "application_id": "app-b"},
        ],
        "allowed_provenance": [{"root_run_id": "root-shared", "event_id": "ambiguous-event"}],
        "context": [
            {
                "provenance": {"root_run_id": "root-shared"},
                "observed_result": "ambiguous",
            }
        ],
    }

    with pytest.raises(ValueError, match="no safely bound input units"):
        orchestrator.run_review("project", "project")

    assert model.calls == []
    assert engine.calls == []


def test_project_collection_never_contains_raw_application_transcripts(
    tmp_path: Path,
) -> None:
    from src.extensions.self_learning.review_orchestration import ReviewOrchestrator

    engine = _Engine(tmp_path / "self_learning.db")
    orchestrator = ReviewOrchestrator(
        engine=engine,
        agent_config=_config(),
        model_resolver=lambda _name: _StructuredModel(),
        render_artifacts=False,
    )

    collected = orchestrator.collect("project", "project")

    assert "raw_transcripts" not in collected
    assert "tool_results" not in collected


def test_review_trigger_modes_are_synchronous_and_threshold_driven() -> None:
    from src.extensions.self_learning.review_orchestration import ReviewOrchestrator

    application_context = {
        "source_runs": [{"root_run_id": f"root-{index}", "application_id": "app-a"} for index in range(5)],
        "context": [],
    }
    project_context = {
        "source_runs": [],
        "context": [{"kind": "candidate"} for _ in range(5)],
    }

    assert not ReviewOrchestrator.review_due(
        {"trigger": {"mode": "manual"}},
        application_context,
        scope_type="application",
        successful_root_finished=True,
    )
    assert ReviewOrchestrator.review_due(
        {"trigger": {"mode": "after_run"}},
        application_context,
        scope_type="application",
        successful_root_finished=True,
    )
    assert not ReviewOrchestrator.review_due(
        {"trigger": {"mode": "after_run"}},
        {"source_runs": [], "context": []},
        scope_type="application",
        successful_root_finished=True,
    )
    assert ReviewOrchestrator.review_due(
        {"trigger": {"mode": "batch", "min_completed_runs": 5}},
        application_context,
        scope_type="application",
        successful_root_finished=True,
    )
    assert ReviewOrchestrator.review_due(
        {"trigger": {"mode": "batch", "min_candidates": 5}},
        project_context,
        scope_type="project",
        successful_root_finished=True,
    )


def test_project_collection_does_not_repropose_an_existing_project_candidate(
    tmp_path: Path,
) -> None:
    from src.extensions.self_learning.persistence.memory_store import MemoryStore
    from src.extensions.self_learning.persistence.review_engine import ReviewEngine
    from src.extensions.self_learning.review_orchestration import ReviewOrchestrator

    db_path = tmp_path / "self_learning.db"
    store = MemoryStore(db_path, agent_config=_config())
    for application_id in ("app-a", "app-b"):
        store.add_typed(
            "application",
            kind="fact",
            memory_key="shared:page-size",
            payload={"text": "The page size is 100 rows."},
            scope_id=application_id,
        )
    engine = ReviewEngine(db_path)
    orchestrator = ReviewOrchestrator(
        engine=engine,
        agent_config=_config(),
        render_artifacts=False,
    )
    assert len(orchestrator.collect("project", "project")["context"]) == 1
    engine.review(
        "project",
        "project",
        [
            {
                "kind": "fact",
                "memory_key": "shared:page-size",
                "payload": {"text": "The page size is 100 rows."},
                "approval": "manual",
            }
        ],
    )

    assert orchestrator.collect("project", "project")["context"] == []


def test_project_collection_consumes_direct_project_evidence_once(
    tmp_path: Path,
) -> None:
    from src.extensions.self_learning.event_schema import CanonicalSessionEvent
    from src.extensions.self_learning.persistence.ledger import SelfLearningLedger
    from src.extensions.self_learning.persistence.review_engine import ReviewEngine
    from src.extensions.self_learning.review_orchestration import ReviewOrchestrator
    from src.lib.trusted_memory_evidence import TRUSTED_MEMORY_EVIDENCE_KIND

    db_path = tmp_path / "self_learning.db"
    ledger = SelfLearningLedger(db_path)
    ledger.append_runtime_event(
        CanonicalSessionEvent(
            event_id="project-evidence-event",
            run_id="root-project-evidence",
            root_run_id="root-project-evidence",
            application_id="app-a",
            event_type="tool_result",
            tool_name="contract_reader",
            status="completed",
            output_data={"result": "The project page size is 100 rows."},
        ),
        trusted_evidence=(
            {
                "kind": TRUSTED_MEMORY_EVIDENCE_KIND,
                "scope": "project",
                "source": "contract_reader",
                "text": "The project page size is 100 rows.",
            },
        ),
    )
    ledger.append_runtime_event(
        CanonicalSessionEvent(
            event_id="project-root-complete",
            run_id="root-project-evidence",
            root_run_id="root-project-evidence",
            application_id="app-a",
            event_type="run_completed",
            status="completed",
            output_data={"result": "complete"},
        )
    )
    engine = ReviewEngine(db_path)
    orchestrator = ReviewOrchestrator(
        engine=engine,
        agent_config=_config(),
        render_artifacts=False,
    )
    collected = orchestrator.collect("project", "project")
    assert len(collected["context"]) == 1
    reviewed = engine.review(
        "project",
        "project",
        [],
        source_runs=[("root-project-evidence", "app-a")],
    )

    assert orchestrator.collect("project", "project")["context"] == []
    engine.rollback(reviewed.review_id)
    assert len(orchestrator.collect("project", "project")["context"]) == 1


def test_artifact_failure_rolls_back_activation_and_keeps_source_run_retryable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import sqlite3

    import pytest

    from src.extensions.self_learning.event_schema import CanonicalSessionEvent
    from src.extensions.self_learning.persistence.evidence_gate import SQLiteEvidenceGate
    from src.extensions.self_learning.persistence.ledger import SelfLearningLedger
    from src.extensions.self_learning.persistence.review_engine import ReviewEngine
    from src.extensions.self_learning.review_artifacts import ReviewArtifactRenderer
    from src.extensions.self_learning.review_orchestration import ReviewOrchestrator
    from src.lib.trusted_memory_evidence import TRUSTED_MEMORY_EVIDENCE_KIND

    db_path = tmp_path / "self_learning.db"
    ledger = SelfLearningLedger(db_path)
    fact_text = "The Application export limit is 100 rows."
    ledger.append_runtime_event(
        CanonicalSessionEvent(
            event_id="artifact-failure-fact",
            run_id="root-artifact-failure",
            root_run_id="root-artifact-failure",
            application_id="app-a",
            event_type="tool_result",
            tool_name="contract_reader",
            status="completed",
            output_data={"result": fact_text},
            metadata={"tool_call_id": "call-artifact-failure"},
        ),
        trusted_evidence=(
            {
                "kind": TRUSTED_MEMORY_EVIDENCE_KIND,
                "scope": "application",
                "source": "contract_reader",
                "text": fact_text,
            },
        ),
    )
    ledger.append_runtime_event(
        CanonicalSessionEvent(
            event_id="artifact-failure-completed",
            run_id="root-artifact-failure",
            root_run_id="root-artifact-failure",
            application_id="app-a",
            event_type="run_completed",
            status="completed",
            output_data={"result": "complete"},
        )
    )

    model = _StructuredModel()
    model.generate = lambda _messages, **_kwargs: SimpleNamespace(
        content=json.dumps(
            {
                "candidates": [
                    {
                        "kind": "fact",
                        "memory_key": "export:limit",
                        "payload": {"text": fact_text},
                        "provenance": [
                            {
                                "root_run_id": "root-artifact-failure",
                                "application_id": "app-a",
                                "event_id": "artifact-failure-fact",
                                "tool_call_id": "call-artifact-failure",
                            }
                        ],
                    }
                ]
            }
        )
    )
    engine = ReviewEngine(db_path, evidence_gate=SQLiteEvidenceGate(db_path))
    orchestrator = ReviewOrchestrator(
        engine=engine,
        agent_config=_config(),
        model_resolver=lambda _name: model,
    )

    monkeypatch.setattr(
        ReviewArtifactRenderer,
        "_render_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("review artifact disk failure")),
    )

    with pytest.raises(OSError, match="review artifact disk failure"):
        orchestrator.run_review("application", "app-a")

    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM memory_items WHERE state IN ('active_confirmed','active_unreviewed')"
            ).fetchone()[0]
            == 0
        )
        failed_review_id, status = conn.execute(
            "SELECT review_id,status FROM review_batches ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert status == "rolled_back"
        mutations = conn.execute(
            "SELECT rolled_back_at FROM review_mutations WHERE review_id=?",
            (failed_review_id,),
        ).fetchall()
        assert mutations and all(row[0] for row in mutations)

    scope_root = tmp_path / "reviews" / "applications" / "app-a"
    assert (scope_root / "batches" / failed_review_id / "review.json").is_file()
    assert not (scope_root / "INBOX.md").exists()

    assert orchestrator.unreviewed_application_ids() == ["app-a"]

    retry = ReviewOrchestrator(
        engine=engine,
        agent_config=_config(),
        model_resolver=lambda _name: model,
        render_artifacts=False,
    ).run_review("application", "app-a")

    assert retry.candidates[0].outcome == "activated"
    assert orchestrator.unreviewed_application_ids() == []
