"""
CLI entry point for the src package.

After ``pip install -e .``, the ``loom`` command is available::

    # Run a supervisor agent
    loom run applications/<app>/workflows/<agent>.yaml

    # Generate a demo script
    loom create applications/<app>/workflows/<agent>.yaml
    loom create applications/<app>/workflows/<agent>.yaml -o my_app.py
"""

from __future__ import annotations

import sys

import click


_MAIN_EPILOG = """\
\b
Examples:
  loom run applications/<app>/workflows/<agent>.yaml
  loom create applications/<app>/workflows/<agent>.yaml
  loom create applications/<app>/workflows/<agent>.yaml -o my_app.py
  loom ui

Use 'loom <command> -h' for more details on each command.
"""


@click.group(context_settings={"help_option_names": ["-h", "--help"]}, epilog=_MAIN_EPILOG)
def main():
    """AgentLoom – AI agent framework CLI."""
    import os
    from pathlib import Path as _Path

    try:
        from src.lib.config import C

        agent_root = _Path(C.agent_root).resolve()
        if _Path.cwd().resolve() != agent_root:
            os.chdir(agent_root)
    except Exception:
        pass  # discovery may fail here; let sub-commands report the real error


_RUN_EPILOG = """\
\b
Examples:
  loom run applications/test_demo/workflows/test_agent.yaml
  loom run applications/test_demo/workflows/test_agent.yaml --log-to-file
  loom run applications/test_demo/workflows/test_agent.yaml --resume task_xxx
"""


@main.command(epilog=_RUN_EPILOG)
@click.argument("yaml_path")
@click.option("--log-to-file", is_flag=True, default=False, help="Persist logs to a file.")
@click.option("--resume", "resume_task_id", default=None, help="Resume from a checkpoint task ID.")
def run(yaml_path: str, log_to_file: bool, resume_task_id: str | None):
    """Run a supervisor agent from a YAML configuration."""
    from src.runner import run_app

    try:
        result = run_app(yaml_path, log_to_file=log_to_file, resume_task_id=resume_task_id)
        click.echo(result)
    except KeyboardInterrupt:
        click.echo("\nInterrupted. Use --resume to continue.", err=True)
        sys.exit(130)
    except Exception as exc:
        click.echo(f"\n Execution failed: {exc}", err=True)
        sys.exit(1)


# ─────────────────────────────────────────────
# loom list-tasks
# ─────────────────────────────────────────────

@main.command("list-tasks")
@click.option("--detail", is_flag=True, default=False, help="Show worker-level details.")
def list_tasks(detail: bool):
    """List all resumable checkpoint tasks."""
    from src.lib.checkpoint.checkpoint_manager import list_all_tasks

    tasks = list_all_tasks()
    if not tasks:
        click.echo("No resumable tasks found.")
        return

    _ICONS = {"interrupted": "⏸", "failed": "❌", "running": "🔄", "completed": "✅", "crashed": "💥"}
    for t in tasks:
        status = t.get("status", "")
        icon = _ICONS.get(status, "?")
        line = (
            f"  {icon}  {t['task_id']}  [{t['agent_name']}]  "
            f"{status}  {t.get('interrupted_at') or t.get('created_at', '')}"
        )
        if status == "crashed":
            line += "  (process died — resumable)"
        click.echo(line)

        # Show worker details if --detail flag is set.
        if detail:
            workers = t.get("workers", [])
            for i, w in enumerate(workers):
                is_last = (i == len(workers) - 1)
                prefix = "  └─" if is_last else "  ├─"
                w_icon = _ICONS.get(w.get("status", ""), "?")
                ci = w.get("call_index", 0)
                w_step = w.get("step")
                step_info = f" ({w_step} steps)" if w_step else ""
                w_err = w.get("error")
                err_info = f"  err: {w_err[:60]}" if w_err else ""
                click.echo(
                    f"       {prefix} {w_icon} {w.get('agent_name', '?')} #{ci}"
                    f"  {w.get('status', '?')}{step_info}{err_info}"
                )


# ─────────────────────────────────────────────
# loom clean-tasks
# ─────────────────────────────────────────────

@main.command("clean-tasks")
@click.option("--all", "clean_all", is_flag=True, default=False, help="Remove ALL checkpoints.")
@click.option("--before", "before_days", type=int, default=None, help="Remove checkpoints older than N days.")
def clean_tasks(clean_all: bool, before_days: int | None):
    """Clean old checkpoint data."""
    from src.lib.checkpoint.checkpoint_manager import _resolve_checkpoint_base_dir

    base_dir = _resolve_checkpoint_base_dir()
    if not base_dir.exists():
        click.echo("No checkpoints to clean.")
        return

    total_removed = 0
    for agent_dir in base_dir.iterdir():
        if not agent_dir.is_dir():
            continue
        # Check if any child contains checkpoint data (v2 or legacy layout).
        has_ckpt = False
        for child in agent_dir.iterdir():
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name == "checkpoints":
                has_ckpt = True
                break
            if (child / "checkpoints").is_dir():
                has_ckpt = True
                break
        if not has_ckpt:
            continue
        from src.lib.checkpoint import CheckpointManager
        cm = CheckpointManager(agent_dir.name, base_dir=base_dir)
        if clean_all:
            total_removed += cm.cleanup_old_tasks(max_age_seconds=0)
        elif before_days is not None:
            total_removed += cm.cleanup_old_tasks(max_age_seconds=before_days * 86400)
        else:
            total_removed += cm.cleanup_old_tasks()  # default: 7 days
    click.echo(f"Cleaned {total_removed} checkpoint(s).")


# ─────────────────────────────────────────────
# loom dashboard
# ─────────────────────────────────────────────

@main.command("dashboard")
def dashboard():
    """Launch the terminal TUI task monitoring dashboard (Textual)."""
    from src.framework.ui.dashboard import run_dashboard

    run_dashboard()


_CREATE_EPILOG = """\
\b
Examples:
  loom create applications/test_demo/workflows/test_agent.yaml
  loom create applications/test_demo/workflows/test_agent.yaml -o my_app.py
"""


@main.command(epilog=_CREATE_EPILOG)
@click.argument("yaml_path")
@click.option("-o", "--output", default=None, help="Output file path. Defaults to applications/{category}/{name}_app.py.")
def create(yaml_path: str, output: str | None):
    """Generate a minimal demo script for a supervisor YAML config."""
    from src.scaffold import create_demo_script

    try:
        generated = create_demo_script(yaml_path, output_path=output, interactive=True)
        click.echo(f" Demo script generated: {generated}")
    except FileExistsError as exc:
        click.echo(f"\n  {exc}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"\n Failed: {exc}", err=True)
        sys.exit(1)


# ─────────────────────────────────────────────
# loom ui
# ─────────────────────────────────────────────

_VALID_LOG_EXT = {".json", ".log"}

_UI_EPILOG = """\
\b
Examples:
  loom ui                          # interactive wizard, all defaults
  loom ui --port 9090              # skip port prompt, use 9090

The wizard will guide you through:
  [1/3] Server port
  [2/3] Auto-open browser
  [3/3] Runtime JSON log file (real-time monitoring)

Agent topology (supervisor + workers) is auto-discovered at runtime.
You can also load JSON files from the web UI at any time.
"""


def _resolve_path(raw: str, agent_root):
    """Resolve a user-supplied path (relative to *agent_root* or absolute)."""
    from pathlib import Path as _P
    p = _P(raw)
    if not p.is_absolute():
        p = _P(agent_root) / p
    return p.resolve()


def _prompt_port(default: int = 8080) -> int:
    """Interactive port prompt with retry loop."""
    while True:
        raw = click.prompt(
            click.style("  [1/3] ", fg="cyan") + "Server port",
            default=str(default),
            show_default=False,
            prompt_suffix=f" (default {default}, press Enter to use default): ",
        )
        raw = raw.strip()
        if not raw:
            click.echo(click.style(f"  ✔ Port: {default}", fg="green"))
            return default
        try:
            port = int(raw)
        except ValueError:
            click.echo(click.style("  ⚠ Port must be an integer between 1024~65535, please retry.", fg="yellow"))
            continue
        if not (1024 <= port <= 65535):
            click.echo(click.style("  ⚠ Port must be an integer between 1024~65535, please retry.", fg="yellow"))
            continue
        click.echo(click.style(f"  ✔ Port: {port}", fg="green"))
        return port


def _prompt_browser(default: bool = True) -> bool:
    """Interactive browser prompt with retry loop."""
    while True:
        raw = click.prompt(
            click.style("  [2/3] ", fg="cyan") + "Auto-open browser?",
            default="Y" if default else "N",
            show_default=False,
            prompt_suffix=" (Y/n, press Enter for Y): ",
        )
        raw = raw.strip().lower()
        if raw in ("", "y", "yes"):
            click.echo(click.style("  ✔ Auto-open browser: Yes", fg="green"))
            return True
        if raw in ("n", "no"):
            click.echo(click.style("  ✔ Auto-open browser: No", fg="green"))
            return False
        click.echo(click.style("  ⚠ Please enter Y or N, please retry.", fg="yellow"))


def _prompt_log(agent_root) -> str | None:
    """Interactive JSON log prompt with retry loop."""
    click.echo(click.style("  [3/3] ", fg="cyan") + "Runtime JSON log file " + click.style("(press Enter to skip)", dim=True) + ":")
    click.echo("         Monitor a JSON log file for real-time agent execution visualization.")
    click.echo("         Supports relative path (from AgentLoom root) or absolute path.")
    while True:
        raw = click.prompt("       ", default="", show_default=False, prompt_suffix="> ").strip()
        if not raw:
            click.echo(click.style("  ✔ Log: (skipped)", fg="green"))
            return None
        from pathlib import Path as _P
        if _P(raw).suffix.lower() not in _VALID_LOG_EXT:
            click.echo(click.style(f"  ⚠ Only .json/.log files are supported, please retry (Enter to skip).", fg="yellow"))
            continue
        resolved = _resolve_path(raw, agent_root)
        if not resolved.exists():
            # JSON log may not exist yet — just warn, allow it
            click.echo(click.style(f"  ⚠ File does not exist yet (will wait for it): {resolved}", fg="yellow"))
        display = raw if len(raw) < 80 else str(resolved)
        click.echo(click.style(f"  ✔ Log: {display}", fg="green"))
        return str(resolved)


@main.command(epilog=_UI_EPILOG)
@click.option("-p", "--port", type=int, default=None, help="Server port (skip interactive prompt).")
@click.option("--no-browser", is_flag=True, default=False, help="Don't auto-open browser (skip interactive prompt).")
def ui(port: int | None, no_browser: bool):
    """Launch the agent visualisation web UI (interactive setup wizard)."""
    from pathlib import Path as _P

    # Determine agent_root
    try:
        from src.lib.config import C
        agent_root = str(C.agent_root)
    except Exception:
        agent_root = str(_P.cwd())

    # Banner
    click.echo()
    click.echo("  ╔══════════════════════════════════════════════╗")
    click.echo("  ║       AgentLoom Visualization Setup Wizard       ║")
    click.echo("  ╚══════════════════════════════════════════════╝")
    click.echo()

    # [1/3] Port
    if port is not None:
        if not (1024 <= port <= 65535):
            click.echo(click.style(f"  ⚠ Invalid --port {port}. Must be 1024~65535.", fg="red"), err=True)
            sys.exit(1)
        click.echo(click.style(f"  [1/3] ", fg="cyan") + f"Server port: {port} (from --port)")
        chosen_port = port
    else:
        chosen_port = _prompt_port()

    # [2/3] Browser
    if no_browser:
        click.echo(click.style(f"  [2/3] ", fg="cyan") + "Auto-open browser: No (from --no-browser)")
        auto_browser = False
    else:
        auto_browser = _prompt_browser()

    # [3/3] Log
    log_file = _prompt_log(agent_root)

    # Summary
    click.echo()
    click.echo("  ──────────────────────────────────────")
    click.echo("    Configuration:")
    click.echo(f"      Port:     {chosen_port}")
    click.echo(f"      Browser:  {'Auto-open' if auto_browser else 'Disabled'}")
    click.echo(f"      Log:      {log_file or '(none)'}")
    click.echo("  ──────────────────────────────────────")
    click.echo()
    click.echo("  Starting server...")
    click.echo()

    # Start server
    from src.ui.server import start_server

    try:
        start_server(
            port=chosen_port,
            auto_browser=auto_browser,
            log_file=log_file,
            agent_root=agent_root,
        )
    except Exception as exc:
        click.echo(f"\n  Server failed: {exc}", err=True)
        sys.exit(1)
