import os
import sys
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from agentloom.config import C
from agentloom.execution.logging import get_logger

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


_ACTIVE_PROJECT_LOCK = Lock()
_ACTIVE_PROJECT: Path | None = None
_ACTIVE_PROJECT_SCOPES = 0


@contextmanager
def bind_workspace_project(project_root: str | Path):
    """Allow overlapping Runs only for one process cwd/project, with no Worker quota.

    Mounting changes process cwd and sys.path. Context copying cannot isolate a
    different project while synchronous Python callbacks are still running.
    Same-project Applications and batches may overlap without a capacity limit.
    """
    global _ACTIVE_PROJECT, _ACTIVE_PROJECT_SCOPES
    root = Path(project_root).resolve()
    with _ACTIVE_PROJECT_LOCK:
        if _ACTIVE_PROJECT_SCOPES and _ACTIVE_PROJECT != root:
            raise RuntimeError('concurrent Application scopes must use the same project root because workspace cwd is process-owned')
        _ACTIVE_PROJECT = root
        _ACTIVE_PROJECT_SCOPES += 1
    try:
        yield
    finally:
        with _ACTIVE_PROJECT_LOCK:
            _ACTIVE_PROJECT_SCOPES -= 1
            if not _ACTIVE_PROJECT_SCOPES:
                _ACTIVE_PROJECT = None
