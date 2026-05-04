"""Sandbox manager — wraps shell commands with OS-level isolation.

Supports two sandbox backends:
- ``bwrap`` (bubblewrap) — Linux namespace isolation (default)
- ``docker`` — Docker container isolation

Configuration (config/system.yaml):

    tools:
      shell:
        sandbox:
          enabled: false
          mode: "bwrap"           # bwrap | docker | none
          allow_write: [".", "/tmp"]
          deny_write: ["/etc", "/usr"]
          network_isolation: false
          excluded_commands: []
"""

import shutil
from dataclasses import dataclass, field
from typing import List, Optional

from src.lib.config import C
from src.lib.logging import get_logger

logger = get_logger(__name__)


@dataclass
class SandboxConfig:
    """Runtime sandbox configuration."""
    enabled: bool = False
    mode: str = "bwrap"                            # bwrap | docker | none
    allow_write: List[str] = field(default_factory=lambda: [".", "/tmp"])
    deny_write: List[str] = field(default_factory=lambda: ["/etc", "/usr"])
    network_isolation: bool = False
    excluded_commands: List[str] = field(default_factory=list)


def _load_sandbox_config() -> SandboxConfig:
    """Load sandbox configuration from system.yaml."""
    raw = C.get_nested("shell_settings", "sandbox", default=None)
    if raw is None or not isinstance(raw, dict):
        return SandboxConfig()

    return SandboxConfig(
        enabled=bool(raw.get("enabled", False)),
        mode=str(raw.get("mode", "bwrap")),
        allow_write=list(raw.get("allow_write", [".", "/tmp"])),
        deny_write=list(raw.get("deny_write", ["/etc", "/usr"])),
        network_isolation=bool(raw.get("network_isolation", False)),
        excluded_commands=list(raw.get("excluded_commands", [])),
    )


def _match_excluded_command(command: str, patterns: List[str]) -> bool:
    """Check if a command matches any exclusion pattern.

    Supported patterns:
    - Exact:   "npm run lint"     → matches only exact string
    - Prefix:  "docker:*"         → matches "docker ps", "docker build", etc.
    - Simple:  "docker"           → matches "docker" as first word
    """
    if not patterns:
        return False

    stripped = command.strip()
    for pattern in patterns:
        pattern = pattern.strip()
        if not pattern:
            continue

        if pattern.endswith(":*"):
            prefix = pattern[:-2]
            if stripped == prefix or stripped.startswith(prefix + " "):
                return True
        elif stripped == pattern:
            return True

    return False


class SandboxManager:
    """Manages sandbox wrapping for shell commands.

    Usage:
        manager = SandboxManager()
        if manager.should_sandbox(command):
            wrapped = manager.wrap_command(command, cwd="/path/to/project")
            # Execute wrapped command instead of original
        else:
            # Execute original command directly
    """

    def __init__(self, config: Optional[SandboxConfig] = None):
        self._config = config or _load_sandbox_config()

    @property
    def config(self) -> SandboxConfig:
        return self._config

    def is_enabled(self) -> bool:
        """Check if sandboxing is globally enabled."""
        return self._config.enabled

    def should_sandbox(self, command: str) -> bool:
        """Determine whether a command should be sandboxed.

        Returns False if:
        - Sandbox is disabled globally
        - Command matches an excluded pattern
        - Sandbox mode is "none"
        """
        if not self._config.enabled:
            return False

        if self._config.mode == "none":
            return False

        if _match_excluded_command(command, self._config.excluded_commands):
            logger.debug("Command excluded from sandbox: %s", command[:80])
            return False

        return True

    def wrap_command(self, command: str, cwd: Optional[str] = None) -> str:
        """Wrap a command with sandbox isolation.

        Args:
            command: The original shell command.
            cwd: Working directory (defaults to current dir).

        Returns:
            The wrapped command string ready for execution.
            If sandbox is not available, returns the original command.
        """
        if self._config.mode == "bwrap":
            return self._wrap_bwrap(command, cwd)
        elif self._config.mode == "docker":
            return self._wrap_docker(command, cwd)
        else:
            return command

    def is_available(self) -> bool:
        """Check if the configured sandbox backend is available."""
        if self._config.mode == "bwrap":
            return shutil.which("bwrap") is not None
        elif self._config.mode == "docker":
            return shutil.which("docker") is not None
        elif self._config.mode == "none":
            return True
        return False

    def get_unavailable_reason(self) -> Optional[str]:
        """Return human-readable reason if sandbox is unavailable."""
        if self._config.mode == "bwrap" and not shutil.which("bwrap"):
            return (
                "bubblewrap (bwrap) is not installed. "
                "Install with: apt install bubblewrap (Debian/Ubuntu) "
                "or dnf install bubblewrap (Fedora)"
            )
        if self._config.mode == "docker" and not shutil.which("docker"):
            return "Docker is not installed or not in PATH."
        return None

    # -----------------------------------------------------------------
    # Backend: bubblewrap
    # -----------------------------------------------------------------

    def _wrap_bwrap(self, command: str, cwd: Optional[str] = None) -> str:
        """Wrap command with bubblewrap namespace isolation."""
        import os
        import shlex

        if not shutil.which("bwrap"):
            logger.warning("bwrap not found, running command without sandbox")
            return command

        cwd = cwd or os.getcwd()
        args = ["bwrap"]

        # Read-only bind root filesystem
        args += ["--ro-bind", "/", "/"]

        # Writable paths
        for path in self._config.allow_write:
            abs_path = os.path.abspath(os.path.join(cwd, path)) if not os.path.isabs(path) else path
            if os.path.exists(abs_path):
                args += ["--bind", abs_path, abs_path]

        # Explicit read-only overrides for denied write paths
        for path in self._config.deny_write:
            if os.path.exists(path):
                args += ["--ro-bind", path, path]

        # Fresh tmpfs for /tmp
        args += ["--tmpfs", "/tmp"]

        # Mount /proc and /dev
        args += ["--proc", "/proc"]
        args += ["--dev", "/dev"]

        # Network isolation
        if self._config.network_isolation:
            args += ["--unshare-net"]

        # PID isolation + cleanup on parent exit
        args += ["--unshare-pid", "--die-with-parent"]

        # Set working directory
        args += ["--chdir", cwd]

        # Execute the command via bash
        args += ["--", "bash", "-c", command]

        return " ".join(shlex.quote(a) for a in args)

    # -----------------------------------------------------------------
    # Backend: Docker
    # -----------------------------------------------------------------

    def _wrap_docker(self, command: str, cwd: Optional[str] = None) -> str:
        """Wrap command with Docker container isolation."""
        import os
        import shlex

        if not shutil.which("docker"):
            logger.warning("docker not found, running command without sandbox")
            return command

        cwd = cwd or os.getcwd()
        args = ["docker", "run", "--rm"]

        # Mount workspace
        for path in self._config.allow_write:
            abs_path = os.path.abspath(os.path.join(cwd, path)) if not os.path.isabs(path) else path
            args += ["-v", f"{abs_path}:{abs_path}"]

        # Network isolation
        if self._config.network_isolation:
            args += ["--network", "none"]

        # Working directory
        args += ["-w", cwd]

        # Use a minimal image
        args += ["ubuntu:latest", "bash", "-c", command]

        return " ".join(shlex.quote(a) for a in args)
