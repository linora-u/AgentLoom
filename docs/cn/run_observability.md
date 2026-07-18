# 结构化 Run API 与可观测性

当外部程序需要可靠跟踪 AgentLoom 执行时，应使用结构化接口。它提供不可变的 run receipt、分配运行目录后的 typed errors，以及有版本的生命周期事件流；`run_app()` 继续作为返回字符串的兼容接口。

## Python API

```python
from src.runner import execute_app

events = []
result = execute_app(
    "applications/example/workflows/supervisor.yaml",
    task_override="概括这个仓库",
    event_sink=events.append,
)

print(result.output)
print(result.run.run_id, result.run.manifest_path, result.run.log_path)
print([event.event for event in events])
```

`execute_app(...) -> ApplicationRunResult` 支持与 `run_app()` 相同的 `resume_task_id`、`task_override`、`file_logging`，并额外接受同步 `event_sink`。结果包含 `output`、`started_at`、`ended_at`，以及 `RunInfo` receipt：`application_id`、`task_id`、`run_id`、`run_dir`、`manifest_path`、`log_path`。

配置在 preflight 被拒绝时尚未分配存储：Python 抛出原配置异常，sink 只收到一个包含 typed `RunRejection` 的 `run.rejected`，没有 `run` receipt。分配成功后，事件序列是 `run.started` 加且仅加一个终态：

- `run.completed`，包含 `output`；
- `run.failed`，包含 `error` 与 `phase`，Python 抛出携带同一 `RunInfo` 的 `ApplicationRunError`；
- `run.interrupted`，包含 `error` 与 `phase`，Python 抛出 `ApplicationRunInterrupted`。提供 resume 前必须检查其 `resumable` 标志；尚未产生可恢复状态时，该值可能为 `false`。

生命周期事件使用 `schema_version: 1`。普通 observer 异常不会改变 run 结果；进程控制异常仍按进程控制信号处理。事件同步投递，因此 sink 应保持快速。

## JSONL CLI 协议

```bash
uv run loom run applications/example/workflows/supervisor.yaml \
  --output-format jsonl
```

JSONL 模式下，stdout 只输出生命周期事件，每行一个 JSON 对象。Application、Python、native 和子进程输出全部重定向到 stderr，避免污染协议。

```json
{"schema_version":1,"event":"run.started","run":{"application_id":"example","task_id":"task_...","run_id":"run_...","run_dir":"...","manifest_path":"...","log_path":"..."},"occurred_at":"..."}
{"schema_version":1,"event":"run.completed","run":{"application_id":"example","task_id":"task_...","run_id":"run_...","run_dir":"...","manifest_path":"...","log_path":"..."},"occurred_at":"...","output":"done"}
```

Preflight 拒绝只产生 `run.rejected`，其中 `phase: "preflight"`、`error: {kind, message, retryable}`。因为没有创建 run 目录，所以不会伪造 `run` 对象。

退出码：成功为 `0`，普通失败或拒绝为 `1`，可信的暂时性 Provider 失败为 `75`，中断为 `130`。调用方应按事件类型和结构化 error 字段决策，不要解析日志。Receipt 路径可以定位 manifest、有限日志和[持久化运行证据](checkpoint.md#检查真实运行证据)。
