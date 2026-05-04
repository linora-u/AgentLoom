# Shell 安全快速参考

## 全部可配置项

```
shell_settings.
├── allowed_commands: "*"                        # 命令白名单
├── allowed_operators: "*"                       # 操作符白名单
├── security_checks:                             # 10 个独立开关
│   ├── command_substitution: true               #   $() 和反引号
│   ├── parameter_expansion: true                #   ${}
│   ├── process_substitution: true               #   <(), >()
│   ├── env_injection: true                      #   LD_PRELOAD, PATH
│   ├── control_characters: true                 #   隐藏控制字符
│   ├── dangerous_shell_prefix: true             #   sudo, bash -c, env
│   ├── zsh_dangerous_commands: true             #   zmodload, ztcp
│   ├── incomplete_commands: true                #   未闭合引号
│   ├── ifs_injection: true                      #   IFS 操纵
│   └── destructive_patterns: true               #   rm -rf /, mkfs
├── allowed_paths: ["."]                         # 允许操作路径
├── dangerous_paths: ["/", "/etc", ...]          # 危险路径
├── block_destructive: true                      # 启用危险路径拦截
├── background_tasks:                            # 后台任务
│   ├── enabled: true
│   ├── max_concurrent: 10
│   ├── auto_background_on_timeout: true
│   ├── max_output_bytes: 104857600
│   ├── stall_detection: true
│   └── stall_threshold_seconds: 45
└── audit_log:                                   # Per-Agent 审计日志
    ├── enabled: true                            #   总开关
    └── log_success: false                       #   是否记录成功命令
```

## 配置覆盖层级

```
config/system.yaml (全局默认)
  └─ applications/<app>/config/system.yaml (应用级覆盖)
      └─ agent.yaml shell_settings.* (Agent 级覆盖)
```

## Shell 工具参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `command` | `str` | — | 要执行的命令 |
| `timeout` | `int` | `120` | 超时秒数（最大 1800） |
| `run_in_background` | `bool` | `false` | 后台执行 |

## 相关工具

| 工具 | 功能 |
|------|------|
| `shell_tool(command, timeout, run_in_background)` | 执行 Shell 命令 |
| `check_background_task(task_id)` | 查看后台任务状态 |
| `kill_background_task(task_id)` | 终止后台任务 |
| `list_background_tasks()` | 列出所有后台任务 |

## 安全层级总览

| 层级 | 模块 | 可配置？ | 用途 |
|------|------|:-------:|------|
| 1. 安全检查 | `security.py` | ✅ 10 个开关 | 拦截注入、提权、破坏 |
| 2. 路径验证 | `path_validation.py` | ✅ 3 个设置 | 强制目录边界 |
| 3. 命令白名单 | `validator.py` | ✅ 2 个设置 | 限制允许的命令 |
| 4. 硬编码底线 | 多处 | ❌ | 不可绕过（sudo, rm -rf /, API keys） |
| 5. 环境清理 | `subprocess_env.py` | ❌ | 移除 29 个敏感变量 |
| 6. 停滞检测 | `stall_watchdog.py` | ✅ 阈值 | 交互式提示自动终止 |
| 7. 大小看门狗 | `tree_kill.py` | ✅ 上限 | 输出超 100MB 终止 |
| 8. 审计日志 | `shell_audit_log.py` | ✅ 3 个设置 | Per-Agent 安全事件日志 |
