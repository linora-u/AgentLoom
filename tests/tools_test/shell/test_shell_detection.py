"""Tests for shell detection logic aligned with Claude Code's findSuitableShell().

Covers:
- _is_executable() two-tier check (X_OK + --version fallback)
- find_suitable_shell() priority chain ($SHELL -> which -> hardcoded -> error)
- Preference-aware ordering (bash vs zsh based on $SHELL)
- Only bash/zsh are accepted (fish, csh, ksh, sh rejected)
- Cache behaviour (lru_cache)
- Windows fallback (best-effort)
"""

import os
import stat
import subprocess
import tempfile

import pytest

from src.tools.shell.process import (
    _is_executable,
    _is_supported_shell,
    find_suitable_shell,
)


# ---------------------------------------------------------------------------
# _is_executable() — 5 cases
# ---------------------------------------------------------------------------

class TestIsExecutable:
    """Two-tier executability check aligned with Claude Code isExecutable()."""

    def test_real_executable(self):
        """12a: A real shell binary should be detected as executable."""
        # /bin/sh exists on virtually all Unix systems
        assert _is_executable("/bin/sh") is True

    def test_nonexistent_path(self):
        """12b: A path that does not exist should return False."""
        assert _is_executable("/nonexistent/shell/binary") is False

    def test_existing_but_not_executable(self):
        """12c: A file that exists but lacks execute permission should return False."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sh") as tmp:
            tmp.write(b"#!/bin/sh\necho hello\n")
            tmp_path = tmp.name
        try:
            # Remove all execute bits
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            assert _is_executable(tmp_path) is False
        finally:
            os.unlink(tmp_path)

    def test_xok_fails_but_version_succeeds(self, monkeypatch):
        """12d: When X_OK check fails, fallback to --version execution (Nix compat)."""
        real_bash = "/bin/bash"
        if not os.path.isfile(real_bash):
            pytest.skip("/bin/bash not available")

        # Mock os.access to always return False, but the binary is real
        original_access = os.access
        def fake_access(path, mode, **kwargs):
            if mode == os.X_OK:
                return False
            return original_access(path, mode, **kwargs)

        monkeypatch.setattr(os, "access", fake_access)
        # Should still succeed via --version fallback
        assert _is_executable(real_bash) is True

    def test_both_tiers_fail(self, monkeypatch):
        """12e: When both X_OK and --version fail, return False."""
        # Create a file that exists but is not executable
        with tempfile.NamedTemporaryFile(delete=False, suffix=".sh") as tmp:
            tmp.write(b"not a real shell\n")
            tmp_path = tmp.name
        try:
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            assert _is_executable(tmp_path) is False
        finally:
            os.unlink(tmp_path)

    def test_empty_string(self):
        """Edge case: empty string should return False."""
        assert _is_executable("") is False

    def test_none_input(self):
        """Edge case: None should return False."""
        assert _is_executable(None) is False


# ---------------------------------------------------------------------------
# _is_supported_shell() — basic checks
# ---------------------------------------------------------------------------

class TestIsSupportedShell:
    """Only bash and zsh paths should be accepted."""

    @pytest.mark.parametrize("path,expected", [
        ("/bin/bash", True),
        ("/usr/bin/zsh", True),
        ("/usr/local/bin/bash", True),
        ("/opt/homebrew/bin/zsh", True),
        ("/bin/fish", False),
        ("/bin/sh", False),
        ("/bin/csh", False),
        ("/bin/ksh", False),
        ("/bin/dash", False),
        ("/bin/tcsh", False),
        ("", False),
        (None, False),
    ])
    def test_shell_type_classification(self, path, expected):
        assert _is_supported_shell(path) is expected


# ---------------------------------------------------------------------------
# find_suitable_shell() — $SHELL handling — 6 cases
# ---------------------------------------------------------------------------

class TestFindSuitableShellEnvShell:
    """Priority 1: $SHELL environment variable (validated)."""

    def test_valid_bash_shell(self, monkeypatch):
        """13a: $SHELL=/bin/bash and it is executable -> use it."""
        bash = "/bin/bash"
        if not os.path.isfile(bash):
            pytest.skip("/bin/bash not available")
        monkeypatch.setenv("SHELL", bash)
        assert find_suitable_shell() == bash

    def test_valid_zsh_shell(self, monkeypatch):
        """13b: $SHELL=/usr/bin/zsh and it is executable -> use it."""
        import shutil
        zsh = shutil.which("zsh")
        if not zsh:
            pytest.skip("zsh not available")
        monkeypatch.setenv("SHELL", zsh)
        assert find_suitable_shell() == zsh

    def test_unsupported_shell_skipped(self, monkeypatch):
        """13c: $SHELL=/bin/fish (not bash/zsh) -> skip, use which fallback."""
        monkeypatch.setenv("SHELL", "/bin/fish")
        result = find_suitable_shell()
        # Should NOT be /bin/fish — should have fallen back
        assert "fish" not in result
        assert "bash" in result or "zsh" in result

    def test_nonexistent_bash_skipped(self, monkeypatch):
        """13d: $SHELL contains 'bash' but path doesn't exist -> skip."""
        monkeypatch.setenv("SHELL", "/nonexistent/path/bash")
        result = find_suitable_shell()
        assert result != "/nonexistent/path/bash"
        assert os.path.isfile(result)

    def test_shell_unset(self, monkeypatch):
        """13e: $SHELL not set at all -> fall back to which detection."""
        monkeypatch.delenv("SHELL", raising=False)
        result = find_suitable_shell()
        assert "bash" in result or "zsh" in result

    def test_shell_empty_string(self, monkeypatch):
        """13f: $SHELL is empty string -> fall back to which detection."""
        monkeypatch.setenv("SHELL", "")
        result = find_suitable_shell()
        assert "bash" in result or "zsh" in result


# ---------------------------------------------------------------------------
# find_suitable_shell() — which() detection — 4 cases
# ---------------------------------------------------------------------------

class TestFindSuitableShellWhich:
    """Priority 2: shutil.which() lookup."""

    def test_which_finds_bash(self, monkeypatch):
        """14a: which finds bash only -> return bash path."""
        monkeypatch.delenv("SHELL", raising=False)
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/bash" if cmd == "bash" else None)
        monkeypatch.setattr("src.tools.shell.process._is_executable", lambda p: "/usr/bin/bash" == p)
        result = find_suitable_shell()
        assert result == "/usr/bin/bash"

    def test_which_finds_zsh_only(self, monkeypatch):
        """14b: which finds zsh but not bash -> return zsh."""
        monkeypatch.delenv("SHELL", raising=False)
        monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/zsh" if cmd == "zsh" else None)
        monkeypatch.setattr("src.tools.shell.process._is_executable", lambda p: "/usr/bin/zsh" == p)
        result = find_suitable_shell()
        assert result == "/usr/bin/zsh"

    def test_preference_bash_first(self, monkeypatch):
        """14c: $SHELL hints bash -> bash appears before zsh in candidates."""
        monkeypatch.setenv("SHELL", "/nonexistent/bash")  # hints bash preference
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}" if cmd in ("bash", "zsh") else None)
        monkeypatch.setattr("src.tools.shell.process._is_executable",
                            lambda p: p in ("/usr/bin/bash", "/usr/bin/zsh"))
        result = find_suitable_shell()
        assert result == "/usr/bin/bash"

    def test_preference_zsh_first(self, monkeypatch):
        """14d: $SHELL hints zsh -> zsh appears before bash in candidates."""
        monkeypatch.setenv("SHELL", "/nonexistent/zsh")  # hints zsh preference
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}" if cmd in ("bash", "zsh") else None)
        monkeypatch.setattr("src.tools.shell.process._is_executable",
                            lambda p: p in ("/usr/bin/bash", "/usr/bin/zsh"))
        result = find_suitable_shell()
        assert result == "/usr/bin/zsh"


# ---------------------------------------------------------------------------
# find_suitable_shell() — hardcoded fallback — 3 cases
# ---------------------------------------------------------------------------

class TestFindSuitableShellFallback:
    """Priority 3: Hardcoded path scanning."""

    def test_fallback_to_bin_bash(self, monkeypatch):
        """15a: $SHELL invalid, which fails -> /bin/bash found."""
        monkeypatch.delenv("SHELL", raising=False)
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        monkeypatch.setattr("src.tools.shell.process._is_executable",
                            lambda p: p == "/bin/bash")
        result = find_suitable_shell()
        assert result == "/bin/bash"

    def test_fallback_homebrew_path(self, monkeypatch):
        """15b: Only /opt/homebrew/bin/zsh exists -> returns it."""
        monkeypatch.delenv("SHELL", raising=False)
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        monkeypatch.setattr("src.tools.shell.process._is_executable",
                            lambda p: p == "/opt/homebrew/bin/zsh")
        result = find_suitable_shell()
        assert result == "/opt/homebrew/bin/zsh"

    def test_all_fail_raises_error(self, monkeypatch):
        """15c: All paths fail -> FileNotFoundError with helpful message."""
        monkeypatch.delenv("SHELL", raising=False)
        monkeypatch.setattr("shutil.which", lambda cmd: None)
        monkeypatch.setattr("src.tools.shell.process._is_executable", lambda p: False)
        with pytest.raises(FileNotFoundError, match="No suitable shell found"):
            find_suitable_shell()


# ---------------------------------------------------------------------------
# find_suitable_shell() — caching — 2 cases
# ---------------------------------------------------------------------------

class TestFindSuitableShellCache:
    """Result caching via @lru_cache."""

    def test_cache_reuses_result(self, monkeypatch):
        """16a: Second call reuses cached result (detection logic not re-run)."""
        call_count = 0
        original_which = __import__("shutil").which

        def counting_which(cmd):
            nonlocal call_count
            call_count += 1
            return original_which(cmd)

        monkeypatch.setattr("shutil.which", counting_which)
        monkeypatch.delenv("SHELL", raising=False)

        result1 = find_suitable_shell()
        count_after_first = call_count
        result2 = find_suitable_shell()

        assert result1 == result2
        assert call_count == count_after_first  # No additional which() calls

    def test_cache_clear_allows_redetection(self, monkeypatch):
        """16b: cache_clear() forces re-detection."""
        import shutil as _shutil
        monkeypatch.setenv("SHELL", _shutil.which("bash") or _shutil.which("zsh") or "/bin/bash")
        result1 = find_suitable_shell()

        find_suitable_shell.cache_clear()

        # Now change $SHELL
        zsh = _shutil.which("zsh")
        if zsh and zsh != result1:
            monkeypatch.setenv("SHELL", zsh)
            result2 = find_suitable_shell()
            assert result2 == zsh


# ---------------------------------------------------------------------------
# Windows — 2 cases
# ---------------------------------------------------------------------------

class TestWindowsShellDetection:
    """Windows shell detection (best-effort)."""

    def test_comspec_valid(self, monkeypatch):
        """17a: $COMSPEC set and valid -> use it."""
        from src.tools.shell.process import _resolve_shell_path_windows
        monkeypatch.setenv("COMSPEC", "/bin/bash")  # Not real Windows but testable
        monkeypatch.setattr("src.tools.shell.process._is_executable", lambda p: p == "/bin/bash")
        result = _resolve_shell_path_windows()
        assert result == "/bin/bash"

    def test_windows_all_fail(self, monkeypatch):
        """17b: All Windows paths fail -> FileNotFoundError."""
        from src.tools.shell.process import _resolve_shell_path_windows
        monkeypatch.delenv("COMSPEC", raising=False)
        monkeypatch.setattr("os.path.isfile", lambda p: False)
        with pytest.raises(FileNotFoundError, match="No suitable Windows shell"):
            _resolve_shell_path_windows()
