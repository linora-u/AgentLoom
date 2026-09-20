# Goal Mode

Goal Mode lets a root Supervisor advance one objective across runtime segments.
It retains conversation memory, Worker state, and checkpoints. An ordinary
`final_answer` or `max_steps` boundary starts another continuation segment;
only explicit completion ends the Goal. Interruption and real errors stop the run.

## Configuration

Only a top-level Supervisor can enable Goal Mode:

```yaml
goal: true
# Equivalent: goal: {enabled: true}
```

Use `goal: false` or omit it to disable. Mapping form requires boolean `enabled`.
Legacy `token_budget` values are silently ignored; Goal has no cost/token ceiling.
Worker YAML cannot define Goal Mode.

The objective combines `description`, `workflow`, and the runtime task. A workflow
list is numbered and merged into one objective; ordinary non-Goal workflow lists
retain their sequential execution semantics.

## Lifecycle and tools

- `active`: continue work; a normal final answer does not complete the Goal.
- `complete`: the root called `update_goal(status="complete", evidence="...")`.

Only the root Supervisor receives `get_goal` and `update_goal`. Completion requires
non-empty evidence and is terminal and idempotent. Worker identity is checked by
the handlers. Todo completion does not change Goal state.

Continuation uses the same runtime and conversation. Later prompts include Goal
identity and state, without restarting completed work. After completion, the root
may make one in-process request exposing only `final_answer` to deliver its reply.
Planning and smart-summary calls cannot consume that allowance. A restored
completed Goal returns its stored evidence without rerunning work.

## Checkpoints and observability

With checkpoints enabled, `<application_id>/<task_id>/goal.json` stores identity,
objective fingerprint, state, `goal_started`, evidence, and timestamps. Resume
preserves the Goal and checks that description, workflow, and runtime task still
match. Corrupt Goal state or disabling an active Goal remains an error.

Old budget and usage fields in Goal checkpoints are silently ignored. A legacy
`budget_limited` Goal resumes as `active`; an already completed Goal stays complete.
No budget increase or configuration migration is required.

```bash
uv run loom run applications/<app>/workflows/<agent>.yaml --resume <task_id>
```

Run manifests and lifecycle events include canonical Goal state. Before successful
checkpoint cleanup, evidence is copied to `audit/goal.json`. CLI text shows Goal
status; TUI shows its objective, state, and completion evidence. Ordinary model
usage remains in runtime audit records, independently of Goal state.

Schedules run the same YAML with the same continuation, completion, and recovery
semantics. Goal work continues until explicit completion, interruption, or error.
