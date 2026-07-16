"""
Terminal TUI dashboard for monitoring AgentLoom tasks.

Launch via ``loom dashboard``.  Uses the Textual framework to display a
real-time DataTable of all checkpoint tasks with heartbeat status.

Keyboard shortcuts:
    q / Ctrl+C  — quit
    r           — manual refresh
    e           — expand / collapse worker details for the selected task
    c           — copy selected task ID to clipboard
    d           — delete selected task checkpoint
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, Static

# ── Status formatting ────────────────────────────────────────────────

_STATUS_MAP: dict[str, tuple[str, str]] = {
    # status  → (emoji, rich style)
    "running":     ("🟢", "bold green"),
    "crashed":     ("🔴", "bold red"),
    "failed":      ("🔴", "bold red"),
    "interrupted": ("🟡", "bold yellow"),
    "completed":   ("⚪", "dim"),
    "stopped":     ("⏹️", "dim"),
    "exited":      ("⏹️", "dim"),
    "unknown":     ("⚫", "dim"),
}


def _fmt_status(status: str) -> Text:
    emoji, style = _STATUS_MAP.get(status, ("⚫", "dim"))
    if not style:
        style = "dim"
    return Text.from_markup(f"[{style}]{emoji} {status}[/]")


def _fmt_heartbeat_age(age: float | None) -> str:
    if age is None:
        return "—"
    if age > 30:
        return "stale"
    if age < 60:
        return f"{int(age)}s ago"
    if age < 3600:
        return f"{int(age // 60)}m ago"
    return f"{int(age // 3600)}h ago"


def _fmt_created(created_at: str) -> str:
    """Trim ISO datetime to readable format."""
    if not created_at:
        return "—"
    try:
        return created_at[5:16].replace("T", " ")
    except Exception:
        return created_at[:16]


TaskIdentity = tuple[str, str]


def _task_row_key(application_id: str, task_id: str) -> str:
    """Build a collision-free row key for the canonical task identity."""

    return f"task:{len(application_id)}:{application_id}:{task_id}"


def _find_task(
    tasks: Iterable[Mapping[str, Any]],
    identity: TaskIdentity,
) -> Mapping[str, Any] | None:
    application_id, task_id = identity
    return next(
        (
            task
            for task in tasks
            if task.get("application_id") == application_id
            and task.get("task_id") == task_id
        ),
        None,
    )


def _get_row_key(table: DataTable) -> str | None:
    """Extract the opaque key from the current cursor row."""

    if table.row_count == 0:
        return None
    row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
    return row_key.value if hasattr(row_key, "value") else str(row_key)


def _checkpoints_root() -> Path:
    from src.lib.config import C
    from src.lib.runtime import resolve_runtime_home

    return resolve_runtime_home(C.raw, agent_root=C.agent_root).checkpoints_root


def _delete_dashboard_task(target: Mapping[str, Any]) -> bool:
    """Delete through the same inactive-task lease used by the CLI cleaner."""

    from src.lib.checkpoint.checkpoint_manager import (
        delete_checkpoint_task_if_inactive,
    )

    return delete_checkpoint_task_if_inactive(Path(str(target["checkpoint_dir"])))


# ── Textual App ──────────────────────────────────────────────────────

class TaskDashboardApp(App):
    """AgentLoom Task Dashboard — terminal TUI for monitoring tasks."""

    TITLE = "AgentLoom Dashboard"
    CSS = """
    Screen {
        background: $surface;
    }
    #summary {
        dock: top;
        height: 1;
        padding: 0 2;
        background: $primary-background;
        color: $text;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True, priority=True),
        Binding("r", "refresh", "Refresh", show=True, priority=True),
        Binding("e", "expand", "Expand Workers", show=True, priority=True),
        Binding("c", "copy_id", "Copy Task ID", show=True, priority=True),
        Binding("d", "delete_task", "Delete", show=True, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._expanded_tasks: set[TaskIdentity] = set()
        self._row_identities: dict[str, tuple[TaskIdentity, bool]] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading…", id="summary")
        table = DataTable(id="tasks", zebra_stripes=True, cursor_type="row")
        table.add_columns(
            "Task ID", "Application", "Agent", "Status", "Steps", "PID",
            "Heartbeat", "Files", "Created",
        )
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_tasks()
        self.set_interval(2, self._refresh_tasks)

    def _refresh_tasks(self) -> None:
        from src.lib.checkpoint.checkpoint_manager import list_all_tasks

        tasks = list_all_tasks(checkpoints_root=_checkpoints_root())
        table: DataTable = self.query_one("#tasks")
        summary: Static = self.query_one("#summary")

        cursor_row = table.cursor_row if table.row_count > 0 else 0

        table.clear()
        self._row_identities.clear()
        if not tasks:
            summary.update("  No tasks found.  Press [bold]q[/] to quit.")
            return

        counts: dict[str, int] = {}
        for t in tasks:
            s = t.get("status", "unknown")
            counts[s] = counts.get(s, 0) + 1
        parts = [f"Total: {len(tasks)}"]
        for s in ("running", "crashed", "failed", "interrupted", "completed"):
            if counts.get(s):
                emoji, _ = _STATUS_MAP.get(s, ("", ""))
                parts.append(f"{emoji} {s}: {counts[s]}")
        summary.update("  " + "  │  ".join(parts) + "  │  Auto-refresh 2s")

        for t in tasks:
            task_id = t.get("task_id", "")
            application_id = t.get("application_id", "")
            identity = (application_id, task_id)
            row_key = _task_row_key(application_id, task_id)
            self._row_identities[row_key] = (identity, False)
            workers = t.get("workers", [])
            has_workers = len(workers) > 0
            is_expanded = identity in self._expanded_tasks

            expand_icon = ""
            if has_workers:
                expand_icon = "▼ " if is_expanded else "▶ "

            fh_files = t.get("fh_tracked_files", 0)
            fh_snaps = t.get("fh_snapshots", 0)
            fh_text = f"{fh_files}/{fh_snaps}" if fh_files or fh_snaps else "—"

            table.add_row(
                task_id,                             # ← full task_id, no truncation
                application_id,
                expand_icon + t.get("agent_name", ""),
                _fmt_status(t.get("status", "unknown")),
                (str(t.get("step")) if t.get("step") is not None else "—"),
                str(t.get("pid") or "—"),
                _fmt_heartbeat_age(t.get("heartbeat_age")),
                fh_text,
                _fmt_created(t.get("created_at", "")),
                key=row_key,
            )

            if is_expanded and workers:
                for i, w in enumerate(workers):
                    is_last = (i == len(workers) - 1)
                    prefix = "  └─ " if is_last else "  ├─ "
                    ci = w.get("call_index", 0)
                    w_name = f"{prefix}{w.get('agent_name', '?')} #{ci}"
                    w_status = w.get("status", "unknown")
                    w_step = str(w.get("step")) if w.get("step") is not None else "—"
                    w_hb_age = _fmt_heartbeat_age(w.get("heartbeat_age"))
                    w_started = _fmt_created(w.get("started_at", ""))

                    worker_key = f"{row_key}::worker::{i}"
                    self._row_identities[worker_key] = (identity, True)
                    table.add_row(
                        "",
                        "",
                        Text.from_markup(f"[dim]{w_name}[/]"),
                        _fmt_status(w_status),
                        w_step,
                        "—",
                        w_hb_age,
                        "—",
                        w_started,
                        key=worker_key,
                    )

        if cursor_row < table.row_count:
            table.move_cursor(row=cursor_row)

    # ── Actions ──────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._refresh_tasks()
        self.notify("Refreshed", timeout=1)

    def action_expand(self) -> None:
        """Toggle worker expansion for the selected supervisor task."""
        table: DataTable = self.query_one("#tasks")
        row_key = _get_row_key(table)
        selected = self._row_identities.get(row_key or "")
        if selected is None or selected[1]:
            return
        identity = selected[0]
        if identity in self._expanded_tasks:
            self._expanded_tasks.discard(identity)
        else:
            self._expanded_tasks.add(identity)
        self._refresh_tasks()

    def action_copy_id(self) -> None:
        """Copy the full task_id of the selected row to the system clipboard."""
        table: DataTable = self.query_one("#tasks")
        row_key = _get_row_key(table)
        selected = self._row_identities.get(row_key or "")
        if selected is None:
            return
        task_id = selected[0][1]

        try:
            import subprocess
            # Try xclip first, then xsel, then pbcopy (macOS).
            for cmd in (
                ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"],
                ["pbcopy"],
            ):
                try:
                    subprocess.run(
                        cmd, input=task_id.encode(), check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=2,
                    )
                    self.notify(f"Copied: {task_id}", timeout=1)
                    return
                except (FileNotFoundError, subprocess.SubprocessError):
                    continue
            # Fallback: Textual's built-in copy (works in some terminals).
            self.copy_to_clipboard(task_id)
            self.notify(f"Copied: {task_id}", timeout=1)
        except Exception:
            self.notify(f"ID: {task_id}  (copy failed — select manually)", severity="warning", timeout=1)

    def action_delete_task(self) -> None:
        table: DataTable = self.query_one("#tasks")
        row_key = _get_row_key(table)
        selected = self._row_identities.get(row_key or "")
        if selected is None:
            self.notify("No tasks to delete", severity="warning", timeout=1)
            return
        identity, is_worker = selected
        if is_worker:
            self.notify("Select a supervisor row to delete", severity="warning", timeout=1)
            return
        task_id = identity[1]

        from src.lib.checkpoint.checkpoint_manager import list_all_tasks
        tasks = list_all_tasks(checkpoints_root=_checkpoints_root())
        target = _find_task(tasks, identity)
        if not target:
            self.notify(f"Task {task_id} not found", severity="error", timeout=1)
            return

        if _delete_dashboard_task(target):
            self._expanded_tasks.discard(identity)
            self.notify(f"Deleted {task_id}", severity="information", timeout=1)
            self._refresh_tasks()
        else:
            self.notify(
                f"Task {task_id} is active or could not be deleted",
                severity="warning",
                timeout=2,
            )


def run_dashboard() -> None:
    """Entry point called by ``loom dashboard``."""
    app = TaskDashboardApp()
    app.run()
