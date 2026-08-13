# Hooks

AgentLoom Hooks are independent runtime extensions. Skills provide prompt
instructions and resources; they cannot declare or register Hooks.

## Configuration

Hooks are authorized only by an explicit `hooks:` block in global system,
application system, or Agent YAML. A block can reference reusable Bundles and
declare Shell Hooks directly:

```yaml
hooks:
  bundles:
    local-audit:
      path: hooks/local-audit

  PreToolUse:
    - id: workspace.normalize-write
      matcher: "write_file|edit_file"
      command: "python hooks/check_write.py"
      timeout: 20
```

Bundle directories contain an exact-case `HOOK.yaml` plus their scripts:

```yaml
name: local-audit
description: Record selected runtime events.
hooks:
  TaskCreated:
    - id: local-audit.task-created
      command: "python scripts/on_task_start.py"
      timeout: 20
```

Bundle directories are never auto-discovered and cannot reference other Bundles.
The configured Bundle key must equal `HOOK.yaml.name`. Relative Bundle paths
resolve from the declaring AgentLoom/application root. Bundle commands run with
the Bundle directory as cwd; direct commands run from the declaring root.

Each enabled entry accepts only:

| Field | Required | Meaning |
|---|---:|---|
| `id` | yes | Stable global Hook identity |
| `command` | yes | Trusted Shell command |
| `matcher` | tool events only | `*` or a full-match regular expression |
| `timeout` | no | Positive seconds; default `20` |
| `enabled` | no | Defaults to `true`; `false` is a tombstone |

Configuration order is global system, application system, then Agent. Within
one layer, Bundle entries run before direct entries and retain YAML order. A
higher layer can fully replace a lower Hook with the same ID under the same
event, or remove it with `id` plus `enabled: false`. Partial merges, duplicate
IDs in one layer, and reuse of an ID under another event are errors.

A Bundle can be disabled in a higher layer without repeating its path:

```yaml
hooks:
  bundles:
    local-audit:
      enabled: false
  PreToolUse:
    - id: workspace.normalize-write
      enabled: false
```

## Events and failure semantics

| Event | Semantics |
|---|---|
| `PreToolUse` | Fail-closed gate and sequential input transform |
| `PostToolUse` | Successful tool observer |
| `PostToolUseFailure` | Tool-exception observer; blocked calls do not emit it |
| `SessionStart`, `SessionEnd` | Root-run lifecycle |
| `Stop` | Fail-closed final-answer gate |
| `StopFailure` | Failed-completion observer |
| `SubagentStart`, `SubagentStop` | Parent-owned Worker lifecycle |
| `TaskCreated`, `TaskCompleted` | Root task lifecycle |

An exception, timeout, non-zero exit, invalid JSON, or illegal result field
blocks `PreToolUse` and `Stop`. Observer errors are diagnosed and later Hooks
continue. All matching Hooks execute sequentially; each `PreToolUse` Handler
receives the input produced by the previous Handler, and block immediately
short-circuits the chain.

## Shell wire format

The command receives one versioned JSON object on stdin. It includes Hook/event
identity, root and local run IDs, Agent/task identity, project/cwd, step number,
tool input/result, and the tool input schema. Hook-specific environment
variables and temporary JSON files are not a second input protocol.

The command writes exactly one JSON object to stdout:

```json
{
  "decision": "modify",
  "modified_input": {"file_path": "safe/output.md"},
  "agent_context": "The path was normalized.",
  "user_message": "Using the safe output directory.",
  "reason": "Workspace policy",
  "telemetry": {"policy": "workspace"}
}
```

`decision` is `allow`, `block`, or `modify`. Only `PreToolUse` may return a
partial `modified_input`; only `PreToolUse` and `Stop` may block. Observer and
lifecycle Hooks must allow and cannot rewrite completed results. Unknown fields
are invalid. `agent_context` enters the next model turn and `user_message` is
delivered exactly once through the active Hook Run.

## Runtime contract

An Agent compiles one immutable `HookPlan`, retaining Handler IDs, source
provenance, stable order, and a fingerprint. Each invocation creates an
isolated `HookRun`; root and Worker runs never share effects or metrics. Tool
definitions may be cached, but every wrapped invocation requires an explicitly
bound Hook Run.

Tool input follows this non-configurable sequence:

```text
initial decode
→ configured PreToolUse transforms/gates
→ final strict decode
→ CoreToolGuard
→ final-input file-history/self-learning recording
→ tool side effect
→ outcome recording
→ configured Post observer
```

`CoreToolGuard` and final-input recording are framework invariants, not Hook
IDs. A blocked invocation has a distinct blocked outcome, has no tool side
effect, and is not treated as tool failure.

Configuring a Shell Hook authorizes trusted local code. AgentLoom filters
sensitive inherited environment values and kills the marked process tree on
timeout. It does not claim portable network isolation and therefore exposes no
Hook-level `allow-network` or `allow-scripts` flag.

Prompt, HTTP, Agent, background/async, once-only, post-response rewrite, global
registries, and automatic Bundle discovery are intentionally unsupported.
