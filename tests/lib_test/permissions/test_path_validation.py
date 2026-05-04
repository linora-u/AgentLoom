"""Tests for the shared path validation library.

Covers:
- Normal: workspace paths, include_paths, relative resolution, tilde, result fields
- Abnormal: outside workspace, UNC, Windows patterns, exclude, include/exclude conflict, errors
- Boundary: symlink escape/within, dot-dot, empty, workspace root, file:// protocol, triple-dot
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch

from src.lib.permissions.path_validation import (
    PathValidationResult,
    has_suspicious_windows_pattern,
    is_vulnerable_unc_path,
    resolve_symlink_chain,
    validate_path,
)

# =========================================================================
# Helpers
# =========================================================================

class _MockedWorkspace:
    """Context manager that mocks workspace root, include and exclude paths.

    Configures the workspace module by patching ``get_workspace_root``,
    ``get_allowed_directories``, ``get_rule_exclude_paths``.

    Since ``validate_path()`` only queries ``get_rule_exclude_paths(tool_name)``
    when ``tool_name`` is provided, we also store exclude_paths so that tests
    can pass them via ``extra_exclude`` when calling ``validate_path()`` directly.
    """
    def __init__(self, ws_path, include_paths=None, exclude_paths=None):
        self.exclude_paths = exclude_paths or []
        allowed = [Path(ws_path).resolve()]
        if include_paths:
            allowed.extend(Path(p).resolve() for p in include_paths)
        self.patches = [
            patch(
                "src.lib.permissions.workspace.get_workspace_root",
                return_value=Path(ws_path).resolve(),
            ),
            patch(
                "src.lib.permissions.workspace.get_allowed_directories",
                return_value=allowed,
            ),
            patch(
                "src.lib.permissions.workspace.get_rule_exclude_paths",
                return_value=self.exclude_paths,
            ),
        ]
        self.mocks = []

    def __enter__(self):
        self.mocks = [p.start() for p in self.patches]
        return self

    def __exit__(self, *args):
        for p in self.patches:
            p.stop()


# =========================================================================
# Normal paths (6 cases)
# =========================================================================

class TestValidatePathNormal:
    """Paths within allowed boundaries should be allowed."""

    def test_within_workspace(self, tmp_path):
        """Path inside workspace root → allowed."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "file.txt").write_text("hello")
        with _MockedWorkspace(ws):
            result = validate_path(str(ws / "file.txt"))
            assert result.allowed is True
            assert result.reason == ""

    def test_with_global_include_paths(self, tmp_path):
        """Path inside global include_paths → allowed."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        ext = tmp_path / "external"
        ext.mkdir()
        (ext / "data.csv").write_text("col1,col2")
        with _MockedWorkspace(ws, include_paths=[str(ext)]):
            result = validate_path(str(ext / "data.csv"))
            assert result.allowed is True

    def test_with_extra_include(self, tmp_path):
        """Path inside extra_include (per-rule) → allowed."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        extra = tmp_path / "extra"
        extra.mkdir()
        (extra / "file.py").write_text("code")
        with _MockedWorkspace(ws, include_paths=[str(extra)]):
            result = validate_path(str(extra / "file.py"), extra_include=[str(extra)])
            assert result.allowed is True

    def test_relative_resolution(self, tmp_path):
        """Relative path resolves against cwd."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "src").mkdir()
        with _MockedWorkspace(ws):
            # Patch cwd to ws
            with patch("os.getcwd", return_value=str(ws)):
                result = validate_path("src")
                assert result.allowed is True

    def test_tilde_expansion(self, tmp_path, monkeypatch):
        """~/path correctly expands to home directory."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        home = tmp_path / "fakehome"
        home.mkdir()
        (home / "file.txt").write_text("content")
        monkeypatch.setenv("HOME", str(home))
        with _MockedWorkspace(ws, include_paths=[str(home)]):
            result = validate_path("~/file.txt")
            assert result.allowed is True

    def test_result_has_resolved_path(self, tmp_path):
        """Successful result contains the resolved Path object."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "file.py").write_text("code")
        with _MockedWorkspace(ws):
            result = validate_path(str(ws / "file.py"))
            assert result.resolved_path is not None
            assert result.resolved_path == (ws / "file.py").resolve()


# =========================================================================
# Abnormal paths (6 cases)
# =========================================================================

class TestValidatePathAbnormal:
    """Paths that should be blocked."""

    def test_outside_workspace_blocked(self, tmp_path):
        """Path outside workspace and include_paths → blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        with _MockedWorkspace(ws):
            result = validate_path(str(outside / "file.txt"))
            assert result.allowed is False
            assert "outside allowed directories" in result.reason

    def test_unc_blocked(self):
        """UNC path → blocked with UNC reason."""
        result = validate_path("\\\\server\\share\\file.txt")
        assert result.allowed is False
        assert "UNC" in result.reason

    def test_windows_pattern_blocked(self):
        """Windows special pattern → blocked."""
        result = validate_path("C:\\file.txt:hidden_stream")
        assert result.allowed is False
        assert "suspicious Windows" in result.reason

    def test_exclude_paths_blocked(self, tmp_path):
        """Path in exclude_paths → blocked."""
        ws = tmp_path / "workspace"
        (ws / "secrets").mkdir(parents=True)
        (ws / "secrets" / "key.pem").write_text("secret")
        with _MockedWorkspace(ws, exclude_paths=["secrets"]):
            result = validate_path(
                str(ws / "secrets" / "key.pem"),
                extra_exclude=["secrets"],
            )
            assert result.allowed is False
            assert "excluded" in result.reason

    def test_include_exclude_conflict(self, tmp_path):
        """Path in both include_paths and exclude_paths → exclude wins."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        shared = tmp_path / "shared"
        (shared / "secrets").mkdir(parents=True)
        (shared / "secrets" / "key.pem").write_text("secret")
        with _MockedWorkspace(
            ws,
            include_paths=[str(shared)],
            exclude_paths=[str(shared / "secrets")],
        ):
            result = validate_path(
                str(shared / "secrets" / "key.pem"),
                extra_exclude=[str(shared / "secrets")],
            )
            assert result.allowed is False
            assert "excluded" in result.reason

    def test_exception_handling(self):
        """Invalid input should not crash, returns blocked."""
        # Patch to force an error during path resolution
        with patch("src.lib.permissions.workspace.get_allowed_directories",
                    side_effect=RuntimeError("boom")):
            with patch("src.lib.permissions.workspace.get_workspace_root",
                       return_value=Path("/fake")):
                with patch("src.lib.permissions.workspace.get_rule_exclude_paths",
                           return_value=[]):
                    # The function should handle the error gracefully
                    # It may raise or return blocked, but NOT return allowed=True
                    try:
                        result = validate_path("/some/path")
                        assert result.allowed is False
                    except RuntimeError:
                        pass  # Also acceptable — error propagated


# =========================================================================
# Boundary conditions (7 cases)
# =========================================================================

class TestValidatePathBoundary:
    """Edge cases and boundary conditions."""

    def test_symlink_escape_blocked(self, tmp_path):
        """Symlink inside workspace pointing outside → blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        link = ws / "escape"
        link.symlink_to(outside)
        with _MockedWorkspace(ws):
            result = validate_path(str(link / "secret.txt"))
            assert result.allowed is False

    def test_symlink_within_workspace(self, tmp_path):
        """Symlink inside workspace pointing to workspace → allowed."""
        ws = tmp_path / "workspace"
        (ws / "real").mkdir(parents=True)
        (ws / "real" / "file.txt").write_text("content")
        link = ws / "link"
        link.symlink_to(ws / "real")
        with _MockedWorkspace(ws):
            result = validate_path(str(link / "file.txt"))
            assert result.allowed is True

    def test_dot_dot_traversal(self, tmp_path):
        """../../etc/passwd correctly normalised → blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _MockedWorkspace(ws):
            with patch("os.getcwd", return_value=str(ws)):
                result = validate_path("../../etc/passwd")
                assert result.allowed is False

    def test_empty_string(self):
        """Empty path → blocked."""
        result = validate_path("")
        assert result.allowed is False
        assert "empty" in result.reason.lower()

    def test_workspace_root_itself(self, tmp_path):
        """Workspace root path → allowed."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _MockedWorkspace(ws):
            result = validate_path(str(ws))
            assert result.allowed is True

    def test_file_uri_protocol(self, tmp_path):
        """file:///path protocol is stripped correctly."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "file.py").write_text("code")
        with _MockedWorkspace(ws):
            result = validate_path(f"file://{ws / 'file.py'}")
            assert result.allowed is True

    def test_triple_dot_blocked(self):
        """Triple-dot path component → blocked (Windows trick)."""
        result = validate_path("/some/.../path")
        assert result.allowed is False
        assert "suspicious Windows" in result.reason


# =========================================================================
# Security function unit tests
# =========================================================================

class TestSecurityFunctions:
    """Direct tests for UNC, Windows, and symlink detection functions."""

    @pytest.mark.parametrize("path", [
        "\\\\server\\share",
        "//server/share",
        "\\\\192.168.1.1\\share",
        "//192.168.1.1/share",
    ])
    def test_unc_detection(self, path):
        assert is_vulnerable_unc_path(path) is True

    def test_unc_safe_paths(self):
        assert is_vulnerable_unc_path("/normal/path") is False
        assert is_vulnerable_unc_path("relative/path") is False

    @pytest.mark.parametrize("path", [
        "file.txt:hidden",       # ADS
        "PROGRA~1",              # 8.3 short name
        "\\\\?\\C:\\long",       # Long path prefix
        "//?/C:/long",           # Long path prefix (forward slash)
        "path/with/trailing. ",  # Trailing dots/spaces
        "file.CON",              # DOS device name
        "/some/.../path",        # Triple dots
    ])
    def test_windows_pattern_detection(self, path):
        assert has_suspicious_windows_pattern(path) is True

    def test_windows_safe_paths(self):
        assert has_suspicious_windows_pattern("/normal/file.txt") is False
        assert has_suspicious_windows_pattern("src/main.py") is False

    def test_symlink_chain_nonexistent(self, tmp_path):
        """Non-existent path returns just the input."""
        result = resolve_symlink_chain(str(tmp_path / "nonexistent"))
        assert result == [str(tmp_path / "nonexistent")]

    def test_symlink_chain_real_file(self, tmp_path):
        """Real file (not symlink) returns just the input."""
        f = tmp_path / "real.txt"
        f.write_text("content")
        result = resolve_symlink_chain(str(f))
        assert len(result) == 1

    def test_symlink_chain_follows_link(self, tmp_path):
        """Symlink chain is followed correctly."""
        target = tmp_path / "target.txt"
        target.write_text("content")
        link = tmp_path / "link.txt"
        link.symlink_to(target)
        result = resolve_symlink_chain(str(link))
        assert len(result) >= 2  # original + resolved target


# =========================================================================
# Glob / wildcard validate_path tests (NEW)
# =========================================================================

class TestValidatePathGlob:
    """Tests for glob, wildcard, and special characters in validate_path."""

    def test_wildcard_include_allows_any(self, tmp_path):
        """include_paths=["*"] allows access to any path."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        outside = tmp_path / "anywhere"
        outside.mkdir()
        (outside / "file.txt").write_text("data")
        with _MockedWorkspace(ws):
            with patch("src.lib.permissions.workspace.get_allowed_directories",
                       return_value=[Path("*")]):
                result = validate_path(str(outside / "file.txt"))
                assert result.allowed is True

    def test_wildcard_exclude_blocks_all(self, tmp_path):
        """exclude_paths=["*"] blocks everything."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "file.txt").write_text("data")
        result = validate_path(
            str(ws / "file.txt"),
            extra_exclude=["*"],
        )
        assert result.allowed is False
        assert "wildcard" in result.reason.lower()

    def test_glob_exclude_pattern(self, tmp_path):
        """Glob pattern in exclude blocks matching path."""
        ws = tmp_path / "workspace"
        (ws / "logs").mkdir(parents=True)
        (ws / "logs" / "app.log").write_text("log")
        with _MockedWorkspace(ws):
            result = validate_path(
                str(ws / "logs" / "app.log"),
                extra_exclude=[str(ws / "log*")],
            )
            assert result.allowed is False
            assert "excluded" in result.reason.lower()

    def test_exclude_priority_over_include(self, tmp_path):
        """When both include and exclude match, exclude wins."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "file.txt").write_text("data")
        with _MockedWorkspace(ws, include_paths=[str(shared)]):
            result = validate_path(
                str(shared / "file.txt"),
                extra_exclude=[str(shared)],
            )
            assert result.allowed is False

    def test_unicode_path(self, tmp_path):
        """Path with unicode characters validates correctly."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        udir = ws / "数据"
        udir.mkdir()
        (udir / "文件.txt").write_text("内容")
        with _MockedWorkspace(ws):
            result = validate_path(str(udir / "文件.txt"))
            assert result.allowed is True

    def test_space_in_path(self, tmp_path):
        """Path with spaces validates correctly."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        spaced = ws / "my project"
        spaced.mkdir()
        (spaced / "file.py").write_text("code")
        with _MockedWorkspace(ws):
            result = validate_path(str(spaced / "file.py"))
            assert result.allowed is True

    def test_deeply_nested_path(self, tmp_path):
        """Deeply nested path (50+ levels) validates correctly."""
        ws = tmp_path / "workspace"
        current = ws
        for i in range(50):
            current = current / f"level{i}"
        current.mkdir(parents=True)
        (current / "deep.txt").write_text("deep")
        with _MockedWorkspace(ws):
            result = validate_path(str(current / "deep.txt"))
            assert result.allowed is True
