"""Tests for shell command path boundary validation."""

import os
from pathlib import Path
import pytest
from unittest.mock import patch

from src.tools.shell.path_validation import (
    check_path_constraints,
    _filter_out_flags,
    _resolve_path,
    _is_path_within_allowed,
    _is_dangerous_removal_path,
    _extract_redirect_targets,
    _has_cd_in_compound,
    _build_allowed_roots,
    DEFAULT_DANGEROUS_PATHS,
)

# =========================================================================
# Mock config helpers
# =========================================================================

# Mock for shell-specific settings (dangerous_paths, block_destructive)
def _mock_shell_config_default(*args, default=None):
    """Mock _get_shell_config_path returning defaults for shell-specific settings."""
    key = args[0] if args else None
    if key == "dangerous_paths":
        return None  # use defaults
    if key == "block_destructive":
        return True
    return default


def _mock_shell_config_no_destructive(*args, default=None):
    """Mock _get_shell_config_path with block_destructive=false."""
    key = args[0] if args else None
    if key == "dangerous_paths":
        return None
    if key == "block_destructive":
        return False
    return default


def _patch_allowed_dirs(*dirs):
    """Mock get_allowed_directories to return given directories."""
    resolved = [Path(d).resolve() for d in dirs]
    return patch(
        "src.tools.shell.path_validation.get_allowed_directories",
        return_value=resolved,
    )


def _ws_patch():
    """Mock allowed dirs to just the current working directory (default workspace)."""
    return _patch_allowed_dirs(os.getcwd())


# =========================================================================
# Normal path — safe commands pass validation
# =========================================================================

class TestSafeCommands:
    """Commands within project directory should pass."""

    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "cat README.md",
        "grep -r 'pattern' src/",
        "find . -name '*.py'",
        "head -n 10 pyproject.toml",
        "wc -l src/tools/shell/security.py",
        "diff file1.txt file2.txt",
        "echo hello",
    ])
    def test_safe_relative_commands(self, cmd):
        """Relative paths within cwd should pass."""
        with _ws_patch():
            check_path_constraints(cmd)  # should not raise

    def test_empty_command_passes(self):
        check_path_constraints("")
        check_path_constraints("   ")

    def test_unknown_command_passes(self):
        """Commands not in PATH_EXTRACTORS should pass (no path to check)."""
        with _ws_patch():
            check_path_constraints("python -m pytest")

    def test_safe_redirect(self):
        """Redirect within project dir should pass."""
        with _ws_patch():
            check_path_constraints("echo hello > output.txt")

    def test_safe_rm_project_dir(self):
        """rm within project dir should pass."""
        with _ws_patch():
            check_path_constraints("rm -rf ./build")

    def test_wildcard_allowed_root_allows_workspace_path(self):
        """The shared permissions wildcard sentinel allows any shell path."""
        assert _is_path_within_allowed(os.getcwd(), ["*"]) is True


# =========================================================================
# Abnormal path — boundary violations blocked
# =========================================================================

class TestPathBoundaryViolations:
    """Commands accessing outside project should be blocked."""

    def test_block_absolute_escape(self):
        with _ws_patch():
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("cd /etc")

    def test_block_relative_escape(self):
        with _ws_patch():
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("cat ../../../etc/passwd")

    def test_block_tilde_escape(self):
        """~ expands to home dir which is likely outside project."""
        home = os.path.expanduser("~")
        cwd = os.getcwd()
        if not home.startswith(cwd):
            with _ws_patch():
                with pytest.raises(ValueError, match="outside allowed workspace"):
                    check_path_constraints("cat ~/secret.txt")


class TestDangerousRemoval:
    """Dangerous rm/rmdir paths should be blocked."""

    @pytest.mark.parametrize("cmd", [
        "rm -rf /",
        "rm -rf /etc",
        "rm -rf /usr",
        "rm -rf /home",
        "rmdir /var",
    ])
    def test_block_dangerous_rm(self, cmd):
        # Include / in allowed dirs so boundary check passes, but dangerous check blocks
        with _patch_allowed_dirs(os.getcwd(), "/"):
            with pytest.raises(ValueError, match="[Dd]angerous.*critical"):
                check_path_constraints(cmd)

    def test_dangerous_rm_allowed_when_disabled(self):
        """When block_destructive=false, dangerous paths skip the danger check.
        But they may still fail the workspace boundary check.
        """
        with _ws_patch(), \
             patch("src.tools.shell.path_validation._get_shell_config_path",
                   side_effect=_mock_shell_config_no_destructive):
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("rm -rf /etc")


class TestRedirectValidation:
    """Output redirect targets should be validated."""

    def test_block_redirect_outside(self):
        with _ws_patch():
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("echo x > /etc/passwd")

    def test_safe_redirect_inside(self):
        with _ws_patch():
            check_path_constraints("echo x > ./output.txt")  # should not raise

    def test_dev_null_always_allowed(self):
        """Redirecting to /dev/null is always safe."""
        with _ws_patch():
            check_path_constraints("echo x > /dev/null")  # should not raise


class TestCdWriteCombo:
    """cd + write in compound commands should be blocked."""

    def test_block_cd_then_rm(self):
        """cd to outside workspace + write triggers boundary error first."""
        with _ws_patch():
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("cd /tmp && rm -rf evil.txt")

    def test_block_cd_then_redirect(self):
        with _ws_patch():
            with pytest.raises(ValueError, match="change directories.*redirection"):
                check_path_constraints("cd src && echo x > config.json")

    def test_cd_then_read_ok(self):
        """cd + read-only is allowed."""
        with _ws_patch():
            check_path_constraints("cd src && ls -la")  # should not raise


# =========================================================================
# Boundary / edge cases
# =========================================================================

class TestEdgeCases:
    """Edge cases and helper functions."""

    def test_filter_out_flags_basic(self):
        assert _filter_out_flags(['-r', '-f', 'file.txt']) == ['file.txt']

    def test_filter_out_flags_double_dash(self):
        """After --, everything is positional."""
        assert _filter_out_flags(['--', '-evil_file']) == ['-evil_file']

    def test_filter_out_flags_empty(self):
        assert _filter_out_flags([]) == []

    def test_resolve_path_relative(self):
        cwd = '/home/user/project'
        expected = os.path.join(
            os.path.realpath('/home'),
            'user/project/src/main.py',
        )
        assert _resolve_path('src/main.py', cwd) == expected

    def test_resolve_path_absolute(self):
        assert _resolve_path('/etc/passwd', '/home/user') == os.path.realpath('/etc/passwd')

    def test_resolve_path_tilde(self):
        result = _resolve_path('~/file.txt', '/home/user/project')
        assert result.endswith('file.txt')
        assert '~' not in result

    def test_is_path_within_allowed(self):
        assert _is_path_within_allowed('/home/user/project/src', ['/home/user/project']) is True
        assert _is_path_within_allowed('/etc/passwd', ['/home/user/project']) is False

    def test_is_dangerous_removal_path(self):
        assert _is_dangerous_removal_path('/', DEFAULT_DANGEROUS_PATHS) is True
        assert _is_dangerous_removal_path('/etc', DEFAULT_DANGEROUS_PATHS) is True
        assert _is_dangerous_removal_path(os.path.realpath('/etc'), DEFAULT_DANGEROUS_PATHS) is True
        assert _is_dangerous_removal_path('/home/user/project', DEFAULT_DANGEROUS_PATHS) is False

    def test_extract_redirect_targets(self):
        assert _extract_redirect_targets("echo x > file.txt") == ["file.txt"]
        assert _extract_redirect_targets("echo x >> log.txt") == ["log.txt"]
        assert _extract_redirect_targets("echo x > /dev/null") == []
        assert _extract_redirect_targets("ls 2>&1") == []

    def test_has_cd_in_compound(self):
        assert _has_cd_in_compound("cd src && ls") is True
        assert _has_cd_in_compound("ls && cat file") is False
        assert _has_cd_in_compound("cd /tmp ; rm file") is True

    def test_custom_allowed_paths(self):
        """/tmp is allowed when included in allowed directories."""
        with _patch_allowed_dirs(os.getcwd(), "/tmp"):
            check_path_constraints("cat /tmp/test.txt")  # should not raise

    def test_nonexistent_child_under_symlinked_include_path_allowed(self):
        """/tmp/nonexistent canonicalizes under /private/tmp on macOS."""
        target = "/tmp/agentloom_shell_nonexistent_child"
        assert _resolve_path(target, os.getcwd()) == os.path.join(
            os.path.realpath("/tmp"),
            "agentloom_shell_nonexistent_child",
        )
        with _patch_allowed_dirs(os.getcwd(), "/tmp"):
            check_path_constraints(f"cat {target}")  # should not raise

    def test_path_traversal_normalized(self):
        """Path traversal attempts should be normalized and caught."""
        with _ws_patch():
            with pytest.raises(ValueError, match="outside allowed workspace"):
                check_path_constraints("cat ./../../etc/passwd")

    def test_resolve_path_preserves_logical_symlink_path(self, tmp_path):
        """_resolve_path returns the shell-facing logical path."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "file.txt").write_text("hello")
        link = tmp_path / "link"
        link.symlink_to(real_dir)

        result = _resolve_path("link/file.txt", str(tmp_path))
        assert result == str(link / "file.txt")

    def test_path_comparison_resolves_symlink_alias(self, tmp_path):
        """Allowed roots compare filesystem identity, not path spelling."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "file.txt").write_text("hello")
        alias = tmp_path / "alias"
        alias.symlink_to(real_dir)

        assert _is_path_within_allowed(str(alias / "file.txt"), [str(real_dir)]) is True

    def test_path_comparison_blocks_symlink_escape(self, tmp_path):
        """A workspace symlink to an outside directory is still outside."""
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        link = workspace / "escape"
        link.symlink_to(outside)

        assert _is_path_within_allowed(str(link / "secret.txt"), [str(workspace)]) is False

    def test_dangerous_path_comparison_resolves_alias(self, tmp_path):
        """Dangerous path checks compare canonical identity as well."""
        real_dir = tmp_path / "critical"
        real_dir.mkdir()
        alias = tmp_path / "critical_alias"
        alias.symlink_to(real_dir)

        assert _is_dangerous_removal_path(str(alias), {str(real_dir)}) is True

    def test_resolve_path_nonexistent_still_normalizes(self):
        """_resolve_path normalizes non-existent paths without crash."""
        result = _resolve_path("../foo/bar/../baz", "/home/user/project")
        expected = os.path.join(os.path.realpath('/home'), 'user/foo/baz')
        assert result == expected


# =========================================================================
# _build_allowed_roots delegates to shared library
# =========================================================================

class TestBuildAllowedRootsDelegation:
    """Verify _build_allowed_roots delegates to permissions.workspace."""

    def test_build_allowed_roots_returns_shared_lib_result(self, tmp_path):
        """_build_allowed_roots returns paths from get_allowed_directories."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        ext = tmp_path / "external"
        ext.mkdir()
        with _patch_allowed_dirs(str(ws), str(ext)):
            roots = _build_allowed_roots(str(ws))
            assert len(roots) == 2
            assert str(ws.resolve()) in roots
            assert str(ext.resolve()) in roots
