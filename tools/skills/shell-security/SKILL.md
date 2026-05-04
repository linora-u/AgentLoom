---
name: shell-security
description: "Shell tool security configuration and troubleshooting. Use when setting up shell command whitelists, adjusting security checks, configuring per-agent shell permissions, debugging blocked commands, or reviewing shell execution architecture."
version: "1.0.0"
allowed-tools: "Read, Bash, Grep, Glob"
---

# Shell Security Configuration

Reference skill for configuring and troubleshooting the `shell_tool` security system — command whitelists, 10 security checks, path boundary validation, background tasks, and foreground stall detection.

## Applicable Scenarios

- Setting up shell command whitelists (`allowed_commands`) for restricted agents
- Adjusting security check toggles (`security_checks.*`) for build/development agents
- Configuring per-agent shell permissions (read-only vs developer vs full-trust)
- Debugging "Blocked" errors from security checks
- Understanding the three-layer security architecture
- Configuring background task behavior and stall detection thresholds
- Troubleshooting commands that hang or get auto-terminated

## Non-Applicable Scenarios

- General Agent YAML configuration → see `create-app` skill
- Creating new tools or Skills → see `create-skill` skill
- Reviewing workflow architecture → see `workflow-review` skill

## Architecture Overview

The shell tool implements **defense-in-depth** with three independent security layers:

```
Command Input
  │
  ├── Layer 1: Command Security Checks (security.py)
  │   └── 10 configurable pattern checks + hardcoded safety net
  │
  ├── Layer 2: Path Boundary Validation (path_validation.py)
  │   └── allowed_paths + dangerous_paths + block_destructive
  │
  ├── Layer 3: Command/Operator Whitelist (validator.py)
  │   └── allowed_commands + allowed_operators
  │
  ▼
Execution (subprocess.Popen, stdin=DEVNULL, start_new_session=True)
  │
  ├── SizeWatchdog (100MB output limit)
  ├── StallWatchdog (interactive prompt detection, 45s threshold)
  └── 1s polling loop (foreground stall auto-kill)
```

## Configurable Settings (15 items via YAML)

All settings are read from `shell_settings.*` in `config/system.yaml` and can be overridden per-agent via Agent YAML.

### Command Whitelists

| Key | Default | Description |
|-----|---------|-------------|
| `allowed_commands` | `"*"` | Allowed command names; `"*"` = no restriction |
| `allowed_operators` | `"*"` | Allowed shell operators; `"*"` = no restriction |

### Security Checks (10 sub-toggles)

All default to `true` when absent. Set individual checks to `false` to disable:

| Key | Blocks | Can Disable? |
|-----|--------|:---:|
| `command_substitution` | `$()` and backticks | Yes (build scripts) |
| `parameter_expansion` | `${}` parameter expansion | Yes (build scripts) |
| `process_substitution` | `<()` / `>()` | Generally keep on |
| `env_injection` | `LD_PRELOAD`, `PATH` injection | ⚠️ Keep on |
| `control_characters` | Hidden control chars | ⚠️ Keep on |
| `dangerous_shell_prefix` | `sudo`, `bash -c`, `env` | ⚠️ Keep on |
| `zsh_dangerous_commands` | `zmodload`, `ztcp` etc. | Generally keep on |
| `incomplete_commands` | Unclosed quotes | Yes (build scripts) |
| `ifs_injection` | IFS manipulation | ⚠️ Keep on |
| `destructive_patterns` | `rm -rf /`, `mkfs` | ⚠️ Keep on |

### Path Boundary

| Key | Default | Description |
|-----|---------|-------------|
| `allowed_paths` | `["."]` | Directories where file ops are permitted |
| `dangerous_paths` | 17 system paths | Blocked from destructive ops (rm, rmdir) |
| `block_destructive` | `true` | Enable dangerous path blocking |

### Background Tasks

| Key | Default | Description |
|-----|---------|-------------|
| `background_tasks.enabled` | `true` | Enable background task system |
| `background_tasks.max_concurrent` | `10` | Max concurrent background tasks |
| `background_tasks.auto_background_on_timeout` | `true` | Auto-promote on timeout |
| `background_tasks.max_output_bytes` | `104857600` | Output limit (100MB) |
| `background_tasks.stall_detection` | `true` | Enable stall detection |
| `background_tasks.stall_threshold_seconds` | `45` | Stall detection threshold |

### Audit Log

Per-agent shell security audit log.  Writes structured events (blocks, stalls,
timeouts, path violations) to `.logs/{agent_name}/{timestamp}/shell_audit.log`
(co-located with the agent run log) so users can quickly diagnose shell
permission issues.

| Key | Default | Description |
|-----|---------|-------------|
| `audit_log.enabled` | `true` | Master switch for audit logging |
| `audit_log.log_success` | `false` | Also log successful command executions |

Each audit entry contains: timestamp, event type, agent name, command,
check/message details, and an **actionable suggestion** telling the user
which YAML setting to change.  Event types:

| Event | Trigger |
|-------|--------|
| `SECURITY_BLOCK` | Command blocked by a security check |
| `PATH_VIOLATION` | Path boundary violation |
| `WHITELIST_REJECT` | Command/operator not in whitelist |
| `STALL_DETECTED` | Foreground stall (interactive prompt) |
| `TIMEOUT` | Command exceeded timeout (killed) |
| `BACKGROUND_PROMOTION` | Timeout → auto-promoted to background |
| `SANDBOX_WRAP` | Command wrapped in sandbox |
| `COMMAND_SUCCESS` | Successful execution (only if `log_success: true`) |

## Hardcoded Safety Net (cannot be disabled)

- 20 dangerous shell prefix blacklist (`sudo`, `bash`, `pkexec` etc.)
- 18 zsh dangerous builtins (`zmodload`, `zf_rm` etc.)
- 15 destructive command regex patterns (`rm -rf /`, fork bomb etc.)
- 29 sensitive env vars auto-scrubbed (API keys, DB passwords etc.)
- Max timeout cap: 1800 seconds
- Max output file: 100MB (SizeWatchdog)

## Per-Agent Override

Use the top-level `shell_settings` key in Agent YAML to override shell security:

```yaml
# Read-only agent example
tools:
  - name: "shell_tool"
  - name: "read_file"
shell_settings:
  allowed_commands: ["ls", "cat", "grep", "find", "wc", "pwd"]
  allowed_operators: ["|", "&&"]
  block_destructive: true
```

```yaml
# Developer agent — relax $() and ${}
tools:
  - name: "shell_tool"
  - name: "edit_file"
shell_settings:
  security_checks:
    command_substitution: false
    parameter_expansion: false
```

> See [references/per-agent-examples.md](references/per-agent-examples.md) for complete examples.

## Troubleshooting

> See [references/troubleshooting.md](references/troubleshooting.md) for common errors and solutions.

## Notes

- `stdin=subprocess.DEVNULL` on all subprocesses — interactive commands get EOF immediately
- StallWatchdog is still needed for commands that write prompts then block on timers/TTY
- `pipe_redirect.py` normalizes `< /dev/null` placement in pipelines (prevents `rg` stdin hang)
- Environment snapshot captured once at session init, replayed via `source snapshot.sh`
- CWD tracked via out-of-band file (`pwd -P >| cwd_file`), not embedded in stdout
