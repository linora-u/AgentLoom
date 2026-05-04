# 断点续跑与会话恢复

## 概述

AgentLoom 支持多 Agent 任务的**断点续跑**（Checkpoint & Resume）能力。当长时间运行的任务被中断（用户 Ctrl-C、进程崩溃、机器重启等）时，可以从上次中断处继续执行，而无需从头开始。

核心组件：

| 组件 | 说明 |
|------|------|
| **CheckpointManager** | 持久化层：原子 JSON 写入、任务树管理、checkpoint 文件组织 |
| **CheckpointCoordinator** | 协调层：ContextVar 单例，管理 checkpoint 生命周期 |
| **CheckpointSerializer** | 序列化层：smolagents MemoryStep 的序列化/反序列化 |
| **ConversationRecovery** | 恢复管道：过滤未完成的 tool 调用、孤立思考步骤、空步骤 |
| **FileHistoryManager** | 文件历史：编辑前备份、步骤快照、回滚到指定步骤 |
| **SupervisorHeartbeat** | 心跳监控：Supervisor 进程存活检测 |
| **WorkerHeartbeat** | 工作心跳：Worker 调用级别的存活检测 |

## 存储布局

Checkpoint 数据写入每次运行的时间戳日志目录，与运行日志共存：

```
.logs/{agent_name}/
├── .task_index.json                        # 索引：task_id → timestamp 映射（用于 --resume 快速查找）
├── 20260413_104447/                        # 每次运行的时间戳目录
│   ├── {agent_name}.log                    # 运行日志
│   └── checkpoints/{task_id}/              # 该次运行的 checkpoint 数据
│       ├── task_tree.json                  # 任务元数据（状态、worker 调用记录）
│       ├── checkpoint.json                 # Supervisor Agent memory 快照
│       ├── heartbeat.json                  # Supervisor 心跳（PID、时间戳）
│       ├── file-history/                   # 文件编辑历史备份
│       └── workers/{worker_name}/
│           ├── checkpoint.json             # Worker Agent memory 快照
│           └── heartbeat.json              # Worker 心跳
└── 20260413_104837/
    ├── {agent_name}.log
    └── checkpoints/{task_id}/
        └── ...
```

**关键设计**：
- Checkpoint 始终在首次创建时的时间戳目录下，resume 时不会迁移到新目录
- `.task_index.json` 记录 `task_id → timestamp` 的映射，`--resume` 时 O(1) 定位
- 如果索引丢失，系统会自动扫描所有时间戳目录作为降级回退

## 配置

在 `config/system.yaml` 中配置：

```yaml
checkpoint:
  enabled: true              # 全局开关
  cleanup_on_success: true   # 任务成功后自动清理 checkpoint
  max_resume_age: 604800     # checkpoint 最大保留时间（秒），默认 7 天
  heartbeat_interval: 5      # 心跳写入间隔（秒）
```

## CLI 命令

### 运行任务

```bash
# 正常运行
loom run applications/<app>/workflows/<agent>.yaml

# 开启日志文件写入
loom run applications/<app>/workflows/<agent>.yaml --log-to-file

# 从 checkpoint 恢复
loom run applications/<app>/workflows/<agent>.yaml --resume task_xxx
```

### 查看可恢复的任务

```bash
loom list-tasks
loom list-tasks --detail
```

### 清理旧 checkpoint

```bash
loom clean-tasks          # 清理过期的 checkpoint
loom clean-tasks --all    # 清理所有 checkpoint
```

## 恢复管道

当使用 `--resume` 恢复时，系统执行以下管道（移植自参考实现）：

```
加载已持久化的 MemoryStep
  → filter_unresolved_tool_uses()    # 移除未完成的 tool 调用
  → filter_orphaned_thinking()       # 移除孤立的思考步骤
  → filter_empty_steps()             # 移除完全空白的步骤
  → detect_turn_interruption()       # 检测中断类型
  → 注入已清理的步骤到 Agent 内存
  → 继续执行
```

### 中断检测

| 中断类型 | 说明 |
|---------|------|
| `none` | 任务正常完成（有 final_answer 或已完成的 tool 调用） |
| `interrupted_turn` | 任务在执行中被中断（有 tool_calls 但无 observations） |

## 文件历史（File History）

文件历史功能自动在 Agent 编辑文件之前创建备份，支持回滚到任意步骤：

- **自动触发**：通过 `PRE_TOOL_USE` hook 自动拦截 `edit_file`、`write_file`、`create_file` 等工具
- **三阶段锁安全**：检查 → I/O → 提交，最小化锁持有时间
- **快照管理**：最多保留 100 个快照，超出自动淘汰最旧的
- **Null-backup**：文件不存在时记录空备份，回滚时自动删除

存储结构：

```
.logs/{agent_name}/{timestamp}/checkpoints/{task_id}/file-history/
    {sha256_hash}@v1    # 编辑前备份
    {sha256_hash}@v2    # 步骤后快照
    snapshots.json       # 持久化索引
```

## Worker 跳过机制

多 Agent 任务中，已完成的 Worker 在恢复时会被自动跳过：

1. Worker 启动时计算输入的 SHA256 哈希
2. 完成后将结果和哈希存入 checkpoint
3. 恢复时检查哈希匹配，命中则直接返回缓存结果

## 崩溃检测

通过心跳文件实现崩溃检测：

- Supervisor 和 Worker 每 5 秒写入心跳文件（PID + 时间戳）
- 恢复时检查：
  - 心跳文件不存在 → 崩溃
  - 心跳状态为 stopped/exited → 崩溃
  - PID 不存在 → 崩溃
  - 时间戳超过 30 秒未更新 → 崩溃

## 故障排除

### 恢复失败："No checkpoint found"

确认 `.logs/{agent_name}/{timestamp}/checkpoints/` 目录下存在对应的 task_id 目录。使用 `loom list-tasks` 查看可用任务。

### 恢复失败："Checkpoint expired"

checkpoint 超过了 `max_resume_age` 配置的保留时间（默认 7 天）。

### 恢复失败："checkpoint is disabled"

检查 `config/system.yaml` 中 `checkpoint.enabled` 是否为 `true`。
