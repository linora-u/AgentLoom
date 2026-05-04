# Shell 工具问题排查指南

## 命令被拦截

### "Command contains command substitution"

```
Blocked: Command contains command substitution: $(...)
```

**原因**：`security_checks.command_substitution` 为 `true`（默认）。

**解决**：在 Agent YAML 中关闭该检查：
```yaml
shell_settings:
  security_checks:
    command_substitution: false
```

### "Command starts with dangerous prefix"

```
Blocked: Command starts with dangerous shell prefix: sudo
```

**原因**：`sudo` 同时在可配置检查和硬编码黑名单中。

**注意**：即使设置 `dangerous_shell_prefix: false`，`sudo` 仍被硬编码安全底线阻止。这是设计如此。

### "Command not in allowed commands"

```
Blocked: Command 'wget' is not in allowed commands list
```

**原因**：`allowed_commands` 设置为不包含 `wget` 的列表。

**解决**：添加到白名单或使用通配符 `"*"`。

### "Path is within dangerous paths"

```
Blocked: Path /etc/passwd is within dangerous paths
```

**原因**：`block_destructive: true` 且操作路径在 `dangerous_paths` 中。

**解决**：在 Agent YAML 中调整 `dangerous_paths` 或 `allowed_paths`（谨慎使用）。

---

## 停滞与超时

### 命令被自动终止并显示 Stall Warning

```
[Stall Warning: Background task "fg-12345" appears to be waiting for interactive input.]
```

**原因**：命令输出了类似 `(y/n)` 的提示后 45 秒无输出增长，被 StallWatchdog 检测并终止。

**解决**：使用非交互参数：
- `apt-get install -y` 代替 `apt-get install`
- `DEBIAN_FRONTEND=noninteractive apt-get install` 用于 apt
- `echo y | command` 管道输入

### 命令超时后自动转为后台

```
[Background Task: abc123] Command promoted to background after 120s timeout.
```

**原因**：命令超过 120 秒，`auto_background_on_timeout: true`。

**解决**：
1. 使用 `check_background_task('abc123')` 查看进度
2. 增大 timeout：`shell_tool(command="...", timeout=300)`
3. 显式后台执行：`shell_tool(command="...", run_in_background=True)`

---

## 管道问题

### `rg pattern | wc -l` 挂起

**原因**：`rg` 无文件参数时从 stdin 读取。管道中 `< /dev/null` 重定向应用到整个管道而非第一个命令。

**处理方式**：`pipe_redirect.py` 自动重排为 `rg pattern < /dev/null | wc -l`。

**如果仍挂起**：命令包含复杂语法（`$()`、反引号、控制结构），`pipe_redirect` 保守跳过。手动添加：
```
rg pattern < /dev/null | wc -l
```

---

## 环境变量

### 环境变量不跨命令持久化

**这是设计如此**。`export` 仅在当前命令生效。每条命令是独立子进程。

**解决**：合并到单条命令：
```
export MY_VAR=hello && echo $MY_VAR
```

---

## 审计日志

所有 Shell 安全事件（拦截、停滞、超时、路径违规）会写入专用的 Per-Agent 审计日志文件。

**文件位置**：`.logs/{agent_name}/{timestamp}/shell_audit.log`（与 Agent 运行日志同目录）

### 查找审计日志
```bash
find .logs/ -name 'shell_audit.log' -type f | sort
```

### 查看某个 Agent 最新的审计日志
```bash
ls -td .logs/my_agent/*/ | head -1 | xargs -I{} cat {}/shell_audit.log
```

### 搜索特定类型的事件
```bash
grep -r 'SECURITY_BLOCK' .logs/my_agent/
grep -r 'WHITELIST_REJECT' .logs/my_agent/
grep -r 'STALL_DETECTED' .logs/my_agent/
```

每条记录包含可操作的 **suggestion** 字段，告诉你该改哪个 YAML 配置。示例：
```
[2026-04-08 13:41:46] [SECURITY_BLOCK] agent=code_reviewer
  command: $(cat /etc/passwd)
  check: command_substitution
  message: Blocked: $() command substitution detected
  suggestion: To disable this check for a specific agent, add the following
    to the agent YAML:
      shell_settings:
        security_checks:
          command_substitution: false
```

### 关闭审计日志
在 `config/system.yaml` 或 Agent YAML 中设置 `shell_settings.audit_log.enabled: false` 即可关闭审计日志。
