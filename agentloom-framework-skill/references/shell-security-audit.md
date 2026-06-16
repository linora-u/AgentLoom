# Shell 权限与审计验证

## 先观察，再收敛

当用户不确定 `shell_settings` 应该怎么配时，不要先猜一份白名单。最短路径是：

1. 用隔离 runtime 跑一次真实 workflow。
2. 打开同目录的 `.logs/<agent>/<timestamp>/shell_audit.log`。
3. 先读 `[POLICY_SNAPSHOT]`，确认本次有效策略：`allowed_commands`、`allowed_operators`、`security_checks`、`dangerous_paths`、`sandbox_enabled`。
4. 再读拦截事件：`WHITELIST_REJECT`、`PATH_VIOLATION`、`SECURITY_BLOCK`、`STALL_DETECTED`、`TIMEOUT`、`BACKGROUND_PROMOTION`、`SANDBOX_UNAVAILABLE`。
5. 只把真实需要的命令、操作符、路径或 sandbox 例外写进 Agent YAML / Worker YAML / 应用级 `config/system.yaml`。
6. 用收敛后的策略重跑真实 workflow，确认该允许的允许、该拒绝的拒绝。

默认 `allowed_commands: "*"` 与 `allowed_operators: "*"` 是全放行。即使没有任何命令被拦截，也必须能在 audit log 里看到 `[POLICY_SNAPSHOT]`，否则无法证明“没拦截”是因为策略全允许，而不是审计缺失。

## AgentLoom 配置点

```yaml
shell_settings:
  allowed_commands:
    - "echo"
    - "ls"
    - "grep"
  allowed_operators: ["|", "&&"]
  audit_log:
    enabled: true
    log_policy_snapshot: true
    log_success: false
```

- `allowed_commands` 限制命令名；`"*"` 表示命令名全允许。
- `allowed_operators` 限制 shell 操作符；`"*"` 表示操作符全允许。
- `audit_log.log_policy_snapshot` 默认应为 `true`，每次 run 写一条有效策略快照。
- `audit_log.log_success` 默认保持 `false`，避免成功命令淹没安全信号；只有调试执行流水时再打开。
- Supervisor 与 Worker 的有效配置独立重建。Worker 需要相同 shell 权限时，写在 Worker YAML 或应用级 `config/system.yaml`，不要假设 Supervisor 运行时覆盖会传下去。

## 审计报告怎么读

重点看这些字段：

- `[POLICY_SNAPSHOT] allowed_commands` / `allowed_operators`：本次是否全允许，或实际白名单是什么。
- `[WHITELIST_REJECT] suggestion`：命令拒绝应该建议 `allowed_commands`，操作符拒绝应该建议 `allowed_operators`。
- `[PATH_VIOLATION]`：路径 allow/exclude 是否和预期一致；macOS 上要注意 `/tmp` 与 `/private/tmp`、`/etc` 与 `/private/etc` 的 canonical 关系。
- `[SECURITY_BLOCK] check`：哪个安全检查触发，不要只看命令字符串。
- `[STALL_DETECTED]` / `[TIMEOUT]` / `[BACKGROUND_PROMOTION]`：长任务是否被正确转后台或停止。
- `[SANDBOX_UNAVAILABLE]`：配置要求 sandbox 但本机 backend 不可用，不能当作 sandbox 已生效。

## 借鉴规则

从 `claude-code` 借鉴：

- 权限决策要能区分配置允许、配置拒绝、用户批准、hook/分类器判断；AgentLoom 至少要在 audit log 里保留策略快照和拒绝原因。
- sandbox 是独立安全边界，不等同于命令白名单；sandbox 不可用时必须显式记录，不能悄悄当作已隔离。
- 危险命令或过宽 wrapper 不应被轻易建议成 allow rule；建议文案必须指向正确配置项。
- deny、ask、tool-specific safety check 必须优先于 bypass/allow；“危险模式”只能减少询问，不能越过安全检查。
- 复合命令不能只看拆分后的子命令；原始命令里的 pipe、redirect、`cd` 后路径、wrapper/env 前缀都要参与审计。

从 `pi` 借鉴：

- 如果项目本身不把权限系统当安全边界，就要明说信任边界，并推荐容器或 sandbox 扩展。
- sandbox 策略要可展示、可查询；AgentLoom 通过 `POLICY_SNAPSHOT` 把当前策略落到 audit log。
- sandbox 初始化失败必须暴露给用户，不能伪造 PASS。
- Skill、Hook、Extension 都是同进程高信任代码；它们可以做权限 gate，但不是安全边界。除非执行被委托到 OS/container/VM/sandbox，否则不要把 hook 拦截描述成隔离。

## 安全边界判断

给用户解释 shell 安全时，必须分清：

- 权限 gate：`allowed_commands`、`allowed_operators`、path allow/deny、hook 拦截、tool allowlist。它们减少误操作和暴露面，但仍在当前进程权限内。
- 安全边界：OS sandbox、Docker、VM、远端受控 backend。只有这些能限制进程实际可读写/联网范围。

如果启用 sandbox，要说明命令到底在 host、本地 sandbox、Docker、远端 sandbox，还是某个 routed backend 中执行。若 backend 不可用，必须记录 `[SANDBOX_UNAVAILABLE]` 并按未隔离处理。

## Bypass 与 fail-closed

不要把“全允许”或“跳过询问”写成安全方案：

- `allowed_commands: "*"` / `allowed_operators: "*"` 是最大执行面，只适合可信开发环境或先观察 audit 的探索阶段。
- 如果任务依赖 sandbox 作为安全边界，必须验证 backend 可用；不可用时要 fail closed 或明确标记未隔离，不能继续声称已隔离。
- sandbox `excluded_commands`、hook allow、tool allowlist 是便利/路由规则，不是安全边界；命中后仍要保留 audit 证据。
- 遇到解析失败、复合命令过复杂、wrapper/env 前缀不确定时，按不可信处理，要求显式配置或阻断。

## 必跑真实验证

修改 shell 权限、审计、sandbox、路径安全、后台任务或 stall 逻辑时，不能只跑单测。至少选择相关真实 Application，使用隔离 runtime：

```bash
export AGENT_LOOM_RUNTIME_ROOT=/tmp/agentloom-runtime-shell-security
```

建议矩阵：

```bash
.venv/bin/loom run applications/test_shell_audit/workflows/test_shell_policy_snapshot_agent.yaml --log-to-file
.venv/bin/loom run applications/test_shell_audit/workflows/test_shell_audit_log_agent.yaml --log-to-file
.venv/bin/loom run applications/test_shell_audit/workflows/test_shell_audit_signals_agent.yaml --log-to-file
.venv/bin/loom run applications/test_shell_allowlist_matrix/workflows/test_shell_allowlist_matrix_agent.yaml --log-to-file
.venv/bin/loom run applications/test_demo/workflows/test_path_access_control_agent.yaml --log-to-file
.venv/bin/loom run applications/test_demo/workflows/test_security_transparency_agent.yaml --log-to-file
.venv/bin/loom run applications/test_demo/workflows/test_background_task_agent.yaml --log-to-file
.venv/bin/loom run applications/test_demo/workflows/test_shell_stall_detection_agent.yaml --log-to-file
```

通过标准：

- LLM final 不能只说 PASS，必须列出实际 `shell_audit.log` 路径和关键证据行。
- agent log 与 shell audit log 父目录一致。
- 全允许场景必须有 `[POLICY_SNAPSHOT]`，且显示 `allowed_commands: *` / `allowed_operators: *`。
- 收敛白名单场景必须证明允许命令成功、未允许命令被拒绝、未允许操作符被拒绝。
- `;` 等操作符被拒绝时 suggestion 必须是 `allowed_operators`，不得建议放进 `allowed_commands`。
- timeout/stall/background 场景结束后检查无 `sleep 300` 等残留进程。
- sandbox 不可用时记录真实 unavailable reason；不可伪造 sandbox PASS。

## 单测与静态校验

```bash
.venv/bin/python -m pytest tests/tools_test/shell -q
.venv/bin/python -m py_compile src/tools/shell/*.py src/trace/task_context.py
.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py --app-root applications/test_shell_audit
.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py --app-root applications/test_shell_allowlist_matrix
```

失败时先定位根因，不要为了旧调用方式做兼容层。Shell 安全策略属于执行边界，修复要以当前真实 contract 为准。
