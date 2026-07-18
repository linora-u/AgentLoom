from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.extensions.self_learning.memory_store import MemoryStore


def _config(*, approval: object = False, project_budget: int = 8000) -> dict:
    return {
        "application_id": "curated_store_test",
        "self_learning": {
            "memory": {
                "write_approval": approval,
                "scope_budgets": {
                    "project": project_budget,
                    "application": 6000,
                },
            }
        },
    }


def test_model_facing_memory_schema_has_one_canonical_write_contract() -> None:
    from src.lib.smolagents.tools.tools import ensure_tool_wrapped
    from src.tools.self_learning.memory_tool import memory

    model_tool = ensure_tool_wrapped([memory])[0]
    description = " ".join(model_tool.description.split())
    properties = model_tool.inputs

    assert properties["action"]["enum"] == ["list", "add", "replace", "remove"]
    assert properties["scope"]["enum"] == ["project", "app"]
    assert 'memory(action="add", scope="project", content="<standalone fact>")' in description
    assert 'Never use action="store"' in description
    assert "not ``fact``, ``key``, or ``value``" in description
    assert "Repository-wide or checkout-wide facts must use ``project``" in description
    assert "Use ``app`` only when the source explicitly limits the fact" in description


def test_concurrent_exact_adds_create_one_active_row(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=_config())

    def add_once(_index: int) -> dict:
        return store.add("project", "The concurrent fixture has one canonical value.")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(add_once, range(64)))

    assert all(result["ok"] is True for result in results)
    assert len({int(result["id"]) for result in results}) == 1
    assert len(store.list("project")) == 1


def test_parallel_adds_cannot_jointly_exceed_scope_capacity(tmp_path: Path) -> None:
    store = MemoryStore(
        tmp_path / "self_learning.db",
        agent_config=_config(project_budget=500),
    )

    def add_once(index: int) -> dict:
        return store.add("project", f"fact-{index:02d}-" + ("x" * 90))

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(add_once, range(32)))

    stored_chars = sum(len(item["content"]) for item in store.list("project"))
    assert stored_chars <= 500
    assert any(result.get("error") == "capacity_exceeded" for result in results)


def test_ambiguous_substring_target_requires_an_exact_id(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "self_learning.db")
    store.add("project", "Export records use a stable account identifier.")
    store.add("project", "Import records use a stable account identifier.")

    with pytest.raises(ValueError, match="ambiguous"):
        store.remove("project", "stable account identifier")

    assert len(store.list("project")) == 2


@pytest.mark.parametrize(
    ("action", "target"),
    [
        ("remove", "%"),
        ("replace", "_"),
    ],
)
@pytest.mark.parametrize("approval", [False, True], ids=["direct", "approval-staging"])
def test_memory_target_wildcards_are_literal_not_sql_patterns(
    tmp_path: Path,
    *,
    action: str,
    target: str,
    approval: bool,
) -> None:
    config = _config(approval=approval)
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=config)
    original = "The only stored fact has ordinary punctuation."
    store.add("project", original)

    with pytest.raises(KeyError, match="Memory target not found"):
        store.handle_tool_action(
            action,
            scope="project",
            target=target,
            content="A replacement must not reach the unrelated row.",
            root_run_id="literal-target-root",
            agent_config=config,
        )

    assert [item["content"] for item in store.list("project")] == [original]
    assert store.list_pending() == []


@pytest.mark.parametrize(
    ("target", "content"),
    [
        ("%", "Exports include a literal 100% completion marker."),
        ("_", "Exports use the literal snake_case field name."),
        (r"\\", r"Exports use the literal C:\\archive path."),
    ],
)
def test_memory_target_special_characters_still_match_their_literal_text(
    tmp_path: Path,
    *,
    target: str,
    content: str,
) -> None:
    store = MemoryStore(tmp_path / "self_learning.db")
    item = store.add("project", content)

    removed = store.remove("project", target)

    assert removed["removed_id"] == item["id"]
    assert store.list("project") == []


def test_string_false_does_not_enable_write_approval(tmp_path: Path) -> None:
    config = _config(approval="false")
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=config)

    result = store.handle_tool_action(
        "add",
        scope="project",
        content="String false keeps direct writes direct.",
        root_run_id="root-string-false",
        agent_config=config,
    )

    assert result["ok"] is True
    assert result["pending"] is False
    assert store.list_pending() == []


def test_approval_add_does_not_stage_an_already_active_fact(tmp_path: Path) -> None:
    config = _config(approval=True)
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=config)
    active = store.add("project", "The API limit is 100 rows.")

    result = store.handle_tool_action(
        "add",
        scope="project",
        content="  the api limit is 100 rows.  ",
        root_run_id="root-active-duplicate",
        agent_config=config,
    )

    assert result["duplicate"] is True
    assert result["pending"] is False
    assert result["id"] == active["id"]
    assert store.list_pending() == []


def test_approval_add_deduplicates_normalized_pending_facts(tmp_path: Path) -> None:
    config = _config(approval=True)
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=config)

    first = store.handle_tool_action(
        "add",
        scope="project",
        content="A durable fact.",
        root_run_id="root-pending-first",
        agent_config=config,
    )
    normalized_duplicate = store.handle_tool_action(
        "add",
        scope="project",
        content="  a  durable FACT.  ",
        root_run_id="root-pending-second",
        agent_config=config,
    )
    exact_duplicate = store.handle_tool_action(
        "add",
        scope="project",
        content="A durable fact.",
        root_run_id="root-pending-third",
        agent_config=config,
    )

    assert first["duplicate"] is False
    assert normalized_duplicate == {
        "ok": True,
        "pending": True,
        "duplicate": True,
        "id": first["id"],
    }
    assert exact_duplicate == normalized_duplicate
    assert len(store.list_pending()) == 1


def test_approval_add_does_not_read_pending_payloads_for_dedup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(approval=True)
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=config)
    original_connect = store._connect

    def connect_without_pending_payload_reads() -> sqlite3.Connection:
        conn = original_connect()

        def authorize(
            action: int,
            table: str | None,
            column: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if (
                action == sqlite3.SQLITE_READ
                and table == "memory_pending_writes"
                and column == "payload_json"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(authorize)
        return conn

    monkeypatch.setattr(store, "_connect", connect_without_pending_payload_reads)

    result = store.handle_tool_action(
        "add",
        scope="project",
        content="A new durable fact.",
        root_run_id="root-no-payload-scan",
        agent_config=config,
    )

    assert result["ok"] is True
    assert result["duplicate"] is False


def test_memory_cli_exposes_only_the_simplified_public_commands() -> None:
    from src.__main__ import memory

    result = CliRunner().invoke(memory, ["--help"])
    assert result.exit_code == 0
    commands = set(re.findall(r"^  ([a-z][a-z-]+)\s{2,}", result.output, flags=re.MULTILINE))
    for command in ("list", "add", "replace", "remove", "pending", "approve", "reject", "stats", "export"):
        assert command in commands
    for removed in ("distill", "curate", "feedback", "conflicts", "jobs", "retry-job", "apply"):
        assert removed not in commands


def test_initialization_deletes_legacy_markdown_mirrors(tmp_path: Path) -> None:
    db = tmp_path / ".agentloom" / "self_learning.db"
    memory_dir = db.parent / "memory"
    app_dir = memory_dir / "applications"
    app_dir.mkdir(parents=True)
    secret = "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
    (memory_dir / "MEMORY.md").write_text(f"api_key = {secret}\n", encoding="utf-8")
    stale = app_dir / "legacy.md"
    stale.write_text("removed fact must not survive\n", encoding="utf-8")
    legacy_db_files = [
        memory_dir / "memory.db",
        memory_dir / "memory.db-wal",
        memory_dir / "memory.db-shm",
    ]
    for path in legacy_db_files:
        path.write_bytes(secret.encode())

    store = MemoryStore(db)

    assert not (memory_dir / "MEMORY.md").exists()
    assert not stale.exists()
    assert not any(path.exists() for path in legacy_db_files)
    store.add("app", "A durable app fact.", scope_id="artifact_app")
    assert list(app_dir.glob("*.md")) == []


def test_initialization_does_not_recreate_the_removed_memory_directory(
    tmp_path: Path,
) -> None:
    db = tmp_path / ".agentloom" / "self_learning.db"

    MemoryStore(db)

    assert not (db.parent / "memory").exists()


def test_cached_initialization_still_removes_recreated_legacy_artifacts(
    tmp_path: Path,
) -> None:
    db = tmp_path / ".agentloom" / "self_learning.db"
    MemoryStore(db)
    memory_dir = db.parent / "memory"
    memory_dir.mkdir()
    markdown = memory_dir / "MEMORY.md"
    legacy_db = memory_dir / "memory.db"
    markdown.write_text("stale", encoding="utf-8")
    legacy_db.write_bytes(b"stale")

    MemoryStore(db)

    assert not markdown.exists()
    assert not legacy_db.exists()


def test_legacy_cleanup_never_follows_a_memory_directory_symlink(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".agentloom"
    external = tmp_path / "user-docs"
    external.mkdir()
    user_markdown = external / "README.md"
    user_markdown.write_text("keep me", encoding="utf-8")
    state.mkdir()
    (state / "memory").symlink_to(external, target_is_directory=True)

    MemoryStore(state / "self_learning.db")

    assert user_markdown.read_text(encoding="utf-8") == "keep me"


def test_application_disable_prevents_snapshot_injection(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "self_learning.db")
    store.add("project", "This fact must be hidden when self-learning is disabled.")

    snapshot = store.snapshot_for_prompt(
        agent_config={
            "application_id": "disabled_app",
            "self_learning": {"enabled": False},
        }
    )

    assert snapshot == ""


def test_unresolved_application_scope_fails_closed_instead_of_sharing_default(tmp_path: Path) -> None:
    config = {
        "self_learning": {
            "enabled": True,
            "memory": {"write_approval": False},
        }
    }
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=config)

    result = store.handle_tool_action(
        "add",
        scope="app",
        content="This anonymous app fact must not be shared.",
        root_run_id="anonymous-root",
        agent_config=config,
    )

    assert result == {"ok": False, "error": "missing_application_context"}
    assert store.list() == []


def test_model_memory_never_uses_another_threads_global_application_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from src.tools.self_learning.memory_tool import memory
    from src.trace import bind_root_run, clear_current_agent_config, set_current_agent_config

    runtime_root = tmp_path / ".agentloom"
    monkeypatch.setattr(
        "src.extensions.self_learning.paths._runtime_config_section",
        lambda: {"root_dir": str(runtime_root)},
    )
    set_current_agent_config(
        {
            "application_id": "app_from_other_thread",
            "self_learning": {"enabled": True},
        }
    )

    def write_without_local_app() -> dict:
        with bind_root_run("thread-root"):
            return json.loads(
                memory(
                    action="add",
                    scope="app",
                    content="This must not leak into the other thread's app.",
                )
            )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(write_without_local_app).result()
    finally:
        clear_current_agent_config()

    assert result == {"ok": False, "error": "missing_agent_context"}
    assert MemoryStore(runtime_root / "self_learning.db").list() == []


def test_removed_legacy_memory_arguments_fail_loudly(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "self_learning.db")

    with pytest.raises(TypeError):
        store.add("project", "A durable fact.", proposal=True)  # type: ignore[call-arg]
    with pytest.raises(AttributeError):
        store.reject("all")  # type: ignore[attr-defined]

    assert store.list() == []
