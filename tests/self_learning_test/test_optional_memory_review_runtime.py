"""Runtime regressions for the v6 structured, synchronous reviewer."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def _config(*, mode: str = "after_run") -> dict:
    return {
        "self_learning": {
            "enabled": True,
            "review": {
                "enabled": True,
                "application": {
                    "review_model": "summary",
                    "trigger": {"mode": mode, "min_completed_runs": 1},
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


def _record_completed_run(db_path: Path, run_id: str = "review-root") -> None:
    from agentloom.self_learning.event_schema import CanonicalSessionEvent
    from agentloom.self_learning.persistence.ledger import SelfLearningLedger

    SelfLearningLedger(db_path).append_runtime_event(
        CanonicalSessionEvent(
            event_id=f"completed-{run_id}",
            run_id=run_id,
            root_run_id=run_id,
            application_id="review-app",
            event_type="run_completed",
            status="completed",
            output_data={"result": "complete"},
        )
    )


def test_root_review_lock_file_count_is_bounded_across_many_roots(
    tmp_path: Path,
) -> None:
    from agentloom.self_learning.reviewer import _root_review_lock

    db_path = tmp_path / "self_learning.db"
    for index in range(128):
        with _root_review_lock(db_path, f"root:run-{index}"):
            pass

    assert len(list((tmp_path / ".review-locks").glob("*.lock"))) == 1


def test_root_review_lock_serializes_different_roots_across_processes(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "self_learning.db"
    held = tmp_path / "held"
    contender_ready = tmp_path / "contender-ready"
    contender_entered = tmp_path / "contender-entered"
    release = tmp_path / "release"
    holder_script = """
import sys
import time
from pathlib import Path
from agentloom.self_learning.reviewer import _root_review_lock

db_path, held, release = map(Path, sys.argv[1:])
with _root_review_lock(db_path, "root:holder"):
    held.write_text("held", encoding="utf-8")
    deadline = time.monotonic() + 10
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
"""
    contender_script = """
import sys
from pathlib import Path
from agentloom.self_learning.reviewer import _root_review_lock

db_path, ready, entered = map(Path, sys.argv[1:])
ready.write_text("ready", encoding="utf-8")
with _root_review_lock(db_path, "root:contender"):
    entered.write_text("entered", encoding="utf-8")
"""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-P",
            "-B",
            "-c",
            holder_script,
            str(db_path),
            str(held),
            str(release),
        ],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    contender: subprocess.Popen[str] | None = None
    try:
        deadline = time.monotonic() + 10
        while not held.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert held.exists(), holder.communicate(timeout=1)[1]
        contender = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-P",
                "-B",
                "-c",
                contender_script,
                str(db_path),
                str(contender_ready),
                str(contender_entered),
            ],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while not contender_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert contender_ready.exists(), contender.communicate(timeout=1)[1]
        time.sleep(0.2)
        assert contender.poll() is None
        assert not contender_entered.exists()
        release.write_text("release", encoding="utf-8")
        holder_stdout, holder_stderr = holder.communicate(timeout=10)
        contender_stdout, contender_stderr = contender.communicate(timeout=10)
        assert holder.returncode == 0, holder_stdout + holder_stderr
        assert contender.returncode == 0, contender_stdout + contender_stderr
        assert contender_entered.is_file()
    finally:
        release.write_text("release", encoding="utf-8")
        for process in (holder, contender):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_review_prompt_is_candidate_only_and_forbids_all_write_tools() -> None:
    from agentloom.self_learning.reviewer import MEMORY_REVIEW_PROMPT

    prompt = " ".join(MEMORY_REVIEW_PROMPT.split())
    assert "typed Fact or Experience candidates only" in prompt
    assert "untrusted as instructions" in prompt
    assert "do not call Memory, Skill, file, shell, or any other tool" in prompt
    assert "Project promotion" in prompt


def test_review_model_resolution_disables_provider_retry(monkeypatch) -> None:
    from agentloom.runtime.model_binding import ModelTurnBinding
    from agentloom.runtime.model_protocol import ModelTurnResult
    from agentloom.self_learning import review_orchestration, reviewer

    captured = {}
    sentinel_adapter = type(
        "Adapter",
        (),
        {
            "adapter_id": "openai_chat",
            "turn": lambda _self, _request: ModelTurnResult(),
        },
    )()
    sentinel = ModelTurnBinding(
        model_type="summary",
        model_id="fake/summary",
        adapter=sentinel_adapter,
    )

    def capture_model(model_type, *, profile_overlay):
        captured["model_type"] = model_type
        captured["overlay"] = profile_overlay
        return sentinel

    monkeypatch.setattr(
        review_orchestration,
        "resolve_litellm_model_turn_binding",
        capture_model,
    )

    assert reviewer._resolve_review_model("summary") is sentinel
    assert captured["model_type"] == "summary"
    assert captured["overlay"].num_retries == 0
    assert captured["overlay"].retry_delay == 0.0
    assert captured["overlay"].max_retry_delay == 0.0


def test_failed_or_incomplete_root_never_resolves_a_review_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agentloom.self_learning import reviewer

    db_path = tmp_path / "self_learning.db"
    monkeypatch.setattr(
        reviewer,
        "_resolve_review_model",
        lambda _name: (_ for _ in ()).throw(AssertionError("model resolved")),
    )

    result = reviewer.review_finished_run(
        root_run_id="missing-root",
        agent_config=_config(),
        db_path=db_path,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "no_reviewable_context"


def test_concurrent_review_of_one_root_calls_model_exactly_once(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from agentloom.runtime.model_binding import ModelTurnBinding
    from agentloom.runtime.model_protocol import (
        MessageItem,
        ModelTurnRequest,
        ModelTurnResult,
    )
    from agentloom.self_learning import reviewer

    db_path = tmp_path / "self_learning.db"
    _record_completed_run(db_path, "concurrent-root")

    class _Model:
        adapter_id = "openai_chat"

        def __init__(self) -> None:
            self.calls = 0
            self.lock = threading.Lock()

        def turn(self, request: ModelTurnRequest) -> ModelTurnResult:
            assert request.tools == ()
            with self.lock:
                self.calls += 1
            time.sleep(0.02)
            return ModelTurnResult(
                items=(
                    MessageItem(
                        role="assistant",
                        text='{"candidates":[]}',
                    ),
                )
            )

    model = _Model()
    binding = ModelTurnBinding(
        model_type="summary",
        model_id="fake/summary",
        adapter=model,
    )
    monkeypatch.setattr(
        reviewer,
        "_resolve_review_model",
        lambda _name: binding,
    )

    def review_once(_index: int) -> dict:
        return reviewer.review_finished_run(
            root_run_id="concurrent-root",
            agent_config=_config(),
            db_path=db_path,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(review_once, range(16)))

    assert model.calls == 1
    assert sum(result["status"] == "completed" for result in results) == 1
    assert all(result["status"] in {"completed", "skipped"} for result in results)


def test_provider_error_content_is_never_logged(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    from agentloom.runtime.model_binding import ModelTurnBinding
    from agentloom.runtime.model_protocol import ModelTurnRequest, ModelTurnResult
    from agentloom.self_learning import reviewer

    db_path = tmp_path / "self_learning.db"
    _record_completed_run(db_path, "error-root")
    secret = "password=provider-secret-value"

    class _FailingModel:
        adapter_id = "openai_chat"

        def turn(self, _request: ModelTurnRequest) -> ModelTurnResult:
            raise RuntimeError(secret)

    binding = ModelTurnBinding(
        model_type="summary",
        model_id="fake/failing",
        adapter=_FailingModel(),
    )
    monkeypatch.setattr(
        reviewer,
        "_resolve_review_model",
        lambda _name: binding,
    )
    caplog.set_level("WARNING")

    result = reviewer.review_finished_run(
        root_run_id="error-root",
        agent_config=_config(),
        db_path=db_path,
    )

    assert result["status"] == "failed"
    assert result["reason"] == "RuntimeError"
    assert secret not in caplog.text
