"""Tests for sandbox manager and should_use_sandbox decision logic."""

import os
import pytest
from unittest.mock import patch, MagicMock

from src.utils.sandbox.sandbox_manager import (
    SandboxManager,
    SandboxConfig,
    _match_excluded_command,
    _load_sandbox_config,
)
from src.tools.shell.should_use_sandbox import should_use_sandbox


# =========================================================================
# SandboxConfig / loading
# =========================================================================

class TestSandboxConfig:
    """Test sandbox configuration loading."""

    @patch("src.utils.sandbox.sandbox_manager.C.get_nested", return_value=None)
    def test_default_config_disabled(self, mock_config):
        config = _load_sandbox_config()
        assert config.enabled is False
        assert config.mode == "bwrap"
        assert "." in config.allow_write

    @patch("src.utils.sandbox.sandbox_manager.C.get_nested", return_value={
        "enabled": True, "mode": "docker",
        "allow_write": ["/workspace"], "deny_write": ["/etc"],
        "network_isolation": True, "excluded_commands": ["git push:*"],
    })
    def test_custom_config(self, mock_config):
        config = _load_sandbox_config()
        assert config.enabled is True
        assert config.mode == "docker"
        assert "/workspace" in config.allow_write
        assert config.network_isolation is True
        assert "git push:*" in config.excluded_commands


# =========================================================================
# Exclusion matching
# =========================================================================

class TestExclusionMatching:
    """Test command exclusion pattern matching."""

    def test_exact_match(self):
        assert _match_excluded_command("npm run lint", ["npm run lint"]) is True

    def test_exact_no_match(self):
        assert _match_excluded_command("npm run test", ["npm run lint"]) is False

    def test_prefix_match(self):
        assert _match_excluded_command("docker ps", ["docker:*"]) is True
        assert _match_excluded_command("docker build .", ["docker:*"]) is True

    def test_prefix_no_match(self):
        assert _match_excluded_command("npm install", ["docker:*"]) is False

    def test_prefix_bare_command(self):
        assert _match_excluded_command("docker", ["docker:*"]) is True

    def test_empty_patterns(self):
        assert _match_excluded_command("any command", []) is False

    def test_empty_command(self):
        assert _match_excluded_command("", ["docker:*"]) is False


# =========================================================================
# SandboxManager.should_sandbox
# =========================================================================

class TestShouldSandbox:
    """Test sandbox decision logic."""

    def test_disabled_returns_false(self):
        manager = SandboxManager(SandboxConfig(enabled=False))
        assert manager.should_sandbox("ls -la") is False

    def test_enabled_returns_true(self):
        manager = SandboxManager(SandboxConfig(enabled=True))
        assert manager.should_sandbox("ls -la") is True

    def test_mode_none_returns_false(self):
        manager = SandboxManager(SandboxConfig(enabled=True, mode="none"))
        assert manager.should_sandbox("ls -la") is False

    def test_excluded_command(self):
        config = SandboxConfig(enabled=True, excluded_commands=["git push:*"])
        manager = SandboxManager(config)
        assert manager.should_sandbox("git push origin main") is False
        assert manager.should_sandbox("git status") is True

    def test_is_enabled_property(self):
        assert SandboxManager(SandboxConfig(enabled=True)).is_enabled() is True
        assert SandboxManager(SandboxConfig(enabled=False)).is_enabled() is False


# =========================================================================
# should_use_sandbox (thin wrapper)
# =========================================================================

class TestShouldUseSandboxWrapper:
    """Test the should_use_sandbox top-level function."""

    @patch("src.tools.shell.should_use_sandbox.SandboxManager")
    def test_delegates_to_manager(self, MockManager):
        instance = MockManager.return_value
        instance.should_sandbox.return_value = True
        assert should_use_sandbox("ls") is True
        instance.should_sandbox.assert_called_once_with("ls")


# =========================================================================
# SandboxManager.wrap_command
# =========================================================================

class TestWrapCommand:
    """Test command wrapping for different backends."""

    def test_bwrap_wrap_contains_ro_bind(self):
        """bwrap wrapper should contain --ro-bind / /."""
        config = SandboxConfig(enabled=True, mode="bwrap")
        manager = SandboxManager(config)
        with patch("shutil.which", return_value="/usr/bin/bwrap"):
            wrapped = manager.wrap_command("ls -la", cwd="/tmp/test")
        assert "--ro-bind" in wrapped
        assert "ls -la" in wrapped

    def test_bwrap_wrap_network_isolation(self):
        config = SandboxConfig(enabled=True, mode="bwrap", network_isolation=True)
        manager = SandboxManager(config)
        with patch("shutil.which", return_value="/usr/bin/bwrap"):
            wrapped = manager.wrap_command("curl example.com", cwd="/tmp/test")
        assert "--unshare-net" in wrapped

    def test_bwrap_wrap_die_with_parent(self):
        config = SandboxConfig(enabled=True, mode="bwrap")
        manager = SandboxManager(config)
        with patch("shutil.which", return_value="/usr/bin/bwrap"):
            wrapped = manager.wrap_command("ls", cwd="/tmp/test")
        assert "--die-with-parent" in wrapped

    def test_docker_wrap_contains_docker_run(self):
        config = SandboxConfig(enabled=True, mode="docker")
        manager = SandboxManager(config)
        with patch("shutil.which", return_value="/usr/bin/docker"):
            wrapped = manager.wrap_command("ls -la", cwd="/tmp/test")
        assert "docker" in wrapped
        assert "run" in wrapped
        assert "ls -la" in wrapped

    def test_docker_network_none(self):
        config = SandboxConfig(enabled=True, mode="docker", network_isolation=True)
        manager = SandboxManager(config)
        with patch("shutil.which", return_value="/usr/bin/docker"):
            wrapped = manager.wrap_command("ls", cwd="/tmp/test")
        assert "--network" in wrapped
        assert "none" in wrapped

    def test_mode_none_returns_original(self):
        config = SandboxConfig(enabled=True, mode="none")
        manager = SandboxManager(config)
        assert manager.wrap_command("ls -la") == "ls -la"

    def test_bwrap_not_installed_fallback(self):
        """When bwrap is not installed, return original command."""
        config = SandboxConfig(enabled=True, mode="bwrap")
        manager = SandboxManager(config)
        with patch("shutil.which", return_value=None):
            wrapped = manager.wrap_command("ls -la")
        assert wrapped == "ls -la"


# =========================================================================
# Availability check
# =========================================================================

class TestAvailability:
    """Test sandbox backend availability detection."""

    def test_bwrap_available(self):
        config = SandboxConfig(mode="bwrap")
        manager = SandboxManager(config)
        with patch("shutil.which", return_value="/usr/bin/bwrap"):
            assert manager.is_available() is True

    def test_bwrap_not_available(self):
        config = SandboxConfig(mode="bwrap")
        manager = SandboxManager(config)
        with patch("shutil.which", return_value=None):
            assert manager.is_available() is False
            reason = manager.get_unavailable_reason()
            assert reason is not None
            assert "bwrap" in reason.lower()

    def test_mode_none_always_available(self):
        config = SandboxConfig(mode="none")
        manager = SandboxManager(config)
        assert manager.is_available() is True
        assert manager.get_unavailable_reason() is None
