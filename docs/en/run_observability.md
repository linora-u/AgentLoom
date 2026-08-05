# Structured Run API and Observability

Use the structured interface when another program must track an AgentLoom run reliably. It provides one immutable run receipt, typed post-allocation failures, and a versioned lifecycle-event stream. `run_app()` remains the string-returning compatibility API.

## Python API

```python
from src.runner import execute_app

events = []
result = execute_app(
    "applications/example/workflows/supervisor.yaml",
    task_override="Summarize the repository",
    event_sink=events.append,
)

print(result.output)
print(result.run.run_id, result.run.manifest_path, result.run.log_path)
print([event.event for event in events])
```

`execute_app(...) -> ApplicationRunResult` accepts the same `resume_task_id`, `task_override`, and `file_logging` options as `run_app()`, plus a synchronous `event_sink`. The result contains `output`, `started_at`, `ended_at`, optional structured `goal`, and a `RunInfo` receipt with `application_id`, `task_id`, `run_id`, `run_dir`, `manifest_path`, and `log_path`.

Preflight configuration rejection happens before storage is allocated: the original configuration exception is raised and the sink receives one `run.rejected` event with a typed `RunRejection`; it has no `run` receipt. Once allocation succeeds, the sink receives `run.started` followed by exactly one terminal event:

- `run.completed` with `output`;
- `run.budget_limited` with canonical `goal`, `error`, and `phase`, while Python raises `ApplicationRunBudgetLimited` containing the same `RunInfo`, Goal snapshot, and resumable task id;
- `run.failed` with `error` and `phase`, while Python raises `ApplicationRunError` containing the same `RunInfo`;
- `run.interrupted` with `error` and `phase`, while Python raises `ApplicationRunInterrupted`. Inspect its `resumable` flag before offering resume; an interruption before recoverable state exists can set it to `false`.

Lifecycle events use `schema_version: 1`. Ordinary observer exceptions do not change the run outcome; process-control exceptions remain process-control signals. Keep the sink fast because delivery is synchronous.

## JSON and JSONL CLI Protocol

```bash
uv run loom run applications/example/workflows/supervisor.yaml \
  --output-format json

uv run loom run applications/example/workflows/supervisor.yaml \
  --output-format jsonl
```

Both machine-readable modes reserve stdout for the protocol. `json` emits exactly
one terminal or rejected lifecycle event. `jsonl` streams lifecycle events, including
`run.started`, one object per line. Application, Python, native, and child-process
output is redirected to stderr so it cannot corrupt either protocol.

```json
{"schema_version":1,"event":"run.started","run":{"application_id":"example","task_id":"task_...","run_id":"run_...","run_dir":"...","manifest_path":"...","log_path":"..."},"occurred_at":"..."}
{"schema_version":1,"event":"run.completed","run":{"application_id":"example","task_id":"task_...","run_id":"run_...","run_dir":"...","manifest_path":"...","log_path":"..."},"occurred_at":"...","output":"done"}
```

Goal events include a structured `goal` object with `status`, `token_budget`,
prompt/completion/total usage, `remaining_tokens`, objective fingerprint,
evidence, and timestamps. Text mode also prints
`Goal: <status> | tokens: <used>/<budget>`. Budget exhaustion exits `1` but is
not `run.failed`; automation should handle `run.budget_limited`, edit YAML, and
resume. See [Goal Mode](goal_mode.md).

A rejected preflight emits only `run.rejected`, with `phase: "preflight"` and `error: {kind, message, retryable}`. It deliberately has no `run` object because no run directory exists.

Exit codes are `0` for success, `1` for ordinary or rejected failure, `75` for a trusted transient provider failure, and `130` for interruption. Consumers should branch on event type and structured error fields instead of parsing logs. The receipt paths lead to the manifest, bounded log, and [persisted run evidence](checkpoint.md#inspecting-real-run-evidence).
