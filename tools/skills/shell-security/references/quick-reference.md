# Shell Security Quick Reference

## All Configurable Settings

```
shell_settings.
├── allowed_commands: "*"                        # Command whitelist
├── allowed_operators: "*"                       # Operator whitelist
├── security_checks:                             # 10 independent toggles
│   ├── command_substitution: true               #   $() and backticks
│   ├── parameter_expansion: true                #   ${}
│   ├── process_substitution: true               #   <(), >()
│   ├── env_injection: true                      #   LD_PRELOAD, PATH
│   ├── control_characters: true                 #   hidden control chars
│   ├── dangerous_shell_prefix: true             #   sudo, bash -c, env
│   ├── zsh_dangerous_commands: true             #   zmodload, ztcp
│   ├── incomplete_commands: true                #   unclosed quotes
│   ├── ifs_injection: true                      #   IFS manipulation
│   └── destructive_patterns: true               #   rm -rf /, mkfs
├── allowed_paths: ["."]                         # Permitted directories
├── dangerous_paths: ["/", "/etc", ...]          # Protected paths
├── block_destructive: true                      # Enable path protection
├── background_tasks:                            # Background tasks
│   ├── enabled: true
│   ├── max_concurrent: 10
│   ├── auto_background_on_timeout: true
│   ├── max_output_bytes: 104857600
│   ├── stall_detection: true
│   └── stall_threshold_seconds: 45
└── audit_log:                                   # Per-agent audit log
    ├── enabled: true                            #   master switch
    └── log_success: false                       #   also log successes
```

## Config Override Hierarchy

```
config/system.yaml (global defaults)
  └─ applications/<app>/config/system.yaml (app-level override)
      └─ agent.yaml shell_settings.* (per-agent override)
```

## Shell Tool Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `command` | `str` | — | Shell command to execute |
| `timeout` | `int` | `120` | Timeout in seconds (max 1800) |
| `run_in_background` | `bool` | `false` | Run as background task |

## Related Tools

| Tool | Purpose |
|------|---------|
| `shell_tool(command, timeout, run_in_background)` | Execute shell commands |
| `check_background_task(task_id)` | Check background task status |
| `kill_background_task(task_id)` | Kill a background task |
| `list_background_tasks()` | List all background tasks |

## Security Layers Summary

| Layer | Module | Configurable? | Purpose |
|-------|--------|:---:|---------|
| 1. Security Checks | `security.py` | ✅ 10 toggles | Block injection, escalation, destruction |
| 2. Path Validation | `path_validation.py` | ✅ 3 settings | Enforce directory boundaries |
| 3. Command Whitelist | `validator.py` | ✅ 2 settings | Restrict allowed commands |
| 4. Hardcoded Safety | multiple | ❌ | Unbypassable baseline (sudo, rm -rf /, API keys) |
| 5. Env Scrubbing | `subprocess_env.py` | ❌ | Remove 29 sensitive env vars |
| 6. Stall Detection | `stall_watchdog.py` | ✅ threshold | Auto-kill on interactive prompts |
| 7. Size Watchdog | `tree_kill.py` | ✅ max_bytes | Kill on output > 100MB |
| 8. Audit Log | `shell_audit_log.py` | ✅ 3 settings | Per-agent security event log |
