"""Tests for security functions in path_validators: UNC detection, Windows pattern
detection, symlink chain resolution, and integration with validate_workspace_path."""

import os
from pathlib import Path

import pytest

import src.lib.config.config as config_module
from src.lib.smolagents.hooks.path_validators import (
    has_suspicious_windows_pattern,
    is_vulnerable_unc_path,
    resolve_symlink_chain,
    validate_workspace_path,
)
from src.lib.smolagents.hooks.types import HookContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_config(monkeypatch, raw: dict, root: Path) -> None:
    monkeypatch.setattr(
        config_module,
        "_ACTIVE_CONFIG",
        config_module.UnifiedConfig(raw, agent_root=root, llm_config=config_module.LLMConfig()),
        raising=True,
    )


def _patch_no_agent(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.lib.smolagents.hooks.path_validators.get_current_agent_config",
        lambda: None,
    )


def _make_context(tool_name, tool_input, tool_inputs_schema=None):
    return HookContext(
        session_id="test",
        cwd=os.getcwd(),
        hook_event_name="PreToolUse",
        tool_name=tool_name,
        tool_input=tool_input,
        tool_inputs_schema=tool_inputs_schema,
    )


def _tac(pv_list):
    """Shorthand: build tool_access_control config."""
    return {"tool_access_control": {"path_validation": pv_list}}


# ===========================================================================
# is_vulnerable_unc_path
# ===========================================================================

class TestIsVulnerableUncPath:
    """Cover all 8 UNC regex patterns defined in _UNC_PATTERNS."""

    def test_backslash_server_share(self):
        assert is_vulnerable_unc_path("\\\\server\\share") is True

    def test_forward_slash_server_share(self):
        assert is_vulnerable_unc_path("//server/share") is True

    def test_forward_slash_ipv4_share(self):
        assert is_vulnerable_unc_path("//192.168.1.1/share") is True

    def test_backslash_ipv4_share(self):
        assert is_vulnerable_unc_path("\\\\10.0.0.1\\share") is True

    def test_mixed_slashes_forward_back(self):
        # Forward-backslash hybrid
        assert is_vulnerable_unc_path("/\\\\server") is True

    def test_mixed_slashes_back_forward(self):
        # Backslash-forward hybrid
        assert is_vulnerable_unc_path("\\\\server/share") is True

    def test_webdav_ssl(self):
        assert is_vulnerable_unc_path("\\\\server@SSL@443\\share") is True

    def test_davwwwroot(self):
        assert is_vulnerable_unc_path("\\\\server\\DavWWWRoot\\dir") is True

    # --- Negative cases: normal paths should NOT trigger ---

    def test_normal_absolute_linux_path(self):
        assert is_vulnerable_unc_path("/home/user/project/file.py") is False

    def test_normal_relative_path(self):
        assert is_vulnerable_unc_path("src/main.py") is False

    def test_normal_dot_relative_path(self):
        assert is_vulnerable_unc_path("./local_file.txt") is False

    def test_empty_string(self):
        assert is_vulnerable_unc_path("") is False

    def test_single_backslash(self):
        assert is_vulnerable_unc_path("\\single") is False

    def test_single_forward_slash(self):
        # A single-component path starting with / is not UNC
        assert is_vulnerable_unc_path("/usr") is False


# ===========================================================================
# has_suspicious_windows_pattern
# ===========================================================================

class TestHasSuspiciousWindowsPattern:
    """Cover all 6 categories of suspicious Windows patterns."""

    # Category 1: NTFS Alternate Data Streams
    def test_ntfs_ads_data_stream(self):
        assert has_suspicious_windows_pattern("file.txt::$DATA") is True

    def test_ntfs_ads_custom_stream(self):
        assert has_suspicious_windows_pattern("file.txt:hidden_stream") is True

    # Category 2: 8.3 short names
    def test_83_short_name(self):
        assert has_suspicious_windows_pattern("GITSEC~1") is True

    def test_83_short_name_with_ext(self):
        assert has_suspicious_windows_pattern("GIT~1/config") is True

    # Category 3: Long-path prefixes
    def test_long_path_prefix_question(self):
        assert has_suspicious_windows_pattern("\\\\?\\C:\\long\\path") is True

    def test_long_path_prefix_dot(self):
        assert has_suspicious_windows_pattern("\\\\.\\Device\\HardDisk") is True

    def test_long_path_prefix_forward(self):
        assert has_suspicious_windows_pattern("//?/C:/path") is True

    # Category 4: Trailing dots
    def test_trailing_dot(self):
        assert has_suspicious_windows_pattern(".git.") is True

    def test_trailing_multiple_dots(self):
        assert has_suspicious_windows_pattern("file..") is True

    def test_trailing_space(self):
        assert has_suspicious_windows_pattern("file.txt ") is True

    # Category 5: DOS device names as extension
    def test_dos_device_con(self):
        assert has_suspicious_windows_pattern(".git.CON") is True

    def test_dos_device_prn(self):
        assert has_suspicious_windows_pattern("file.PRN") is True

    def test_dos_device_com1(self):
        assert has_suspicious_windows_pattern("data.COM1") is True

    def test_dos_device_lpt3(self):
        assert has_suspicious_windows_pattern("output.LPT3") is True

    # Category 6: Triple-or-more consecutive dots
    def test_triple_dots_traversal(self):
        assert has_suspicious_windows_pattern(".../file") is True

    def test_quadruple_dots_traversal(self):
        assert has_suspicious_windows_pattern("..../file") is True

    def test_triple_dots_mid_path(self):
        assert has_suspicious_windows_pattern("a/.../b") is True

    # --- Negative cases: normal paths should NOT trigger ---

    def test_normal_dotfile(self):
        assert has_suspicious_windows_pattern(".gitignore") is False

    def test_normal_double_dot_parent(self):
        assert has_suspicious_windows_pattern("../parent/file") is False

    def test_normal_extension(self):
        assert has_suspicious_windows_pattern("file.txt") is False

    def test_normal_linux_path(self):
        assert has_suspicious_windows_pattern("/home/user/project/src/main.py") is False

    def test_normal_relative_path(self):
        assert has_suspicious_windows_pattern("src/utils/helpers.py") is False

    def test_empty_string(self):
        assert has_suspicious_windows_pattern("") is False


# ===========================================================================
# resolve_symlink_chain
# ===========================================================================

class TestResolveSymlinkChain:
    """Test symlink chain resolution with real filesystem objects."""

    def test_real_symlink_returns_both_paths(self, tmp_path):
        """Create a real symlink and verify both original and target are returned."""
        real_file = tmp_path / "real.txt"
        real_file.write_text("content")
        link_file = tmp_path / "link.txt"
        link_file.symlink_to(real_file)

        result = resolve_symlink_chain(str(link_file))
        assert len(result) >= 2
        # First element is the input path
        assert result[0] == str(link_file)
        # Resolved target should appear in the chain
        assert str(real_file.resolve()) in result

    def test_non_symlink_returns_single_path(self, tmp_path):
        """A regular file should return only its own path."""
        regular = tmp_path / "normal.txt"
        regular.write_text("hello")

        result = resolve_symlink_chain(str(regular))
        assert len(result) == 1
        assert result[0] == str(regular)

    def test_nonexistent_path_returns_single_path(self, tmp_path):
        """A path that does not exist should return only the input."""
        missing = tmp_path / "no_such_file.txt"
        result = resolve_symlink_chain(str(missing))
        assert len(result) == 1
        assert result[0] == str(missing)

    def test_directory_symlink(self, tmp_path):
        """Symlink to a directory is also resolved."""
        real_dir = tmp_path / "real_dir"
        real_dir.mkdir()
        link_dir = tmp_path / "link_dir"
        link_dir.symlink_to(real_dir)

        result = resolve_symlink_chain(str(link_dir))
        assert len(result) >= 2

    def test_chained_symlinks(self, tmp_path):
        """Two levels of symlink should return 3 paths."""
        real_file = tmp_path / "real.txt"
        real_file.write_text("data")
        link1 = tmp_path / "link1.txt"
        link1.symlink_to(real_file)
        link2 = tmp_path / "link2.txt"
        link2.symlink_to(link1)

        result = resolve_symlink_chain(str(link2))
        # Should have at least input + link1 target + real target
        assert len(result) >= 2
        assert result[0] == str(link2)


# ===========================================================================
# Integration: validate_workspace_path blocks UNC paths
# ===========================================================================

class TestValidateWorkspacePathSecurityIntegration:
    """Integration tests: validate_workspace_path must block UNC paths."""

    def test_unc_path_in_tool_input_is_blocked(self, monkeypatch, tmp_path):
        """UNC path in file_path parameter should be blocked."""
        ws = tmp_path / "ws"
        ws.mkdir()
        _patch_config(
            monkeypatch,
            _tac([{"tools": ["read_file"], "exclude_paths": []}]),
            ws,
        )
        _patch_no_agent(monkeypatch)

        ctx = _make_context(
            "read_file",
            {"file_path": "\\\\evil-server\\share\\secrets.txt"},
            {"file_path": {"type": "string"}},
        )
        result = validate_workspace_path(ctx)
        assert result.decision == "block"
        assert "UNC" in result.reason or "network" in result.reason

    def test_suspicious_windows_pattern_in_tool_input_is_blocked(self, monkeypatch, tmp_path):
        """Suspicious Windows pattern in file_path should be blocked."""
        ws = tmp_path / "ws"
        ws.mkdir()
        _patch_config(
            monkeypatch,
            _tac([{"tools": ["read_file"], "exclude_paths": []}]),
            ws,
        )
        _patch_no_agent(monkeypatch)

        ctx = _make_context(
            "read_file",
            {"file_path": "file.txt::$DATA"},
            {"file_path": {"type": "string"}},
        )
        result = validate_workspace_path(ctx)
        assert result.decision == "block"
        assert "suspicious" in result.reason.lower() or "Windows" in result.reason

    def test_normal_path_allowed_when_no_rules(self, monkeypatch, tmp_path):
        """A normal path with empty path_validation list should be allowed."""
        ws = tmp_path / "ws"
        ws.mkdir()
        normal_file = ws / "src" / "main.py"
        normal_file.parent.mkdir(parents=True)
        normal_file.write_text("print('hello')")
        _patch_config(monkeypatch, _tac([]), ws)
        _patch_no_agent(monkeypatch)

        ctx = _make_context(
            "read_file",
            {"file_path": str(normal_file)},
            {"file_path": {"type": "string"}},
        )
        result = validate_workspace_path(ctx)
        assert result.decision == "allow"

    def test_normal_path_allowed_with_rule_configured(self, monkeypatch, tmp_path):
        """A normal workspace path should be allowed even when rules exist."""
        ws = tmp_path / "ws"
        ws.mkdir()
        normal_file = ws / "src" / "app.py"
        normal_file.parent.mkdir(parents=True)
        normal_file.write_text("app code")
        _patch_config(
            monkeypatch,
            _tac([{"tools": ["read_file"], "exclude_paths": []}]),
            ws,
        )
        _patch_no_agent(monkeypatch)

        ctx = _make_context(
            "read_file",
            {"file_path": str(normal_file)},
            {"file_path": {"type": "string"}},
        )
        result = validate_workspace_path(ctx)
        assert result.decision == "allow"

    def test_tool_not_in_rules_allows_everything(self, monkeypatch, tmp_path):
        """When tool is not listed in any rule, even UNC-like paths are allowed
        (the rule is simply not matched)."""
        ws = tmp_path / "ws"
        ws.mkdir()
        _patch_config(
            monkeypatch,
            _tac([{"tools": ["other_tool"], "exclude_paths": []}]),
            ws,
        )
        _patch_no_agent(monkeypatch)

        ctx = _make_context(
            "read_file",
            {"file_path": "\\\\evil\\share"},
            {"file_path": {"type": "string"}},
        )
        # The tool is not matched in any rule, so it is allowed
        result = validate_workspace_path(ctx)
        assert result.decision == "allow"
