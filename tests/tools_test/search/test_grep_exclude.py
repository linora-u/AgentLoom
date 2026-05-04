"""Tests for search exclude pattern utilities in search_utils."""

import pytest

from src.tools.search.search_utils import (
    get_search_exclude_patterns as _get_search_exclude_patterns,
    get_python_exclude_dirs as _get_python_exclude_dirs,
    SKIP_DIRS as _SKIP_DIRS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# The grep_tool does a lazy import of _resolve_tool_access_control_config
# inside _get_search_exclude_patterns.  We mock it at its origin module so
# the import inside the function picks up the mock.
_RESOLVE_MOCK_TARGET = (
    "src.lib.permissions.workspace._resolve_tool_access_control_config"
)


def _make_tac_config(rules):
    """Build a tool_access_control config dict with path_validation rules."""
    return {"path_validation": rules}


def _mock_resolve_factory(tac_cfg: dict):
    """Return a function that replaces _resolve_tool_access_control_config."""
    def _mock():
        return tac_cfg
    return _mock


# ===========================================================================
# _get_search_exclude_patterns
# ===========================================================================

class TestGetSearchExcludePatterns:
    """Test ripgrep-compatible exclusion pattern generation."""

    def test_patterns_from_grep_search_rule(self, monkeypatch):
        """Rule targeting 'grep_search' produces correct patterns."""
        rules = [
            {
                "tools": ["grep_search"],
                "exclude_paths": ["secrets", "private/keys"],
            }
        ]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_search_exclude_patterns()
        assert "!**/secrets/**" in result
        assert "!**/private/keys/**" in result

    def test_patterns_from_wildcard_rule(self, monkeypatch):
        """Rule targeting '*' (wildcard) also produces patterns."""
        rules = [
            {
                "tools": ["*"],
                "exclude_paths": ["vendor"],
            }
        ]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_search_exclude_patterns()
        assert "!**/vendor/**" in result

    def test_patterns_from_glob_search_rule(self, monkeypatch):
        """Rule targeting 'glob_search' matches when tool_name is glob_search."""
        rules = [
            {
                "tools": ["glob_search"],
                "exclude_paths": ["build"],
            }
        ]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_search_exclude_patterns(tool_name="glob_search")
        assert "!**/build/**" in result

    def test_empty_config_returns_empty_list(self, monkeypatch):
        """No path_validation rules -> empty exclusion list."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory({}),
        )
        result = _get_search_exclude_patterns()
        assert result == []

    def test_empty_rules_list_returns_empty(self, monkeypatch):
        """Empty path_validation list -> empty exclusion list."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config([])),
        )
        result = _get_search_exclude_patterns()
        assert result == []

    def test_unrelated_tool_rule_ignored(self, monkeypatch):
        """Rules that don't match grep_search/glob_search/* are ignored."""
        rules = [
            {
                "tools": ["read_file"],
                "exclude_paths": ["secrets"],
            }
        ]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_search_exclude_patterns()
        assert result == []

    def test_trailing_slash_stripped(self, monkeypatch):
        """Trailing slashes in exclude_paths are stripped before formatting."""
        rules = [
            {
                "tools": ["grep_search"],
                "exclude_paths": ["logs/"],
            }
        ]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_search_exclude_patterns()
        assert "!**/logs/**" in result

    def test_duplicate_patterns_deduplicated(self, monkeypatch):
        """Duplicate exclude_paths across rules should be deduplicated."""
        rules = [
            {"tools": ["grep_search"], "exclude_paths": ["secrets"]},
            {"tools": ["*"], "exclude_paths": ["secrets"]},
        ]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_search_exclude_patterns()
        assert result.count("!**/secrets/**") == 1

    def test_whitespace_only_paths_ignored(self, monkeypatch):
        """Whitespace-only strings in exclude_paths are skipped."""
        rules = [
            {"tools": ["grep_search"], "exclude_paths": ["  ", "", "valid"]},
        ]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_search_exclude_patterns()
        assert "!**/valid/**" in result
        assert len(result) == 1

    def test_non_string_exclude_paths_ignored(self, monkeypatch):
        """Non-string items in exclude_paths are skipped."""
        rules = [
            {"tools": ["grep_search"], "exclude_paths": [123, None, "real"]},
        ]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_search_exclude_patterns()
        assert result == ["!**/real/**"]

    def test_invalid_rule_not_dict_ignored(self, monkeypatch):
        """Non-dict entries in rules list are ignored."""
        rules = ["not_a_dict", {"tools": ["grep_search"], "exclude_paths": ["ok"]}]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_search_exclude_patterns()
        assert result == ["!**/ok/**"]


# ===========================================================================
# _get_python_exclude_dirs
# ===========================================================================

class TestGetPythonExcludeDirs:
    """Test that Python fallback merges _SKIP_DIRS with configured excludes."""

    def test_default_skip_dirs_always_present(self, monkeypatch):
        """Even without config, all _SKIP_DIRS are present."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory({}),
        )
        result = _get_python_exclude_dirs()
        for d in _SKIP_DIRS:
            assert d in result

    def test_extra_dirs_merged_from_config(self, monkeypatch):
        """Configured exclude_paths are merged into the skip set."""
        rules = [
            {"tools": ["grep_search"], "exclude_paths": ["my_secrets", "temp_data"]},
        ]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_python_exclude_dirs()
        assert "my_secrets" in result
        assert "temp_data" in result
        # Original dirs still present
        assert ".git" in result
        assert "node_modules" in result

    def test_result_is_frozenset(self, monkeypatch):
        """Return type should be frozenset."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory({}),
        )
        result = _get_python_exclude_dirs()
        assert isinstance(result, frozenset)

    def test_empty_config_returns_default_only(self, monkeypatch):
        """No config -> result equals _SKIP_DIRS exactly."""
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory({}),
        )
        result = _get_python_exclude_dirs()
        assert result == _SKIP_DIRS

    def test_nested_path_extracts_top_directory(self, monkeypatch):
        """A nested exclude like 'deep/nested/dir' is cleaned to 'deep/nested/dir'."""
        rules = [
            {"tools": ["grep_search"], "exclude_paths": ["deep/nested/dir"]},
        ]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_python_exclude_dirs()
        assert "deep/nested/dir" in result


# ===========================================================================
# T5: _build_rg_args integration — verify exclude globs in ripgrep args
# ===========================================================================

class TestBuildRgArgsIntegration:
    """Verify that _build_rg_args includes configured exclude glob patterns."""

    def test_exclude_patterns_in_rg_args(self, monkeypatch):
        """Exclude patterns from config should appear as --glob args."""
        from src.tools.search.grep_tool.grep_tool import _build_rg_args, _RG_PATH
        if _RG_PATH is None:
            pytest.skip("ripgrep not available")

        rules = [{"tools": ["grep_search"], "exclude_paths": ["secrets", "build"]}]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        from pathlib import Path
        args = _build_rg_args(
            "pattern", Path("/tmp"), "", "content",
            True, 0, 0, 0, False,
        )
        # Check that --glob !secrets/** and --glob !build/** appear in args
        found_secrets = False
        found_build = False
        for i, arg in enumerate(args):
            if arg == "--glob" and i + 1 < len(args):
                if args[i + 1] == "!**/secrets/**":
                    found_secrets = True
                if args[i + 1] == "!**/build/**":
                    found_build = True
        assert found_secrets, "!secrets/** not in ripgrep args"
        assert found_build, "!build/** not in ripgrep args"

    def test_no_config_no_extra_globs(self, monkeypatch):
        """Without config, only VCS exclude globs should be present."""
        from src.tools.search.grep_tool.grep_tool import _build_rg_args, _RG_PATH
        if _RG_PATH is None:
            pytest.skip("ripgrep not available")

        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory({}),
        )
        from pathlib import Path
        args = _build_rg_args(
            "pattern", Path("/tmp"), "", "content",
            True, 0, 0, 0, False,
        )
        # Should have VCS globs but no custom exclude patterns
        custom_excludes = [
            args[i + 1] for i, arg in enumerate(args)
            if arg == "--glob" and i + 1 < len(args) and args[i + 1].startswith("!") and "git" not in args[i + 1] and "svn" not in args[i + 1] and "hg" not in args[i + 1] and "bzr" not in args[i + 1] and "jj" not in args[i + 1] and "sl" not in args[i + 1]
        ]
        assert custom_excludes == []


# ===========================================================================
# T10: _get_python_exclude_dirs edge cases
# ===========================================================================

class TestGetPythonExcludeDirsEdgeCases:
    """Edge cases for directory name extraction from exclude patterns."""

    def test_pattern_with_nested_glob_star(self, monkeypatch):
        """Pattern '!build/dist/**' extracts 'build/dist'."""
        rules = [{"tools": ["grep_search"], "exclude_paths": ["build/dist"]}]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_python_exclude_dirs()
        assert "build/dist" in result

    def test_single_dot_git_excluded(self, monkeypatch):
        """Pattern '!.git/**' -> '.git' already in SKIP_DIRS."""
        rules = [{"tools": ["grep_search"], "exclude_paths": [".git"]}]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_python_exclude_dirs()
        assert ".git" in result

    def test_empty_exclude_no_extras(self, monkeypatch):
        """Empty exclude_paths adds nothing beyond SKIP_DIRS."""
        rules = [{"tools": ["grep_search"], "exclude_paths": []}]
        monkeypatch.setattr(
            _RESOLVE_MOCK_TARGET,
            _mock_resolve_factory(_make_tac_config(rules)),
        )
        result = _get_python_exclude_dirs()
        assert result == _SKIP_DIRS
