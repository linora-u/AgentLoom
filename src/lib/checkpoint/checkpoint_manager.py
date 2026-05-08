"""
Checkpoint persistence manager.

Manages the on-disk checkpoint tree under::

    {logging.dir}/{supervisor_name}/{timestamp}/checkpoints/{task_id}/
        task_tree.json
        heartbeat.json
        checkpoint.json                          # supervisor
        workers/{worker_name}/checkpoint.json    # per worker

Checkpoint data lives inside the per-run timestamp log directory so that
all artefacts for a single run (log, checkpoint, file-history) are
co-located.  A lightweight index file (``.task_index.json``) under each
agent directory maps ``task_id`` to the ``timestamp`` directory name,
enabling fast lookup during ``--resume``.

The base directory defaults to the ``logging.dir`` setting from
``config/system.yaml`` (typically ``.logs``).  Override via ``base_dir``
constructor arg or the ``AGENT_LOOM_RUNTIME_ROOT`` env var.

All writes are **atomic**: data is flushed to a ``.tmp`` file first and
then renamed, so a crash mid-write never corrupts the checkpoint.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from src.lib.checkpoint.serializer import CheckpointSerializer
from src.lib.heartbeat.status import (
    detect_crashed_status as _detect_crashed_status,
    detect_worker_call_crashed as _detect_worker_call_crashed,
)


# =========================================================================
# Runtime-root resolution
# =========================================================================

def _find_agent_loom_root() -> Path:
    """Derive the AgentLoom project root directory.

    Resolution order mirrors ``skills/agent-recall-with-files/scripts/common.py``.
    """
    env_root = os.environ.get("AGENT_LOOM_RUNTIME_ROOT", "").strip()
    if env_root:
        return Path(env_root)

    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "config" / "llm.yaml").exists():
            return current
        current = current.parent

    candidate = Path(__file__).resolve().parent.parent.parent.parent
    if (candidate / "pyproject.toml").exists():
        return candidate

    return Path.cwd()


def _resolve_checkpoint_base_dir() -> Path:
    """Return the base directory for all checkpoint data.

    Resolution order:
    1. ``AGENT_LOOM_RUNTIME_ROOT`` env var  →  ``{value}/.logs``
       (allows tests and CI to redirect all runtime artefacts)
    2. ``logging.dir`` from ``config/system.yaml``
    3. Fallback: ``{project_root}/.logs``

    The result is that checkpoint files live alongside agent run logs
    under the same root directory (typically ``.logs/``).
    """
    # 1. Env-var override (tests / CI).
    env_root = os.environ.get("AGENT_LOOM_RUNTIME_ROOT", "").strip()
    if env_root:
        return Path(env_root).resolve()

    # 2. Config-driven.
    try:
        from src.lib.config import C
        log_dir = C.get_nested("logging", "dir", default=".logs")
    except Exception:
        log_dir = ".logs"

    base = Path(log_dir)
    if not base.is_absolute():
        base = _find_agent_loom_root() / base
    return base.resolve()


# =========================================================================
# Task-tree migration helpers
# =========================================================================


def _migrate_task_tree_workers(tree: dict) -> dict:
    """Auto-upgrade workers from v1 (single dict) to v2 (list of calls).

    v1 format::

        "workers": {"w1": {"status": "completed", ...}}

    v2 format::

        "workers": {"w1": [{"call_index": 0, "status": "completed", ...}]}
    """
    workers = tree.get("workers")
    if not isinstance(workers, dict):
        return tree
    migrated = False
    for name, entry in list(workers.items()):
        if isinstance(entry, dict):
            # v1 → v2: wrap single dict in a list
            entry.setdefault("call_index", 0)
            entry.setdefault("input_hash", "")
            workers[name] = [entry]
            migrated = True
    return tree





# =========================================================================
# CheckpointManager
# =========================================================================


class CheckpointManager:
    """Manages checkpoint files for a single supervisor agent.

    Checkpoint layout (v2 — per-run timestamp directory)::

        {base_dir}/{supervisor_name}/{timestamp}/checkpoints/{task_id}/
            task_tree.json
            heartbeat.json
            checkpoint.json
            workers/{worker_name}/checkpoint.json

    A lightweight index file keeps the ``task_id → timestamp`` mapping::

        {base_dir}/{supervisor_name}/.task_index.json

    Args:
        supervisor_name: The YAML ``name`` field of the supervisor agent.
        base_dir: Override the logging root (mainly for tests).
            Defaults to ``logging.dir`` from system config.
        run_log_dir: The per-run timestamp log directory for the current
            execution (e.g. ``.logs/agent/20260413_104447/``).  When
            provided, new checkpoints are stored under this directory.
            When *None* (e.g. for ``list-tasks`` / ``clean-tasks``),
            the manager operates in **scan-only** mode and resolves
            task directories via the index file or directory scan.
    """

    def __init__(
        self,
        supervisor_name: str,
        base_dir: Path | None = None,
        run_log_dir: Path | None = None,
    ):
        self._supervisor_name = supervisor_name
        self._explicit_base_dir = base_dir is not None
        if base_dir is None:
            base_dir = _resolve_checkpoint_base_dir()
        self._base_dir = base_dir
        # Agent-level root: .logs/{supervisor_name}/
        self._agent_root = base_dir / supervisor_name

        # Per-run checkpoint root.
        if run_log_dir is not None:
            run_log_dir = Path(run_log_dir).resolve()
            self._checkpoints_root: Path | None = run_log_dir / "checkpoints"
        elif self._explicit_base_dir:
            # Legacy / test mode: caller passed base_dir explicitly but no
            # run_log_dir.  Fall back to the v1 flat layout for backward
            # compatibility (tests, CLI scan-then-write).
            self._checkpoints_root = self._agent_root / "checkpoints"
        else:
            # Production scan-only mode (list-tasks, clean-tasks).
            self._checkpoints_root = None

        import threading
        self._tree_lock = threading.Lock()
        self._index_lock = threading.Lock()

    # ── path helpers ─────────────────────────────────────────────────────

    def _task_dir(self, task_id: str) -> Path:
        """Return the on-disk directory for *task_id*.

        Resolution order:
        1. If ``_checkpoints_root`` is set (active run or legacy/test mode),
           return ``{_checkpoints_root}/{task_id}/`` directly.
        2. Otherwise (scan-only mode) consult the task index for a previously
           saved mapping.
        3. Fall back to scanning all timestamp directories.
        4. Also check the legacy flat layout under ``{agent_root}/checkpoints/``.
        """
        if self._checkpoints_root is not None:
            return self._checkpoints_root / task_id

        # Scan-only mode: resolve via index or directory scan.
        idx_dir = self._resolve_task_run_dir(task_id)
        if idx_dir is not None:
            return idx_dir / "checkpoints" / task_id

        # Check legacy flat layout: {agent_root}/checkpoints/{task_id}/
        legacy = self._agent_root / "checkpoints" / task_id
        if legacy.is_dir():
            return legacy

        # Should not normally reach here for known tasks.
        raise FileNotFoundError(
            f"Cannot locate checkpoint directory for task {task_id} "
            f"under {self._agent_root}/"
        )

    def _supervisor_ckpt(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "checkpoint.json"

    def _worker_ckpt(self, task_id: str, worker_name: str) -> Path:
        return self._task_dir(task_id) / "workers" / worker_name / "checkpoint.json"

    def _task_tree_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "task_tree.json"

    def _heartbeat_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "heartbeat.json"

    # ── task index (task_id → timestamp directory) ───────────────────────

    def _index_path(self) -> Path:
        """Return path to ``.task_index.json`` under the agent root."""
        return self._agent_root / ".task_index.json"

    def _load_index(self) -> dict:
        """Load the full task index (thread-safe)."""
        with self._index_lock:
            return self._read_json(self._index_path()) or {}

    def _save_index(self, index: dict) -> None:
        """Persist the full task index atomically (thread-safe)."""
        with self._index_lock:
            self._atomic_write(self._index_path(), index)

    def save_task_to_index(self, task_id: str) -> None:
        """Record the mapping ``task_id → current run_log_dir`` in the index.

        Must be called during task creation (not resume).  Requires that
        ``run_log_dir`` was provided at construction time.
        """
        if self._checkpoints_root is None:
            return  # scan-only mode — nothing to index.
        run_dir_name = self._checkpoints_root.parent.name
        with self._index_lock:
            index = self._read_json(self._index_path()) or {}
            index[task_id] = {
                "run_dir": run_dir_name,
                "created_at": datetime.now().astimezone().isoformat(),
            }
            self._atomic_write(self._index_path(), index)

    def remove_task_from_index(self, task_id: str) -> None:
        """Remove a task entry from the index (e.g. after cleanup)."""
        with self._index_lock:
            index = self._read_json(self._index_path()) or {}
            if task_id in index:
                del index[task_id]
                self._atomic_write(self._index_path(), index)

    def _resolve_task_run_dir(self, task_id: str) -> Path | None:
        """Find the timestamp run-dir that contains *task_id*.

        1. Consult the index file first (fast O(1) lookup).
        2. Fall back to scanning all timestamp directories (slow).
        """
        # 1. Index lookup.
        index = self._load_index()
        entry = index.get(task_id)
        if entry:
            run_dir_name = entry.get("run_dir", "")
            candidate = self._agent_root / run_dir_name
            if (candidate / "checkpoints" / task_id).is_dir():
                return candidate

        # 2. Full directory scan.
        return self._scan_for_task(task_id)

    def _scan_for_task(self, task_id: str) -> Path | None:
        """Scan all timestamp directories for *task_id* (slow fallback).

        Only searches the v2 layout (timestamp dirs containing ``checkpoints/``).
        The legacy flat layout is handled separately in ``_task_dir``.
        """
        if not self._agent_root.is_dir():
            return None
        for child in sorted(self._agent_root.iterdir(), reverse=True):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name == "checkpoints":
                continue  # Skip legacy flat directory.
            candidate = child / "checkpoints" / task_id
            if candidate.is_dir():
                return child
        return None

    # ── atomic write ─────────────────────────────────────────────────────

    @staticmethod
    def _atomic_write(path: Path, data: dict) -> None:
        """Write *data* as JSON atomically (tmp + rename)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Use PID+thread to avoid tmp-file collisions under concurrency.
        import threading
        tid = threading.get_ident()
        tmp = path.with_suffix(f".{os.getpid()}.{tid}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        """Read a JSON file, returning *None* when absent or corrupt."""
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    # ── task tree ────────────────────────────────────────────────────────

    def save_task_tree(self, task_id: str, tree: dict) -> Path:
        """Persist the execution-tree metadata (thread-safe)."""
        with self._tree_lock:
            p = self._task_tree_path(task_id)
            self._atomic_write(p, tree)
            return p

    def load_task_tree(self, task_id: str) -> dict | None:
        with self._tree_lock:
            tree = self._read_json(self._task_tree_path(task_id))
        if tree is not None:
            tree = _migrate_task_tree_workers(tree)
        return tree

    def update_task_tree(self, task_id: str, updater) -> dict:
        """Atomically read-modify-write the task tree (thread-safe).

        Args:
            task_id: The task to update.
            updater: A callable ``(tree: dict) -> dict`` that receives
                the current tree (or ``{}``) and returns the modified
                tree to persist.

        Returns:
            The updated tree dict.
        """
        with self._tree_lock:
            tree = self._read_json(self._task_tree_path(task_id)) or {}
            if tree:
                tree = _migrate_task_tree_workers(tree)
            updated = updater(tree)
            p = self._task_tree_path(task_id)
            self._atomic_write(p, updated)
            return updated

    # ── supervisor checkpoint ────────────────────────────────────────────

    def save_supervisor_checkpoint(
        self,
        task_id: str,
        *,
        memory_steps: list,
        task_text: str,
        status: str,
        config_snapshot: dict | None = None,
        result: str | None = None,
        error: str | None = None,
    ) -> Path:
        """Save the supervisor's ``memory.steps`` plus metadata."""
        data = {
            "agent_name": self._supervisor_name,
            "agent_type": "supervisor",
            "task_id": task_id,
            "task_text": task_text,
            "status": status,
            "step_count": len(memory_steps),
            "memory_steps": CheckpointSerializer.serialize_memory_steps(memory_steps),
            "saved_at": datetime.now().astimezone().isoformat(),
        }
        if config_snapshot:
            data["config_snapshot"] = config_snapshot
        if result is not None:
            data["result"] = result
        if error is not None:
            data["error"] = error
        p = self._supervisor_ckpt(task_id)
        self._atomic_write(p, data)
        return p

    def load_supervisor_checkpoint(self, task_id: str) -> dict | None:
        return self._read_json(self._supervisor_ckpt(task_id))

    # ── worker checkpoint ────────────────────────────────────────────────

    def save_worker_checkpoint(
        self,
        task_id: str,
        worker_name: str,
        *,
        call_index: int = 0,
        input_hash: str = "",
        memory_steps: list | None = None,
        task_input: str = "",
        status: str = "completed",
        result: str | None = None,
        error: str | None = None,
    ) -> Path:
        data: dict[str, Any] = {
            "agent_name": worker_name,
            "agent_type": "worker",
            "task_id": task_id,
            "call_index": call_index,
            "input_hash": input_hash,
            "task_input": task_input,
            "status": status,
            "saved_at": datetime.now().astimezone().isoformat(),
        }
        if memory_steps:
            data["step_count"] = len(memory_steps)
            data["memory_steps"] = CheckpointSerializer.serialize_memory_steps(memory_steps)
        if result is not None:
            data["result"] = result
        if error is not None:
            data["error"] = error
        p = self._worker_ckpt(task_id, worker_name)
        self._atomic_write(p, data)
        return p

    def load_worker_checkpoint(self, task_id: str, worker_name: str) -> dict | None:
        return self._read_json(self._worker_ckpt(task_id, worker_name))

    # ── listing / enumeration ────────────────────────────────────────────

    def _iter_all_task_dirs(self) -> list[Path]:
        """Return every ``checkpoints/{task_id}/`` directory under the agent root.

        Scans all timestamp sub-directories for their ``checkpoints/`` children.
        Also checks the legacy flat layout (``{agent_root}/checkpoints/``) for
        backward compatibility with tests and explicit ``base_dir`` usage.
        """
        if not self._agent_root.is_dir():
            return []
        seen: set[str] = set()
        task_dirs: list[Path] = []
        for child in sorted(self._agent_root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            # Check both the timestamp layout ({timestamp}/checkpoints/)
            # and the legacy flat layout (checkpoints/ directly under agent root).
            ckpt_root = child / "checkpoints" if child.name != "checkpoints" else child
            if not ckpt_root.is_dir():
                continue
            for td in sorted(ckpt_root.iterdir()):
                if td.is_dir() and td.name not in seen:
                    seen.add(td.name)
                    task_dirs.append(td)
        return task_dirs

    def list_tasks(self) -> list[dict]:
        """Return metadata for every checkpoint under this supervisor.

        Each entry contains at least ``task_id``, ``agent_name``, ``status``,
        and ``saved_at``.  Tasks whose ``status`` is ``"running"`` but whose
        heartbeat PID is dead are reported as ``"crashed"``.
        """
        all_task_dirs = self._iter_all_task_dirs()
        if not all_task_dirs:
            return []

        entries: list[dict] = []
        for task_dir in all_task_dirs:
            if not task_dir.is_dir():
                continue
            tree = self._read_json(task_dir / "task_tree.json")
            if tree:
                entry = {
                    "task_id": tree.get("task_id", task_dir.name),
                    "agent_name": tree.get("agent_name", self._supervisor_name),
                    "status": tree.get("status", "unknown"),
                    "created_at": tree.get("created_at", ""),
                    "interrupted_at": tree.get("interrupted_at", ""),
                }
            else:
                # Fallback: derive from supervisor checkpoint
                sup = self._read_json(task_dir / "checkpoint.json")
                if sup:
                    entry = {
                        "task_id": sup.get("task_id", task_dir.name),
                        "agent_name": sup.get("agent_name", self._supervisor_name),
                        "status": sup.get("status", "unknown"),
                        "created_at": sup.get("saved_at", ""),
                        "interrupted_at": sup.get("saved_at", ""),
                    }
                else:
                    continue

            # Read heartbeat for crash detection + dashboard metadata.
            hb = self._read_json(task_dir / "heartbeat.json")
            if entry["status"] == "running":
                entry["status"] = _detect_crashed_status(hb)

            # Attach heartbeat details for dashboard display.
            if hb:
                entry["step"] = hb.get("step")
                entry["pid"] = hb.get("pid")
                hb_ts = hb.get("timestamp")
                entry["heartbeat_ts"] = hb_ts
                entry["heartbeat_age"] = (
                    round(time.time() - hb_ts, 1) if hb_ts else None
                )
            else:
                entry["step"] = None
                entry["pid"] = None
                entry["heartbeat_ts"] = None
                entry["heartbeat_age"] = None

            # ── File history stats (snapshot count + tracked file count) ──
            fh_index = self._read_json(task_dir / "file-history" / "snapshots.json")
            if fh_index:
                snaps = fh_index.get("snapshots", [])
                tracked = set()
                for s in snaps:
                    tracked.update(s.get("tracked_file_backups", {}).keys())
                entry["fh_snapshots"] = len(snaps)
                entry["fh_tracked_files"] = len(tracked)
            else:
                entry["fh_snapshots"] = 0
                entry["fh_tracked_files"] = 0

            # ── Worker details (from task_tree + per-worker heartbeat) ──
            workers_detail: list[dict] = []
            tree_workers = (tree or {}).get("workers", {})
            for w_name, w_calls in tree_workers.items():
                if not isinstance(w_calls, list):
                    w_calls = [w_calls]
                # Try to read the per-worker heartbeat file.
                w_hb_path = task_dir / "workers" / w_name / "heartbeat.json"
                w_hb = self._read_json(w_hb_path)
                for w_call in w_calls:
                    ci = w_call.get("call_index", 0)
                    w_status = w_call.get("status", "unknown")
                    # Crash detection: if task_tree says running, check heartbeat.
                    if w_status == "running":
                        if w_hb is not None:
                            w_status = _detect_worker_call_crashed(w_hb, ci)
                        elif entry["status"] == "crashed":
                            # No worker heartbeat; supervisor crashed → infer.
                            w_status = "crashed"
                    w_detail: dict = {
                        "agent_name": w_name,
                        "call_index": ci,
                        "status": w_status,
                        "error": w_call.get("error"),
                    }
                    # Enrich from worker heartbeat calls dict.
                    if w_hb is not None:
                        hb_call = w_hb.get("calls", {}).get(str(ci), {})
                        w_detail["step"] = hb_call.get("step")
                        w_detail["thread_id"] = hb_call.get("thread_id")
                        w_detail["started_at"] = hb_call.get("started_at")
                        w_detail["finished_at"] = hb_call.get("finished_at")
                        w_hb_ts = w_hb.get("timestamp")
                        w_detail["heartbeat_age"] = (
                            round(time.time() - w_hb_ts, 1) if w_hb_ts else None
                        )
                    else:
                        w_detail["step"] = None
                        w_detail["heartbeat_age"] = None
                    workers_detail.append(w_detail)
            entry["workers"] = workers_detail

            entries.append(entry)
        return entries

    # ── cleanup ──────────────────────────────────────────────────────────

    def delete_task(self, task_id: str) -> bool:
        """Delete all checkpoint files for *task_id* and remove from index."""
        try:
            d = self._task_dir(task_id)
        except FileNotFoundError:
            self.remove_task_from_index(task_id)
            return False
        if d.exists():
            shutil.rmtree(d)
            self.remove_task_from_index(task_id)
            return True
        return False

    def cleanup_old_tasks(self, max_age_seconds: int = 7 * 86400) -> int:
        """Remove checkpoints older than *max_age_seconds*.  Returns count."""
        all_task_dirs = self._iter_all_task_dirs()
        if not all_task_dirs:
            return 0

        cutoff = time.time() - max_age_seconds
        removed = 0
        for task_dir in all_task_dirs:
            try:
                mtime = task_dir.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                task_id = task_dir.name
                shutil.rmtree(task_dir, ignore_errors=True)
                self.remove_task_from_index(task_id)
                removed += 1
        return removed


# =========================================================================
# Cross-supervisor listing (for CLI ``list-tasks``)
# =========================================================================


def list_all_tasks(base_dir: Path | None = None) -> list[dict]:
    """Scan **all** supervisor directories for checkpoint tasks.

    Used by ``loom list-tasks``.  Scans every agent directory and its
    timestamp sub-directories for checkpoint data.  Also checks the legacy
    flat layout (``{agent}/checkpoints/``).
    """
    if base_dir is None:
        base_dir = _resolve_checkpoint_base_dir()
    if not base_dir.exists():
        return []

    all_entries: list[dict] = []
    for agent_dir in sorted(base_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        # Check if any child contains checkpoint data (v2 timestamp layout
        # or legacy flat layout).
        has_checkpoints = False
        for child in agent_dir.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            # Legacy flat: {agent}/checkpoints/
            if child.name == "checkpoints":
                has_checkpoints = True
                break
            # v2: {agent}/{timestamp}/checkpoints/
            if (child / "checkpoints").is_dir():
                has_checkpoints = True
                break
        if not has_checkpoints:
            continue
        cm = CheckpointManager(agent_dir.name, base_dir=base_dir)
        all_entries.extend(cm.list_tasks())
    return all_entries
