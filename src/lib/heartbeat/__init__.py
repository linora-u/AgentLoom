"""
Heartbeat writers for agent process liveness monitoring.

Classes:
    SupervisorHeartbeat  — single-process heartbeat for supervisor agents.
    WorkerHeartbeat      — aggregated multi-call heartbeat for worker agents.
    BaseHeartbeatWriter  — abstract base (for custom heartbeat types).

Functions:
    detect_crashed_status        — check supervisor heartbeat for crash.
    detect_worker_call_crashed   — check a specific worker call for crash.

Backward-compatible alias:
    HeartbeatWriter = SupervisorHeartbeat
"""

from src.lib.heartbeat._base import BaseHeartbeatWriter
from src.lib.heartbeat.supervisor_heartbeat import SupervisorHeartbeat
from src.lib.heartbeat.worker_heartbeat import WorkerHeartbeat
from src.lib.heartbeat.status import (
    HEARTBEAT_STALE_THRESHOLD,
    detect_crashed_status,
    detect_worker_call_crashed,
)

# Backward-compatible alias — existing code that imports HeartbeatWriter
# continues to work without changes.
HeartbeatWriter = SupervisorHeartbeat

__all__ = [
    "BaseHeartbeatWriter",
    "SupervisorHeartbeat",
    "HeartbeatWriter",
    "WorkerHeartbeat",
    "HEARTBEAT_STALE_THRESHOLD",
    "detect_crashed_status",
    "detect_worker_call_crashed",
]
