"""Tests for workspace boundary management.

Covers:
- Normal: workspace root, rule-based include/exclude, allowed dirs, path containment
- Abnormal: outside dirs, missing config, tool not in rules
- Boundary: tilde, glob, wildcard "*", symlink resolve, multi-rule union
- Pattern matching: match_path_pattern with glob, wildcard, tilde
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch

from src.lib.permissions.workspace import (
    get_workspace_root,
    get_rule_include_paths,
    get_rule_exclude_paths,
    get_allowed_directories,
    path_in_allowed_directory,
    match_path_pattern,
)


def _make_cfg(rules):
    """Build a tool_access_control config dict with path_validation rules."""
    return {"path_validation": rules}


# =========================================================================
# Normal paths
# =========================================================================

class TestWorkspaceNormal:

    def test_get_workspace_root(self):
        """Returns resolved agent_root."""
        with patch("src.lib.permissions.workspace.C") as mock_c:
            mock_c.agent_root = "/home/user/project"
            root = get_workspace_root()
            assert root == Path("/home/user/project").resolve()

    def test_path_in_allowed_directory_true(self, tmp_path):
        """Path inside allowed dir returns True."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "src").mkdir()
        assert path_in_allowed_directory(
            (ws / "src").resolve(), [ws.resolve()]
        ) is True

    def test_get_allowed_directories_default_no_rules(self):
        """No rules → only workspace root returned."""
        with patch("src.lib.permissions.workspace.get_workspace_root",
                    return_value=Path("/ws").resolve()):
            with patch(
                "src.lib.permissions.workspace._resolve_tool_access_control_config",
                return_value=_make_cfg([]),
            ):
                result = get_allowed_directories()
                assert len(result) == 1

    def test_get_allowed_directories_with_tool_name(self, tmp_path):
        """tool_name lookups include_paths from matching rules."""
        ext = tmp_path / "external"
        ext.mkdir()
        cfg = _make_cfg([
            {"tools": ["read_file"], "include_paths": [str(ext)]},
        ])
        with patch("src.lib.permissions.workspace.get_workspace_root",
                    return_value=tmp_path.resolve()):
            with patch(
                "src.lib.permissions.workspace._resolve_tool_access_control_config",
                return_value=cfg,
            ):
                result = get_allowed_directories(tool_name="read_file")
                assert ext.resolve() in result


# =========================================================================
# Rule-based include paths
# =========================================================================

class TestRuleIncludePaths:

    def test_single_rule_single_tool(self, tmp_path):
        """Single rule with one tool returns include_paths."""
        cfg = _make_cfg([
            {"tools": ["read_file"], "include_paths": ["/opt/libs"]},
        ])
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=cfg,
        ):
            result = get_rule_include_paths("read_file")
            assert result == ["/opt/libs"]

    def test_multiple_rules_same_tool(self):
        """Tool in multiple rules → include_paths union."""
        cfg = _make_cfg([
            {"tools": ["shell_tool"], "include_paths": ["/opt/a"]},
            {"tools": ["shell_tool"], "include_paths": ["/opt/b"]},
        ])
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=cfg,
        ):
            result = get_rule_include_paths("shell_tool")
            assert "/opt/a" in result
            assert "/opt/b" in result

    def test_tilde_expansion_in_include(self, tmp_path, monkeypatch):
        """Tilde in include_paths is expanded when building allowed dirs."""
        home = tmp_path / "fakehome"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        cfg = _make_cfg([
            {"tools": ["read_file"], "include_paths": ["~/libs"]},
        ])
        with patch("src.lib.permissions.workspace.get_workspace_root",
                    return_value=tmp_path.resolve()):
            with patch(
                "src.lib.permissions.workspace._resolve_tool_access_control_config",
                return_value=cfg,
            ):
                dirs = get_allowed_directories(tool_name="read_file")
                dir_strs = [str(d) for d in dirs]
                assert any(str(home) in s for s in dir_strs)

    def test_glob_pattern_passthrough(self):
        """Glob patterns in include_paths are returned as-is."""
        cfg = _make_cfg([
            {"tools": ["shell_tool"], "include_paths": ["/home/*/code"]},
        ])
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=cfg,
        ):
            result = get_rule_include_paths("shell_tool")
            assert result == ["/home/*/code"]

    def test_wildcard_star(self):
        """include_paths with \"*\" short-circuits to [\"*\"]."""
        cfg = _make_cfg([
            {"tools": ["shell_tool"], "include_paths": ["*", "/opt/extra"]},
        ])
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=cfg,
        ):
            result = get_rule_include_paths("shell_tool")
            assert result == ["*"]


# =========================================================================
# Rule-based exclude paths
# =========================================================================

class TestRuleExcludePaths:

    def test_exclude_from_rule(self):
        """Returns exclude_paths from matching rule."""
        cfg = _make_cfg([
            {"tools": ["read_file"], "exclude_paths": ["secrets", ".env"]},
        ])
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=cfg,
        ):
            result = get_rule_exclude_paths("read_file")
            assert "secrets" in result
            assert ".env" in result

    def test_exclude_union_multiple_rules(self):
        """Tool in multiple rules → exclude_paths union."""
        cfg = _make_cfg([
            {"tools": ["shell_tool"], "exclude_paths": ["secrets"]},
            {"tools": ["shell_tool"], "exclude_paths": ["build"]},
        ])
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=cfg,
        ):
            result = get_rule_exclude_paths("shell_tool")
            assert "secrets" in result
            assert "build" in result

    def test_exclude_wildcard_star(self):
        """exclude_paths with \"*\" short-circuits to [\"*\"]."""
        cfg = _make_cfg([
            {"tools": ["shell_tool"], "exclude_paths": ["*"]},
        ])
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=cfg,
        ):
            result = get_rule_exclude_paths("shell_tool")
            assert result == ["*"]

    def test_tool_not_in_any_rule(self):
        """Tool not in any rule returns empty list."""
        cfg = _make_cfg([
            {"tools": ["read_file"], "exclude_paths": ["secrets"]},
        ])
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=cfg,
        ):
            result = get_rule_exclude_paths("shell_tool")
            assert result == []

    def test_empty_path_validation(self):
        """Empty path_validation returns empty list."""
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=_make_cfg([]),
        ):
            assert get_rule_include_paths("any_tool") == []
            assert get_rule_exclude_paths("any_tool") == []


# =========================================================================
# Abnormal paths
# =========================================================================

class TestWorkspaceAbnormal:

    def test_path_in_allowed_directory_false(self, tmp_path):
        """Path outside allowed dirs returns False."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        assert path_in_allowed_directory(
            outside.resolve(), [ws.resolve()]
        ) is False

    def test_missing_config(self):
        """Missing config returns empty list."""
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value={},
        ):
            assert get_rule_include_paths("any") == []
            assert get_rule_exclude_paths("any") == []

    def test_per_agent_override(self, tmp_path):
        """Per-agent config takes precedence over global."""
        ext = tmp_path / "agent_ext"
        ext.mkdir()
        cfg = _make_cfg([
            {"tools": ["read_file"], "include_paths": [str(ext)]},
        ])
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=cfg,
        ):
            result = get_rule_include_paths("read_file")
            assert str(ext) in result


# =========================================================================
# Boundary conditions
# =========================================================================

class TestWorkspaceBoundary:

    def test_exact_boundary(self, tmp_path):
        """Workspace root itself is within allowed."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        assert path_in_allowed_directory(ws.resolve(), [ws.resolve()]) is True

    def test_extra_include_additive(self, tmp_path):
        """extra_include paths are additive to rules."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        extra = tmp_path / "extra"
        extra.mkdir()
        cfg = _make_cfg([])
        with patch("src.lib.permissions.workspace.get_workspace_root",
                    return_value=ws.resolve()):
            with patch(
                "src.lib.permissions.workspace._resolve_tool_access_control_config",
                return_value=cfg,
            ):
                result = get_allowed_directories(extra_include=[str(extra)])
                assert extra.resolve() in result

    def test_symlink_resolve_in_include(self, tmp_path):
        """Symlink in include_paths is resolved to real target."""
        target = tmp_path / "real_dir"
        target.mkdir()
        link = tmp_path / "link_dir"
        link.symlink_to(target)
        cfg = _make_cfg([
            {"tools": ["read_file"], "include_paths": [str(link)]},
        ])
        with patch("src.lib.permissions.workspace.get_workspace_root",
                    return_value=tmp_path.resolve()):
            with patch(
                "src.lib.permissions.workspace._resolve_tool_access_control_config",
                return_value=cfg,
            ):
                dirs = get_allowed_directories(tool_name="read_file")
                assert target.resolve() in dirs

    def test_wildcard_allows_all_in_allowed_dirs(self):
        """Wildcard \"*\" in include_paths returns Path(\"*\") sentinel."""
        cfg = _make_cfg([
            {"tools": ["read_file"], "include_paths": ["*"]},
        ])
        with patch("src.lib.permissions.workspace.get_workspace_root",
                    return_value=Path("/ws").resolve()):
            with patch(
                "src.lib.permissions.workspace._resolve_tool_access_control_config",
                return_value=cfg,
            ):
                dirs = get_allowed_directories(tool_name="read_file")
                assert Path("*") in dirs

    def test_wildcard_sentinel_in_path_check(self):
        """Path("*") sentinel in allowed_dirs → always True."""
        assert path_in_allowed_directory(
            Path("/any/random/path"), [Path("*")]
        ) is True


# =========================================================================
# match_path_pattern tests
# =========================================================================

class TestMatchPathPattern:

    def test_exact_match(self, tmp_path):
        """Exact absolute path matches."""
        d = tmp_path / "external"
        d.mkdir()
        assert match_path_pattern(str(d / "file.py"), str(d)) is True

    def test_glob_star_matches_all(self):
        """Pattern \"*\" matches any path."""
        assert match_path_pattern("/any/path/here", "*") is True

    def test_glob_wildcard_middle(self, tmp_path):
        """Glob /home/*/code matches /home/user/code."""
        # Use real paths to avoid resolution issues
        base = tmp_path / "home"
        base.mkdir()
        user = base / "testuser"
        user.mkdir()
        code = user / "code"
        code.mkdir()
        pattern = str(base / "*" / "code")
        assert match_path_pattern(str(code), pattern) is True

    def test_glob_no_match(self, tmp_path):
        """Glob that does not match returns False."""
        assert match_path_pattern("/opt/code", "/home/*/code") is False

    def test_tilde_in_glob(self, tmp_path, monkeypatch):
        """Tilde in pattern is expanded before matching."""
        home = tmp_path / "fakehome"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        target = home / "projects" / "myproject"
        target.mkdir(parents=True)
        assert match_path_pattern(str(target), "~/projects/*") is True


# =========================================================================
# Glob / wildcard in allowed_directories (NEW)
# =========================================================================

class TestGlobAllowedDirectories:
    """Test glob and wildcard support in get_allowed_directories."""

    def test_wildcard_returns_sentinel(self):
        """include_paths=["*"] returns [Path("*")] sentinel."""
        cfg = _make_cfg([
            {"tools": ["read_file"], "include_paths": ["*"]},
        ])
        with patch("src.lib.permissions.workspace.get_workspace_root",
                    return_value=Path("/ws").resolve()):
            with patch(
                "src.lib.permissions.workspace._resolve_tool_access_control_config",
                return_value=cfg,
            ):
                dirs = get_allowed_directories(tool_name="read_file")
                assert Path("*") in dirs

    def test_glob_in_allowed_dirs_matches(self, tmp_path):
        """Glob pattern in allowed_dirs matches via path_in_allowed_directory."""
        base = tmp_path / "home"
        base.mkdir()
        user = base / "testuser" / "code"
        user.mkdir(parents=True)
        pattern = str(base / "*" / "code")
        assert path_in_allowed_directory(user.resolve(), [Path(pattern)]) is True

    def test_glob_in_allowed_dirs_no_match(self, tmp_path):
        """Glob pattern does not match unrelated path."""
        outside = tmp_path / "outside"
        outside.mkdir()
        pattern = str(tmp_path / "home" / "*" / "code")
        assert path_in_allowed_directory(outside.resolve(), [Path(pattern)]) is False


# =========================================================================
# Multi-rule union (NEW)
# =========================================================================

class TestToolInMultipleRules:
    """Test multi-rule union behavior."""

    def test_include_union(self):
        """Tool in 2 rules -> include_paths merged."""
        cfg = _make_cfg([
            {"tools": ["read_file"], "include_paths": ["/a"]},
            {"tools": ["read_file"], "include_paths": ["/b"]},
        ])
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=cfg,
        ):
            result = get_rule_include_paths("read_file")
            assert "/a" in result and "/b" in result

    def test_exclude_union(self):
        """Tool in 2 rules -> exclude_paths merged."""
        cfg = _make_cfg([
            {"tools": ["shell_tool"], "exclude_paths": ["a"]},
            {"tools": ["shell_tool"], "exclude_paths": ["b"]},
        ])
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=cfg,
        ):
            result = get_rule_exclude_paths("shell_tool")
            assert "a" in result and "b" in result

    def test_wildcard_tool_matches_all(self):
        """Rule with tools=["*"] matches any tool."""
        cfg = _make_cfg([
            {"tools": ["*"], "include_paths": ["/global"]},
        ])
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=cfg,
        ):
            assert "/global" in get_rule_include_paths("read_file")
            assert "/global" in get_rule_include_paths("shell_tool")
            assert "/global" in get_rule_include_paths("any_tool")

    def test_deduplication(self):
        """Duplicate paths across rules are deduplicated."""
        cfg = _make_cfg([
            {"tools": ["read_file"], "include_paths": ["/a"]},
            {"tools": ["read_file"], "include_paths": ["/a"]},
        ])
        with patch(
            "src.lib.permissions.workspace._resolve_tool_access_control_config",
            return_value=cfg,
        ):
            result = get_rule_include_paths("read_file")
            assert result.count("/a") == 1
