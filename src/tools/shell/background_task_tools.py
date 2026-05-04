"""Agent-facing tools for managing background shell tasks.

Provides three tools that allow agents to monitor and control
long-running commands that have been promoted to background execution:

- ``check_background_task`` — inspect status and recent output
- ``kill_background_task`` — terminate a running background task
- ``list_background_tasks`` — list all tracked background tasks
"""

import time

from src.lib.logging import get_logger
from src.tools.shell.background_task import BackgroundTaskRegistry

logger = get_logger(__name__)


def check_background_task(task_id: str) -> str:
    """Check the status and recent output of a background task.

    Retrieve the current state of a background shell command including
    its execution status, elapsed time, exit code (if finished), output
    file size, and the most recent lines of output.

    Background tasks are created automatically when a shell command
    exceeds its timeout and is promoted to run in the background, or
    when ``shell_tool`` is called with ``run_in_background=True``.

    Use this tool to monitor long-running commands such as builds,
    test suites, or server processes that were moved to the background.

    Args:
        task_id: The background task identifier (returned by shell_tool
            when a command is promoted to background).

    Returns:
        A formatted status report including:
        - Current status (running / completed / failed / killed)
        - Elapsed time
        - Exit code (if finished)
        - Output file size
        - Last 20 lines of output
        - Stall warning if the task appears stuck on interactive input

    Examples:
        check_background_task("a1b2c3d4e5f6")
    """
    if not task_id or not isinstance(task_id, str):
        return "Error: task_id must be a non-empty string."

    task_id = task_id.strip()
    registry = BackgroundTaskRegistry.get_instance()
    task = registry.get(task_id)

    if task is None:
        # Provide helpful context — list available IDs.
        all_tasks = registry.list_all()
        if all_tasks:
            ids = ", ".join(t.task_id for t in all_tasks)
            return (
                f"Error: No background task found with id '{task_id}'.\n"
                f"Available task IDs: {ids}"
            )
        return (
            f"Error: No background task found with id '{task_id}'.\n"
            "No background tasks are currently tracked."
        )

    # Build status report.
    lines = [
        f"Background Task: {task.task_id}",
        f"Status: {task.status}",
        f"Command: {task.command[:200]}",
        f"PID: {task.pid}",
        f"Elapsed: {_format_duration(task.elapsed_seconds)}",
    ]

    if task.exit_code is not None:
        lines.append(f"Exit Code: {task.exit_code}")

    lines.append(f"Output Size: {_format_bytes(task.output_size)}")

    if task.stall_message:
        lines.append(f"\n⚠ STALL WARNING:\n{task.stall_message}")

    # Tail of output.
    tail = task.read_output_tail(n_lines=20)
    if tail:
        lines.append(f"\n--- Last 20 lines of output ---\n{tail}")
    else:
        lines.append("\n(No output produced yet)")

    return "\n".join(lines)


def kill_background_task(task_id: str) -> str:
    """Terminate a running background task.

    Send a graceful termination signal (SIGTERM) followed by a forced
    kill (SIGKILL) to a background shell command.  Returns the final
    status and recent output of the terminated task.

    Use this when a background task is no longer needed, appears stuck,
    or is consuming too many resources.

    Args:
        task_id: The background task identifier to kill.

    Returns:
        Final status and last 20 lines of output from the killed task.
        If the task has already finished, returns its final status.

    Examples:
        kill_background_task("a1b2c3d4e5f6")
    """
    if not task_id or not isinstance(task_id, str):
        return "Error: task_id must be a non-empty string."

    task_id = task_id.strip()
    registry = BackgroundTaskRegistry.get_instance()

    task = registry.get(task_id)
    if task is None:
        return f"Error: No background task found with id '{task_id}'."

    if task.is_terminal:
        tail = task.read_output_tail(n_lines=20)
        return (
            f"Task '{task_id}' has already finished.\n"
            f"Status: {task.status}\n"
            f"Exit Code: {task.exit_code}\n"
            f"Elapsed: {_format_duration(task.elapsed_seconds)}\n"
            + (f"\n--- Last 20 lines of output ---\n{tail}" if tail else "")
        )

    # Kill the task.
    updated = registry.kill_task(task_id)
    if updated is None:
        return f"Error: Task '{task_id}' disappeared during kill attempt."

    tail = updated.read_output_tail(n_lines=20)
    return (
        f"Task '{task_id}' has been killed.\n"
        f"Status: {updated.status}\n"
        f"Exit Code: {updated.exit_code}\n"
        f"Elapsed: {_format_duration(updated.elapsed_seconds)}\n"
        + (f"\n--- Last 20 lines of output ---\n{tail}" if tail else "")
    )


def list_background_tasks() -> str:
    """List all tracked background tasks.

    Returns a formatted table of all background tasks (both running
    and recently completed) including their status, command, elapsed
    time, and exit code.

    Use this to get an overview of all background work before deciding
    which tasks to check or kill.

    Returns:
        A formatted table of background tasks.  If no tasks exist,
        returns a message indicating the registry is empty.

    Examples:
        list_background_tasks()
    """
    registry = BackgroundTaskRegistry.get_instance()
    tasks = registry.list_all()

    if not tasks:
        return "No background tasks are currently tracked."

    # Sort: running first, then by start_time descending.
    tasks.sort(key=lambda t: (t.is_terminal, -t.start_time))

    lines = [
        f"{'TASK ID':<14} {'STATUS':<12} {'ELAPSED':>10} {'EXIT':>6}  COMMAND",
        "-" * 78,
    ]

    for task in tasks:
        elapsed = _format_duration(task.elapsed_seconds)
        exit_str = str(task.exit_code) if task.exit_code is not None else "-"
        cmd = task.command[:40]
        if len(task.command) > 40:
            cmd += "..."
        stall = " ⚠STALL" if task.stall_message else ""
        lines.append(
            f"{task.task_id:<14} {task.status + stall:<12} "
            f"{elapsed:>10} {exit_str:>6}  {cmd}"
        )

    lines.append(f"\nTotal: {len(tasks)} task(s), "
                  f"{sum(1 for t in tasks if not t.is_terminal)} running")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_duration(seconds: float) -> str:
    """Format seconds as a human-readable duration string."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h{mins:02d}m"


def _format_bytes(size: int) -> str:
    """Format a byte count as a human-readable string."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size / (1024 * 1024 * 1024):.1f} GB"
