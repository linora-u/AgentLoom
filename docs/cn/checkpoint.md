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
│   │   ├── shell.jsonl.1 ... shell.jsonl.2
│   │   ├── task_tree.json
│   │   ├── task_events.jsonl
│   │   └── goal.json            # Goal Mode 运行证据（存在时）
│   └── artifacts/
│       ├── result.txt
│       ├── shell/
│       ├── background/
│       └── skills/
├── checkpoints/<application_id>/<task_id>/
│   ├── task_events.jsonl
│   ├── task_tree.json
│   ├── checkpoint.json
│   ├── heartbeat.json
│   ├── todos.json
│   ├── todos.lock
│   ├── goal.json                # canonical Goal 状态（存在时）
│   ├── goal.lock
│   ├── workers/<worker_name>/
│   │   ├── calls/<call_index>/checkpoint.json
│   │   └── heartbeat.json
│   ├── context_store/
│   └── file-history/
├── schedules/{jobs.json,serve-status.json,executions/}
├── sessions/
├── learning/
└── self_learning.db
```

关键边界：

- `manifest.json` 用 `task_id` 反向关联逻辑任务；checkpoint 的 run 事件和 heartbeat 记录当前 `run_id`。
- `task_events.jsonl` 是持久化的任务/Worker 事件源，`task_tree.json` 是便于查看的投影。
- 成功结果会复制到 `artifacts/result.txt`。存在 checkpoint 证据时，run 还会保存 `audit/task_tree.json` 与 `audit/task_events.jsonl`。Manifest 只在相应证据真实存在时写入 `result_artifact`、`task_tree_artifact`、`task_events_artifact` 和 `task_events_complete`。
- 即使 `cleanup_on_success` 删除了可恢复 checkpoint，这些紧凑证据仍可检查；raw artifact retention 仍可清理体积较大的 shell/background/skill artifacts。
- Checkpoint 直接按 `<application_id>/<task_id>` 定位，不依赖日志目录、`.task_index.json` 或历史 run 扫描。
- 用户交付物仍由 Application 的 `output_dir` 管理，runtime 清理不会遍历 Application output 目录。
- 持久 Schedule 与调度服务状态位于同一 runtime root 的 `schedules/` 下，覆盖 root 时会一起移动。
- Agent 的持久 recall 使用 `.agentloom/workspaces/agents/<application_id>/<agent_path>/` 下、归 Application 所有的 `insights.md`。当前任务的 Todo 在启用 checkpoint 时随 `<application_id>/<task_id>/todos.json` 共同恢复和清理；未启用 checkpoint 时只保存在本次 run 的内存中。它既不是 run artifact，也不承担长期项目管理。
- Goal Mode 使用 task-scoped `goal.json` 保存 objective 指纹、`goal_started`、状态和 evidence。`active`、`interrupted`、`failed`、`crashed` 都保留 checkpoint；只有显式完成后才进入现有成功清理流程。清理前状态会复制到 run manifest 和 `audit/goal.json`。

### 终态 lifecycle ownership

一个 Application Run owner 统一结算终态。Supervisor 只上报 output、memory
snapshot、error 和可选 Goal projection，不再独立提交第二套 Application 终态。
owner 按固定顺序完成一次事务：

```text
Supervisor report
  -> terminal checkpoint/task event
  -> result 与 audit evidence
  -> terminal manifest
  -> 可选的成功 checkpoint 删除
  -> resource cleanup
```

如果 evidence 或 manifest finalization 在 checkpoint 删除前失败，同一个 owner 会把
暂定成功改写为 `failed` 或 `interrupted` 并保留 checkpoint。如果删除已经开始后才被
中断，run 会报告 `resumable: false`，且不会重建一个缺少真实进度的空 checkpoint。

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

Goal resume 额外校验规范化 workflow 和原 runtime task 的指纹。
旧预算字段静默忽略，旧 `budget_limited` Goal 按活动状态恢复。活动 Goal 对应 YAML
改为禁用或目标内容改变仍会拒绝恢复。详见 [Goal Mode](goal_mode.md)。

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

每次 Worker 调用拥有独立的 `workers/<worker>/calls/<call_index>/checkpoint.json`，并发调用不会互相覆盖。Worker 输入完全相同时，Resume 才会复用未完成的 call index。

调用身份根据完整格式化后的 Worker task 计算哈希，包括 query 中的空白字符。把换行改成空格或改写 query 会改变身份，产生新调用。Application 必须逐字重放原始 query；复杂 checkpoint Supervisor 为首次执行和 resume 提供同一条固定单行 Python 字符串。Runtime 不会归一化不同输入来合并调用。

Supervisor 与 Worker heartbeat 都记录当前 `run_id`。崩溃检测会检查 heartbeat 是否缺失/停止、PID 是否存活以及时间戳是否过期。Resume 后会在同一个 task checkpoint 中写入新 attempt 的 `run_id`。

回放会跳过损坏或只写了一半的 JSONL 尾行，避免一次异常退出让前面的有效 task events 全部不可读。

## Runtime 保留策略

自动 runtime 清理最多每 `runtime.cleanup_interval_hours` 执行一次（默认 24 小时），且只遍历 `.agentloom/runs`：

- 成功 run：默认保留 7 天；
- 失败/中断 run：默认保留 30 天；
- 原始 `artifacts/`：默认保留 3 天；
- manifest 状态为 running 或未知：保留；
- checkpoints、workspaces、self-learning 状态和 Application outputs：run 清理永不删除。

也可以显式执行同一策略：

```bash
loom clean-runtime
```

Checkpoint 过期与删除保持 task-scoped：`max_resume_age` 决定任务是否还能 resume，`cleanup_on_success` 删除成功任务状态，`loom clean-tasks` 提供显式 checkpoint 清理。

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

该逻辑任务原始 `created_at` 已超过 `checkpoint.max_resume_age`。Resume 不会重写 `created_at` 来复活过期任务。

### 没有 `runtime.log`

检查 `logging.file_enabled`，以及本次运行是否使用了 `--no-file-log`。关闭文件日志不会关闭 checkpoint 或 Shell audit。
