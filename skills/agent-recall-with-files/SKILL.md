---
name: agent-recall-with-files
description: Maintain task recovery notes and durable, cross-task Agent experience in canonical AgentLoom workspace files. Use for multi-step work that may resume after interruption, or when later tasks should reuse verified pitfalls, decisions, dependencies, and configuration facts. Skip one-shot answers and short lookups where recording would cost more than recovery.
---

# Agent Recall With Files

Maintain three small Markdown files with different lifecycles:

| File | Lifecycle | Question answered |
|---|---|---|
| `tasks/<task_id>/context.md` | Current task; preserved on resume | What is the goal and current state? |
| `tasks/<task_id>/trace.md` | Current task; preserved on resume | What meaningful actions just happened? |
| `insights.md` | Same Application and Agent across tasks | What verified knowledge should future tasks reuse? |

Use the exact paths supplied in AgentLoom runtime context. Do not reconstruct the
project root or invent Agent aliases such as `supervisor` and `worker`.

## Activation boundary

Loading this Skill adds instructions only. It does not execute scripts or grant
file access. AgentLoom discovers root and Application Skills automatically; the
model activates this body with `skill(name="agent-recall-with-files")` when the
task matches.

The Hook Bundle is optional and separately authorized:

```yaml
hooks:
  bundles:
    agent-recall-with-files:
      path: hooks/agent-recall-with-files
```

Enable the Bundle when the Agent should receive canonical paths, prior notes,
and stale-record reminders automatically. Without it, use this workflow only
when the task already exposes the three canonical paths and authorized file
tools.

## Recording workflow

1. Read existing `insights.md` before planning. Check `[pitfall]` entries first.
2. Replace placeholders in `context.md` with the goal, current state, remaining
   work, and key files. Add the task start to `trace.md`.
3. Append to `trace.md` after a meaningful action: a completed scan, file change,
   failed approach, or finished subtask.
4. Refresh `context.md` when the current state or remaining work changes.
5. Add to `insights.md` only after knowledge is verified and likely to matter in
   another task. Do not add a “no new insights” entry.
6. Before finishing, make `context.md` and `trace.md` reflect the final state.

Use this format for durable entries:

```text
- [YYYY-MM-DD] [tag] Specific fact, consequence, and reuse guidance.
```

| Tag | Durable knowledge |
|---|---|
| `[pitfall]` | A reproducible trap and how to avoid it |
| `[decision]` | A choice plus the constraint or evidence behind it |
| `[fact]` | A verified property of the codebase or environment |
| `[dependency]` | A version or system requirement |
| `[perf]` | A measured performance finding |
| `[config]` | A configuration detail that is easy to misuse |

Keep entries short. Include concrete paths, commands, identifiers, or observed
results when they make the knowledge reusable.

## Hook Bundle behavior

When enabled, the Bundle:

- creates missing task files without overwriting resume state;
- preserves Application/Agent-scoped `insights.md` across tasks;
- injects full context, the latest 20 trace lines, and the latest 30 insight lines
  before matched file, Shell, and search tools;
- waits through steps 1–3, then reminds only when context or trace becomes stale;
- compacts older insight entries into a bounded tag summary after 80 lines while
  retaining the newest 30 entries verbatim;
- leaves `Stop` default-allow, so recording never traps an Agent in a completion
  loop;
- never deletes similarly named files from the project root.

The Bundle does not observe every possible tool. MCP, Worker, and custom tool
calls may require an explicit trace update before the next matched tool.

## References

- Read [references/examples.md](references/examples.md) when you need concrete
  examples of context, trace, and durable insight entries.
- Read [references/reference.md](references/reference.md) when debugging file
  lifecycle, template detection, compaction, or Hook coverage.
