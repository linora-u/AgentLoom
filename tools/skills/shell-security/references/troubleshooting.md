# Shell Tool Troubleshooting Guide

## Command Blocked Errors

### "Command contains command substitution"

```
Blocked: Command contains command substitution: $(...)
```

**Cause**: `security_checks.command_substitution` is `true` (default).

**Fix**: Disable in Agent YAML if the use is legitimate (e.g., build scripts):
```yaml
shell_settings:
  security_checks:
    command_substitution: false
```

### "Command contains parameter expansion"

```
Blocked: Command contains parameter expansion: ${...}
```

**Cause**: `security_checks.parameter_expansion` is `true` (default).

**Fix**: Same approach — disable for agents that need `${}`:
```yaml
shell_settings:
  security_checks:
    parameter_expansion: false
```

### "Command starts with dangerous prefix"

```
Blocked: Command starts with dangerous shell prefix: sudo
```

**Cause**: `sudo` is in both the configurable check AND the hardcoded blacklist.

**Note**: Even if you set `security_checks.dangerous_shell_prefix: false`, `sudo` remains blocked by the hardcoded safety net. This is by design.

### "Command not in allowed commands"

```
Blocked: Command 'wget' is not in allowed commands list
```

**Cause**: `allowed_commands` is set to a specific list that doesn't include `wget`.

**Fix**: Add the command to the whitelist:
```yaml
shell_settings:
  allowed_commands: ["ls", "cat", "grep", "wget"]
```

Or use wildcard to allow all: `allowed_commands: "*"`

### "Path is within dangerous paths"

```
Blocked: Path /etc/passwd is within dangerous paths
```

**Cause**: `block_destructive: true` and the operation targets a path in `dangerous_paths`.

**Fix**: Adjust `dangerous_paths` or `allowed_paths` in Agent YAML (use with caution).

---

## Stall and Timeout Issues

### Command auto-terminated with Stall Warning

```
[Stall Warning: Background task "fg-12345" appears to be waiting for interactive input.
Last output line: Continue? (y/n)
Consider killing this task and re-running with non-interactive flags.]
```

**Cause**: The command produced a prompt-like output (`(y/n)`, `Continue?`, etc.) then stopped producing output for 45 seconds. The StallWatchdog detected this and the polling loop killed the process.

**Fix**: Use non-interactive flags:
- `apt-get install -y` instead of `apt-get install`
- `pip install --yes` for interactive installers
- `echo y | command` to pipe input
- `DEBIAN_FRONTEND=noninteractive apt-get install` for apt

### Command promoted to background after timeout

```
[Background Task: abc123] Command promoted to background after 120s timeout.
Use check_background_task('abc123') to monitor.
```

**Cause**: Command ran longer than `timeout` (default 120s) with `auto_background_on_timeout: true`.

**Fix options**:
1. Use `check_background_task('abc123')` to monitor progress
2. Increase timeout: `shell_tool(command="...", timeout=300)`
3. Run explicitly in background: `shell_tool(command="...", run_in_background=True)`

### Command killed after 120s timeout (no background promotion)

**Cause**: `auto_background_on_timeout: false` or `background_tasks.enabled: false`.

**Fix**: Enable background tasks:
```yaml
shell_settings:
  background_tasks:
    enabled: true
    auto_background_on_timeout: true
```

---

## Pipeline Issues

### `rg pattern | wc -l` hangs

**Cause**: Without arguments, `rg` reads from stdin. In a pipeline with `eval`, the `< /dev/null` redirect applies to the whole pipeline, not just the first command.

**How it's handled**: The `pipe_redirect.py` module automatically rearranges the command to `rg pattern < /dev/null | wc -l`, placing the redirect after the first command in the pipeline.

**If it still hangs**: The command may contain complex syntax (`$()`, backticks, control structures) that causes `pipe_redirect` to conservatively skip rearrangement. Manually add `< /dev/null`:
```
rg pattern < /dev/null | wc -l
```

---

## Environment Issues

### Environment variables not persisting across commands

**This is by design**. `export` statements only take effect within the current command. Each command runs as a separate subprocess.

**Workaround**: Combine commands in a single call:
```
export MY_VAR=hello && echo $MY_VAR
```

### PATH changes not persisting

PATH is captured in the environment snapshot at session init. Subsequent `export PATH=...` in individual commands will not persist.

**Workaround**: If you need a persistent PATH change, modify the shell profile file and reinitialize the session.

---

## Diagnostic Commands

Check if a command would be blocked (without executing):
```python
from src.shell_settings.security import check_command_security
result = check_command_security("sudo rm -rf /")
# Returns error message or None
```

Check stall watchdog patterns:
```python
from src.shell_settings.stall_watchdog import PROMPT_PATTERNS
line = "Continue? (y/n)"
matches = any(p.search(line) for p in PROMPT_PATTERNS)
# True — this line would trigger stall detection
```

---

## Audit Log

All shell security events (blocks, stalls, timeouts, path violations) are
written to a dedicated per-agent audit log file.

**File location**: `.logs/{agent_name}/{timestamp}/shell_audit.log` (co-located with agent run log)

### Finding audit logs
```bash
find .logs/ -name 'shell_audit.log' -type f | sort
```

### Reading the latest audit log for an agent
```bash
ls -td .logs/my_agent/*/ | head -1 | xargs -I{} cat {}/shell_audit.log
```

### Grep for specific blocked commands
```bash
grep -r 'SECURITY_BLOCK' .logs/my_agent/
grep -r 'WHITELIST_REJECT' .logs/my_agent/
grep -r 'STALL_DETECTED' .logs/my_agent/
```

Each entry includes an actionable **suggestion** field that tells you
exactly which YAML configuration to change. Example:
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

### Disabling audit logging
Set `shell_settings.audit_log.enabled: false` in `config/system.yaml` or
per-agent in the agent YAML to disable audit logging entirely.
