---
name: agent-recall-with-files
description: Cross-session experience recall via file-based memory. Agents accumulate insights (pitfalls, decisions, facts) that persist across runs, plus ephemeral context and trace files for the current task.
version: "6.0.0"
allowed-tools: "Read, Write, Edit, Bash, Glob, Grep"
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
  TaskCompleted:
    - hooks:
        - type: command
          command: python ./scripts/on_task_complete.py
  StopFailure:
    - hooks:
        - type: command
          command: python ./scripts/on_task_fail.py
  SubagentStart:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_subtask_start.py
  SubagentStop:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_subtask_finish.py
  PreToolUse:
    - matcher: "Write|Edit|Bash|Read|Glob|Grep"
      hooks:
        - type: command
          command: python ./scripts/on_pre_tool_use.py
  PostToolUse:
    - matcher: "Write|Edit|Bash|Read|Glob|Grep"
      hooks:
        - type: command
          command: python ./scripts/on_post_tool_use.py
  Stop:
    - hooks:
        - type: command
          command: python ./scripts/on_stop.py
---

# Agent Recall With Files

File-based memory that lets agents recall experience from previous sessions.

Hooks bootstrap missing templates, remind, and surface recent context. They do not record task-specific content for you.

## When to Use

Use this skill when:

- The task has multiple steps or phases and can drift out of short-term context.
- You want agents to learn from past runs — avoid repeated pitfalls, remember key decisions.
- The agent might be interrupted or restarted mid-task and needs fast context recovery.
- You need a durable execution trace in the canonical AgentLoom workspace.

Skip this skill when:

- The task is a one-shot answer or quick lookup.
- The work is so small that runtime bookkeeping would outweigh the task itself.

## Runtime Files

Files are split by lifecycle under `.agentloom/workspaces/agents/<application_id>/<agent_path>/`:

| File | Lifecycle | Purpose |
|------|-----------|---------|
| `tasks/<task_id>/context.md` | Per task | Task goal, current status snapshot, remaining items. Preserved on resume. |
| `tasks/<task_id>/trace.md` | Per task | Chronological action log. Preserved on resume. |
| `insights.md` | **Cross-task** | Application/agent-scoped experience: pitfalls, decisions, facts. Never cleared automatically. |

The framework injects the exact canonical workspace paths into hook processes. Skill code never discovers a project root or creates a second runtime root.

In this repository the skill package lives at `AgentLoom/skills/agent-recall-with-files/`.

Inside AgentLoom workflows it is referenced as `skills/agent-recall-with-files`.

Hook commands run with the skill package root as working directory, so `./scripts` resolves to `AgentLoom/skills/agent-recall-with-files/scripts/` in this repo.

## Core Principle

`context.md` answers: **what is my current task and where am I?**

`trace.md` answers: **what did I just do?**

`insights.md` answers: **what stable knowledge should survive across sessions?**

Manual runtime recording is part of task execution, not cleanup to postpone until the end.

## Insights Tag System

Every entry in `insights.md` should follow this format:

```
- [YYYY-MM-DD] [tag] Specific, actionable description.
```

Available tags:

| Tag | When to use |
|-----|-------------|
| `[pitfall]` | A mistake or trap that wasted time. Future agents should avoid it. |
| `[decision]` | A deliberate choice with reasoning. Future agents should respect it. |
| `[fact]` | A verified truth about the codebase, environment, or tooling. |
| `[dependency]` | A version, library, or system requirement that matters. |
| `[perf]` | A performance-related observation or optimization. |
| `[config]` | A configuration detail that is easy to get wrong. |

## Required Routine

Follow this routine every time you use this skill:

1. **Start with context and trace**
   - Replace template placeholders in `context.md` with the actual task goal and scope.
   - Add the first real entry to `trace.md`.

2. **Review insights from previous sessions**
   - If `insights.md` has content from prior runs, read it before planning your approach.
   - Pay special attention to `[pitfall]` entries.

3. **After each meaningful action, update `trace.md`**
   - Meaningful actions: creating/updating files, finishing a scan, changing approach after an error, completing a subtask step.

4. **When task state changes, update `context.md`**
   - Update the Current Status and Remaining sections.
   - This is your recovery snapshot — keep it fresh.

5. **When something becomes durable, update `insights.md`**
   - Durable items: confirmed pitfalls, stable decisions, verified facts, dependency constraints.
   - Use the tag format. Be specific.

6. **Before finishing, self-check all three files**
   - `context.md` must show task-specific content, not the template.
   - `trace.md` must show task-specific actions.
   - `insights.md` must contain either task-specific entries or an explicit note.

## If You Have No Insights

Do not leave `insights.md` untouched just because no insights emerged yet.

Write an explicit note such as:

- `- [YYYY-MM-DD] [fact] No new insights this session; task was straightforward.`

That tells the next session the file was reviewed intentionally rather than forgotten.

## Bootstrap

At task start the runtime bootstrap:

1. Removes legacy planning artifacts from the workspace root.
2. Ensures the current `tasks/<task_id>/` workspace exists.
3. Creates `context.md` and `trace.md` only when missing, so resume keeps task state.
4. **Preserves `insights.md`** if it already has content (cross-task persistence).
5. If `insights.md` exceeds the line threshold, compresses it via summarization.

## Hook Behavior

- `TaskStart` creates missing task files and preserves task state on resume plus cross-task insights.
- `SubtaskStart` bootstraps the runtime directory and template files for sub-agents (creates context.md, trace.md, insights.md if missing), then returns a progress-recording reminder. Existing files are never overwritten.
- `SubtaskFinish` only returns a reminder. It does not mutate `trace.md`, `context.md`, or `insights.md`.
- `TaskComplete` and `TaskFail` only return reminders. They do not write runtime files.
- `PreToolUse` injects the full `context.md`, tail of `trace.md` (20 lines), and tail of `insights.md` (30 lines) into agent context. Empty-template files produce a brief "(no notes yet)" marker instead of the full template text.
- `PostToolUse` uses a **freshness-driven reminder engine**: it tracks file modification times and only reminds when files become stale (not updated for N steps). Steps 1-3 are silent (grace period). Reminders have a cooldown to prevent spamming. The agent's current step_number is passed from the framework via `$STEP_NUMBER` env var.
- `Stop` is default-allow. This skill relies on agent discipline, not hard gating.
- All hook output is wrapped in `<system-reminder>` tags at the HookManager layer, signaling to the LLM that these are system-level instructions with higher priority than regular content.

## Operating Rules

- Keep entries short and concrete.
- Update `trace.md` after meaningful execution steps.
- Update `context.md` when task state changes (new goal, status shift, items completed).
- Update `insights.md` only when you learn something worth keeping across sessions.
- Use the tag format in `insights.md`.
- Always use the exact current agent name in the runtime path.
- Do not invent alias directories such as `supervisor` or `worker`.

## Red Flags

If you catch yourself thinking any of the following, stop and update runtime first:

- "I'll finish the main task and backfill runtime later."
- "The hooks already reminded me, so I can ignore it for now."
- "There is no new insight, so `insights.md` can stay as the template."
- "I saw a pitfall but it's too minor to record."

These are the common ways runtime recording gets silently dropped.

## Templates and References

- [templates/context.md](templates/context.md)
- [templates/trace.md](templates/trace.md)
- [templates/insights.md](templates/insights.md)
- [references/reference.md](references/reference.md)
- [references/examples.md](references/examples.md)
