import os
import sys
from pathlib import Path
from threading import Lock

from src.lib.config import C
from src.lib.logging import get_logger

logger = get_logger(__name__)

_MOUNT_LOCK = Lock()
_LAST_MOUNTED_WORKSPACE: Path | None = None
_ORIGINAL_CWD: Path | None = None


def resolve_workspace_root() -> Path:
    """Return the project root directory.

    The workspace root is always equal to ``C.agent_root``.
    """
    return Path(C.agent_root).resolve()


def ensure_workspace_mounted_once(ensure_src_path: bool = True) -> None:
    global _LAST_MOUNTED_WORKSPACE, _ORIGINAL_CWD

    try:
        target_cwd = resolve_workspace_root()
    except Exception as exc:
        logger.warning("Failed to resolve workspace root: %s", exc)
        return

    if not target_cwd.exists():
        logger.warning("Configured workspace root does not exist: %s", target_cwd)
        return

    with _MOUNT_LOCK:
        current_cwd = Path.cwd().resolve()
        if _LAST_MOUNTED_WORKSPACE == target_cwd and current_cwd == target_cwd:
            return

        if _ORIGINAL_CWD is None:
            _ORIGINAL_CWD = current_cwd

        if ensure_src_path:
            original_cwd_str = str(_ORIGINAL_CWD)
            if original_cwd_str not in sys.path:
                logger.info("Adding original CWD to sys.path: %s", original_cwd_str)
                sys.path.insert(0, original_cwd_str)

        if current_cwd != target_cwd:
            logger.info("Mounting workspace: %s", target_cwd)
            os.chdir(target_cwd)
            logger.info("Current Working Directory is now: %s", os.getcwd())

        _LAST_MOUNTED_WORKSPACE = target_cwd
