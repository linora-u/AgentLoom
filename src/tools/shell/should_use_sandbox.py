"""Shell sandbox decision logic.

Determines whether a given shell command should be executed
inside a sandbox.  This is the bridge between the shell tool
and the sandbox manager (src.utils.sandbox).

Decision flow:
1. Is sandbox enabled globally? → No → skip
2. Is mode "none"? → skip
3. Does command match exclusion pattern? → skip
4. Default → use sandbox
"""

from src.utils.sandbox import SandboxManager


def should_use_sandbox(command: str) -> bool:
    """Determine whether a command should run in a sandbox.

    Reads configuration from ``shell_settings.sandbox`` in system.yaml.

    Args:
        command: The shell command to evaluate.

    Returns:
        True if the command should be sandboxed.
    """
    manager = SandboxManager()
    return manager.should_sandbox(command)


def get_sandbox_manager() -> SandboxManager:
    """Return a configured SandboxManager instance.

    Call this to wrap commands when ``should_use_sandbox()`` returns True.
    """
    return SandboxManager()
