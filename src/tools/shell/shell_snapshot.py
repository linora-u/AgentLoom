"""Shell environment snapshot — capture user's shell config at session start.

Runs a detection script in the user's login shell to capture aliases,
functions, and shell options, then saves the result as a sourceable
``snapshot.sh`` file.  Subsequent commands ``source`` this snapshot to
restore the user's environment without needing a long-lived PTY session.

Design aligned with Claude Code's ShellSnapshot.ts.
"""

import os
import subprocess
import tempfile
from typing import Optional

from src.lib.logging import get_logger

logger = get_logger(__name__)

# Maximum time allowed for snapshot generation.
_SNAPSHOT_TIMEOUT = 10  # seconds

# Maximum output size from the snapshot script (1 MB).
_SNAPSHOT_MAX_BUFFER = 1024 * 1024


def _build_bash_snapshot_script() -> str:
    """Build the bash snapshot capture script.

    Captures: functions, options, aliases, PATH.
    """
    return r"""
# --- AgentLoom snapshot capture (bash) ---
# Functions
declare -f 2>/dev/null || true

echo '# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---'

# Shell options
shopt -p 2>/dev/null || true

echo '# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---'

# Aliases
alias -p 2>/dev/null || alias 2>/dev/null || true

echo '# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---'

# PATH — preserve the fully resolved PATH from the login shell
echo "export PATH=\"$PATH\""
"""


def _build_zsh_snapshot_script() -> str:
    """Build the zsh snapshot capture script.

    Captures: functions, options, aliases, PATH.
    """
    return r"""
# --- AgentLoom snapshot capture (zsh) ---
# Functions
typeset -f 2>/dev/null || true

echo '# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---'

# Shell options
setopt 2>/dev/null || true

echo '# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---'

# Aliases
alias -L 2>/dev/null || alias 2>/dev/null || true

echo '# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---'

# PATH — preserve the fully resolved PATH from the login shell
echo "export PATH=\"$PATH\""
"""


def _format_zsh_options(options_block: str) -> str:
    """Convert zsh ``setopt`` output into sourceable ``setopt <name>`` lines."""
    lines = []
    for raw_line in options_block.splitlines():
        option = raw_line.strip()
        if not option or option.startswith("#"):
            continue
        if option.startswith("setopt "):
            lines.append(option)
        else:
            lines.append(f"setopt {option} 2>/dev/null || true")
    return "\n".join(lines)


def _build_snapshot_content(raw_output: str, shell_path: str) -> str:
    """Parse raw snapshot output and build a sourceable script.

    Args:
        raw_output: Combined stdout from the snapshot capture script.
        shell_path: Path to the shell binary (for determining bash vs zsh).

    Returns:
        A string suitable for writing to a snapshot.sh file.
    """
    is_zsh = "zsh" in os.path.basename(shell_path).lower()

    parts = raw_output.split("# --- AGENTLOOM_SNAPSHOT_SEPARATOR ---")
    functions_block = parts[0].strip() if len(parts) > 0 else ""
    options_block = parts[1].strip() if len(parts) > 1 else ""
    aliases_block = parts[2].strip() if len(parts) > 2 else ""
    path_block = parts[3].strip() if len(parts) > 3 else ""

    lines = [
        "# AgentLoom shell environment snapshot",
        "# Auto-generated — do not edit",
        "",
    ]

    # PATH — must come first so functions/aliases can find binaries.
    if path_block:
        lines.append("# --- Captured PATH ---")
        lines.append(path_block)
        lines.append("")

    # Functions
    if functions_block:
        lines.append("# --- Captured functions ---")
        lines.append(functions_block)
        lines.append("")

    # Options
    if options_block:
        lines.append("# --- Captured options ---")
        if is_zsh:
            options_block = _format_zsh_options(options_block)
        lines.append(options_block)
        lines.append("")

    # Aliases
    if aliases_block:
        lines.append("# --- Captured aliases ---")
        lines.append(aliases_block)
        lines.append("")

    # Inject extglob protection to prevent TOCTOU attacks.
    # Validation sees the raw command; if extglob is on, the shell might
    # re-parse patterns between validation and execution.
    if is_zsh:
        lines.append("# --- Extglob protection (TOCTOU defense) ---")
        lines.append("setopt NO_EXTENDED_GLOB 2>/dev/null || true")
    else:
        lines.append("# --- Extglob protection (TOCTOU defense) ---")
        lines.append("shopt -u extglob 2>/dev/null || true")

    lines.append("")
    return "\n".join(lines)


def create_snapshot(
    shell_path: str,
    env: Optional[dict] = None,
) -> Optional[str]:
    """Capture the user's shell environment and save it as a snapshot file.

    Runs the shell in login mode (``-l``) to pick up ``~/.bashrc`` /
    ``~/.zshrc`` initialisation, then captures functions, options, and
    aliases.

    Args:
        shell_path: Absolute path to the bash or zsh binary.
        env: Optional environment dict (defaults to sanitised env).

    Returns:
        Absolute path to the generated snapshot file, or ``None`` if
        snapshot generation failed (caller should fall back to ``-l``
        login mode).
    """
    is_zsh = "zsh" in os.path.basename(shell_path).lower()
    script = _build_zsh_snapshot_script() if is_zsh else _build_bash_snapshot_script()

    if env is None:
        from src.tools.shell.subprocess_env import build_subprocess_env
        env = build_subprocess_env()

    try:
        result = subprocess.run(
            [shell_path, "-l", "-c", script],
            capture_output=True,
            text=True,
            timeout=_SNAPSHOT_TIMEOUT,
            env=env,
            errors="replace",
        )
        raw_output = result.stdout or ""
    except subprocess.TimeoutExpired:
        logger.warning(
            "Snapshot generation timed out after %ds for %s — "
            "falling back to login shell mode",
            _SNAPSHOT_TIMEOUT, shell_path,
        )
        return None
    except Exception as exc:
        logger.warning(
            "Snapshot generation failed for %s: %s — "
            "falling back to login shell mode",
            shell_path, exc,
        )
        return None

    if not raw_output.strip():
        logger.debug("Snapshot output is empty for %s — skipping", shell_path)
        return None

    content = _build_snapshot_content(raw_output, shell_path)

    # Write to a temp file that persists for the session lifetime.
    try:
        fd, snapshot_path = tempfile.mkstemp(
            prefix="agentloom_snapshot_",
            suffix=".sh",
        )
        with os.fdopen(fd, "w") as f:
            f.write(content)
        logger.debug(
            "Shell snapshot created: %s (%d bytes)",
            snapshot_path, len(content),
        )
        return snapshot_path
    except Exception as exc:
        logger.warning("Failed to write snapshot file: %s", exc)
        return None


def remove_snapshot(snapshot_path: Optional[str]) -> None:
    """Remove a snapshot file (best-effort cleanup)."""
    if snapshot_path and os.path.exists(snapshot_path):
        try:
            os.remove(snapshot_path)
        except OSError:
            pass
