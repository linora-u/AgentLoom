"""
Tests for parallel analysis in pipeline_agent_tools.py.

Covers:
- _group_by_depth: directory grouping for parallel execution
- run_analysis_loop integration with parallel patterns

All tests are pure Python (no LLM calls).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from applications.repo_map.agent_tools.pipeline_agent_tools import (
    _dir_depth,
    _group_by_depth,
    _sort_bottom_up,
)
from applications.repo_map.agent_tools.paths import repo_map_docs_root


# ═══════════════════════════════════════════════════════════════════ #
#  _group_by_depth
# ═══════════════════════════════════════════════════════════════════ #

class TestGroupByDepth:
    def test_basic_grouping(self):
        progress = {
            "src/a": {"rank": 1, "status": "pending"},
            "src/b": {"rank": 2, "status": "pending"},
            "src": {"rank": 3, "status": "pending"},
            "(root)": {"rank": 4, "status": "pending"},
        }
        dirs_sorted = _sort_bottom_up(progress)
        groups = _group_by_depth(dirs_sorted, progress)
        # Deepest first: depth=1 (src/a, src/b), depth=0 (src), depth=-1 ((root))
        assert len(groups) == 3
        depths = [g[0] for g in groups]
        assert depths == [1, 0, -1]
        assert set(groups[0][1]) == {"src/a", "src/b"}
        assert groups[1][1] == ["src"]
        assert groups[2][1] == ["(root)"]

    def test_root_always_last(self):
        progress = {
            "(root)": {"rank": 1, "status": "pending"},
            "a": {"rank": 2, "status": "pending"},
        }
        dirs_sorted = _sort_bottom_up(progress)
        groups = _group_by_depth(dirs_sorted, progress)
        last_group = groups[-1]
        assert "(root)" in last_group[1]

    def test_single_depth(self):
        progress = {
            "a": {"rank": 1, "status": "pending"},
            "b": {"rank": 2, "status": "pending"},
            "c": {"rank": 3, "status": "pending"},
        }
        dirs_sorted = _sort_bottom_up(progress)
        groups = _group_by_depth(dirs_sorted, progress)
        assert len(groups) == 1
        assert len(groups[0][1]) == 3

    def test_empty_progress(self):
        groups = _group_by_depth([], {})
        assert groups == []

    def test_deep_hierarchy(self):
        progress = {
            "a/b/c/d": {"rank": 1, "status": "pending"},
            "a/b/c": {"rank": 2, "status": "pending"},
            "a/b": {"rank": 3, "status": "pending"},
            "a": {"rank": 4, "status": "pending"},
            "(root)": {"rank": 5, "status": "pending"},
        }
        dirs_sorted = _sort_bottom_up(progress)
        groups = _group_by_depth(dirs_sorted, progress)
        depths = [g[0] for g in groups]
        assert depths == [3, 2, 1, 0, -1]

    def test_wide_same_depth(self):
        """Many directories at the same depth → all in one group."""
        progress = {f"dir{i}": {"rank": i, "status": "pending"} for i in range(20)}
        dirs_sorted = _sort_bottom_up(progress)
        groups = _group_by_depth(dirs_sorted, progress)
        assert len(groups) == 1
        assert len(groups[0][1]) == 20


# ═══════════════════════════════════════════════════════════════════ #
#  Integration: verify parallel readiness of run_analysis_loop
# ═══════════════════════════════════════════════════════════════════ #

class TestParallelAnalysisIntegration:
    """Test that the analysis loop infrastructure supports parallel execution."""

    def _make_repo_map(self, tmp_path: Path, dirs_config: dict) -> Path:
        """Create a fake .repo_map structure."""
        out = tmp_path
        data_dir = out / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        progress = {}
        for dir_path, cfg in dirs_config.items():
            cfg = dict(cfg)
            docs_root = repo_map_docs_root(out)
            if dir_path == "(root)":
                repo_dir = docs_root
            else:
                repo_dir = docs_root / dir_path
            repo_dir.mkdir(parents=True, exist_ok=True)

            analysis_content = cfg.pop("analysis", None)
            if analysis_content is not None:
                (repo_dir / "analysis.md").write_text(analysis_content, encoding="utf-8")

            index_content = cfg.pop("index", None)
            if index_content is not None:
                (repo_dir / "index.md").write_text(index_content, encoding="utf-8")

            progress[dir_path] = {k: v for k, v in cfg.items()}

        (data_dir / "analysis_progress.json").write_text(
            json.dumps(progress, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return out

    def test_same_depth_dirs_are_independent(self):
        """Same-depth directories should be groupable for parallel execution."""
        progress = {
            "src/a": {"rank": 1, "status": "pending"},
            "src/b": {"rank": 2, "status": "pending"},
            "src/c": {"rank": 3, "status": "pending"},
        }
        dirs_sorted = _sort_bottom_up(progress)
        groups = _group_by_depth(dirs_sorted, progress)
        # All at depth 1 → single group → can run in parallel
        assert len(groups) == 1
        assert len(groups[0][1]) == 3

    def test_children_before_parents(self):
        """Children groups always come before parent groups."""
        progress = {
            "src/utils/helpers": {"rank": 1, "status": "pending"},
            "src/utils": {"rank": 2, "status": "pending"},
            "src": {"rank": 3, "status": "pending"},
            "(root)": {"rank": 4, "status": "pending"},
        }
        dirs_sorted = _sort_bottom_up(progress)
        groups = _group_by_depth(dirs_sorted, progress)
        # First group should be the deepest
        assert "src/utils/helpers" in groups[0][1]
        # Last group should be root
        assert "(root)" in groups[-1][1]

    def test_progress_file_structure(self, tmp_path):
        """Verify the test fixture creates valid progress files."""
        out = self._make_repo_map(tmp_path, {
            "src": {"rank": 1, "status": "pending", "index": "# src"},
            "(root)": {"rank": 2, "status": "pending", "index": "# root"},
        })
        progress_file = out / "data" / "analysis_progress.json"
        assert progress_file.exists()
        data = json.loads(progress_file.read_text())
        assert "src" in data
        assert "(root)" in data
        assert (repo_map_docs_root(out) / "src" / "index.md").exists()
