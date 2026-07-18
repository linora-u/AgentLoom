# 断点续跑与运行时存储

## 概述

AgentLoom 将“一次执行尝试”和“需要恢复的逻辑任务”分开管理：

- `run_id` 标识一次执行 attempt。正常运行和每次 resume 都生成新的 `run_id` 与 run 目录。
- `task_id` 标识同一个逻辑任务。resume 保持 `task_id` 不变，继续使用原 checkpoint 目录。

因此，日志轮转或 run 清理不会破坏任务恢复状态；并发 Application 和任务也不会共用日志或 artifact 路径。

## 存储布局

`runtime.root_dir` 是框架运行时的唯一根目录，默认值为 `.agentloom`：

```text
.agentloom/
├── runs/<application_id>/<run_id>/
│   ├── manifest.json
│   ├── logs/
│   │   ├── runtime.log
│   │   └── runtime.log.1 ... runtime.log.3
│   ├── audit/
│   │   ├── shell.jsonl
│   │   └── shell.jsonl.1 ... shell.jsonl.2
│   └── artifacts/
│       ├── shell/
│       ├── background/
│       └── skills/
├── checkpoints/<application_id>/<task_id>/
│   ├── task_events.jsonl
│   ├── task_tree.json
│   ├── checkpoint.json
│   ├── heartbeat.json
│   ├── workers/<worker_name>/
│   │   ├── calls/<call_index>/checkpoint.json
│   │   └── heartbeat.json
│   ├── context_store/
│   └── file-history/
├── sessions/
├── learning/
├── self_learning.db
└── legacy/logs-v1-<timestamp>/
```

关键边界：

- `manifest.json` 用 `task_id` 反向关联逻辑任务；checkpoint 的 run 事件和 heartbeat 记录当前 `run_id`。
- `task_events.jsonl` 是持久化的任务/Worker 事件源，`task_tree.json` 是便于查看的投影。
- Checkpoint 直接按 `<application_id>/<task_id>` 定位，不依赖日志目录、`.task_index.json` 或历史 run 扫描。
- 用户交付物仍由 Application 的 `output_dir` 管理，runtime 清理不会遍历 Application output 目录。
- Agent 的 recall/todo 状态位于 `.agentloom/workspaces/agents/<application_id>/<agent_path>/`。持久化的 `insights.md` 归 Application 所有；task 文件隔离在 `tasks/<task_id>/`，不属于 run artifacts。

## 配置

在 `config/system.yaml` 中配置运行时存储、有界日志和 resume：

```yaml
runtime:
  root_dir: ".agentloom"
  successful_run_retention_days: 7
  failed_run_retention_days: 30
  artifact_retention_days: 3
  cleanup_interval_hours: 24

logging:
  level: "INFO"
  console_enabled: true
  file_enabled: true
  max_file_bytes: 26214400  # runtime.log 每段 25 MiB
  backup_count: 3

checkpoint:
  enabled: true
  cleanup_on_success: true
  max_resume_age: 604800
  heartbeat_interval: 5
```

文件日志默认开启且有界：runtime log 保留当前 25 MiB 文件和 3 个备份；Shell audit 独立按每段 10 MiB、2 个备份轮转。

`runtime.root_dir` 支持绝对路径和相对路径；相对路径从 AgentLoom 项目根目录解析。隔离验证时，在验证 checkout 的全局配置里把该字段指向临时绝对目录。

## 运行与恢复

```bash
# 新逻辑任务 + 新执行 attempt
loom run applications/<app>/workflows/<agent>.yaml

# 仅关闭本次执行的文件 runtime log；checkpoint 仍正常工作
loom run applications/<app>/workflows/<agent>.yaml --no-file-log

# 新执行 attempt + 原逻辑任务/checkpoint
loom run applications/<app>/workflows/<agent>.yaml --resume task_xxx
```

CLI 不再提供 `--log-to-file`。默认是否写文件由 `logging.file_enabled` 决定，`--no-file-log` 是单次运行的关闭开关。

查看或清理 checkpoint：

```bash
loom list-tasks
loom list-tasks --detail
loom clean-tasks
loom clean-tasks --before 3
loom clean-tasks --all
```

`cleanup_on_success: true` 时，成功任务的整个 checkpoint 目录会被删除。关闭该清理时，completed checkpoint 会保留供检查，但它是终态，不能 resume。failed、interrupted 或 crashed 任务在 `max_resume_age` 内可恢复；其 ContextStore 和 file-history 与 task checkpoint 位于同一目录并共享生命周期。`loom list-tasks` 会列出所有仍保留的 checkpoint 及其状态。

## 恢复行为

Resume 的恢复链路为：

```text
加载已持久化的 MemorySteps
  -> 移除未完成 tool use、孤立步骤和空步骤
  -> 恢复 task-scoped ContextStore 与 file-history
  -> 在原 call_index 下恢复未完成的 Worker memory
  -> 输入哈希一致时复用已完成 Worker 结果
  -> 使用新的 run_id 继续执行
```

每次 Worker 调用拥有独立的 `workers/<worker>/calls/<call_index>/checkpoint.json`，并发调用不会互相覆盖。Resume 会复用未完成的 call index，不会为同一段中断工作再创建一个重复调用。

Supervisor 与 Worker heartbeat 都记录当前 `run_id`。崩溃检测会检查 heartbeat 是否缺失/停止、PID 是否存活以及时间戳是否过期。Resume 后会在同一个 task checkpoint 中写入新 attempt 的 `run_id`。

回放会跳过损坏或只写了一半的 JSONL 尾行，避免一次异常退出让前面的有效 task events 全部不可读。

## Runtime 保留策略

自动 runtime 清理最多每 `runtime.cleanup_interval_hours` 执行一次（默认 24 小时），且只遍历 `.agentloom/runs`：

- 成功 run：默认保留 7 天；
- 失败/中断 run：默认保留 30 天；
- 原始 `artifacts/`：默认保留 3 天；
- manifest 状态为 running 或未知：保留；
- `.agentloom/legacy/`、checkpoints、workspaces 和 Application outputs：run 清理永不删除。

也可以显式执行同一策略：

```bash
loom clean-runtime
```

Checkpoint 过期与删除保持 task-scoped：`max_resume_age` 决定任务是否还能 resume，`cleanup_on_success` 删除成功任务状态，`loom clean-tasks` 提供显式 checkpoint 清理。

## 从 `.logs` 一次性迁移

先预览：

```bash
loom migrate-runtime --dry-run
```

扫描会完全忽略旧 `.task_index.json`，直接读取真实 checkpoint 目录及 task events/tree；测试任务、过期任务会被排除，只选择仍有可恢复 memory、ContextStore 或 file-history 进度的任务。

确认候选后应用：

```bash
loom migrate-runtime --apply
```

Apply 会经过 staging，校验 checksum 和可恢复进度，再原子 rename 到 `.agentloom/checkpoints/<application_id>/<task_id>/`。全部候选验证成功后，整个旧 `.logs` 会被原子归档到 `.agentloom/legacy/logs-v1-<timestamp>/`；新运行时不会双读该归档。

迁移完成后，应对每个重要任务执行真实 resume，并验证旧 ContextRef retrieve 与 file-history 状态，再把迁移判定为通过。

## 检查真实运行证据

不要只看退出码或 final answer。必须读取该 attempt 的 manifest、runtime log 和 Shell audit：

```bash
manifest=$(find .agentloom/runs -name manifest.json -type f -print | sort | tail -1)
run_dir=$(dirname "$manifest")

sed -n '1,160p' "$manifest"
tail -n 100 "$run_dir/logs/runtime.log"
tail -n 100 "$run_dir/audit/shell.jsonl"
```

对 resume 任务，确认新 manifest 使用新的 `run_id`、原来的 `task_id`，再检查：

```bash
find .agentloom/checkpoints/<application_id>/<task_id> -maxdepth 5 -type f -print
```

## 故障排除

### Resume 失败：`No checkpoint found`

运行 `loom list-tasks --detail`，确认 task 位于本次 workflow 对应的同一 `application_id` 下。Checkpoint 查找具有 Application scope。

### Resume 失败：`Checkpoint expired`

该逻辑任务原始 `created_at` 已超过 `checkpoint.max_resume_age`。迁移和 resume 不会重写 `created_at` 来复活过期任务。

### 没有 `runtime.log`

检查 `logging.file_enabled`，以及本次运行是否使用了 `--no-file-log`。关闭文件日志不会关闭 checkpoint 或 Shell audit。

### `.logs` 仍然存在

新运行不会再写 `.logs`。先用 `loom migrate-runtime --dry-run` 检查，再用 `--apply` 归档旧目录；验证可恢复任务之前不要直接删除它。
