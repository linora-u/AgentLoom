from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from click.testing import CliRunner

from src.extensions.self_learning.memory_store import MemoryStore


def _config(*, project_budget: int = 8000) -> dict:
    return {
        "application_id": "curated_store_test",
        "self_learning": {
            "memory": {
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

    assert properties["action"]["enum"] == ["list", "propose"]
    assert properties["scope"]["enum"] == ["project", "app"]
    assert properties["kind"]["enum"] == ["fact", "experience"]
    assert "A model candidate cannot activate, replace, remove, promote" in description
    assert "Project promotion is a separate human review action" in description
    assert "Multi-step procedures, scripts, assets" in description


def test_model_facing_memory_rejects_project_proposals(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from src.tools.self_learning import memory_tool

    monkeypatch.setattr(memory_tool, "current_session_run_id", lambda: "root-project-proposal")
    monkeypatch.setattr(memory_tool, "_current_agent_config", lambda: _config())

    result = json.loads(
        memory_tool.memory(
            action="propose",
            scope="project",
            memory_key="project:must-be-reviewed",
            text="A model must not publish this Project fact directly.",
        )
    )

    assert result["ok"] is False
    assert result["error"] == "project_promotion_requires_review"


def test_concurrent_exact_adds_create_one_active_row(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=_config())

    def add_once(_index: int) -> dict:
        return store.add("project", "The concurrent fixture has one canonical value.")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(add_once, range(64)))

    assert all(result["ok"] is True for result in results)
    assert len({int(result["id"]) for result in results}) == 1
    assert len(store.list("project")) == 1


def test_administrator_add_replace_and_remove_are_direct_confirmed_mutations(
    tmp_path: Path,
) -> None:
    store = MemoryStore(tmp_path / "self_learning.db")

    created = store.add(
        "project",
        "The export limit is 100 rows.",
        memory_key="export:limit",
    )
    assert created["pending"] is False
    assert created["state"] == "active_confirmed"

    replaced = store.replace(
        "project",
        str(created["id"]),
        "The export limit is 200 rows.",
    )
    assert replaced["pending"] is False
    active = store.list("project")
    assert [(item["content"], item["state"], item["activation_source"]) for item in active] == [
        ("The export limit is 200 rows.", "active_confirmed", "admin")
    ]

    removed = store.remove("project", str(replaced["id"]))
    assert removed == {"ok": True, "pending": False, "removed_id": replaced["id"]}
    assert store.list("project") == []


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
def test_memory_target_wildcards_are_literal_not_sql_patterns(
    tmp_path: Path,
    *,
    action: str,
    target: str,
) -> None:
    config = _config()
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=config)
    original = "The only stored fact has ordinary punctuation."
    store.add("project", original)

    with pytest.raises(KeyError, match="Memory target not found"):
        if action == "remove":
            store.remove("project", target)
        else:
            store.replace(
                "project",
                target,
                "A replacement must not reach the unrelated row.",
            )

    assert [item["content"] for item in store.list("project")] == [original]


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


def test_memory_cli_exposes_only_the_simplified_public_commands() -> None:
    from src.__main__ import memory

    result = CliRunner().invoke(memory, ["--help"])
    assert result.exit_code == 0
    commands = set(re.findall(r"^  ([a-z][a-z-]+)\s{2,}", result.output, flags=re.MULTILINE))
    for command in ("list", "add", "replace", "remove", "pending", "stats", "export"):
        assert command in commands
    for removed in (
        "approve",
        "reject",
        "distill",
        "curate",
        "feedback",
        "conflicts",
        "jobs",
        "retry-job",
        "apply",
    ):
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
            "memory": {},
        }
    }
    store = MemoryStore(tmp_path / "self_learning.db", agent_config=config)

    with pytest.raises(ValueError, match="missing_application_context"):
        store.handle_tool_action(
            "propose",
            scope="app",
            kind="fact",
            memory_key="anonymous-app-fact",
            payload={"text": "This anonymous app fact must not be shared."},
            root_run_id="anonymous-root",
            agent_config=config,
        )

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
                    action="propose",
                    scope="app",
                    memory_key="thread-local-app-fact",
                    text="This must not leak into the other thread's app.",
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
