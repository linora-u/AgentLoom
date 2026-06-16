"""Shell session state manager for stateless subprocess execution.

Maintains per-agent session state (CWD) across multiple command
invocations without keeping a persistent process.  Each command spawns
a fresh subprocess; session continuity is achieved by replaying the
captured CWD and sourcing the environment snapshot.

Environment variables are NOT tracked between commands — ``export``
statements are ephemeral and die with the subprocess (aligned with
Claude Code's design where only hook scripts and ``/env`` persist
env vars, not implicit command-side exports).
"""

import os
import tempfile
import uuid
from typing import Optional

from src.lib.logging import get_logger

logger = get_logger(__name__)


class ShellSession:
    """Tracks session state for a stateless shell execution model.

    Instead of maintaining a long-lived PTY process, each command
    execution spawns a fresh subprocess.  This class preserves context
    between invocations by tracking:

    - **CWD**: updated after each command via an out-of-band temp file.

    The session is identified by a unique ``session_id`` which is used to
    namespace temporary files so multiple sessions never collide.

    Note: Environment variable deltas are intentionally NOT tracked.
    Each command inherits the baseline environment from the parent
    process plus the snapshot (which includes PATH).  ``export``
    statements within a command are ephemeral.
    """

    def __init__(self, session_id: Optional[str] = None):
        self._session_id = session_id or uuid.uuid4().hex[:12]
        self._cwd: Optional[str] = None

        # Temp dir for session state files (CWD tracking).
        # Created lazily on first use.
        self._state_dir: Optional[str] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def cwd(self) -> Optional[str]:
        """Current working directory as of the last command execution."""
        return self._cwd

    @cwd.setter
    def cwd(self, value: Optional[str]) -> None:
        self._cwd = value

    # ------------------------------------------------------------------
    # State file management
    # ------------------------------------------------------------------

    def _ensure_state_dir(self) -> str:
        """Return (and create if needed) the session state directory."""
        if self._state_dir is None:
            self._state_dir = tempfile.mkdtemp(
                prefix=f"agentloom_session_{self._session_id}_"
            )
        return self._state_dir

    @property
    def cwd_file(self) -> str:
        """Path to the temp file where logical ``pwd`` output is written."""
        return os.path.join(self._ensure_state_dir(), "cwd.txt")

    # ------------------------------------------------------------------
    # CWD update
    # ------------------------------------------------------------------

    def update_cwd_from_file(self) -> Optional[str]:
        """Read the CWD tracking file and update internal state.

        Returns the new CWD, or None if the file is missing or invalid.
        """
        try:
            if not os.path.exists(self.cwd_file):
                return None
            raw = open(self.cwd_file, "r").read().strip()
            if raw and os.path.isdir(raw):
                self._cwd = raw
                return raw
            elif raw:
                # Directory may have been deleted by the command itself
                logger.debug(
                    "CWD path from tracking file no longer exists: %s", raw
                )
                return None
        except (OSError, IOError) as exc:
            logger.debug("Failed to read CWD tracking file: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Remove all temporary session files."""
        if self._state_dir and os.path.isdir(self._state_dir):
            import shutil
            try:
                shutil.rmtree(self._state_dir, ignore_errors=True)
            except Exception:
                pass
            self._state_dir = None

    def __del__(self):
        try:
            self.cleanup()
        except Exception:
            pass
