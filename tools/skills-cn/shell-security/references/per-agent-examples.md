# Per-Agent Shell 安全覆盖示例

## 场景 1：只读审计 Agent

仅允许查看命令，阻止所有写入和破坏性操作。

```yaml
name: "readonly_auditor"
description: "只读代码审计 Agent"
model_type: "powerful"
tool_call_type: "code_act"

# Per-agent 配置：tools 列表 + shell_settings 覆盖
tools:
  - name: "shell_tool"
  - name: "read_file"
  - name: "grep_search"
shell_settings:
  allowed_commands:
    - "ls"
    - "cat"
    - "head"
    - "tail"
    - "grep"
    - "find"
    - "wc"
    - "pwd"
    - "file"
    - "stat"
  allowed_operators: ["|", "&&"]
  block_destructive: true
  allowed_paths: ["."]

workflow: |
  你是一个只读代码审计 Agent，只能查看文件内容，不能修改。
```

## 场景 2：开发 Agent

放宽 `$()` 和 `${}` 用于构建脚本，但保留安全底线。

```yaml
name: "developer"
description: "开发与测试 Agent"
model_type: "powerful"
tool_call_type: "code_act"

tools:
  - name: "shell_tool"
  - name: "edit_file"
shell_settings:
  allowed_commands: "*"
  allowed_operators: "*"
  security_checks:
    command_substitution: false     # 允许 $()，构建脚本需要
    parameter_expansion: false      # 允许 ${}，变量处理需要
    dangerous_shell_prefix: true    # 仍然禁止 sudo
    destructive_patterns: true      # 仍然禁止 rm -rf /
  allowed_paths: [".", "/tmp"]
  background_tasks:
    stall_threshold_seconds: 30     # 更快检测停滞

workflow: |
  你是一个开发 Agent，可以编写代码、运行构建和测试。
```

## 场景 3：最小权限 Agent — 禁用 Shell

不声明 `shell_tool`，Agent 无法执行任何 Shell 命令。

```yaml
name: "text_analyzer"
description: "纯文本分析 Agent，不需要 Shell"
model_type: "fast"
tool_call_type: "code_act"
tools:

  - name: "read_file"
  - name: "grep_search"

workflow: |
  你是一个文本分析 Agent，只能读取和搜索文件。
```

## 场景 4：CI/CD 流水线 Agent

允许广泛命令但限制在项目目录和 /tmp。

```yaml
name: "ci_runner"
description: "CI/CD 流水线执行 Agent"
model_type: "powerful"
tool_call_type: "code_act"

tools:
  - name: "shell_tool"
  - name: "read_file"
shell_settings:
  allowed_commands: "*"
  allowed_operators: "*"
  security_checks:
    command_substitution: false
    parameter_expansion: false
    process_substitution: false
    incomplete_commands: false
  allowed_paths: [".", "/tmp"]
  block_destructive: true
  background_tasks:
    enabled: true
    auto_background_on_timeout: true
    stall_threshold_seconds: 20

workflow: |
  你是一个 CI/CD Agent，负责运行构建、测试和部署脚本。
```

## 覆盖合并规则

| 数据类型 | 合并行为 |
|---------|---------|
| 字典 (`security_checks`) | 按键覆盖；未声明的键保持上级值 |
| 列表 (`allowed_commands`, `dangerous_paths`) | **完全替换** |
| 标量 (`block_destructive`, `stall_threshold_seconds`) | 完全替换 |

> 列表是**整体替换而非追加**。覆盖 `dangerous_paths` 时需写完整列表。
