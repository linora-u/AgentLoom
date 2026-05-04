"""Tests for glob_tool exclude_paths integration (Layer 2 filtering).

Verifies that glob_search respects tool_access_control.exclude_paths
configuration by filtering out results from excluded directories, for
both the ripgrep backend and the Python fallback.
"""

import os
from pathlib import Path

import pytest

from src.tools.search.glob_tool.glob_tool import (
    glob_search,
    _filter_excluded_paths,
    _glob_with_python,
)

# The mock target is the same as in test_grep_exclude.py — the shared
# _resolve_tool_access_control_config function in path_validators.
_RESOLVE_MOCK_TARGET = (
    "src.lib.permissions.workspace._resolve_tool_access_control_config"
)


def _make_tac_config(rules):
    return {"path_validation": rules}


def _mock_resolve_factory(tac_cfg: dict):
    def _mock():
        return tac_cfg
    return _mock


def _build_tree(tmp_path):
    """Create a test directory tree with normal and excluded files.

    Structure:
      ws/
        src/
          main.py
          utils.py
        secrets/
          key.pem
          config.py
        build/
          output.js
    """
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "secrets").mkdir(parents=True)
    (ws / "build").mkdir(parents=True)
    (ws / "src" / "main.py").write_text("print('hello')")
    (ws / "src" / "utils.py").write_text("def helper(): pass")
    (ws / "secrets" / "key.pem").write_text("-----BEGIN KEY-----")
    (ws / "secrets" / "config.py").write_text("password = 'secret'")
    (ws / "build" / "output.js").write_text("console.log('built')")
    return ws


# ===========================================================================
# Python fallback filtering
# ===========================================================================

class TestFilterExcludedPaths:
    """_filter_excluded_paths removes files in excluded directories."""

    def test_excludes_configured_directory(self, monkeypatch):
        """Files in excluded directories are removed."""
        rules = [{"tools": ["glob_search"], "exclude_paths": ["secrets"]}]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        files = ["src/main.py", "src/utils.py", "secrets/key.pem", "secrets/config.py"]
        filtered = _filter_excluded_paths(files)
        assert "src/main.py" in filtered
        assert "src/utils.py" in filtered
        assert not any("secrets" in f for f in filtered)

    def test_excludes_multiple_directories(self, monkeypatch):
        """Multiple exclude_paths all take effect."""
        rules = [{"tools": ["glob_search"], "exclude_paths": ["secrets", "build"]}]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        files = ["src/main.py", "secrets/key.pem", "build/output.js"]
        filtered = _filter_excluded_paths(files)
        assert filtered == ["src/main.py"]

    def test_wildcard_rule_applies(self, monkeypatch):
        """Wildcard '*' tool rule excludes directories."""
        rules = [{"tools": ["*"], "exclude_paths": ["secrets"]}]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        files = ["src/main.py", "secrets/config.py"]
        filtered = _filter_excluded_paths(files)
        assert not any("secrets" in f for f in filtered)

    def test_no_config_returns_all_files(self, monkeypatch):
        """Without exclude config, all files are returned (except VCS dirs)."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory({}),
        )
        files = ["src/main.py", "secrets/config.py"]
        filtered = _filter_excluded_paths(files)
        assert "src/main.py" in filtered
        assert "secrets/config.py" in filtered  # not excluded

    def test_unrelated_tool_rule_ignored(self, monkeypatch):
        """Rules for unrelated tools do not affect filtering."""
        rules = [{"tools": ["read_file"], "exclude_paths": ["secrets"]}]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        files = ["src/main.py", "secrets/config.py"]
        filtered = _filter_excluded_paths(files)
        assert "secrets/config.py" in filtered  # not excluded

    def test_vcs_dirs_always_excluded(self, monkeypatch):
        """VCS directories (.git, node_modules, etc.) are always excluded."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory({}),
        )
        files = ["src/main.py", ".git/config", "node_modules/pkg/index.js"]
        filtered = _filter_excluded_paths(files)
        assert filtered == ["src/main.py"]


# ===========================================================================
# Integration: glob_search end-to-end
# ===========================================================================

class TestGlobSearchExcludeIntegration:
    """End-to-end glob_search with exclude_paths."""

    def test_glob_search_excludes_directory(self, monkeypatch, tmp_path):
        """glob_search() output should not contain excluded directory files."""
        ws = _build_tree(tmp_path)
        rules = [{"tools": ["glob_search"], "exclude_paths": ["secrets"]}]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = glob_search("**/*.py", path=str(ws))
        assert "main.py" in result
        assert "secrets" not in result

    def test_glob_search_no_exclusion_shows_all(self, monkeypatch, tmp_path):
        """Without exclusion rules, secrets files appear in results."""
        ws = _build_tree(tmp_path)
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory({}),
        )
        result = glob_search("**/*.py", path=str(ws))
        assert "main.py" in result
        # secrets/config.py should be visible
        assert "config.py" in result


# ===========================================================================
# Consistency with grep_tool
# ===========================================================================

class TestGlobGrepConsistency:
    """Glob and grep should use the same exclude patterns."""

    def test_both_use_same_shared_util(self):
        """Both tools import from search_utils, not independent implementations."""
        from src.tools.search import search_utils
        import src.tools.search.grep_tool.grep_tool as grep_mod
        import src.tools.search.glob_tool.glob_tool as glob_mod

        # Both tools should import get_search_exclude_patterns from search_utils
        assert grep_mod.get_search_exclude_patterns is search_utils.get_search_exclude_patterns
        assert glob_mod.get_search_exclude_patterns is search_utils.get_search_exclude_patterns
