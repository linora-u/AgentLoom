"""Session-end distillation: session notes and failure patterns become proposals."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import pytest

from src.extensions.self_learning.event_schema import CanonicalSessionEvent, now_iso
from src.extensions.self_learning.ledger import SelfLearningLedger
from src.extensions.self_learning.memory_store import MemoryStore
from src.extensions.self_learning.reviewer import learning_review_hook
from src.lib.smolagents.hooks.types import HookContext


def _run_session_end(context: HookContext, monkeypatch: pytest.MonkeyPatch) -> str | None:
    from src.extensions.self_learning.learning_jobs import LearningJobQueue, LearningJobWorker

    monkeypatch.setattr(
        "src.extensions.self_learning.finalizer.kick_learning_worker",
        lambda *_args, **_kwargs: False,
    )
    assert learning_review_hook(context).decision == "allow"
    return LearningJobWorker(LearningJobQueue(), owner=f"review-{context.session_id}").run_once(
        now=datetime.now().astimezone()
    )


def _tool_error_event(run_id: str, tool_name: str, error: str) -> CanonicalSessionEvent:
    return CanonicalSessionEvent(
        event_id=uuid.uuid4().hex,
        run_id=run_id,
        event_type="tool_error",
        tool_name=tool_name,
        content=error,
        content_text=error,
        output_data={"error": error},
        status="failed",
        created_at=now_iso(),
    )


def test_session_end_distills_explicit_notes_but_not_repeated_failure_templates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    # Pin the deterministic path: this test asserts verbatim-note distillation,
    # and unit tests must not depend on whichever distill_model config ships.
    monkeypatch.setattr(
        "src.extensions.self_learning.reviewer.memory_config",
        lambda: {"distill_enabled": True, "distill_model": "", "auto_apply": "off"},
    )
    run_id = "session_distill_run"
    store = MemoryStore()
    store.add("session", "learned: the export API needs pagination", proposal=False, source="test", scope_id=run_id)

    ledger = SelfLearningLedger()
    for _ in range(3):
        ledger.append_event(_tool_error_event(run_id, "shell_tool", "Permission denied: /etc/hosts"))
    ledger.append_event(_tool_error_event(run_id, "web_fetch", "one-off timeout"))

    context = HookContext(
        session_id=run_id,
        root_run_id=run_id,
        cwd=str(tmp_path),
        hook_event_name="SessionEnd",
        tool_name="",
        tool_input={"task_id": run_id, "agent_name": "demo"},
        tool_response={"result": "done"},
    )
    assert _run_session_end(context, monkeypatch) == "succeeded"

    pending = [item for item in store.list("project") if item["status"] == "pending"]
    contents = [item["content"] for item in pending]
    assert any("export API needs pagination" in content for content in contents), contents
    # Failure rows are safe LLM evidence only. Deterministic fallback must not
    # turn transient errors into imperative memory.
    assert not any("shell_tool" in content for content in contents)
    assert not any("web_fetch" in content for content in contents)
    # Session originals are archived, not re-distilled on a second SessionEnd.
    assert store.list("session", scope_id=run_id) == []

    run_dir = tmp_path / ".agentloom" / "learning" / "runs" / run_id
    proposals_md = (run_dir / "memory_proposals.md").read_text(encoding="utf-8")
    assert "Distilled" in proposals_md or "shell_tool" in proposals_md


def test_distillation_respects_config_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    monkeypatch.setattr(
        "src.extensions.self_learning.reviewer.memory_config",
        lambda: {"distill_enabled": False},
    )
    run_id = "session_no_distill"
    store = MemoryStore()
    store.add("session", "note that must stay put", proposal=False, source="test", scope_id=run_id)

    context = HookContext(
        session_id=run_id,
        root_run_id=run_id,
        cwd=str(tmp_path),
        hook_event_name="SessionEnd",
        tool_name="",
        tool_input={"task_id": run_id, "agent_name": "demo"},
        tool_response={"result": "done"},
    )
    assert _run_session_end(context, monkeypatch) == "succeeded"
    assert store.list("session", scope_id=run_id), "session notes must remain when distillation is disabled"
    assert [item for item in store.list("project") if item["status"] == "pending"] == []


def test_session_end_emits_operator_summary_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog):
    import logging
    import re

    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    monkeypatch.setattr(
        "src.extensions.self_learning.reviewer.memory_config",
        lambda: {"distill_enabled": True, "distill_model": "", "auto_apply": "off"},
    )
    run_id = "session_summary_run"
    store = MemoryStore()
    store.add("session", "learned: summary lines are worth having", proposal=False, source="test", scope_id=run_id)
    context = HookContext(
        session_id=run_id,
        root_run_id=run_id,
        cwd=str(tmp_path),
        hook_event_name="SessionEnd",
        tool_name="",
        tool_input={"task_id": run_id, "agent_name": "demo"},
        tool_response={"result": "done"},
    )
    with caplog.at_level(logging.INFO):
        assert _run_session_end(context, monkeypatch) == "succeeded"
    matches = [
        record.message
        for record in caplog.records
        if re.search(r"Self-learning session summary \[run .*]: distilled 1 \(deterministic\)", record.message)
    ]
    assert matches, [record.message for record in caplog.records]
    summary_md = (tmp_path / ".agentloom" / "learning" / "runs" / run_id / "session_summary.md").read_text(
        encoding="utf-8"
    )
    assert "Self-learning session summary" in summary_md


def test_distill_exception_is_durable_retry_not_fake_success_summary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))

    def boom(*args, **kwargs):
        raise RuntimeError("distill blew up")

    monkeypatch.setattr(
        "src.extensions.self_learning.reviewer.memory_config",
        lambda: {"distill_enabled": True, "distill_model": "", "auto_apply": "off"},
    )
    monkeypatch.setattr(MemoryStore, "apply_job_semantic_plan", boom)
    context = HookContext(
        session_id="summary_after_error",
        root_run_id="summary_after_error",
        cwd=str(tmp_path),
        hook_event_name="SessionEnd",
        tool_name="",
        tool_input={"task_id": "summary_after_error", "agent_name": "demo"},
        tool_response={"result": "done"},
    )
    assert _run_session_end(context, monkeypatch) == "retry"
    from src.extensions.self_learning.learning_jobs import LearningJobQueue

    job = LearningJobQueue().list_jobs(status="retry", limit=10)[0]
    assert "distill blew up" in job["last_error"]
    assert not (tmp_path / ".agentloom" / "learning" / "runs" / "summary_after_error" / "session_summary.md").exists()


def test_task_completed_records_real_task_and_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    context = HookContext(
        session_id="run_tc",
        root_run_id="run_tc",
        cwd=str(tmp_path),
        hook_event_name="TaskCompleted",
        tool_name="task",
        tool_input={"task_id": "task_tc", "agent_name": "demo", "task_text": "Summarize market signals"},
        tool_response={"result": "Signals summarized into outputs/report.md"},
    )
    assert learning_review_hook(context).decision == "allow"
    proposals_md = (tmp_path / ".agentloom" / "learning" / "runs" / "run_tc" / "memory_proposals.md").read_text(
        encoding="utf-8"
    )
    assert "Summarize market signals" in proposals_md
    assert "outputs/report.md" in proposals_md
