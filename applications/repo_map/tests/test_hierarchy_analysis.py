"""
Tests for Bottom-Up hierarchical analysis with children_analyses reuse and
children_hash incremental detection.

All tests are pure Python (no LLM calls). They test the helper functions
extracted from pipeline_agent_tools.py:
  - _dir_depth
  - _sort_bottom_up
  - _get_direct_children
  - _collect_children_analyses
  - _compute_children_hash

Run with:
    cd AgentLoom && .venv/bin/python -m pytest applications/repo_map/tests/test_hierarchy_analysis.py -v
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from applications.repo_map.agent_tools.pipeline_agent_tools import (
    _collect_children_analyses,
    _compute_children_hash,
    _dir_depth,
    _get_direct_children,
    _save_progress,
    _sort_bottom_up,
)
from applications.repo_map.agent_tools.paths import repo_map_docs_root


# ═══════════════════════════════════════════════════════════════════ #
#  Fixtures
# ═══════════════════════════════════════════════════════════════════ #


def _make_repo_map(
    tmp_path: Path,
    dirs_config: dict[str, dict],
) -> Path:
    """
    Helper: create a fake .repo_map structure for testing.

    Args:
        tmp_path: pytest tmp directory
        dirs_config: {dir_path: {"status": ..., "rank": ..., "analysis": "...", "index": "..."}}
            - "analysis": if set, writes analysis.md with this content
            - "index": if set, writes index.md with this content
            - other keys go into analysis_progress.json entry

    Returns:
        output_dir (= tmp_path itself, containing project-repo-map/ and data/)
    """
    out = tmp_path
    data_dir = out / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    progress = {}
    for dir_path, cfg in dirs_config.items():
        cfg = dict(cfg)  # copy to avoid mutating caller's dict

        # Determine filesystem paths
        docs_root = repo_map_docs_root(out)
        if dir_path == "(root)":
            repo_dir = docs_root
        else:
            repo_dir = docs_root / dir_path
        repo_dir.mkdir(parents=True, exist_ok=True)

        # Write analysis.md if provided
        analysis_content = cfg.pop("analysis", None)
        if analysis_content is not None:
            (repo_dir / "analysis.md").write_text(analysis_content, encoding="utf-8")

        # Write index.md if provided
        index_content = cfg.pop("index", None)
        if index_content is not None:
            (repo_dir / "index.md").write_text(index_content, encoding="utf-8")

        # Build progress entry
        entry = {
            "status": cfg.get("status", "pending"),
            "rank": cfg.get("rank", 9999),
        }
        # Copy extra keys (children_hash, index_md_hash, etc.)
        for k, v in cfg.items():
            if k not in ("status", "rank"):
                entry[k] = v
        progress[dir_path] = entry

    # Write analysis_progress.json
    (data_dir / "analysis_progress.json").write_text(
        json.dumps(progress, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


# ═══════════════════════════════════════════════════════════════════ #
#  T1-T5: Bottom-Up Sort Tests
# ═══════════════════════════════════════════════════════════════════ #


class TestBottomUpSort:
    """T1-T5: Verify bottom-up sorting (deepest first, same depth by rank)."""

    def test_basic(self):
        """T1: Basic 3-level nesting: a/b/c → x/y → a/b → a|x → (root)."""
        progress = {
            "(root)": {"rank": 6},
            "a": {"rank": 5},
            "a/b": {"rank": 3},
            "a/b/c": {"rank": 1},
            "x": {"rank": 2},
            "x/y": {"rank": 4},
        }
        result = _sort_bottom_up(progress)
        assert result == ["a/b/c", "a/b", "x/y", "x", "a", "(root)"]

    def test_same_depth_rank(self):
        """T2: Same depth directories sorted by rank ascending."""
        progress = {
            "(root)": {"rank": 99},
            "d1": {"rank": 10},
            "d2": {"rank": 5},
            "d3": {"rank": 20},
            "d4": {"rank": 1},
        }
        result = _sort_bottom_up(progress)
        # All depth=0, sorted by rank: d4(1) d2(5) d1(10) d3(20)
        depth0 = [d for d in result if d != "(root)"]
        assert depth0 == ["d4", "d2", "d1", "d3"]
        assert result[-1] == "(root)"

    def test_wide_tree(self):
        """T3: Wide tree: parent + 84 children (simulates real overnight_tests)."""
        progress = {"(root)": {"rank": 999}, "parent": {"rank": 100}}
        for i in range(84):
            progress[f"parent/child_{i:03d}"] = {"rank": 1000 + i}
        result = _sort_bottom_up(progress)
        # All 84 children (depth=1) before parent (depth=0) before (root)
        assert result[-1] == "(root)"
        assert result[-2] == "parent"
        children = result[:-2]
        assert len(children) == 84
        assert all(c.startswith("parent/child_") for c in children)
        # Children sorted by rank ascending
        ranks = [progress[c]["rank"] for c in children]
        assert ranks == sorted(ranks)

    def test_deep_chain(self):
        """T4: Deep 11-level chain (simulates deepest real paths)."""
        parts = ["a"]
        progress = {"(root)": {"rank": 999}}
        for i in range(11):
            path = "/".join(parts[: i + 1])
            progress[path] = {"rank": 100 - i}  # deeper = lower rank
            if i < 10:
                parts.append(chr(ord("b") + i))
        result = _sort_bottom_up(progress)
        # Deepest should come first
        depths = [_dir_depth(d) for d in result]
        assert depths == sorted(depths, reverse=True)
        assert result[-1] == "(root)"

    def test_root_always_last(self):
        """T5: (root) with rank=0 (highest priority) still sorts last."""
        progress = {
            "(root)": {"rank": 0},
            "z": {"rank": 9999},
            "a": {"rank": 5000},
        }
        result = _sort_bottom_up(progress)
        assert result[-1] == "(root)"
        # depth=0 dirs sorted by rank: a(5000) z(9999)
        assert result == ["a", "z", "(root)"]

    def test_mixed_real_world(self):
        """T18: Real-world data from scc project."""
        progress = {
            "(root)": {"rank": 999},
            "compiler": {"rank": 87249},
            "compiler/customerOp": {"rank": 87872},
            "compiler/customerOp/include": {"rank": 90315},
            "compiler/customerOp/tools": {"rank": 33105},
        }
        result = _sort_bottom_up(progress)
        # depth=2: tools(33105) before include(90315)
        depth2 = [d for d in result if _dir_depth(d) == 2]
        assert depth2 == [
            "compiler/customerOp/tools",
            "compiler/customerOp/include",
        ]
        # depth=2 before depth=1 before depth=0 before (root)
        assert result.index("compiler/customerOp/tools") < result.index(
            "compiler/customerOp"
        )
        assert result.index("compiler/customerOp") < result.index("compiler")
        assert result.index("compiler") < result.index("(root)")


# ═══════════════════════════════════════════════════════════════════ #
#  T6-T9: Collect Children Analyses Tests
# ═══════════════════════════════════════════════════════════════════ #


class TestCollectChildrenAnalyses:
    """T6-T9: Verify collection of direct children's analysis.md."""

    def test_basic(self, tmp_path):
        """T6: Collect 2 completed children, skip 1 pending."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "a": {"status": "pending", "rank": 1},
                "a/b": {
                    "status": "completed",
                    "rank": 2,
                    "analysis": "# Analysis: a/b\n\nB content",
                },
                "a/c": {
                    "status": "completed",
                    "rank": 3,
                    "analysis": "# Analysis: a/c\n\nC content",
                },
                "a/d": {"status": "pending", "rank": 4},
            },
        )
        progress = json.loads(
            (out / "data" / "analysis_progress.json").read_text()
        )
        result = _collect_children_analyses("a", progress, out)
        assert "# Analysis: a/b" in result
        assert "# Analysis: a/c" in result
        assert "a/d" not in result
        assert "---" in result  # separator between parts

    def test_leaf(self, tmp_path):
        """T7: Leaf directory (no children in progress) → empty string."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "a": {"status": "pending", "rank": 1},
                "a/b": {"status": "pending", "rank": 2},
                "a/b/c": {"status": "completed", "rank": 3, "analysis": "leaf"},
            },
        )
        progress = json.loads(
            (out / "data" / "analysis_progress.json").read_text()
        )
        result = _collect_children_analyses("a/b/c", progress, out)
        assert result == ""

    def test_skip_grandchild(self, tmp_path):
        """T8: Only collect direct children, not grandchildren."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "a": {"status": "pending", "rank": 1},
                "a/b": {
                    "status": "completed",
                    "rank": 2,
                    "analysis": "B analysis",
                },
                "a/b/c": {
                    "status": "completed",
                    "rank": 3,
                    "analysis": "C analysis (grandchild)",
                },
            },
        )
        progress = json.loads(
            (out / "data" / "analysis_progress.json").read_text()
        )
        result = _collect_children_analyses("a", progress, out)
        assert "B analysis" in result
        assert "C analysis (grandchild)" not in result

    def test_mixed_status(self, tmp_path):
        """T9: Only collect completed children; skip failed and pending."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "a": {"status": "pending", "rank": 1},
                "a/b": {
                    "status": "completed",
                    "rank": 2,
                    "analysis": "B done",
                },
                "a/c": {"status": "failed", "rank": 3, "analysis": "C failed"},
                "a/d": {"status": "pending", "rank": 4},
                "a/e": {
                    "status": "completed",
                    "rank": 5,
                    "analysis": "E done",
                },
            },
        )
        progress = json.loads(
            (out / "data" / "analysis_progress.json").read_text()
        )
        result = _collect_children_analyses("a", progress, out)
        assert "B done" in result
        assert "E done" in result
        assert "C failed" not in result
        assert "a/d" not in result


# ═══════════════════════════════════════════════════════════════════ #
#  T10-T18: Children Hash Incremental Detection Tests
# ═══════════════════════════════════════════════════════════════════ #


class TestChildrenHash:
    """T10-T18: Verify children_hash computation and incremental behavior."""

    def test_stable(self, tmp_path):
        """T10: Same content → same hash (deterministic)."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "a": {"status": "pending", "rank": 1},
                "a/b": {
                    "status": "completed",
                    "rank": 2,
                    "analysis": "B content",
                },
            },
        )
        progress = json.loads(
            (out / "data" / "analysis_progress.json").read_text()
        )
        h1 = _compute_children_hash("a", progress, out)
        h2 = _compute_children_hash("a", progress, out)
        assert h1 == h2
        assert len(h1) == 32  # MD5 hex digest

    def test_changes_on_content_change(self, tmp_path):
        """T11: Different analysis.md content → different hash."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "a": {"status": "pending", "rank": 1},
                "a/b": {
                    "status": "completed",
                    "rank": 2,
                    "analysis": "original",
                },
            },
        )
        progress = json.loads(
            (out / "data" / "analysis_progress.json").read_text()
        )
        h1 = _compute_children_hash("a", progress, out)

        # Change analysis.md content
        (repo_map_docs_root(out) / "a" / "b" / "analysis.md").write_text(
            "updated content", encoding="utf-8"
        )
        h2 = _compute_children_hash("a", progress, out)
        assert h1 != h2

    def test_detects_reset(self, tmp_path):
        """T12: children_hash changed → parent should be reset to pending."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "a": {
                    "status": "completed",
                    "rank": 1,
                    "children_hash": "old_hash_placeholder",
                    "index": "# a\nsome content",
                },
                "a/b": {
                    "status": "completed",
                    "rank": 2,
                    "analysis": "B analysis v1",
                    "index": "# a/b\nfiles",
                },
            },
        )
        progress_file = out / "data" / "analysis_progress.json"
        progress = json.loads(progress_file.read_text())

        # Record actual children_hash for "a" as the "stored" value
        real_hash = _compute_children_hash("a", progress, out)
        progress["a"]["children_hash"] = real_hash
        _save_progress(progress_file, progress)

        # Now update child's analysis.md (simulating re-analysis of a/b)
        (repo_map_docs_root(out) / "a" / "b" / "analysis.md").write_text(
            "B analysis v2 — significantly different", encoding="utf-8"
        )

        # Re-compute: hash should differ
        new_hash = _compute_children_hash("a", progress, out)
        assert new_hash != real_hash

        # Simulate the check logic from run_analysis_loop
        entry = progress["a"]
        if (
            entry["status"] == "completed"
            and new_hash
            and new_hash != entry.get("children_hash", "")
        ):
            entry["status"] = "pending"

        assert entry["status"] == "pending"

    def test_no_false_positive(self, tmp_path):
        """T13: children_hash unchanged → parent stays completed."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "a": {"status": "completed", "rank": 1},
                "a/b": {
                    "status": "completed",
                    "rank": 2,
                    "analysis": "B stable content",
                },
                "a/c": {
                    "status": "completed",
                    "rank": 3,
                    "analysis": "C stable content",
                },
            },
        )
        progress = json.loads(
            (out / "data" / "analysis_progress.json").read_text()
        )
        # Set stored hash to current
        real_hash = _compute_children_hash("a", progress, out)
        progress["a"]["children_hash"] = real_hash

        # Re-compute (nothing changed)
        new_hash = _compute_children_hash("a", progress, out)
        assert new_hash == real_hash

        # Should NOT reset
        entry = progress["a"]
        if (
            entry["status"] == "completed"
            and new_hash
            and new_hash != entry.get("children_hash", "")
        ):
            entry["status"] = "pending"

        assert entry["status"] == "completed"

    def test_leaf_no_children(self, tmp_path):
        """T14: Leaf directory → children_hash is empty string."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "a": {"status": "pending", "rank": 1},
                "a/b": {"status": "completed", "rank": 2},
            },
        )
        progress = json.loads(
            (out / "data" / "analysis_progress.json").read_text()
        )
        h = _compute_children_hash("a/b", progress, out)
        assert h == ""

    def test_cascade_three_levels(self, tmp_path):
        """T15: 3-level cascade: leaf changes → middle resets → top resets."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "a": {
                    "status": "completed",
                    "rank": 1,
                    "index": "# a",
                    "analysis": "A analysis",
                },
                "a/b": {
                    "status": "completed",
                    "rank": 2,
                    "index": "# a/b",
                    "analysis": "B analysis v1",
                },
                "a/b/c": {
                    "status": "completed",
                    "rank": 3,
                    "index": "# a/b/c",
                    "analysis": "C analysis v1",
                },
            },
        )
        progress_file = out / "data" / "analysis_progress.json"
        progress = json.loads(progress_file.read_text())

        # Record initial hashes
        hash_ab_initial = _compute_children_hash("a/b", progress, out)
        hash_a_initial = _compute_children_hash("a", progress, out)
        progress["a/b"]["children_hash"] = hash_ab_initial
        progress["a"]["children_hash"] = hash_a_initial
        _save_progress(progress_file, progress)

        # --- Step 1: Leaf c changes ---
        (repo_map_docs_root(out) / "a" / "b" / "c" / "analysis.md").write_text(
            "C analysis v2 — updated!", encoding="utf-8"
        )

        # a/b should detect child change
        hash_ab_new = _compute_children_hash("a/b", progress, out)
        assert hash_ab_new != hash_ab_initial, "a/b should detect c changed"

        # Simulate: a/b gets reset to pending
        progress["a/b"]["status"] = "pending"

        # --- Step 2: Simulate a/b re-analysis produces new analysis.md ---
        (repo_map_docs_root(out) / "a" / "b" / "analysis.md").write_text(
            "B analysis v2 — includes new C insights", encoding="utf-8"
        )
        progress["a/b"]["status"] = "completed"
        progress["a/b"]["children_hash"] = hash_ab_new

        # --- Step 3: a should detect a/b changed ---
        hash_a_new = _compute_children_hash("a", progress, out)
        assert hash_a_new != hash_a_initial, "a should detect a/b changed"

        # Simulate: a gets reset to pending
        entry_a = progress["a"]
        if (
            entry_a["status"] == "completed"
            and hash_a_new
            and hash_a_new != entry_a.get("children_hash", "")
        ):
            entry_a["status"] = "pending"

        assert entry_a["status"] == "pending", "Cascade: a must reset when leaf c changed"

    def test_partial_sibling(self, tmp_path):
        """T16: Only 1 of 3 siblings changed → parent hash still changes."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "a": {"status": "completed", "rank": 1},
                "a/b": {
                    "status": "completed",
                    "rank": 2,
                    "analysis": "B stable",
                },
                "a/c": {
                    "status": "completed",
                    "rank": 3,
                    "analysis": "C original",
                },
                "a/d": {
                    "status": "completed",
                    "rank": 4,
                    "analysis": "D stable",
                },
            },
        )
        progress = json.loads(
            (out / "data" / "analysis_progress.json").read_text()
        )
        h1 = _compute_children_hash("a", progress, out)

        # Only change a/c
        (repo_map_docs_root(out) / "a" / "c" / "analysis.md").write_text(
            "C changed!", encoding="utf-8"
        )
        h2 = _compute_children_hash("a", progress, out)
        assert h1 != h2, "Parent hash must change when any sibling changes"

    def test_root_collects_top_level(self, tmp_path):
        """T17: (root) collects all top-level directory analyses."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "compiler": {
                    "status": "completed",
                    "rank": 1,
                    "analysis": "Compiler analysis",
                },
                "tools": {
                    "status": "completed",
                    "rank": 2,
                    "analysis": "Tools analysis",
                },
                "docs": {
                    "status": "completed",
                    "rank": 3,
                    "analysis": "Docs analysis",
                },
                # Nested dirs should NOT be collected for (root)
                "compiler/frontend": {
                    "status": "completed",
                    "rank": 4,
                    "analysis": "Frontend analysis",
                },
            },
        )
        progress = json.loads(
            (out / "data" / "analysis_progress.json").read_text()
        )
        result = _collect_children_analyses("(root)", progress, out)
        assert "Compiler analysis" in result
        assert "Tools analysis" in result
        assert "Docs analysis" in result
        assert "Frontend analysis" not in result  # grandchild

        # Hash should also work for (root)
        h = _compute_children_hash("(root)", progress, out)
        assert len(h) == 32


# ═══════════════════════════════════════════════════════════════════ #
#  Edge cases
# ═══════════════════════════════════════════════════════════════════ #


class TestEdgeCases:
    """Additional edge case tests."""

    def test_dir_depth_values(self):
        """_dir_depth returns correct values for various paths."""
        assert _dir_depth("(root)") == -1
        assert _dir_depth("a") == 0
        assert _dir_depth("a/b") == 1
        assert _dir_depth("a/b/c") == 2
        assert _dir_depth("a/b/c/d/e/f/g/h/i/j/k") == 10

    def test_get_direct_children_root(self):
        """_get_direct_children for (root) returns depth=0 dirs only."""
        all_dirs = ["(root)", "a", "b", "a/x", "b/y", "a/x/z"]
        children = _get_direct_children("(root)", all_dirs)
        assert sorted(children) == ["a", "b"]

    def test_get_direct_children_nested(self):
        """_get_direct_children for nested dir skips grandchildren."""
        all_dirs = ["(root)", "a", "a/b", "a/c", "a/b/d", "a/b/e"]
        children = _get_direct_children("a", all_dirs)
        assert sorted(children) == ["a/b", "a/c"]

    def test_get_direct_children_leaf(self):
        """_get_direct_children for leaf returns empty list."""
        all_dirs = ["(root)", "a", "a/b"]
        children = _get_direct_children("a/b", all_dirs)
        assert children == []

    def test_empty_analysis_md_not_collected(self, tmp_path):
        """Empty analysis.md file is not included in children_analyses."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "a": {"status": "pending", "rank": 1},
                "a/b": {
                    "status": "completed",
                    "rank": 2,
                    "analysis": "",  # empty!
                },
                "a/c": {
                    "status": "completed",
                    "rank": 3,
                    "analysis": "C has content",
                },
            },
        )
        progress = json.loads(
            (out / "data" / "analysis_progress.json").read_text()
        )
        result = _collect_children_analyses("a", progress, out)
        # Only C should be present (B's analysis is empty)
        assert "C has content" in result
        assert "---" not in result  # Only one part, no separator

    def test_children_hash_includes_missing_files(self, tmp_path):
        """Hash distinguishes between missing analysis.md and empty one."""
        out = _make_repo_map(
            tmp_path,
            {
                "(root)": {"status": "pending", "rank": 99},
                "a": {"status": "pending", "rank": 1},
                "a/b": {"status": "completed", "rank": 2},  # no analysis file
                "a/c": {
                    "status": "completed",
                    "rank": 3,
                    "analysis": "",
                },  # empty file
            },
        )
        progress = json.loads(
            (out / "data" / "analysis_progress.json").read_text()
        )
        h1 = _compute_children_hash("a", progress, out)

        # Now create analysis.md for a/b (even empty)
        (repo_map_docs_root(out) / "a" / "b" / "analysis.md").write_text(
            "", encoding="utf-8"
        )
        h2 = _compute_children_hash("a", progress, out)
        assert h1 != h2, "Hash should change when missing file becomes empty file"

    def test_sort_only_root(self):
        """Sort with just (root) returns [(root)]."""
        progress = {"(root)": {"rank": 1}}
        assert _sort_bottom_up(progress) == ["(root)"]

    def test_sort_flat_no_nesting(self):
        """Sort with no nesting — all depth=0 sorted by rank, (root) last."""
        progress = {
            "(root)": {"rank": 0},
            "z": {"rank": 3},
            "m": {"rank": 1},
            "a": {"rank": 2},
        }
        result = _sort_bottom_up(progress)
        assert result == ["m", "a", "z", "(root)"]
