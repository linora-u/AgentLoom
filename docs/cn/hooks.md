# Hooks

AgentLoom Hook 是独立的运行时扩展。Skill 只提供提示词说明与资源，不能声明或注册 Hook。

## 配置

只有全局 system、应用 system 或 Agent YAML 中显式出现的 `hooks:` 才会授权执行。配置既可引用可复用 Bundle，也可直接声明 Shell Hook：

```yaml
hooks:
  bundles:
    agent-visualization:
      path: hooks/agent-visualization

  PreToolUse:
    - id: workspace.normalize-write
      matcher: "write_file|edit_file"
      command: "python hooks/check_write.py"
      timeout: 20
```

Bundle 目录包含大小写固定的 `HOOK.yaml` 和脚本：

```yaml
name: agent-visualization
description: Collect Agent runtime events.
hooks:
  TaskCreated:
    - id: agent-visualization.task-created
      command: "python scripts/on_task_start.py"
      timeout: 20
```

Bundle 永不自动发现，也不能递归引用其他 Bundle。配置 key 必须等于 `HOOK.yaml.name`。相对 Bundle 路径从声明它的 AgentLoom/应用根目录解析；Bundle 命令以 Bundle 目录为 cwd，直接 Hook 以声明它的根目录为 cwd。

启用条目只允许以下字段：

| 字段 | 必填 | 含义 |
|---|---:|---|
| `id` | 是 | 全局稳定 Hook 身份 |
| `command` | 是 | 受信任 Shell 命令 |
| `matcher` | 仅工具事件 | `*` 或完整匹配的正则表达式 |
| `timeout` | 否 | 正秒数，默认 `20` |
| `enabled` | 否 | 默认 `true`；`false` 表示 tombstone |

配置顺序为全局 system、应用 system、Agent。同一层先执行 Bundle 条目，再执行直接条目，并保留 YAML 顺序。高层可在同一事件下用相同 ID 完整替换低层 Hook，或仅用 `id + enabled:false` 删除。禁止字段级合并、同层重复 ID、跨事件复用 ID。

高层可以不重复路径而整体禁用 Bundle：

```yaml
hooks:
  bundles:
    agent-visualization:
      enabled: false
  PreToolUse:
    - id: workspace.normalize-write
      enabled: false
```

## 事件与失败语义

| 事件 | 语义 |
|---|---|
| `PreToolUse` | fail-closed 门禁与顺序输入转换 |
| `PostToolUse` | 成功工具观察 |
| `PostToolUseFailure` | 工具异常观察；被阻止调用不会触发 |
| `SessionStart`, `SessionEnd` | 根运行生命周期 |
| `Stop` | fail-closed 最终答案门禁 |
| `StopFailure` | 失败结束观察 |
| `SubagentStart`, `SubagentStop` | 父运行拥有的 Worker 生命周期 |
| `TaskCreated`, `TaskCompleted` | 根任务生命周期 |

异常、超时、非零退出、非法 JSON 或非法结果字段会阻止 `PreToolUse` 和 `Stop`。观察事件只记录诊断并继续后续 Hook。所有匹配 Hook 顺序执行；每个 `PreToolUse` Handler 都能看到前一个 Handler 的结果，block 会立即短路。

## Shell 传输协议

命令从 stdin 接收一个带版本号的 JSON 对象，其中包含 Hook/事件身份、root/local run ID、Agent/task 身份、project/cwd、step、工具输入/结果和工具输入 schema。不再提供第二套 Hook 环境变量或临时 JSON 文件输入协议。

命令必须向 stdout 写一个 JSON 对象：

```json
{
  "decision": "modify",
  "modified_input": {"file_path": "safe/output.md"},
  "agent_context": "路径已规范化。",
  "user_message": "正在使用安全输出目录。",
  "reason": "Workspace policy",
  "telemetry": {"policy": "workspace"}
}
```

`decision` 只能是 `allow`、`block`、`modify`。只有 `PreToolUse` 可以返回部分 `modified_input`；只有 `PreToolUse` 和 `Stop` 可以 block。观察和生命周期 Hook 必须 allow，且不能改写既成结果。未知字段非法。`agent_context` 进入下一轮模型输入，`user_message` 由当前 Hook Run 恰好投递一次。

## 运行时契约

每个 Agent 编译一个不可变 `HookPlan`，保留 Handler ID、来源、稳定顺序和 fingerprint。每次调用创建隔离的 `HookRun`；根运行与 Worker 不共享效果或指标。工具定义可以缓存，但每次包装工具调用都必须存在显式绑定的 Hook Run。

工具输入经过不可配置的固定顺序：

```text
初始解码
→ 配置 PreToolUse 转换/门禁
→ 最终严格解码
→ CoreToolGuard
→ file-history/self-learning 最终输入记录
→ 工具副作用
→ 结果记录
→ 配置 Post 观察
```

`CoreToolGuard` 与最终输入记录是框架约束，不是可替换 Hook ID。被阻止调用拥有独立 blocked outcome，不产生工具副作用，也不记为工具失败。

配置 Shell Hook 等于授权执行受信任本地代码。AgentLoom 会过滤继承环境中的敏感值，并在超时后终止带运行标记的进程树；它不承诺可移植的网络隔离，因此 Hook 不提供 `allow-network` 或 `allow-scripts`。

Prompt、HTTP、Agent、后台/异步、once、结果改写、全局 registry 和 Bundle 自动发现均不支持。
