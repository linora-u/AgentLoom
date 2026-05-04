"""Security-focused tests for shell path validation.

Covers:
- CWD divergence attacks (session CWD vs Python process CWD)
- cd boundary enforcement (workspace escape via cd)
- Compound command cd chain tracking
- Symlink-based path traversal
- include_paths (via tool_access_control) interaction with dangerous_paths
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.tools.shell.path_validation import (
    check_path_constraints,
    _resolve_path,
    _is_path_within_allowed,
    _build_allowed_roots,
)


# =========================================================================
# Mock config helpers
# =========================================================================

def _make_config_mock(
    dangerous_paths=None,
    block_destructive=True,
):
    """Create a mock for _get_shell_config_path (shell-specific settings only)."""
    def _mock(*args, default=None):
        key = args[0] if args else None
        if key == "dangerous_paths":
            return dangerous_paths  # None => use defaults
        if key == "block_destructive":
            return block_destructive
        return default
    return _mock


def _patch_config(mock_fn):
    """Return a patch decorator for the shell config path."""
    return patch(
        "src.tools.shell.path_validation._get_shell_config_path",
        side_effect=mock_fn,
    )


def _patch_allowed_dirs(*dirs):
    """Return a patch decorator that sets allowed directories via the shared library.

    Each positional arg is a path string. workspace root is NOT auto-added —
    pass it explicitly if needed (mirrors the real get_allowed_directories).
    """
    resolved = [Path(d).resolve() for d in dirs]
    return patch(
        "src.tools.shell.path_validation.get_allowed_directories",
        return_value=resolved,
    )


def _ws_patch(ws_path):
    """Return a context manager that mocks allowed dirs to just the workspace."""
    return _patch_allowed_dirs(str(ws_path))


# =========================================================================
# A. CWD divergence attacks
# =========================================================================

class TestCwdDivergence:
    """Session CWD diverges from Python process CWD."""

    def test_cwd_passed_to_validator(self, tmp_path):
        """When explicit cwd is passed, paths resolve against it."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "src").mkdir()

        with _ws_patch(ws):
            check_path_constraints("ls src", cwd=str(ws))  # should not raise

    def test_first_call_uses_process_cwd(self):
        """When cwd=None, falls back to os.getcwd()."""
        cwd = Path(os.getcwd())
        with _patch_allowed_dirs(str(cwd)):
            check_path_constraints("ls .")

    def test_cwd_divergence_attack_blocked(self, tmp_path):
        """Session CWD outside workspace: relative paths resolved correctly."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        with _ws_patch(ws):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints(
                    "cat ../../etc/passwd",
                    cwd=str(outside),
                )

    def test_session_cwd_outside_workspace_rejects_sibling(self, tmp_path):
        """If session CWD diverges, sibling directory access is blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        sibling = tmp_path / "sibling"
        sibling.mkdir()

        with _patch_allowed_dirs(str(ws)):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints(
                    "cat ../sibling/file.txt",
                    cwd=str(outside),
                )


# =========================================================================
# B. cd boundary enforcement
# =========================================================================

class TestCdBoundary:
    """cd targets must be within allowed directories."""

    def test_cd_within_workspace_allowed(self, tmp_path):
        """cd to subdirectory of workspace is allowed."""
        ws = tmp_path / "workspace"
        (ws / "src" / "tools").mkdir(parents=True)
        with _ws_patch(ws):
            check_path_constraints("cd src/tools", cwd=str(ws))

    def test_cd_to_workspace_root_allowed(self, tmp_path):
        """cd . is always allowed."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _ws_patch(ws):
            check_path_constraints("cd .", cwd=str(ws))

    def test_cd_to_include_paths_allowed(self, tmp_path):
        """cd to a directory in include_paths is allowed."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        shared = tmp_path / "shared" / "data"
        shared.mkdir(parents=True)
        with _patch_allowed_dirs(str(ws), str(shared)):
            check_path_constraints("cd " + str(shared), cwd=str(ws))

    def test_cd_absolute_outside_blocked(self, tmp_path):
        """cd to absolute path outside workspace is blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _ws_patch(ws):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("cd /etc", cwd=str(ws))

    def test_cd_relative_escape_blocked(self, tmp_path):
        """cd ../../.. escaping workspace boundary is blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _ws_patch(ws):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("cd ../../..", cwd=str(ws))

    def test_cd_tilde_blocked(self, tmp_path):
        """cd ~ is blocked when home is outside workspace."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        home = os.path.expanduser("~")
        if not home.startswith(str(ws)):
            with _ws_patch(ws):
                with pytest.raises(ValueError, match="outside allowed workspace"):
                    check_path_constraints("cd ~", cwd=str(ws))

    def test_cd_no_arg_home_blocked(self, tmp_path):
        """cd with no arguments (goes to ~) is blocked when home is outside ws."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        home = os.path.expanduser("~")
        if not home.startswith(str(ws)):
            with _ws_patch(ws):
                with pytest.raises(ValueError, match="outside allowed workspace"):
                    check_path_constraints("cd", cwd=str(ws))


# =========================================================================
# C. Compound command cd chain tracking
# =========================================================================

class TestCompoundCdChain:
    """Compound commands track effective CWD across cd segments."""

    def test_compound_cd_then_read_within_ws(self, tmp_path):
        """cd subdir && ls is allowed when subdir is in workspace."""
        ws = tmp_path / "workspace"
        (ws / "src").mkdir(parents=True)
        with _ws_patch(ws):
            check_path_constraints("cd src && ls -la", cwd=str(ws))

    def test_compound_cd_relative_within_ws(self, tmp_path):
        """cd src && cd ../tests tracks CWD correctly."""
        ws = tmp_path / "workspace"
        (ws / "src").mkdir(parents=True)
        (ws / "tests").mkdir(parents=True)
        with _ws_patch(ws):
            check_path_constraints("cd src && cd ../tests && ls", cwd=str(ws))

    def test_compound_cd_escape_then_read_blocked(self, tmp_path):
        """cd /tmp && ls — cd target outside workspace is blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _ws_patch(ws):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("cd /tmp && ls", cwd=str(ws))

    def test_compound_cd_chain_escape_blocked(self, tmp_path):
        """cd src && cd ../../.. escapes workspace in second cd."""
        ws = tmp_path / "workspace"
        (ws / "src").mkdir(parents=True)
        with _ws_patch(ws):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("cd src && cd ../../.. && ls", cwd=str(ws))

    def test_compound_multi_cd_one_escapes_blocked(self, tmp_path):
        """cd src && cd /etc — second cd escapes."""
        ws = tmp_path / "workspace"
        (ws / "src").mkdir(parents=True)
        with _ws_patch(ws):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("cd src && cd /etc && ls", cwd=str(ws))

    def test_compound_pipe_no_cd_ok(self, tmp_path):
        """grep | head — no cd in compound — should pass."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _ws_patch(ws):
            check_path_constraints("grep -r foo . | head -5", cwd=str(ws))

    def test_compound_semicolon_cd_escape_blocked(self, tmp_path):
        """cd /tmp ; ls — semicolon-separated cd escape is caught."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _ws_patch(ws):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("cd /tmp ; ls", cwd=str(ws))


# =========================================================================
# D. include_paths functionality
# =========================================================================

class TestIncludePaths:
    """tool_access_control.include_paths extends allowed directories for shell."""

    def test_include_paths_absolute_allowed(self, tmp_path):
        """Access to include_paths directory is allowed."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        (external / "data.csv").write_text("col1,col2")
        with _patch_allowed_dirs(str(ws), str(external)):
            check_path_constraints(f"cat {external}/data.csv", cwd=str(ws))

    def test_include_paths_multiple(self, tmp_path):
        """Multiple include_paths all work."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        ext1 = tmp_path / "ext1"
        ext1.mkdir()
        ext2 = tmp_path / "ext2"
        ext2.mkdir()
        with _patch_allowed_dirs(str(ws), str(ext1), str(ext2)):
            check_path_constraints(f"cat {ext1}/a.txt", cwd=str(ws))
            check_path_constraints(f"cat {ext2}/b.txt", cwd=str(ws))

    def test_include_paths_subdirectory_allowed(self, tmp_path):
        """Subdirectories of include_paths are also accessible."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        external = tmp_path / "external"
        (external / "deep" / "nested").mkdir(parents=True)
        with _patch_allowed_dirs(str(ws), str(external)):
            check_path_constraints(f"cat {external}/deep/nested/file.txt", cwd=str(ws))

    def test_include_paths_not_configured_blocked(self, tmp_path):
        """Without include_paths, external access is blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        with _ws_patch(ws):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints(f"cat {external}/secret.txt", cwd=str(ws))

    def test_include_paths_wrong_directory_blocked(self, tmp_path):
        """include /ext1 but access /ext2 is blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        ext1 = tmp_path / "ext1"
        ext1.mkdir()
        ext2 = tmp_path / "ext2"
        ext2.mkdir()
        with _patch_allowed_dirs(str(ws), str(ext1)):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints(f"cat {ext2}/secret.txt", cwd=str(ws))

    def test_include_paths_partial_match_blocked(self, tmp_path):
        """include /home/user but /home/username should NOT match."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        user_dir = tmp_path / "user"
        user_dir.mkdir()
        username_dir = tmp_path / "username"
        username_dir.mkdir()
        with _patch_allowed_dirs(str(ws), str(user_dir)):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints(f"cat {username_dir}/file.txt", cwd=str(ws))

    def test_include_paths_empty_list(self, tmp_path):
        """Empty include_paths is same as not configured."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _ws_patch(ws):
            check_path_constraints("ls .", cwd=str(ws))

    def test_include_paths_nonexistent_directory(self, tmp_path):
        """Non-existent include_path doesn't crash, won't match unrelated paths."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        nonexistent = str(tmp_path / "does_not_exist")
        unrelated = str(tmp_path / "unrelated")
        with _patch_allowed_dirs(str(ws), nonexistent):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints(f"cat {unrelated}/file.txt", cwd=str(ws))

    def test_include_paths_with_cd_allowed(self, tmp_path):
        """cd to include_paths directory is allowed."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        with _patch_allowed_dirs(str(ws), str(external)):
            check_path_constraints(f"cd {external}", cwd=str(ws))


# =========================================================================
# E. Symlink and path normalisation
# =========================================================================

class TestSymlinkAndNormalization:
    """Symlink resolution and path normalization security."""

    def test_normpath_collapses_dotdot(self, tmp_path):
        """./src/../src/file.py normalises to workspace path."""
        ws = tmp_path / "workspace"
        (ws / "src").mkdir(parents=True)
        with _ws_patch(ws):
            check_path_constraints("cat ./src/../src/file.py", cwd=str(ws))

    def test_realpath_resolves_symlink_inside_ws(self, tmp_path):
        """Symlink inside workspace pointing to workspace file is OK."""
        ws = tmp_path / "workspace"
        (ws / "real").mkdir(parents=True)
        (ws / "real" / "file.txt").write_text("content")
        link = ws / "link"
        link.symlink_to(ws / "real")
        with _ws_patch(ws):
            check_path_constraints("cat link/file.txt", cwd=str(ws))

    def test_symlink_escape_blocked(self, tmp_path):
        """Symlink inside workspace pointing outside is blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        link = ws / "escape_link"
        link.symlink_to(outside)
        with _ws_patch(ws):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("cat escape_link/secret.txt", cwd=str(ws))

    def test_multiple_dotdot_levels_blocked(self, tmp_path):
        """Deep .. traversal blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _ws_patch(ws):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("cat ../../../../../etc/passwd", cwd=str(ws))


# =========================================================================
# F. dangerous_paths vs include_paths interaction
# =========================================================================

class TestDangerousVsInclude:
    """dangerous_paths takes precedence over include_paths."""

    def test_rm_in_include_path_allowed(self, tmp_path):
        """rm in include_paths non-dangerous dir is allowed."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        safe = tmp_path / "safe_area"
        safe.mkdir()
        (safe / "tempfile").write_text("temp")
        with _patch_allowed_dirs(str(ws), str(safe)):
            check_path_constraints(f"rm {safe}/tempfile", cwd=str(ws))

    def test_rm_dangerous_even_with_include_blocked(self, tmp_path):
        """rm -rf /etc is blocked even when /etc is in include_paths."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _patch_allowed_dirs(str(ws), "/etc"):
            with pytest.raises(ValueError, match="[Dd]angerous.*critical"):
                check_path_constraints("rm -rf /etc", cwd=str(ws))


# =========================================================================
# G. shell_tool integration (session CWD plumbing)
# =========================================================================

class TestShellToolCwdPlumbing:
    """Verify shell_tool passes session CWD to validator."""

    def test_shell_tool_passes_session_cwd(self):
        """shell_tool should call validate_command with session CWD."""
        from unittest.mock import patch as mp

        with mp("src.tools.shell.shell_tool.get_current_agent_id", return_value="agent-1"), \
             mp("src.tools.shell.shell_tool.ShellProcessRegistry") as mock_registry_cls:

            mock_registry = MagicMock()
            mock_registry.get_session_cwd.return_value = "/fake/session/cwd"
            mock_registry_cls.get_instance.return_value = mock_registry

            with mp("src.tools.shell.shell_tool.validate_command") as mock_validate:
                mock_validate.side_effect = ValueError("blocked for test")

                from src.tools.shell.shell_tool import shell_tool
                try:
                    shell_tool("echo hello")
                except ValueError:
                    pass

                # validate_command should have been called with session CWD
                mock_validate.assert_called_once_with(
                    "echo hello", cwd="/fake/session/cwd"
                )

    def test_shell_tool_no_session_passes_none(self):
        """When no agent context, cwd=None is passed (falls back to os.getcwd)."""
        from unittest.mock import patch as mp

        with mp("src.tools.shell.shell_tool.get_current_agent_id", return_value=None), \
             mp("src.tools.shell.shell_tool.validate_command") as mock_validate:

            mock_validate.side_effect = ValueError("blocked for test")

            from src.tools.shell.shell_tool import shell_tool
            try:
                shell_tool("echo hello")
            except ValueError:
                pass

            mock_validate.assert_called_once_with("echo hello", cwd=None)


# =========================================================================
# H. _build_allowed_roots helper
# =========================================================================

class TestBuildAllowedRoots:
    """Verify _build_allowed_roots delegates to shared permissions library."""

    def test_basic_workspace_only(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _ws_patch(ws):
            roots = _build_allowed_roots(str(ws))
            assert len(roots) == 1
            assert str(ws.resolve()) in roots

    def test_workspace_plus_include(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        ext = tmp_path / "external"
        ext.mkdir()
        with _patch_allowed_dirs(str(ws), str(ext)):
            roots = _build_allowed_roots(str(ws))
            assert len(roots) == 2
            assert str(ext.resolve()) in roots
