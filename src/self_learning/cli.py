"""Self-learning, memory, review, and session CLI commands."""

from __future__ import annotations

import json
from datetime import datetime

import click


def _echo_json(value) -> None:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    click.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@click.group()
def sessions() -> None:
    """Search and manage indexed AgentLoom run history."""


@sessions.command("index")
@click.argument("path", required=False, default=None)
def sessions_index(path: str | None) -> None:
    """Report ledger counts or import canonical self-learning event exports."""

    from agentloom.self_learning.persistence.event_importer import (
        SessionEventImporter,
    )

    _echo_json(SessionEventImporter().index_all(path))


@sessions.command("search")
@click.argument("query")
@click.option("--limit", default=10, show_default=True, type=int)
@click.option("--agent", default=None)
@click.option("--app", default=None)
@click.option("--since", default=None)
@click.option(
    "--scope",
    default="all",
    type=click.Choice(["current_app", "project", "all"]),
    show_default=True,
)
def sessions_search(
    query: str,
    limit: int,
    agent: str | None,
    app: str | None,
    since: str | None,
    scope: str,
) -> None:
    """Search indexed session events."""

    from agentloom.self_learning.persistence.ledger import SelfLearningLedger

    _echo_json(
        SelfLearningLedger().search_events(
            query,
            limit=limit,
            agent=agent,
            app=app,
            since=since,
            scope=scope,
        )
    )


@sessions.command("scroll")
@click.argument("run_id")
@click.argument("event_id", type=int)
@click.option(
    "--direction",
    default="after",
    type=click.Choice(["before", "after"]),
    show_default=True,
)
@click.option("--window", default=5, show_default=True, type=int)
def sessions_scroll(
    run_id: str,
    event_id: int,
    direction: str,
    window: int,
) -> None:
    """Scroll before or after a session event."""

    from agentloom.self_learning.persistence.ledger import SelfLearningLedger

    _echo_json(
        SelfLearningLedger().scroll_events(
            run_id,
            event_id,
            direction=direction,
            window=window,
        )
    )


@sessions.command("prune")
@click.option(
    "--retention-days",
    required=True,
    type=click.IntRange(min=0),
    help="Delete history older than this many days; 0 includes all prior history.",
)
def sessions_prune(retention_days: int) -> None:
    """Prune old run/event history; curated memory is unaffected."""

    from agentloom.self_learning.persistence.ledger import SelfLearningLedger

    _echo_json(SelfLearningLedger().prune_events(retention_days=retention_days))


def _review_scope_selection(
    *,
    application_id: str | None,
    project_scope: bool,
    all_scopes: bool,
) -> tuple[str, str]:
    selections = int(bool(application_id)) + int(project_scope) + int(all_scopes)
    if selections != 1:
        raise click.UsageError(
            "Choose exactly one scope: --application, --project, or the command's --all option."
        )
    if application_id:
        return "application", application_id
    if project_scope:
        return "project", "project"
    return "all", ""


def _review_cli_service():
    root_context = click.get_current_context().find_root()
    if isinstance(root_context.obj, dict) and "review_service" in root_context.obj:
        return root_context.obj["review_service"]
    from agentloom.self_learning.review_artifacts import ReviewCLIService

    return ReviewCLIService()


def _review_cli_call(operation):
    try:
        return operation()
    except click.ClickException:
        raise
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


@click.group()
def learn() -> None:
    """Extract and review self-learning candidates."""


@learn.command("review")
@click.option("--application", "application_id", default=None, help="Review one Application.")
@click.option("--project", "project_scope", is_flag=True, help="Review Project candidates.")
@click.option("--all-unreviewed", is_flag=True, help="Review each Application, then Project.")
@click.option("--dry-run", is_flag=True, help="Render decisions without activating candidates.")
def learn_review_command(
    application_id: str | None,
    project_scope: bool,
    all_unreviewed: bool,
    dry_run: bool,
) -> None:
    """Review exactly one scope, or every unreviewed scope in isolation."""

    selection = _review_scope_selection(
        application_id=application_id,
        project_scope=project_scope,
        all_scopes=all_unreviewed,
    )
    service = _review_cli_call(_review_cli_service)
    result = (
        _review_cli_call(lambda: service.review_all(dry_run=dry_run))
        if selection[0] == "all"
        else _review_cli_call(
            lambda: service.review_one(
                selection[0],
                selection[1],
                dry_run=dry_run,
            )
        )
    )
    _echo_json(result)


@click.group()
def reviews() -> None:
    """Inspect, apply, or roll back scoped review decisions."""


@reviews.command("status")
@click.option("--application", "application_id", default=None, help="Show one Application.")
@click.option("--project", "project_scope", is_flag=True, help="Show Project status.")
@click.option("--all", "all_scopes", is_flag=True, help="Show all review scopes.")
def reviews_status_command(
    application_id: str | None,
    project_scope: bool,
    all_scopes: bool,
) -> None:
    """Show review state for exactly one scope or for all scopes."""

    scope_type, scope_id = _review_scope_selection(
        application_id=application_id,
        project_scope=project_scope,
        all_scopes=all_scopes,
    )
    service = _review_cli_call(_review_cli_service)
    _echo_json(_review_cli_call(lambda: service.status(scope_type, scope_id)))


@reviews.command("apply")
@click.option("--application", "application_id", default=None, help="Apply one Application INBOX.")
@click.option("--project", "project_scope", is_flag=True, help="Apply the Project INBOX.")
def reviews_apply_command(
    application_id: str | None,
    project_scope: bool,
) -> None:
    """Apply decisions from exactly one scoped INBOX."""

    scope_type, scope_id = _review_scope_selection(
        application_id=application_id,
        project_scope=project_scope,
        all_scopes=False,
    )
    service = _review_cli_call(_review_cli_service)
    _echo_json(_review_cli_call(lambda: service.apply(scope_type, scope_id)))


@reviews.command("rollback")
@click.argument("review_id")
def reviews_rollback_command(review_id: str) -> None:
    """Roll back mutations created by one immutable review batch."""

    service = _review_cli_call(_review_cli_service)
    _echo_json(_review_cli_call(lambda: service.rollback(review_id)))


@click.group()
def feedback() -> None:
    """Submit outcome feedback for a completed run."""


@feedback.command("submit")
@click.argument("run_id")
@click.option(
    "--verdict",
    required=True,
    type=click.Choice(["accepted", "rejected", "corrected"]),
)
@click.option(
    "--item",
    "item_id",
    default=None,
    type=click.IntRange(min=1),
    help="Optional affected memory item id.",
)
def feedback_submit_command(
    run_id: str,
    verdict: str,
    item_id: int | None,
) -> None:
    """Record accepted, rejected, or corrected run feedback."""

    service = _review_cli_call(_review_cli_service)
    _echo_json(
        _review_cli_call(
            lambda: service.submit_feedback(
                run_id=run_id,
                verdict=verdict,
                item_id=item_id,
            )
        )
    )


@click.group()
def memory() -> None:
    """Manage durable AgentLoom memory."""


_MEMORY_SCOPES = ["project", "app", "application"]


@memory.command("list")
@click.option("--scope", default=None, type=click.Choice(_MEMORY_SCOPES))
@click.option("--scope-id", default="", help="Application id when scope is app.")
def memory_list(scope: str | None, scope_id: str) -> None:
    """List active curated memory."""

    from agentloom.self_learning.persistence.memory_store import MemoryStore

    _echo_json(MemoryStore().list(scope=scope, scope_id=scope_id))


@memory.command("add")
@click.option(
    "--scope",
    default="project",
    type=click.Choice(_MEMORY_SCOPES),
    show_default=True,
)
@click.option("--scope-id", default="", help="Application id when scope is app.")
@click.argument("content")
def memory_add(scope: str, scope_id: str, content: str) -> None:
    """Add active memory directly from CLI."""

    from agentloom.self_learning.persistence.memory_store import MemoryStore

    _echo_json(MemoryStore().add(scope, content, scope_id=scope_id))


@memory.command("replace")
@click.option(
    "--scope",
    default="project",
    type=click.Choice(_MEMORY_SCOPES),
    show_default=True,
)
@click.option("--scope-id", default="", help="Application id when scope is app.")
@click.argument("target")
@click.argument("content")
def memory_replace(
    scope: str,
    scope_id: str,
    target: str,
    content: str,
) -> None:
    """Replace active memory directly from CLI."""

    from agentloom.self_learning.persistence.memory_store import MemoryStore

    _echo_json(MemoryStore().replace(scope, target, content, scope_id=scope_id))


@memory.command("remove")
@click.option(
    "--scope",
    default="project",
    type=click.Choice(_MEMORY_SCOPES),
    show_default=True,
)
@click.option("--scope-id", default="", help="Application id when scope is app.")
@click.argument("target")
def memory_remove(scope: str, scope_id: str, target: str) -> None:
    """Remove active memory directly from CLI."""

    from agentloom.self_learning.persistence.memory_store import MemoryStore

    _echo_json(MemoryStore().remove(scope, target, scope_id=scope_id))


@memory.command("pending")
@click.option(
    "--status",
    default="pending",
    type=click.Choice(["pending", "approved", "rejected", "stale", "all"]),
    show_default=True,
)
def memory_pending(status: str) -> None:
    """List exact writes waiting for approval (or their audit status)."""

    from agentloom.self_learning.persistence.memory_store import MemoryStore

    _echo_json(
        MemoryStore().list_pending(status=None if status == "all" else status)
    )


@memory.command("stats")
def memory_stats() -> None:
    """Show active memory capacity and pending-write status."""

    from agentloom.self_learning.persistence.memory_store import MemoryStore

    _echo_json(MemoryStore().stats())


@memory.command("export")
@click.option("--out", "out_path", default="", help="Write to this file instead of stdout.")
@click.option(
    "--format",
    "fmt",
    default="json",
    type=click.Choice(["json", "markdown"]),
    show_default=True,
)
def memory_export(out_path: str, fmt: str) -> None:
    """Export active memory and exact pending-write audit rows."""

    from agentloom.self_learning.persistence.memory_store import MemoryStore

    store = MemoryStore()
    items = store.export_items()
    stats = store.stats()
    if fmt == "json":
        payload = {
            "exported_at": datetime.now().astimezone().isoformat(),
            "db_path": str(store.db_path),
            "stats": stats,
            "items": items,
            "pending_writes": store.list_pending(status=None),
        }
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    else:
        by_bucket: dict[str, list[dict]] = {}
        for item in items:
            by_bucket.setdefault(
                f"{item['scope_type']}:{item['scope_id']}",
                [],
            ).append(item)
        lines = ["# AgentLoom Memory Export", ""]
        for bucket_key, bucket_items in by_bucket.items():
            lines.extend((f"## {bucket_key}", ""))
            lines.extend(f"- [{item['id']}] {item['content']}" for item in bucket_items)
            lines.append("")
        rendered = "\n".join(lines).rstrip() + "\n"
    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        click.echo(f"Exported {len(items)} items to {out_path}")
    else:
        click.echo(rendered)


@click.group()
def skills() -> None:
    """Manage skills and skill proposals."""


@skills.group("proposals")
def skill_proposals() -> None:
    """Review and promote generated skill proposals."""


@skill_proposals.command("list")
def skill_proposals_list() -> None:
    """List generated skill proposals."""

    from agentloom.self_learning.proposal_writer import ProposalWriter

    _echo_json(ProposalWriter().list())


@skill_proposals.command("show")
@click.argument("proposal_id")
def skill_proposals_show(proposal_id: str) -> None:
    """Show a generated skill proposal."""

    from agentloom.self_learning.proposal_writer import ProposalWriter

    _echo_json(ProposalWriter().show(proposal_id))


@skill_proposals.command("promote")
@click.argument("proposal_id")
@click.option("--name", "destination", default="", help="Destination active skill name.")
def skill_proposals_promote(proposal_id: str, destination: str) -> None:
    """Promote a proposal with SKILL.md into active skills."""

    from agentloom.self_learning.proposal_writer import ProposalWriter

    _echo_json(ProposalWriter().promote(proposal_id, destination=destination))


@skill_proposals.command("archive")
@click.argument("proposal_id")
def skill_proposals_archive(proposal_id: str) -> None:
    """Archive a generated skill proposal."""

    from agentloom.self_learning.proposal_writer import ProposalWriter

    _echo_json(ProposalWriter().archive(proposal_id))
