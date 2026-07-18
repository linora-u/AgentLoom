"""Tests for per-rule include_paths in path_validators.

Covers: tilde expansion, absolute paths outside workspace, empty include_paths,
include_paths + exclude_paths conflict, and symlink + include_paths combination.
"""

import os
from pathlib import Path

import src.lib.config.config as config_module
from src.lib.smolagents.hooks.path_validators import validate_workspace_path
from src.lib.smolagents.hooks.types import HookContext

# ---------------------------------------------------------------------------
# Helpers (same conventions as test_path_validators_security.py)
# ---------------------------------------------------------------------------


def _patch_config(monkeypatch, raw: dict, root: Path) -> None:
    monkeypatch.setattr(
        config_module,
        "_ACTIVE_CONFIG",
        config_module.UnifiedConfig(raw, agent_root=root, llm_config=config_module.LLMConfig()),
        raising=True,
    )
    # Ensure workspace module reads from the same config (bypass agent context)
    tac = raw.get("tool_access_control", {})
    monkeypatch.setattr(
        "src.lib.permissions.workspace._resolve_tool_access_control_config",
        lambda: tac,
    )


def _patch_no_agent(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.lib.smolagents.hooks.path_validators.get_current_agent_config",
        lambda: None,
    )


def _make_context(tool_name, tool_input, tool_inputs_schema=None):
    return HookContext(
        local_run_id="test",
        cwd=os.getcwd(),
        hook_event_name="PreToolUse",
        tool_name=tool_name,
        tool_input=tool_input,
        tool_inputs_schema=tool_inputs_schema,
    )


def _tac(pv_list):
    """Build tool_access_control config."""
    return {"tool_access_control": {"path_validation": pv_list}}


# ===========================================================================
# include_paths: absolute path outside workspace
# ===========================================================================


class TestIncludePathsAbsolute:
    """Absolute path outside workspace but listed in include_paths -> allow."""

    def test_outside_workspace_in_include_paths_allowed(self, monkeypatch, tmp_path):
        """File outside workspace is allowed when its directory is in include_paths."""
        ws = tmp_path / "ws"
        ws.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        ext_file = external / "data.csv"
        ext_file.write_text("col1,col2")

        _patch_config(
            monkeypatch,
            _tac(
                [
                    {
                        "tools": ["read_file"],
                        "exclude_paths": [],
                        "include_paths": [str(external)],
                    }
                ]
            ),
            ws,
        )
        _patch_no_agent(monkeypatch)

        ctx = _make_context(
            "read_file",
            {"file_path": str(ext_file)},
            {"file_path": {"type": "string"}},
        )
        result = validate_workspace_path(ctx)
        assert result.decision == "allow"

    def test_outside_workspace_not_in_include_paths_blocked(self, monkeypatch, tmp_path):
        """File outside workspace and NOT in include_paths -> block."""
        ws = tmp_path / "ws"
        ws.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        other_file = other / "secret.txt"
        other_file.write_text("secret")

        _patch_config(
            monkeypatch,
            _tac(
                [
                    {
                        "tools": ["read_file"],
                        "exclude_paths": [],
                        "include_paths": [],  # empty
                    }
                ]
            ),
            ws,
        )
        _patch_no_agent(monkeypatch)

        ctx = _make_context(
            "read_file",
            {"file_path": str(other_file)},
            {"file_path": {"type": "string"}},
        )
        result = validate_workspace_path(ctx)
        assert result.decision == "block"
        assert "outside" in result.reason.lower()


# ===========================================================================
# include_paths: tilde expansion
# ===========================================================================


class TestIncludePathsTilde:
    """include_paths with ~ are expanded to the user's home directory."""

    def test_tilde_expansion_allows_home_subdirectory(self, monkeypatch, tmp_path):
        """include_paths: ['~/external-proj'] should expand ~ to home dir."""
        ws = tmp_path / "ws"
        ws.mkdir()
        # Create a directory under the real home to simulate
        # Use a non-existent but realistic path pattern for testing
        # We test the expansion logic by creating a temp dir and using its absolute path
        external = tmp_path / "fake_home" / "external-proj"
        external.mkdir(parents=True)
        ext_file = external / "readme.md"
        ext_file.write_text("# External")

        # Patch expanduser to redirect ~ to our fake home
        monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path / "fake_home")))

        _patch_config(
            monkeypatch,
            _tac(
                [
                    {
                        "tools": ["read_file"],
                        "exclude_paths": [],
                        "include_paths": ["~/external-proj"],
                    }
                ]
            ),
            ws,
        )
        _patch_no_agent(monkeypatch)

        ctx = _make_context(
            "read_file",
            {"file_path": str(ext_file)},
            {"file_path": {"type": "string"}},
        )
        result = validate_workspace_path(ctx)
        assert result.decision == "allow"


# ===========================================================================
# include_paths + exclude_paths conflict
# ===========================================================================


class TestIncludeExcludeConflict:
    """When a path is in both include_paths and exclude_paths, exclude wins."""

    def test_exclude_takes_precedence_over_include(self, monkeypatch, tmp_path):
        """Path in include_paths but also in exclude_paths -> blocked."""
        ws = tmp_path / "ws"
        ws.mkdir()
        shared = ws / "shared"
        shared.mkdir()
        secret_in_shared = shared / "secret.txt"
        secret_in_shared.write_text("top secret")

        _patch_config(
            monkeypatch,
            _tac(
                [
                    {
                        "tools": ["read_file"],
                        "exclude_paths": ["shared"],
                        "include_paths": [str(shared)],
                    }
                ]
            ),
            ws,
        )
        _patch_no_agent(monkeypatch)

        ctx = _make_context(
            "read_file",
            {"file_path": str(secret_in_shared)},
            {"file_path": {"type": "string"}},
        )
        result = validate_workspace_path(ctx)
        assert result.decision == "block"
        assert "excluded" in result.reason.lower()


# ===========================================================================
# include_paths: empty list
# ===========================================================================


class TestIncludePathsEmpty:
    """Empty include_paths means only workspace files are allowed."""

    def test_empty_include_paths_workspace_file_allowed(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        f = ws / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("code")

        _patch_config(
            monkeypatch,
            _tac(
                [
                    {
                        "tools": ["read_file"],
                        "exclude_paths": [],
                        "include_paths": [],
                    }
                ]
            ),
            ws,
        )
        _patch_no_agent(monkeypatch)

        ctx = _make_context(
            "read_file",
            {"file_path": str(f)},
            {"file_path": {"type": "string"}},
        )
        result = validate_workspace_path(ctx)
        assert result.decision == "allow"


# ===========================================================================
# include_paths: multiple directories
# ===========================================================================


class TestIncludePathsMultiple:
    """Multiple include_paths directories are all honored."""

    def test_multiple_include_paths(self, monkeypatch, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        ext1 = tmp_path / "data1"
        ext1.mkdir()
        ext2 = tmp_path / "data2"
        ext2.mkdir()
        f1 = ext1 / "file1.csv"
        f1.write_text("a,b")
        f2 = ext2 / "file2.csv"
        f2.write_text("c,d")

        _patch_config(
            monkeypatch,
            _tac(
                [
                    {
                        "tools": ["read_file"],
                        "exclude_paths": [],
                        "include_paths": [str(ext1), str(ext2)],
                    }
                ]
            ),
            ws,
        )
        _patch_no_agent(monkeypatch)

        # File in ext1 -> allow
        ctx1 = _make_context(
            "read_file",
            {"file_path": str(f1)},
            {"file_path": {"type": "string"}},
        )
        assert validate_workspace_path(ctx1).decision == "allow"

        # File in ext2 -> allow
        ctx2 = _make_context(
            "read_file",
            {"file_path": str(f2)},
            {"file_path": {"type": "string"}},
        )
        assert validate_workspace_path(ctx2).decision == "allow"


# ===========================================================================
# Symlink + include_paths combination
# ===========================================================================


class TestSymlinkIncludePaths:
    """Symlinks resolving into include_paths directories should be allowed."""

    def test_symlink_to_include_path_allowed(self, monkeypatch, tmp_path):
        """Symlink in workspace pointing to file in include_paths dir -> allow."""
        ws = tmp_path / "ws"
        ws.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        real_file = external / "data.txt"
        real_file.write_text("data content")

        link_in_ws = ws / "link_to_data.txt"
        link_in_ws.symlink_to(real_file)

        _patch_config(
            monkeypatch,
            _tac(
                [
                    {
                        "tools": ["read_file"],
                        "exclude_paths": [],
                        "include_paths": [str(external)],
                    }
                ]
            ),
            ws,
        )
        _patch_no_agent(monkeypatch)

        ctx = _make_context(
            "read_file",
            {"file_path": str(link_in_ws)},
            {"file_path": {"type": "string"}},
        )
        result = validate_workspace_path(ctx)
        assert result.decision == "allow"

    def test_symlink_to_outside_no_include_blocked(self, monkeypatch, tmp_path):
        """Symlink resolving outside workspace with no include_paths -> block."""
        ws = tmp_path / "ws"
        ws.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        real_file = external / "secret.txt"
        real_file.write_text("secret")

        link_in_ws = ws / "link_to_secret.txt"
        link_in_ws.symlink_to(real_file)

        _patch_config(
            monkeypatch,
            _tac(
                [
                    {
                        "tools": ["read_file"],
                        "exclude_paths": [],
                        "include_paths": [],  # no external dirs allowed
                    }
                ]
            ),
            ws,
        )
        _patch_no_agent(monkeypatch)

        ctx = _make_context(
            "read_file",
            {"file_path": str(link_in_ws)},
            {"file_path": {"type": "string"}},
        )
        result = validate_workspace_path(ctx)
        assert result.decision == "block"
        assert "outside" in result.reason.lower()
