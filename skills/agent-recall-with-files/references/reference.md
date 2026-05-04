# Reference

## Intent

This skill gives each agent a small, durable workspace under `.runtime/<agent_name>/`.

It enables cross-session experience recall — agents learn from past pitfalls and decisions.

## File Roles

| File | Lifecycle | Role |
|------|-----------|------|
| `context.md` | Cleared per task | Task goal, current status, remaining items. Recovery snapshot. |
| `trace.md` | Cleared per task | Chronological execution log. Append-only within a session. |
| `insights.md` | **Permanent** | Cross-session experience: pitfalls, decisions, facts. Tagged and dated. |

## Template State Does Not Count

If any file still looks like the original template, treat it as not updated yet.

Examples of template state:

- `trace.md` still contains only the placeholder timestamp line.
- `context.md` still has `(What is this task trying to achieve?)` placeholders.
- `insights.md` still contains only generic guidance bullets and no dated entries.

## Insights Entry Format

```
- [YYYY-MM-DD] [tag] Specific, actionable description.
```

Tags: `[pitfall]` `[decision]` `[fact]` `[dependency]` `[perf]` `[config]`

## Recommended Entry Style

- Prefer short bullets.
- Include concrete paths, commands, and identifiers.
- Record failures once with the `[pitfall]` tag, then change approach.
- Always tag insights — untagged entries are harder to scan.

## Cross-Session Behaviour

- `insights.md` is **never automatically cleared**. It accumulates across task runs.
- When it exceeds the line threshold (80 lines), `TaskCreated` compresses older entries into an `## Archive` section and keeps recent entries under `## Recent`.
- `context.md` and `trace.md` are recreated from templates on each new task.

## Minimum Self-Check Before Finishing

- `context.md` contains task-specific content (not just template placeholders).
- `trace.md` contains at least one task-specific action entry.
- `trace.md` mentions the latest meaningful step or produced artifact.
- `insights.md` contains either a dated insight entry or an explicit "no new insights" note.
- All files use the exact current agent directory under `.runtime/<agent_name>/`.

## Hook Summary

- `TaskCreated` bootstraps the runtime directory. Preserves `insights.md`, recreates others.
- `PreToolUse` injects full `context.md`, recent `trace.md` (20 lines), recent `insights.md` (30 lines).
- `PostToolUse` reminds the agent to record meaningful changes.
- `SubagentStop` on failure emphasises logging pitfalls in `insights.md`.
- `Stop` always allows; the skill depends on manual compliance rather than a hard gate.
