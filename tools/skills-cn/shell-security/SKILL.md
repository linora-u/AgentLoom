---
name: shell-security
description: "Shell 工具安全配置与问题排查。适用于设置命令白名单、调整安全检查、配置 Per-Agent Shell 权限、调试被拦截的命令、理解 Shell 执行架构。"
version: "1.0.0"
allowed-tools: "Read, Bash, Grep, Glob"
---

# Shell 安全配置

Shell 工具 (`shell_tool`) 安全体系参考 Skill — 命令白名单、10 项安全检查、路径边界验证、后台任务管理、前台停滞检测。

## 适用场景

- 为受限 Agent 设置命令白名单 (`allowed_commands`)
- 为构建/开发 Agent 调整安全检查开关 (`security_checks.*`)
- 配置 Per-Agent Shell 权限（只读、开发、完全信任）
- 调试 "Blocked" 拦截错误
- 理解三层安全架构
- 配置后台任务行为和停滞检测阈值
- 排查命令挂起或被自动终止的问题

## 不适用场景

- 通用 Agent YAML 配置 → 参见 `create-app` Skill
- 创建新工具或 Skill → 参见 `create-skill` Skill
- 审查工作流架构 → 参见 `workflow-review` Skill

## 架构概览

Shell 工具实现了**纵深防御**的三层独立安全机制：

```
命令输入
  │
  ├── 第 1 层：命令安全检查 (security.py)
  │   └── 10 个可配置检查 + 硬编码安全底线
  │
  ├── 第 2 层：路径边界验证 (path_validation.py)
  │   └── allowed_paths + dangerous_paths + block_destructive
  │
  ├── 第 3 层：命令/操作符白名单 (validator.py)
  │   └── allowed_commands + allowed_operators
  │
  ▼
执行 (subprocess.Popen, stdin=DEVNULL, start_new_session=True)
  │
  ├── SizeWatchdog (100MB 输出上限)
  ├── StallWatchdog (交互式提示检测, 45 秒阈值)
  └── 1 秒轮询循环 (前台停滞自动终止)
```

## 可配置项（15 项，通过 YAML 控制）

所有设置通过 `config/system.yaml` 的 `shell_settings.*` 读取，支持 Agent YAML 覆盖。

### 命令白名单

| 配置键 | 默认值 | 说明 |
|--------|--------|------|
| `allowed_commands` | `"*"` | 允许的命令名；`"*"` = 不限制 |
| `allowed_operators` | `"*"` | 允许的操作符；`"*"` = 不限制 |

### 安全检查（10 个子开关）

全部省略时默认 `true`。单独设为 `false` 可关闭：

| 检查 ID | 拦截内容 | 可关闭？ |
|---------|---------|:-------:|
| `command_substitution` | `$()` 和反引号 | 构建脚本可关 |
| `parameter_expansion` | `${}` 参数展开 | 构建脚本可关 |
| `process_substitution` | `<()` / `>()` | 一般保持开启 |
| `env_injection` | `LD_PRELOAD`, `PATH` 注入 | ⚠️ 保持开启 |
| `control_characters` | 隐藏控制字符 | ⚠️ 保持开启 |
| `dangerous_shell_prefix` | `sudo`, `bash -c`, `env` | ⚠️ 保持开启 |
| `zsh_dangerous_commands` | `zmodload`, `ztcp` 等 | 一般保持开启 |
| `incomplete_commands` | 未闭合引号 | 构建脚本可关 |
| `ifs_injection` | IFS 操纵 | ⚠️ 保持开启 |
| `destructive_patterns` | `rm -rf /`, `mkfs` | ⚠️ 保持开启 |

### 路径边界

| 配置键 | 默认值 | 说明 |
|--------|--------|------|
| `allowed_paths` | `["."]` | 允许操作的目录 |
| `dangerous_paths` | 17 个系统路径 | 禁止破坏性操作的路径 |
| `block_destructive` | `true` | 启用危险路径拦截 |

### 后台任务

| 配置键 | 默认值 | 说明 |
|--------|--------|------|
| `background_tasks.enabled` | `true` | 启用后台任务 |
| `background_tasks.max_concurrent` | `10` | 最大并发数 |
| `background_tasks.auto_background_on_timeout` | `true` | 超时转后台 |
| `background_tasks.max_output_bytes` | `104857600` | 输出上限 (100MB) |
| `background_tasks.stall_detection` | `true` | 启用停滞检测 |
| `background_tasks.stall_threshold_seconds` | `45` | 停滞阈值 |

### 审计日志

Per-Agent Shell 安全审计日志。将结构化事件（拦截、停滞、超时、路径违规）
写入 `.logs/{agent_name}/{timestamp}/shell_audit.log`（与 Agent 运行日志同目录），
方便用户快速定位和配置 Shell 权限。

| 配置键 | 默认值 | 说明 |
|--------|--------|------|
| `audit_log.enabled` | `true` | 审计日志总开关 |
| `audit_log.log_success` | `false` | 是否记录成功执行的命令 |

每条审计记录包含：时间戳、事件类型、Agent 名称、命令、检查/消息详情、
以及**可操作的修复建议**（告诉用户该改哪个 YAML 配置项）。事件类型：

| 事件 | 触发条件 |
|------|--------|
| `SECURITY_BLOCK` | 安全检查拦截命令 |
| `PATH_VIOLATION` | 路径边界违规 |
| `WHITELIST_REJECT` | 命令/操作符不在白名单 |
| `STALL_DETECTED` | 前台停滞（交互式提示检测） |
| `TIMEOUT` | 命令超时（被终止） |
| `BACKGROUND_PROMOTION` | 超时自动提升为后台任务 |
| `SANDBOX_WRAP` | 命令被沙箱包装 |
| `COMMAND_SUCCESS` | 成功执行（仅 `log_success: true` 时） |

## 硬编码安全底线（不可关闭）

- 20 个危险 Shell 前缀黑名单（`sudo`, `bash`, `pkexec` 等）
- 18 个 Zsh 危险内置命令
- 15 个破坏性命令正则
- 29 个敏感环境变量自动清理（API keys, 数据库密码等）
- 最大超时上限 1800 秒
- 输出文件 100MB 上限 (SizeWatchdog)

## Per-Agent 覆盖

在 Agent YAML 中使用顶层 `shell_settings` 覆盖 Shell 安全配置：

```yaml
# 只读 Agent 示例
tools:
  - name: "shell_tool"
  - name: "read_file"
shell_settings:
  allowed_commands: ["ls", "cat", "grep", "find", "wc", "pwd"]
  allowed_operators: ["|", "&&"]
  block_destructive: true
```

```yaml
# 开发 Agent — 放宽 $() 和 ${}
tools:
  - name: "shell_tool"
  - name: "edit_file"
shell_settings:
  security_checks:
    command_substitution: false
    parameter_expansion: false
```

> 完整示例参见 [references/per-agent-examples.md](references/per-agent-examples.md)

## 问题排查

> 参见 [references/troubleshooting.md](references/troubleshooting.md)

## 备注

- 所有子进程 `stdin=subprocess.DEVNULL` — 交互式命令立即 EOF
- StallWatchdog 仍然需要 — 检测写完 prompt 后阻塞在定时器/TTY 的命令
- `pipe_redirect.py` 规范化管道中 `< /dev/null` 的位置（防止 `rg` stdin 挂起）
- 环境快照在会话初始化时捕获一次，通过 `source snapshot.sh` 复用
- CWD 通过带外文件追踪（`pwd -P >| cwd_file`），不嵌入 stdout
