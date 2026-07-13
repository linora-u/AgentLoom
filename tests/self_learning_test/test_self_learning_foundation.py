from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.extensions.self_learning.event_schema import CanonicalSessionEvent, dumps_event
from src.extensions.self_learning.ledger import SelfLearningLedger
from src.extensions.self_learning.memory_store import MemoryStore
from src.extensions.self_learning.proposal_writer import ProposalWriter
from src.extensions.self_learning.reviewer import learning_review_hook
from src.extensions.self_learning.session_index import SessionIndex
from src.extensions.self_learning.session_recorder import SessionRecorder, event_file_for_run
from src.lib.smolagents.hooks.types import HookContext
from src.lib.smolagents.skills.skills import SkillsManager
from src.tools.tool_meta import resolve_tool_function


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _write_event_file(path: Path, events: list[CanonicalSessionEvent]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(dumps_event(event) for event in events) + "\n", encoding="utf-8")


def test_session_index_search_scroll_survives_checkpoint_cleanup_and_redacts(tmp_path: Path):
    event_path = tmp_path / ".agentloom" / "sessions" / "events" / "run_demo.jsonl"
    run_id = "run_demo"
    events = [
        CanonicalSessionEvent(
            event_id="evt_1",
            run_id=run_id,
            task_id="task_1",
            agent_name="demo_agent",
            event_type="task_created",
            source="hook",
            role="lifecycle",
            content=json.dumps(
                {
                    "task_text": "debug pytest failure",
                    "note": "api_key=supersecret123",
                },
                ensure_ascii=False,
            ),
            created_at="2026-06-23T12:00:00+08:00",
        ),
        CanonicalSessionEvent(
            event_id="evt_2",
            run_id=run_id,
            task_id="task_1",
            agent_name="demo_agent",
            tool_name="shell_tool",
            event_type="tool_result",
            source="hook",
            role="tool",
            content="pytest failure fixed password=supersecret123",
            created_at="2026-06-23T12:00:01+08:00",
        ),
    ]
    _write_event_file(event_path, events)

    # Checkpoint cleanup no longer matters because canonical events are stored
    # under .agentloom, not under .logs/checkpoints.
    run_dir = tmp_path / ".logs" / "demo_agent" / "20260623_120000"
    task_dir = run_dir / "checkpoints" / "task_1"
    task_dir.mkdir(parents=True)
    _write_json(task_dir / "checkpoint.json", {"memory_steps": [{"observations": "legacy only"}]})

    index = SessionIndex(tmp_path / ".agentloom" / "self_learning.db")
    stats = index.index_run(event_path)
    assert stats["events_indexed"] == 2

    # Simulate checkpoint cleanup after indexing.
    for path in sorted((run_dir / "checkpoints").rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()

    results = index.search("pytest failure")
    assert results
    assert results[0]["run_id"] == run_id
    assert "supersecret123" not in json.dumps(results, ensure_ascii=False)
    assert "[REDACTED]" in json.dumps(results, ensure_ascii=False)
    assert index.search("legacy only") == []

    after = index.scroll(results[0]["run_id"], int(results[0]["id"]), direction="after", window=5)
    assert isinstance(after, list)


def test_session_search_tool_returns_compact_redacted_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    context = HookContext(
        session_id="recorder_session",
        root_run_id="recorder_session",
        cwd=str(tmp_path),
        hook_event_name="PostToolUse",
        tool_name="shell_tool",
        tool_input={"command": "pytest"},
        # The semicolon closes the unquoted credential before the long safe
        # output; whitespace is intentionally part of an unquoted secret value.
        tool_response={"result": "needle compact api_key=supersecret123; " + ("long output " * 200)},
        task_id="task_1",
        agent_name="demo_agent",
    )
    assert SessionRecorder().record_hook(context).decision == "allow"
    event_file = event_file_for_run("recorder_session")
    assert not event_file.exists()
    db_path = tmp_path / ".agentloom" / "self_learning.db"
    assert db_path.exists()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert conn.execute("SELECT source_path FROM events").fetchone()[0] == ""

    from src.tools.self_learning.session_tools import session_search
    from src.trace import bind_root_run

    with bind_root_run("current-search-run"):
        raw = session_search("needle compact", limit=10)
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert len(raw) < 1500
    assert payload["results"]
    assert len(payload["results"][0]["content"]) <= 80
    assert payload["results"][0]["content_truncated"] is True
    assert "supersecret123" not in raw


def test_session_search_defaults_to_current_application_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    app_a_yaml = tmp_path / "applications" / "alpha_app" / "workflows" / "agent.yaml"
    app_b_yaml = tmp_path / "applications" / "beta_app" / "workflows" / "agent.yaml"
    app_a_yaml.parent.mkdir(parents=True)
    app_b_yaml.parent.mkdir(parents=True)
    app_a_yaml.write_text("name: a\n", encoding="utf-8")
    app_b_yaml.write_text("name: b\n", encoding="utf-8")
    monkeypatch.setattr("src.extensions.self_learning.application_scope._agent_root", lambda: tmp_path)

    for session_id, yaml_path, marker in (
        ("run_a", app_a_yaml, "shared needle alpha only"),
        ("run_b", app_b_yaml, "shared needle beta only"),
    ):
        context = HookContext(
            session_id=session_id,
            root_run_id=session_id,
            cwd=str(tmp_path),
            hook_event_name="PostToolUse",
            tool_name="shell_tool",
            tool_input={"command": "echo", "agent_name": marker, "task_id": session_id},
            tool_response={"result": marker},
            task_id=session_id,
            agent_name="demo_agent",
            agent_config={"_yaml_file_path": str(yaml_path), "name": "demo_agent"},
        )
        assert SessionRecorder().record_hook(context).decision == "allow"

    from src.tools.self_learning.session_tools import session_search
    from src.trace import bind_root_run, clear_current_agent_config, set_current_agent_config

    set_current_agent_config({"_yaml_file_path": str(app_a_yaml), "name": "demo_agent"})
    try:
        with bind_root_run("current-search-run"):
            payload = json.loads(session_search("shared needle"))
            all_payload = json.loads(session_search("shared needle", scope="all"))
    finally:
        clear_current_agent_config()
    assert payload["app"] == "alpha_app"
    assert len(payload["results"]) == 1
    assert payload["results"][0]["application_id"] == "alpha_app"
    assert "alpha only" in payload["results"][0]["content"]

    assert len(all_payload["results"]) == 2


def test_memory_proposal_policy_apply_duplicate_and_snapshot(tmp_path: Path):
    store = MemoryStore(tmp_path / ".agentloom" / "self_learning.db")
    assert not (tmp_path / ".agentloom" / "memory" / "USER.md").exists()

    proposed = store.add("project", "Repo uses pytest", proposal=True, source="test")
    assert proposed["proposal"] is True
    assert store.list(include_pending=False) == []

    duplicate = store.add("project", "Repo uses pytest", proposal=True, source="test")
    assert duplicate["duplicate"] is True

    applied = store.apply(str(proposed["id"]))
    assert applied["ok"] is True
    active = store.list(include_pending=False)
    assert active[0]["content"] == "Repo uses pytest"
    snapshot = store.snapshot_for_prompt(session_run_id="snapshot-run")
    assert "Repo uses pytest" in snapshot
    late = store.add("project", "Late fact after run start", proposal=False, source="test")
    assert "Late fact after run start" not in snapshot
    assert "Late fact after run start" in store.snapshot_for_prompt(session_run_id="snapshot-run")
    store.remove("project", str(late["id"]), proposal=False)

    replaced = store.replace("project", str(active[0]["id"]), "Repo uses pytest and ruff", proposal=False)
    assert "ruff" in store.snapshot_for_prompt(session_run_id="snapshot-run")
    store.remove("project", str(replaced["new_id"]), proposal=False)
    assert store.list(include_pending=False) == []


def test_memory_rejects_user_scope_until_user_identity_exists(tmp_path: Path):
    store = MemoryStore(tmp_path / ".agentloom" / "self_learning.db")

    with pytest.raises(ValueError, match="scope must be one of"):
        store.add("user", "Prefer concise answers", proposal=False, source="test")


def test_application_memory_is_isolated_and_project_memory_is_shared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("src.extensions.self_learning.application_scope._agent_root", lambda: tmp_path)
    store = MemoryStore(tmp_path / ".agentloom" / "self_learning.db")
    app_a_yaml = tmp_path / "applications" / "alpha_app" / "workflows" / "agent.yaml"
    app_b_yaml = tmp_path / "applications" / "beta_app" / "workflows" / "agent.yaml"
    app_a_yaml.parent.mkdir(parents=True)
    app_b_yaml.parent.mkdir(parents=True)
    app_a_yaml.write_text("name: a\n", encoding="utf-8")
    app_b_yaml.write_text("name: b\n", encoding="utf-8")

    store.add("project", "Project-wide pytest convention", proposal=False, source="test")
    store.add("app", "Alpha-only reusable workflow", proposal=False, source="test", scope_id="alpha_app")
    store.add("app", "Beta-only reusable workflow", proposal=False, source="test", scope_id="beta_app")

    alpha_snapshot = store.snapshot_for_prompt(
        agent_config={"_yaml_file_path": str(app_a_yaml)}, session_run_id="snapshot-run"
    )
    beta_snapshot = store.snapshot_for_prompt(
        agent_config={"_yaml_file_path": str(app_b_yaml)}, session_run_id="snapshot-run"
    )

    assert "Project-wide pytest convention" in alpha_snapshot
    assert "Project-wide pytest convention" in beta_snapshot
    assert "Alpha-only reusable workflow" in alpha_snapshot
    assert "Beta-only reusable workflow" not in alpha_snapshot
    assert "Beta-only reusable workflow" in beta_snapshot
    assert "Alpha-only reusable workflow" not in beta_snapshot
    assert (tmp_path / ".agentloom" / "memory" / "applications" / "alpha_app.md").exists()


def test_memory_tool_list_returns_compact_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))

    from src.tools.self_learning.memory_tool import memory
    from src.trace import bind_root_run

    with bind_root_run("root-compact-list"):
        for idx in range(12):
            add_payload = json.loads(
                memory(
                    action="add",
                    scope="project",
                    content=f"compact memory marker {idx} " + ("long text " * 200),
                )
            )
            assert add_payload["proposal"] is True
        raw = memory(action="list", scope="project")
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["item_count"] == 12
    assert payload["items_truncated"] is True
    assert payload["sort"] == "id_desc"
    assert len(raw) < 3000
    assert payload["items"][0]["status"] == "pending"
    assert payload["items"][0]["id"] == 12
    assert payload["items"][0]["content_truncated"] is True
    assert len(payload["items"][0]["content"]) <= 120


def test_skill_proposal_validation_promotion_and_discovery_exclusion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    proposals = tmp_path / "skills" / "generated" / "proposals"
    active = tmp_path / "skills"
    writer = ProposalWriter(proposals_dir=proposals, skills_dir=active)
    result = writer.create(
        action="create",
        name="pytest-helper",
        content="---\nname: pytest-helper\ndescription: Help with pytest failures\n---\n# Use pytest\n",
    )
    proposal_id = Path(result["proposal_path"]).name

    with pytest.raises(ValueError):
        writer.create(action="write_file", name="pytest-helper", path="../escape.txt", content="bad")

    promote = writer.promote(proposal_id)
    assert (Path(promote["path"]) / "SKILL.md").exists()

    manager = SkillsManager()
    discovered = manager.discover_skill_files(active)
    discovered_text = "\n".join(str(path) for path in discovered)
    assert "pytest-helper/SKILL.md" in discovered_text
    assert "generated/proposals" not in discovered_text


def test_reviewer_dedupes_and_stays_failure_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    context = HookContext(
        session_id="session_1",
        root_run_id="session_1",
        cwd=str(tmp_path),
        hook_event_name="TaskCompleted",
        tool_name="task",
        tool_input={"task_id": "task_1", "agent_name": "demo"},
        tool_response={"result": "done"},
    )

    assert learning_review_hook(context).decision == "allow"
    assert learning_review_hook(context).decision == "allow"
    run_dir = tmp_path / ".agentloom" / "learning" / "runs" / "session_1"
    assert (run_dir / "taskcompleted.json").exists()
    assert (run_dir / "memory_proposals.md").exists()
    with sqlite3.connect(tmp_path / ".agentloom" / "self_learning.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0] == 1


def test_reviewer_uses_only_sanitized_root_owned_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / ".agentloom"
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(root))
    secrets = {
        "PATHSECRET7",
        "SESSIONSECRET7",
        "CWDSECRET7",
        "TOOLSECRET7",
    }
    context = HookContext(
        session_id="authorization=Bearer SESSIONSECRET7",
        root_run_id="review-root-safe",
        cwd="client_secret=CWDSECRET7",
        hook_event_name="TaskCompleted",
        tool_name="password=TOOLSECRET7",
        tool_input={"task_id": "api_key=PATHSECRET7", "task_text": "safe task"},
        tool_response={"result": "safe result"},
    )

    result = learning_review_hook(context)

    assert result.success is True
    run_dir = root / "learning" / "runs" / "review-root-safe"
    assert run_dir.is_dir()
    assert not (root / "learning" / "runs" / "api_key_PATHSECRET7").exists()
    artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in run_dir.rglob("*") if path.is_file())
    with sqlite3.connect(root / "self_learning.db") as conn:
        source_run_id, output_json = conn.execute("SELECT source_run_id, output_json FROM review_runs").fetchone()
    persisted = f"{source_run_id}\n{output_json}\n{artifact_text}"
    assert source_run_id == "review-root-safe"
    assert all(secret not in persisted for secret in secrets)


def test_reviewer_missing_root_context_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / ".agentloom"
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(root))
    context = HookContext(
        session_id="session-without-root",
        cwd=str(tmp_path),
        hook_event_name="TaskCompleted",
        tool_name="task",
        tool_input={"task_id": "api_key=SHOULDNOTOWNARTIFACT7"},
        tool_response={"result": "done"},
    )

    result = learning_review_hook(context)

    assert result.success is False
    assert result.outcome == "non_blocking_error"
    assert "missing_root_run_context" in result.reason
    assert not root.exists()


def test_ledger_schema_contains_unified_learning_tables(tmp_path: Path):
    db = tmp_path / ".agentloom" / "self_learning.db"
    SelfLearningLedger(db)
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual')")}
    assert {
        "runs",
        "events",
        "events_fts",
        "events_fts_trigram",
        "memory_items",
        "skill_proposals",
        "review_runs",
        "artifacts",
    }.issubset(tables)


def test_ledger_bulk_application_search_cjk_and_redaction(tmp_path: Path):
    db = tmp_path / ".agentloom" / "self_learning.db"
    index = SessionIndex(db)
    total = 0
    for app_idx in range(5):
        app_id = f"bulk_app_{app_idx}"
        for event_idx in range(45):
            marker = f"BULK_LEDGER_MARKER_{app_idx}_{event_idx}"
            cjk = "中文关键片段" if app_idx == 3 and event_idx % 3 == 0 else "普通内容"
            event = CanonicalSessionEvent(
                event_id=f"bulk_evt_{app_idx}_{event_idx}",
                run_id=f"bulk_run_{app_idx}_{event_idx // 5}",
                task_id=f"bulk_task_{app_idx}_{event_idx}",
                application_id=app_id,
                application_name=app_id,
                application_path=f"/apps/{app_id}",
                workflow_path=f"/apps/{app_id}/workflows/agent.yaml",
                agent_name="bulk_agent",
                tool_name="shell_tool",
                event_type="tool_result",
                phase="tool",
                source="hook",
                role="tool",
                content_text=f"{marker} 批量验证 {cjk} api_key=bulksecret{event_idx}",
                output_data={"result": f"{marker} 批量验证 {cjk}"},
                created_at=f"2026-06-23T12:{event_idx % 60:02d}:00+08:00",
            )
            assert index.record_event(event)["indexed"] is True
            total += 1

    counts = SelfLearningLedger(db).count_events()
    assert counts["events_indexed"] == total

    app_hits = index.search("批量验证", app="bulk_app_3", limit=50)
    assert len(app_hits) == 45
    assert {row["application_id"] for row in app_hits} == {"bulk_app_3"}

    cjk_hits = index.search("中文关键片段", app="bulk_app_3", limit=50)
    assert len(cjk_hits) == 15
    assert "bulksecret" not in json.dumps(app_hits, ensure_ascii=False)
    assert "[REDACTED]" in json.dumps(app_hits, ensure_ascii=False)


def test_tool_resolution_and_cli_signatures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    assert resolve_tool_function("session_search").__name__ == "session_search"
    assert resolve_tool_function("memory").__name__ == "memory"
    assert resolve_tool_function("skill_manage").__name__ == "skill_manage"

    from src.__main__ import main

    runner = CliRunner()
    result = runner.invoke(main, ["memory", "add", "--scope", "project", "CLI memory"])
    assert result.exit_code == 0
    result = runner.invoke(main, ["memory", "list"])
    assert result.exit_code == 0
    assert "CLI memory" in result.output


def test_sessions_and_skill_proposal_cli_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTLOOM_SELF_LEARNING_ROOT", str(tmp_path / ".agentloom"))
    event_path = tmp_path / ".agentloom" / "sessions" / "events" / "cli_run.jsonl"
    _write_event_file(
        event_path,
        [
            CanonicalSessionEvent(
                event_id="cli_evt_1",
                run_id="cli_run",
                task_id="task_cli",
                agent_name="cli_agent",
                tool_name="shell_tool",
                event_type="tool_result",
                source="hook",
                role="tool",
                content="cli needle output",
                created_at="2026-06-23T13:00:00+08:00",
            )
        ],
    )
    index = SessionIndex()
    index.index_run(event_path)
    event_id = int(index.search("cli needle", limit=1)[0]["id"])

    import src.extensions.self_learning.proposal_writer as proposal_module
    from src.__main__ import main
    from src.extensions.self_learning.proposal_writer import ProposalWriter

    monkeypatch.setattr(proposal_module, "skill_proposals_dir", lambda: tmp_path / "skills" / "generated" / "proposals")
    monkeypatch.setattr(proposal_module, "active_skills_dir", lambda: tmp_path / "skills")
    writer = ProposalWriter()
    created = writer.create(
        action="create",
        name="cli-proposal",
        content="---\nname: cli-proposal\ndescription: CLI proposal\n---\n# CLI Proposal\n",
    )
    proposal_id = Path(created["proposal_path"]).name
    archived = writer.create(
        action="create",
        name="cli-archive",
        content="---\nname: cli-archive\ndescription: CLI archive\n---\n# CLI Archive\n",
    )
    archive_id = Path(archived["proposal_path"]).name

    runner = CliRunner()
    result = runner.invoke(main, ["sessions", "search", "cli needle", "--limit", "1"])
    assert result.exit_code == 0
    assert "cli needle output" in result.output

    result = runner.invoke(main, ["sessions", "index"])
    assert result.exit_code == 0
    assert "events_indexed" in result.output

    result = runner.invoke(main, ["sessions", "scroll", "cli_run", str(event_id), "--window", "1"])
    assert result.exit_code == 0

    result = runner.invoke(main, ["skills", "proposals", "list"])
    assert result.exit_code == 0
    assert "cli-proposal" in result.output

    result = runner.invoke(main, ["skills", "proposals", "show", proposal_id])
    assert result.exit_code == 0
    assert "CLI Proposal" in result.output

    result = runner.invoke(main, ["skills", "proposals", "promote", proposal_id, "--name", "cli-promoted"])
    assert result.exit_code == 0
    assert (tmp_path / "skills" / "cli-promoted" / "SKILL.md").exists()

    result = runner.invoke(main, ["skills", "proposals", "archive", archive_id])
    assert result.exit_code == 0
    assert ".archive" in result.output
