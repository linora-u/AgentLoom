"""Task-relevance snapshot ranking: relevance x trust x recency, trust floor, CJK."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from src.extensions.self_learning.memory_store import MemoryStore
from src.extensions.self_learning.ranking import (
    find_conflicts,
    jaccard,
    rank_items,
    temporal_decay,
    tokenize,
)


def _store(tmp_path: Path, **budgets) -> MemoryStore:
    store = MemoryStore(tmp_path / ".agentloom" / "self_learning.db")
    if budgets:
        store._config = dict(store._config)
        store._config["scope_budgets"] = {**store._config["scope_budgets"], **budgets}
    return store


def _set_trust(store: MemoryStore, item_id: int, trust: float) -> None:
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE memory_items SET trust_score = ? WHERE id = ?", (trust, item_id))


# -- Pure ranking functions -------------------------------------------------------


def test_tokenize_extracts_words_and_cjk_bigrams():
    tokens = tokenize("Verify the export API 导出接口分页")
    assert "verify" in tokens
    assert "export" in tokens
    assert "the" not in tokens  # stopword
    assert "导出" in tokens
    assert "接口" in tokens


def test_jaccard_bounds():
    assert jaccard(set(), {"a"}) == 0.0
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert 0.0 < jaccard({"a", "b"}, {"b", "c"}) < 1.0


def test_temporal_decay_prefers_fresh_items_and_floors():
    now = datetime.now().astimezone()
    fresh = temporal_decay(now.isoformat(), now=now)
    month_old = temporal_decay((now - timedelta(days=30)).isoformat(), now=now)
    ancient = temporal_decay((now - timedelta(days=3650)).isoformat(), now=now)
    assert fresh > month_old > ancient
    assert abs(month_old - 0.5) < 0.01  # one half-life
    assert ancient == 0.1  # floor: old trusted facts never vanish entirely


def test_rank_items_puts_task_relevant_first():
    now = datetime.now().astimezone().isoformat()
    items = [
        {"id": 1, "content": "the database uses postgres 15", "updated_at": now, "trust_score": 0.5},
        {"id": 2, "content": "export API paginates at 100 rows", "updated_at": now, "trust_score": 0.5},
    ]
    ranked = rank_items(items, "verify export API pagination behavior")
    assert [item["id"] for item in ranked] == [2, 1]


def test_rank_items_without_task_text_falls_back_to_trust_and_recency():
    old = (datetime.now().astimezone() - timedelta(days=10)).isoformat()
    new = datetime.now().astimezone().isoformat()
    items = [
        {"id": 1, "content": "older fact", "updated_at": old, "trust_score": 0.5},
        {"id": 2, "content": "newer fact", "updated_at": new, "trust_score": 0.5},
        {"id": 3, "content": "older but trusted", "updated_at": old, "trust_score": 0.9},
    ]
    ranked = rank_items(items, "")
    assert ranked[0]["id"] == 3  # trust beats recency at these magnitudes
    assert [item["id"] for item in ranked[1:]] == [2, 1]


def test_ranking_is_deterministic_across_calls():
    now = datetime.now().astimezone().isoformat()
    items = [
        {"id": index, "content": f"identical fact {index % 2}", "updated_at": now, "trust_score": 0.5}
        for index in range(1, 8)
    ]
    first = [item["id"] for item in rank_items(items, "some task")]
    for _ in range(5):
        assert [item["id"] for item in rank_items(items, "some task")] == first


# -- Snapshot integration ---------------------------------------------------------


def test_snapshot_ranks_task_relevant_items_first(tmp_path: Path):
    store = _store(tmp_path)
    store.add("project", "the CI pipeline runs on push to main", proposal=False, source="test")
    store.add("project", "market data comes from the anysearch MCP server", proposal=False, source="test")
    snapshot = store.snapshot_for_prompt(
        agent_config={},
        session_run_id="snapshot-run",
        task_text="collect market data signals from anysearch",
    )
    lines = [line for line in snapshot.splitlines() if line.startswith("- ")]
    assert "anysearch" in lines[0]
    assert "CI pipeline" in lines[1]


def test_low_trust_items_excluded_from_snapshot_but_kept_in_db(tmp_path: Path):
    store = _store(tmp_path)
    kept = store.add("project", "reliable fact about deployment", proposal=False, source="test")
    doubted = store.add("project", "misleading fact about caching", proposal=False, source="test")
    _set_trust(store, doubted["id"], 0.15)  # below the 0.2 snapshot floor
    snapshot = store.snapshot_for_prompt(agent_config={}, session_run_id="snapshot-run")
    assert "reliable fact" in snapshot
    assert "misleading fact" not in snapshot
    assert "entries omitted" in snapshot
    contents = [item["content"] for item in store.list(include_pending=False)]
    assert any("misleading fact" in content for content in contents), "row must stay for inspection"
    assert kept["id"] is not None


def test_cjk_task_tokens_match_cjk_memory(tmp_path: Path):
    store = _store(tmp_path)
    store.add("project", "输出报告统一写入 outputs 目录", proposal=False, source="test")
    store.add("project", "unrelated english-only fact here", proposal=False, source="test")
    snapshot = store.snapshot_for_prompt(
        agent_config={}, session_run_id="snapshot-run", task_text="把今天的报告写入输出目录"
    )
    lines = [line for line in snapshot.splitlines() if line.startswith("- ")]
    assert "outputs" in lines[0]


def test_budget_tail_drop_still_well_formed_with_ranking(tmp_path: Path):
    store = _store(tmp_path)
    for index in range(6):
        store.add("project", f"fact number {index} " + "x" * 80, proposal=False, source="test")
    snapshot = store.snapshot_for_prompt(
        agent_config={}, session_run_id="snapshot-run", max_chars=400, task_text="fact number"
    )
    assert snapshot == "" or (
        snapshot.startswith("<agentloom_memory_snapshot")
        and snapshot.endswith("</agentloom_memory_snapshot>")
    )
    if snapshot:
        assert snapshot.count("<project_memory>") == snapshot.count("</project_memory>")


# -- Conflict heuristic -----------------------------------------------------------


def test_overlapping_divergent_contents_conflict():
    candidates = [
        {"id": 7, "content": "the export API paginates at 100 rows per page"},
        {"id": 8, "content": "completely unrelated statement about logging levels"},
    ]
    conflicts = find_conflicts("the export API paginates at 500 rows per page", candidates)
    assert [c["id"] for c in conflicts] == [7]
    assert 0.25 <= conflicts[0]["score"] < 0.9


def test_unrelated_contents_do_not_conflict_but_near_identical_do():
    candidates = [{"id": 9, "content": "the export API paginates at 100 rows per page"}]
    assert find_conflicts("scheduler cron fires at midnight UTC", candidates) == []
    # Near-identical DIFFERENT text is flagged: exact duplicates never coexist
    # (hash dedup blocks them at insert), so a >=0.9 pair reaching this scan
    # is a restatement or a text differing in a few values.
    assert [c["id"] for c in find_conflicts("The export API paginates at 100 rows per page", candidates)] == [9]


def test_long_text_differing_only_in_one_value_conflicts():
    """Audit counterexample: with the old 0.9 upper bound, a long fact
    differing only in a number escaped both dedup (different hash) and the
    conflict scan, leaving two contradictory actives."""
    base = (
        "the nightly ETL pipeline exports customer orders, enriches them with "
        "geo data, validates schema constraints, and loads the result into the "
        "warehouse partition dated by execution, batching {} records per call"
    )
    candidates = [{"id": 3, "content": base.format(100)}]
    hits = find_conflicts(base.format(500), candidates)
    assert [c["id"] for c in hits] == [3]
    assert hits[0]["score"] >= 0.9
