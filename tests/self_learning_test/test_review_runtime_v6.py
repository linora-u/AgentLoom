from __future__ import annotations

from itertools import product
from pathlib import Path
from types import SimpleNamespace

import pytest


def _record_completed_root(db_path: Path, run_id: str, application_id: str) -> None:
    from src.extensions.self_learning.event_schema import CanonicalSessionEvent
    from src.extensions.self_learning.persistence.ledger import SelfLearningLedger

    SelfLearningLedger(db_path).append_runtime_event(
        CanonicalSessionEvent(
            event_id=f"completed-{run_id}",
            run_id=run_id,
            root_run_id=run_id,
            application_id=application_id,
            event_type="run_completed",
            status="completed",
            output_data={"result": "completed"},
        )
    )


def _config(*, mode: str, min_completed_runs: int = 5) -> dict:
    return {
        "self_learning": {
            "enabled": True,
            "review": {
                "enabled": True,
                "application": {
                    "review_model": "summary",
                    "trigger": {
                        "mode": mode,
                        "min_completed_runs": min_completed_runs,
                    },
                    "approval": {"fact": "auto", "experience": "manual"},
                },
                "project": {
                    "review_model": "summary",
                    "trigger": {"mode": "manual", "min_candidates": 5},
                    "approval": {"fact": "manual", "experience": "manual"},
                },
                "artifacts": {"markdown": False, "review_auto_applied": True},
            },
        }
    }


class _EmptyCandidateModel:
    model_id = "fake/summary"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, _messages, **kwargs):
        self.calls += 1
        assert kwargs == {}
        return SimpleNamespace(content='{"candidates":[]}')


@pytest.mark.parametrize("mode", ["manual", "batch", "after_run"])
@pytest.mark.parametrize(
    ("application_fact", "application_experience", "project_fact", "project_experience"),
    list(product(("auto", "manual"), repeat=4)),
)
def test_all_trigger_and_approval_combinations_are_noninteractive(
    tmp_path: Path,
    monkeypatch,
    mode: str,
    application_fact: str,
    application_experience: str,
    project_fact: str,
    project_experience: str,
) -> None:
    from src.extensions.self_learning import reviewer
    from src.extensions.self_learning.review_orchestration import ReviewOrchestrator

    db_path = tmp_path / "self_learning.db"
    _record_completed_root(db_path, "root-matrix", "app-a")
    config = _config(mode=mode, min_completed_runs=1)
    review = config["self_learning"]["review"]
    review["application"]["approval"] = {
        "fact": application_fact,
        "experience": application_experience,
    }
    review["project"]["trigger"] = {
        "mode": mode,
        "min_candidates": 1,
    }
    review["project"]["approval"] = {
        "fact": project_fact,
        "experience": project_experience,
    }
    model = _EmptyCandidateModel()
    monkeypatch.setattr(reviewer, "_resolve_review_model", lambda _name: model)
    monkeypatch.setattr(
        "builtins.input",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("review attempted interactive input")),
    )
    original_collect = ReviewOrchestrator.collect

    def _collect(self, scope_type: str, scope_id: str):
        if scope_type == "project":
            return {
                "source_runs": [],
                "allowed_provenance": [],
                "context": [{"kind": "code-shaped-project-candidate"}],
            }
        return original_collect(self, scope_type, scope_id)

    monkeypatch.setattr(ReviewOrchestrator, "collect", _collect)

    result = reviewer.review_finished_run(
        root_run_id="root-matrix",
        agent_config=config,
        db_path=db_path,
    )

    if mode == "manual":
        assert result["status"] == "skipped"
        assert model.calls == 0
    else:
        assert result["status"] == "completed"
        assert result["calls"] == 2
        assert model.calls == 2


def test_manual_trigger_never_calls_model_or_reads_user_input(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.extensions.self_learning import reviewer

    db_path = tmp_path / "self_learning.db"
    _record_completed_root(db_path, "root-manual", "app-a")
    monkeypatch.setattr(
        reviewer,
        "_resolve_review_model",
        lambda _name: (_ for _ in ()).throw(AssertionError("model called")),
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("input called")),
    )

    result = reviewer.review_finished_run(
        root_run_id="root-manual",
        agent_config=_config(mode="manual"),
        db_path=db_path,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "trigger_not_due"


def test_after_run_review_uses_structured_model_without_tools_and_consumes_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.extensions.self_learning import reviewer
    from src.extensions.self_learning.persistence.review_engine import ReviewEngine
    from src.extensions.self_learning.review_orchestration import ReviewOrchestrator

    db_path = tmp_path / "self_learning.db"
    _record_completed_root(db_path, "root-after", "app-a")
    model = _EmptyCandidateModel()
    monkeypatch.setattr(reviewer, "_resolve_review_model", lambda _name: model)

    result = reviewer.review_finished_run(
        root_run_id="root-after",
        agent_config=_config(mode="after_run"),
        db_path=db_path,
    )

    assert result["status"] == "completed"
    assert result["calls"] == 1
    assert model.calls == 1
    assert (
        ReviewOrchestrator(
            engine=ReviewEngine(db_path),
            agent_config=_config(mode="after_run"),
            render_artifacts=False,
        ).unreviewed_application_ids()
        == []
    )


def test_model_failure_does_not_consume_unreviewed_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.extensions.self_learning import reviewer
    from src.extensions.self_learning.persistence.review_engine import ReviewEngine
    from src.extensions.self_learning.review_orchestration import ReviewOrchestrator

    db_path = tmp_path / "self_learning.db"
    _record_completed_root(db_path, "root-timeout", "app-a")

    class _FailingModel:
        model_id = "fake/timeout"

        def generate(self, _messages):
            raise TimeoutError("provider timeout")

    monkeypatch.setattr(
        reviewer,
        "_resolve_review_model",
        lambda _name: _FailingModel(),
    )
    config = _config(mode="after_run")

    result = reviewer.review_finished_run(
        root_run_id="root-timeout",
        agent_config=config,
        db_path=db_path,
    )

    assert result["status"] == "failed"
    assert result["reason"] == "TimeoutError"
    assert ReviewOrchestrator(
        engine=ReviewEngine(db_path),
        agent_config=config,
        render_artifacts=False,
    ).unreviewed_application_ids() == ["app-a"]


def test_batch_trigger_waits_for_completed_run_threshold(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.extensions.self_learning import reviewer

    db_path = tmp_path / "self_learning.db"
    _record_completed_root(db_path, "root-one", "app-a")
    monkeypatch.setattr(
        reviewer,
        "_resolve_review_model",
        lambda _name: (_ for _ in ()).throw(AssertionError("model called")),
    )

    result = reviewer.review_finished_run(
        root_run_id="root-one",
        agent_config=_config(mode="batch", min_completed_runs=2),
        db_path=db_path,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "trigger_not_due"
