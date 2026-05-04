"""
Heartbeat-based crash detection utilities.

Centralises the logic for determining whether a supervisor or worker
process is still alive based on its heartbeat file.
"""

from __future__ import annotations

import os
import time

# If heartbeat hasn't been updated for this many seconds, consider the
# process dead.  The default heartbeat interval is 5 s, so 30 s gives a
# comfortable margin.
HEARTBEAT_STALE_THRESHOLD: int = 30


def detect_crashed_status(heartbeat: dict | None) -> str:
    """Return ``"crashed"`` if the heartbeat indicates the process is dead,
    otherwise return ``"running"``.

    Detection heuristics (checked in order):

    1. No heartbeat file at all → ``"crashed"``.
    2. ``status`` is ``"stopped"`` or ``"exited"`` but the task_tree still
       says ``"running"`` → ``"crashed"`` (edge-case: clean exit without
       updating the tree).
    3. PID recorded in the heartbeat is no longer alive → ``"crashed"``.
    4. Heartbeat timestamp is older than *HEARTBEAT_STALE_THRESHOLD* →
       ``"crashed"``.
    """
    if heartbeat is None:
        return "crashed"
    # If heartbeat explicitly says stopped/exited, the process exited normally
    # but task_tree wasn't updated (edge case).
    hb_status = heartbeat.get("status", "")
    if hb_status in ("stopped", "exited"):
        return "crashed"
    # Check PID liveness.
    pid = heartbeat.get("pid")
    if pid is not None:
        try:
            os.kill(pid, 0)  # signal 0 = check existence, no actual signal
        except ProcessLookupError:
            return "crashed"
        except PermissionError:
            pass  # process exists but owned by another user → alive
    # Check timestamp freshness.
    ts = heartbeat.get("timestamp")
    if ts is not None:
        age = time.time() - ts
        if age > HEARTBEAT_STALE_THRESHOLD:
            return "crashed"
    return "running"


def detect_worker_call_crashed(
    worker_heartbeat: dict | None,
    call_index: int | str,
) -> str:
    """Determine whether a specific worker *call* has crashed.

    Uses the **per-worker aggregated heartbeat** file which contains::

        {"agent_name": "...", "timestamp": ..., "calls": {"0": {...}, ...}}

    Returns ``"crashed"`` when the worker heartbeat is stale **and** the
    call's recorded status is still ``"running"``.  Otherwise returns the
    call's own status (or ``"unknown"``).
    """
    if worker_heartbeat is None:
        return "unknown"

    calls = worker_heartbeat.get("calls", {})
    call = calls.get(str(call_index))
    if call is None:
        return "unknown"

    call_status = call.get("status", "unknown")
    # Already in a terminal state → trust it.
    if call_status in ("completed", "failed"):
        return call_status

    # Still "running" – check heartbeat freshness.
    ts = worker_heartbeat.get("timestamp")
    if ts is not None:
        age = time.time() - ts
        if age > HEARTBEAT_STALE_THRESHOLD:
            return "crashed"

    return call_status
