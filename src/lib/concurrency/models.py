"""
Data models for the concurrency module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class TaskResult:
    """
    Result of a single task executed by ParallelAgentExecutor.

    Attributes:
        task_id: Unique identifier for the task.
        status: One of "completed", "failed", or "skipped" (circuit breaker).
        result: Return value from the agent tool (only if completed).
        error: Error message string (only if failed).
        error_trace: Full traceback string (only if failed).
        duration_seconds: Wall-clock execution time.
    """

    task_id: str
    status: str  # "completed" | "failed" | "skipped"
    result: Any = None
    error: Optional[str] = None
    error_trace: Optional[str] = None
    duration_seconds: float = 0.0
