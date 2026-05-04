"""Tests for file tools built-in path validation.

Verifies that read_file, edit_file, and write_file enforce workspace
boundary checks when running inside an agent context.

Covers:
- Normal: workspace files accessible, include_paths files accessible
- Abnormal: outside workspace blocked, UNC blocked
- Boundary: symlink escape, dot-dot traversal, exclude_paths, workspace root
"""

import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.tools.file_ops._safety import validate_file_access


def _mock_agent_context():
    """Mock an active agent context so validate_file_access enforces boundaries."""
    return patch(
        "src.trace.task_context.get_current_agent_config",
        return_value={"name": "test_agent"},
    )


def _mock_workspace(ws_path, include_paths=None, exclude_paths=None):
    """Mock the permissions library for workspace boundary checks.

    Patches validate_path at the point where _safety.py imports it.
    """
    from src.lib.permissions.path_validation import PathValidationResult

    def _validate(path_str, operation="read", tool_name=None, extra_include=None, extra_exclude=None):
        raw = path_str[7:] if path_str.startswith("file://") else path_str
        resolved = Path(os.path.expanduser(raw)).resolve()
        allowed_dirs = [Path(ws_path).resolve()]
        if include_paths:
            allowed_dirs.extend([Path(p).resolve() for p in include_paths])

        in_allowed = any(
            _is_within(resolved, d) for d in allowed_dirs
        )
        if not in_allowed:
            return PathValidationResult(
                allowed=False,
                reason=f"Access denied: Path '{path_str}' is outside allowed directories",
                resolved_path=resolved,
            )

        # Check exclude
        excl = exclude_paths or []
        ws = Path(ws_path).resolve()
        for e in excl:
            ep = Path(e)
            if not ep.is_absolute():
                ep = (ws / e).resolve()
            else:
                ep = ep.resolve()
            if _is_within(resolved, ep):
                return PathValidationResult(
                    allowed=False,
                    reason=f"Access denied: Path '{path_str}' is in excluded directory",
                    resolved_path=resolved,
                )

        return PathValidationResult(allowed=True, resolved_path=resolved)

    return patch("src.lib.permissions.validate_path", side_effect=_validate)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# =========================================================================
# Normal paths (4 cases)
# =========================================================================

class TestFileAccessNormal:

    def test_read_file_within_workspace(self, tmp_path):
        """File inside workspace → no error."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "file.txt").write_text("content")
        with _mock_agent_context(), _mock_workspace(str(ws)):
            validate_file_access(str(ws / "file.txt"), "read")  # should not raise

    def test_write_file_within_workspace(self, tmp_path):
        """Write to workspace file → no error."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _mock_agent_context(), _mock_workspace(str(ws)):
            validate_file_access(str(ws / "new.txt"), "write")

    def test_edit_file_within_workspace(self, tmp_path):
        """Edit workspace file → no error."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "app.py").write_text("code")
        with _mock_agent_context(), _mock_workspace(str(ws)):
            validate_file_access(str(ws / "app.py"), "write")

    def test_file_ops_with_include_paths(self, tmp_path):
        """File in include_paths → no error."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        ext = tmp_path / "external"
        ext.mkdir()
        (ext / "data.csv").write_text("col1,col2")
        with _mock_agent_context(), _mock_workspace(str(ws), include_paths=[str(ext)]):
            validate_file_access(str(ext / "data.csv"), "read")


# =========================================================================
# Abnormal paths (4 cases)
# =========================================================================

class TestFileAccessAbnormal:

    def test_read_file_outside_workspace_blocked(self, tmp_path):
        """Read outside workspace → ValueError."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        with _mock_agent_context(), _mock_workspace(str(ws)):
            with pytest.raises(ValueError, match="outside allowed"):
                validate_file_access(str(outside / "secret.txt"), "read")

    def test_write_file_outside_workspace_blocked(self, tmp_path):
        """Write outside workspace → ValueError."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _mock_agent_context(), _mock_workspace(str(ws)):
            with pytest.raises(ValueError, match="outside allowed"):
                validate_file_access("/etc/passwd", "write")

    def test_edit_file_outside_workspace_blocked(self, tmp_path):
        """Edit outside workspace → ValueError."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _mock_agent_context(), _mock_workspace(str(ws)):
            with pytest.raises(ValueError, match="outside allowed"):
                validate_file_access("/usr/bin/something", "write")

    def test_file_ops_unc_path_blocked(self):
        """UNC path → ValueError (always enforced, no agent context needed)."""
        with pytest.raises(ValueError, match="UNC"):
            validate_file_access("\\\\server\\share\\file.txt", "read")


# =========================================================================
# Boundary conditions (4 cases)
# =========================================================================

class TestFileAccessBoundary:

    def test_file_ops_symlink_escape_blocked(self, tmp_path):
        """Symlink pointing outside workspace → blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        link = ws / "escape"
        link.symlink_to(outside)
        # The resolved path goes outside workspace
        with _mock_agent_context(), _mock_workspace(str(ws)):
            with pytest.raises(ValueError, match="outside allowed"):
                validate_file_access(str(link / "secret.txt"), "read")

    def test_file_ops_dot_dot_traversal_blocked(self, tmp_path):
        """../../../etc/passwd → blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _mock_agent_context(), _mock_workspace(str(ws)):
            with pytest.raises(ValueError, match="outside allowed"):
                validate_file_access(str(ws / "../../../etc/passwd"), "read")

    def test_file_ops_exclude_paths_blocked(self, tmp_path):
        """File in exclude_paths → blocked."""
        ws = tmp_path / "workspace"
        (ws / "secrets").mkdir(parents=True)
        (ws / "secrets" / "key.pem").write_text("secret")
        with _mock_agent_context(), _mock_workspace(str(ws), exclude_paths=["secrets"]):
            with pytest.raises(ValueError, match="excluded"):
                validate_file_access(str(ws / "secrets" / "key.pem"), "read")

    def test_file_ops_workspace_root_allowed(self, tmp_path):
        """Workspace root path itself → allowed."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        with _mock_agent_context(), _mock_workspace(str(ws)):
            validate_file_access(str(ws), "read")  # should not raise


# =========================================================================
# No agent context (bypass)
# =========================================================================

class TestNoAgentContext:
    """When no agent context is active, boundary checks are skipped."""

    def test_no_agent_context_allows_any_path(self, tmp_path):
        """Without agent context, even /tmp paths work (for unit tests)."""
        f = tmp_path / "test.txt"
        f.write_text("content")
        with patch(
            "src.trace.task_context.get_current_agent_config",
            return_value=None,
        ):
            validate_file_access(str(f), "read")  # should not raise

    def test_unc_always_blocked_even_without_context(self):
        """UNC paths are always blocked regardless of agent context."""
        with patch(
            "src.trace.task_context.get_current_agent_config",
            return_value=None,
        ):
            with pytest.raises(ValueError, match="UNC"):
                validate_file_access("\\\\server\\share\\file", "read")
