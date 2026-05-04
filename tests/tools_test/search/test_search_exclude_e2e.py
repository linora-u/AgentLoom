"""End-to-end tests for search exclude functionality.

These tests create REAL filesystem directories, configure exclude rules,
call the ACTUAL grep_search/glob_search functions, and verify that files
in excluded directories are TRULY absent from the results.

This is the critical layer of testing that validates the exclude patterns
actually work with ripgrep and Python fallback — not just that the pattern
strings are formatted correctly.
"""

import os
from pathlib import Path

import pytest

from src.tools.search.grep_tool.grep_tool import grep_search, _RG_PATH
from src.tools.search.glob_tool.glob_tool import glob_search


# ---------------------------------------------------------------------------
# Mock target for tool_access_control config
# ---------------------------------------------------------------------------
_RESOLVE_MOCK_TARGET = (
    "src.lib.permissions.workspace._resolve_tool_access_control_config"
)


def _tac(rules):
    return {"path_validation": rules}


def _mock_cfg(cfg):
    def _mock():
        return cfg
    return _mock


# ---------------------------------------------------------------------------
# Shared fixture: directory tree with excluded directories
# ---------------------------------------------------------------------------

@pytest.fixture
def search_tree(tmp_path):
    """Create a directory tree for testing search exclusion.

    Structure:
        ws/
        ├── src/
        │   ├── main.py          "password = 'abc123'"
        │   └── utils.py         "def helper(): pass"
        ├── secrets/
        │   ├── config.py        "password = 'SUPER_SECRET'"
        │   └── key.pem          "-----BEGIN KEY-----"
        ├── build/
        │   └── output.js        "console.log('password')"
        └── deep/
            └── nested/
                └── hidden/
                    └── data.txt  "password hidden deep"
    """
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "secrets").mkdir(parents=True)
    (ws / "build").mkdir(parents=True)
    (ws / "deep" / "nested" / "hidden").mkdir(parents=True)

    (ws / "src" / "main.py").write_text("password = 'abc123'\n")
    (ws / "src" / "utils.py").write_text("def helper(): pass\n")
    (ws / "secrets" / "config.py").write_text("password = 'SUPER_SECRET'\n")
    (ws / "secrets" / "key.pem").write_text("-----BEGIN KEY-----\n")
    (ws / "build" / "output.js").write_text("console.log('password')\n")
    (ws / "deep" / "nested" / "hidden" / "data.txt").write_text("password hidden deep\n")

    return ws


# ===========================================================================
# grep_search end-to-end: excluded directory files must NOT appear in results
# ===========================================================================

class TestGrepSearchExcludeE2E:
    """grep_search with real filesystem and exclude rules."""

    def test_secrets_excluded_from_grep_results(self, monkeypatch, search_tree):
        """Files in 'secrets/' must not appear in grep_search output."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg(_tac([{
                "tools": ["grep_search"],
                "exclude_paths": ["secrets"],
            }])),
        )
        result = grep_search("password", path=str(search_tree))

        # src/main.py should be found (contains "password")
        assert "main.py" in result
        # secrets/config.py must NOT appear
        assert "secrets" not in result
        assert "SUPER_SECRET" not in result

    def test_multiple_dirs_excluded(self, monkeypatch, search_tree):
        """Both 'secrets/' and 'build/' excluded simultaneously."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg(_tac([{
                "tools": ["grep_search"],
                "exclude_paths": ["secrets", "build"],
            }])),
        )
        result = grep_search("password", path=str(search_tree))

        assert "main.py" in result
        assert "secrets" not in result
        assert "build" not in result
        assert "SUPER_SECRET" not in result
        assert "console.log" not in result

    def test_no_exclude_shows_all_matches(self, monkeypatch, search_tree):
        """Without exclude rules, all files with matches appear."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg({}),
        )
        result = grep_search("password", path=str(search_tree))

        assert "main.py" in result
        # secrets should be visible when not excluded
        assert "config.py" in result or "SUPER_SECRET" in result

    def test_wildcard_rule_excludes_for_grep(self, monkeypatch, search_tree):
        """Wildcard '*' tool rule excludes directories for grep_search."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg(_tac([{
                "tools": ["*"],
                "exclude_paths": ["secrets"],
            }])),
        )
        result = grep_search("password", path=str(search_tree))

        assert "main.py" in result
        assert "secrets" not in result

    def test_unrelated_tool_rule_does_not_exclude(self, monkeypatch, search_tree):
        """Rules for 'read_file' should NOT affect grep_search."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg(_tac([{
                "tools": ["read_file"],
                "exclude_paths": ["secrets"],
            }])),
        )
        result = grep_search("password", path=str(search_tree))

        # secrets NOT excluded (rule doesn't target grep_search)
        assert "config.py" in result or "SUPER_SECRET" in result

    def test_nested_exclude_path(self, monkeypatch, search_tree):
        """Nested exclude path 'deep/nested/hidden' blocks deeply nested files."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg(_tac([{
                "tools": ["grep_search"],
                "exclude_paths": ["deep/nested/hidden"],
            }])),
        )
        result = grep_search("password", path=str(search_tree))

        assert "main.py" in result
        # deep/nested/hidden/data.txt should be excluded
        assert "hidden deep" not in result

    def test_files_with_matches_mode_excludes(self, monkeypatch, search_tree):
        """Exclude also works in files_with_matches output mode."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg(_tac([{
                "tools": ["grep_search"],
                "exclude_paths": ["secrets"],
            }])),
        )
        result = grep_search("password", path=str(search_tree),
                             output_mode="files_with_matches")

        assert "main.py" in result
        assert "secrets" not in result

    def test_count_mode_excludes(self, monkeypatch, search_tree):
        """Exclude also works in count output mode."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg(_tac([{
                "tools": ["grep_search"],
                "exclude_paths": ["secrets"],
            }])),
        )
        result = grep_search("password", path=str(search_tree),
                             output_mode="count")

        assert "main.py" in result
        assert "secrets" not in result


# ===========================================================================
# grep_search Python fallback: same exclude behavior without ripgrep
# ===========================================================================

class TestGrepSearchPythonFallbackExclude:
    """grep_search Python fallback (no ripgrep) must also exclude correctly."""

    def test_python_fallback_excludes_secrets(self, monkeypatch, search_tree):
        """When ripgrep is unavailable, Python fallback still excludes."""
        # Force Python fallback by hiding ripgrep
        monkeypatch.setattr(
            "src.tools.search.grep_tool.grep_tool._RG_PATH", None
        )
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg(_tac([{
                "tools": ["grep_search"],
                "exclude_paths": ["secrets"],
            }])),
        )
        result = grep_search("password", path=str(search_tree))

        assert "main.py" in result
        assert "secrets" not in result
        assert "SUPER_SECRET" not in result

    def test_python_fallback_no_exclude_shows_all(self, monkeypatch, search_tree):
        """Python fallback without exclude shows all matches."""
        monkeypatch.setattr(
            "src.tools.search.grep_tool.grep_tool._RG_PATH", None
        )
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg({}),
        )
        result = grep_search("password", path=str(search_tree))

        assert "main.py" in result
        assert "config.py" in result or "SUPER_SECRET" in result


# ===========================================================================
# glob_search end-to-end: excluded directory files must NOT appear
# ===========================================================================

class TestGlobSearchExcludeE2E:
    """glob_search with real filesystem and exclude rules."""

    def test_secrets_excluded_from_glob_results(self, monkeypatch, search_tree):
        """Files in 'secrets/' must not appear in glob_search output."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg(_tac([{
                "tools": ["glob_search"],
                "exclude_paths": ["secrets"],
            }])),
        )
        result = glob_search("**/*.py", path=str(search_tree))

        assert "main.py" in result
        assert "secrets" not in result

    def test_multiple_dirs_excluded_glob(self, monkeypatch, search_tree):
        """Both 'secrets/' and 'build/' excluded from glob results."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg(_tac([{
                "tools": ["glob_search"],
                "exclude_paths": ["secrets", "build"],
            }])),
        )
        result = glob_search("**/*", path=str(search_tree))

        assert "main.py" in result
        assert "secrets" not in result
        assert "build" not in result

    def test_no_exclude_shows_secrets_in_glob(self, monkeypatch, search_tree):
        """Without exclude, secrets files are visible."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg({}),
        )
        result = glob_search("**/*.py", path=str(search_tree))

        assert "main.py" in result
        assert "config.py" in result  # secrets/config.py visible

    def test_glob_python_fallback_excludes(self, monkeypatch, search_tree):
        """Python fallback glob also excludes correctly."""
        # Force Python fallback
        monkeypatch.setattr(
            "src.tools.search.glob_tool.glob_tool._RG_PATH", None
        )
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg(_tac([{
                "tools": ["glob_search"],
                "exclude_paths": ["secrets"],
            }])),
        )
        result = glob_search("**/*.py", path=str(search_tree), sort_by="name")

        assert "main.py" in result
        assert "secrets" not in result


# ===========================================================================
# Cross-tool consistency: same exclude config affects both grep and glob
# ===========================================================================

class TestCrossToolConsistency:
    """Same exclude_paths config must produce consistent results across tools."""

    def test_both_tools_exclude_same_dir(self, monkeypatch, search_tree):
        """grep_search and glob_search both exclude 'secrets/' with same config."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_cfg(_tac([{
                "tools": ["grep_search", "glob_search"],
                "exclude_paths": ["secrets"],
            }])),
        )

        grep_result = grep_search("password", path=str(search_tree))
        glob_result = glob_search("**/*.py", path=str(search_tree))

        # Both must exclude secrets
        assert "secrets" not in grep_result, "grep_search leaked secrets"
        assert "secrets" not in glob_result, "glob_search leaked secrets"

        # Both must include src/main.py
        assert "main.py" in grep_result
        assert "main.py" in glob_result
