# AgentLoom 系统全局配置 (`system.yaml`) 完整参考

> **文档定位**：本文档详细说明 `config/system.yaml` 的**每一个**配置参数。
> 关于配置文件之间的覆盖关系，请参阅 [配置体系总览](config-overview.md)。
> 关于 LLM 模型参数，请参阅 [LLM 配置文档](llm_config.md)。
> 关于 Agent YAML 参数，请参阅 [Agent 配置文档](agent_config.md)。

`config/system.yaml` 是 AgentLoom 框架的**核心全局配置文件**，控制系统元数据、上下文压缩策略、顶层 prompt、全局 Skills、执行环境、代码执行权限、日志、工具系统、工作空间等。

> ⚠️ **system.yaml 与 llm.yaml 的隔离**：所有 LLM 相关配置（`model`、`llm`、`langfuse`）**必须且只能**放在 `config/llm.yaml` 中。如果在 `system.yaml` 中写入这些键，框架会在加载时自动过滤并输出 warning 日志。详见 [LLM 配置文档](llm_config.md)。

配置加载顺序为 `config/system.yaml` -> `config/llm.yaml` -> `applications/<app>/config/system.yaml`（可选）。应用级 `system.yaml` 的发现基于 Agent YAML 配置文件路径：框架从该文件所在目录向上查找 `workflows/` 目录，其父目录即为应用根目录（app_root）。若 `app_root/config/system.yaml` 存在则自动叠加覆盖，不存在则跳过。嵌套应用（如 `my_app/sub_module`）会命中最近的 `workflows/` 目录，天然隔离。

---

## 目录

- [快速参考：完整 YAML 结构](#快速参考完整-yaml-结构)
- [1. system — 系统元数据](#1-system--系统元数据)
- [2. smart_summary — 上下文压缩策略](#2-smart_summary--上下文压缩策略)
- [3. prompt — 顶层 System Prompt 覆盖](#3-prompt--顶层-system-prompt-覆盖)
- [4. skills — 全局 Skills 配置](#4-skills--全局-skills-配置)
- [5. lsp_servers — LSP 语言服务器配置](#5-lsp_servers--lsp-语言服务器配置)
- [5.5 mcp_servers — MCP 外部工具集成](#55-mcp_servers--mcp-外部工具集成)
- [6. execution_env — 执行环境配置](#6-execution_env--执行环境配置)
- [6. code_agent — CodeAgent 代码执行权限](#6-code_agent--codeagent-代码执行权限)
- [7. logging — 日志配置](#7-logging--日志配置)
- [8. tools — 工具系统配置](#8-tools--工具系统配置)
- [9. tool_access_control — 工具访问控制](#9-tool_access_control--工具访问控制)
- [10. tool_metadata — 工具元数据配置](#10-tool_metadata--工具元数据配置)
- [11. tool_output_limits — 工具输出限制](#11-tool_output_limits--工具输出限制)
- [12. checkpoint — 断点续跑与心跳配置](#12-checkpoint--断点续跑与心跳配置)
- [附录 A：Pydantic 模型对照表](#附录-apydantic-模型对照表)
- [附录 B：应用级覆盖与目录结构](#附录-b应用级覆盖与目录结构)

---

## 快速参考：完整 YAML 结构

以下展示 `config/system.yaml` 的**完整结构与仓库示例值**（并在关键处标注框架 fallback 默认值）：

```yaml
# ============================================
# 系统元数据
# ============================================
system:
  name: "AgentLoom"
  version: "1.0.1"
  user_agent: "AgentLoom/1.0.1"

# ============================================
# 上下文压缩策略
# ============================================
smart_summary: false

# ============================================
# 顶层 Prompt 配置（支持 overlay）
# ============================================
prompt:
  path: "sysprompt/system_prompt.yaml"

# ============================================
# 全局 Skills 配置
# ============================================
skills:
  # NOTE: agent-recall-with-files 默认禁用。
  # 该 Skill 通过 PreToolUse/PostToolUse Hook 在工具结果末尾追加 recall 提示，
  # 弱 LLM 存在注意力稀疏问题导致指令被忽略。仅在使用强 LLM 时手动启用。
  # - path: "skills/agent-recall-with-files"
  - path: "skills/agent-visualization"

# ============================================
# 执行环境全局配置
# ============================================
execution_env:
  type: "local"
  # executor_kwargs: {}

# ============================================
# CodeAgent 代码执行权限
# ============================================
code_agent:
  additional_authorized_imports: "*"
  additional_functions: "*"

# ============================================
# 日志配置
# ============================================
logging:
  enabled: true
  level: "INFO"
  file_path: null
  dir: ".logs"

# ============================================
# 默认加载工具
# ============================================
default_loaded_tools:
  - "load_skill"
  - "list_skills"
  - "shell_tool"
  - "read_file"
  - "grep_search"
  - "glob_search"
  - "edit_file"
  - "write_markdown_file"

# ============================================
# Shell 安全配置
# ============================================
shell_settings:
  allowed_commands: "*"
  allowed_operators: "*"

# ============================================
# 工具别名映射
# ============================================
tools_mapping:
  Claude:
    Read: "read_file"
    Write: "write_markdown_file"
    Bash: "shell_tool"
    Glob: "glob_search"
    Grep: "grep_search"
    Edit: "edit_file"

# ============================================
# 工作空间配置（可选，以下为 Pydantic 默认值，不写也生效）
# ============================================
tool_access_control:
  include_paths: []
  exclude_paths: []
  path_param_patterns:
    - "file_path"
    - "filePath"
    - "directory_path"
    - "directory"
    - "dirPath"
    - "repo_path"
    - "path"
    - "path_str"
    - "file_paths"
    - "filePaths"
    - "fileUri"
  tool_access_control: []

# ============================================
# 断点续跑与心跳配置（可选，以下为框架默认值）
# ============================================
checkpoint:
  enabled: true            # 是否启用断点续跑（全局开关）
  cleanup_on_success: true # 任务成功完成后自动删除 checkpoint 目录
  max_resume_age: 604800   # checkpoint 最大保留时长（秒），超出后不可恢复，默认 7 天
  heartbeat_interval: 5    # 心跳写入频率（秒），用于崩溃检测

```

---

## 1. system — 系统元数据

控制系统的基本身份标识，用于日志、HTTP 请求的 User-Agent 头等。

**YAML 路径**：`system.*`
**Pydantic 模型**：`SystemSettings`

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `system.name` | `str` | `"AgentLoom"` | ❌ 否 | 系统名称，用于日志标识和 User-Agent 构建 |
| `system.version` | `str` | `"1.0.1"` | ❌ 否 | 系统版本号（信息性字段） |
| `system.user_agent` | `str` | `"AgentLoom/1.0.1"` | ❌ 否 | HTTP API 请求时的 User-Agent 字符串 |

**示例**：

```yaml
system:
  name: "my-project-agents"
  version: "2.0.0"
  user_agent: "my-project-agents/2.0.0"
```

---

## 2. smart_summary — 上下文压缩策略

控制对话历史的上下文压缩行为。当 Token 超限时，决定使用 LLM 智能摘要还是简单截断。
该字段在系统配置与应用级覆盖合并后会按原值透传到最终配置；请直接写 `true` / `false`，字符串值可能在不同解析路径下产生歧义。

**YAML 路径**：`smart_summary` (顶层字段)
**Pydantic 字段**：`RootSettings.smart_summary`

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `smart_summary` | `bool` | `true` | ❌ 否 | `true`：对话历史超限时使用 LLM 进行智能摘要压缩；`false`：回退到简单截断 |

**示例**：

```yaml
# 启用智能摘要（推荐用于长对话任务）
smart_summary: true

# 禁用智能摘要（回退到截断，减少 LLM 调用）
smart_summary: false
```

## 3. prompt — 顶层 System Prompt 覆盖

`prompt` 是顶层 overlay 配置键，代码会在 `build_effective_agent_config()` 中保留它并透传到最终合并结果。它支持字符串路径或包含 `path` 的映射，作用是为 Agent 提供自定义 System Prompt 模板。

**YAML 路径**：`prompt` (顶层字段)
**Pydantic 视角**：`RootSettings` 的 extra key；不单独建模，但会参与 overlay 合并

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `prompt` | `str` \| `dict` | — | ❌ 否 | 自定义 System Prompt 模板路径。字符串形式直接指定路径，映射形式使用 `path` 键。最终值按原样进入 effective config |

**示例**：

```yaml
prompt:
  path: "applications/my_app/sysprompt/code_agent.yaml"
```

---

## 4. skills — 全局 Skills 配置

定义所有 Agent 默认继承的全局 Skill 包。Skills 是可复用的指令集（Instruction），加载策略由引用侧的 `invocation-control.allow-model` 控制：`true`（按需加载）、`false`（隐藏）、`"force-inject"`（强制注入到系统提示词）。

**YAML 路径**：`skills` (顶层字段)
**类型**：`list[dict | str]`

### 4.1 Skills 条目格式

Skills 支持两种格式：

#### 完整字典格式

```yaml
skills:
  - path: "skills/agent-recall-with-files"
    platform: "Claude"
```

#### 简写字符串格式

```yaml
skills:
  - "skills/agent-recall-with-files"
```

### 4.2 Skills 条目参数

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `path` | `str` | — | ✅ 是（字典格式时） | Skill 包所在目录路径。相对路径基于 `AGENT_ROOT`（包含 `config/system.yaml` 的项目根目录）解析 |
| `platform` | `str` | — | ❌ 否 | 平台标识符（如 `"Claude"`），用于通过 `tools_mapping` 映射工具别名 |
| `invocation-control` | `dict` | `{"allow-model": true, "allow-hook": true}` | ❌ 否 | 控制 Skill 可见性与 Hook 权限。详见 [Skills 配置文档](skills_config.md#52-invocation-control--调用权限与可见性控制) |

### 4.3 Skills 加载顺序

1. **全局 Skills**：`config/system.yaml` 中 `skills` 列表
2. **AGENT_ROOT Skills**：`AgentLoom/skills/` 目录下自动发现的 Skills
3. **Agent 私有 Skills**：Agent YAML 中 `skills` 字段定义的 Skills

> `AGENT_ROOT` 即包含 `config/system.yaml` 的项目根目录（`C.agent_root`）。

> ⚠️ **同名 Skill 冲突**：同一个 Agent 的最终 Skill 视图中如果出现同名 Skill，框架会报错。

### 4.4 禁用全局 Skills（opt-out）

将 `skills` 设置为空列表可完全禁用第 1、2 层加载（全局条目 + 目录自动发现均跳过）：

```yaml
skills: []   # 显式 opt-out：跳过所有全局 skills，包括 AGENT_ROOT/skills/ 目录
```

| `skills` 值 | 行为 |
|---|---|
| 未配置 / `null` | 不加载全局条目，但仍自动发现 `AGENT_ROOT/skills/` 目录 |
| `[]`（空列表） | **完全禁用**：全局条目和目录自动发现均跳过 |
| `[entries...]` | 加载指定条目，同时自动发现 `AGENT_ROOT/skills/` 目录 |

适用于不需要任何 Skill 的轻量级 Agent（如 intent_labeler）。第 3 层 Agent 私有 Skills 不受影响。

**示例**：

```yaml
skills:
  - path: "skills/agent-recall-with-files"
    invocation-control:
      allow-model: "force-inject"
      allow-hook: true

  - path: "skills/agent-visualization"
    invocation-control:
      allow-model: false
      allow-hook: true

  # 简写格式（默认 allow-model: true, allow-hook: true）
  - "skills/my-custom-skill"
```

---

## 5. lsp_servers — LSP 语言服务器配置

配置 Agent 启动时预热的 LSP 语言服务器。服务器在 Agent 整个生命周期内长期驻留，提供代码智能功能（跳转定义、找引用、符号大纲、悬停类型信息等）。

`uv sync` 后所有必需二进制自动就绪：
- **Python**: `jedi-language-server`（pip 依赖，在 `.venv/bin/`）
- **Go**: `go` 二进制（通过 `go-bin` PyPI 包），`gopls` 通过 `go install` 自动安装
- **TypeScript**: `node` + `npm`（通过 `nodejs-bin` PyPI 包），`typescript-language-server` 通过 npm 自动安装
- **Rust/Java/C#/Kotlin**: 底层库自动下载

```yaml
lsp_servers:
  enabled: true                    # false 可关闭所有 LSP 服务
  max_restarts: 3                  # 崩溃自动重启上限（全局默认）
  servers:                         # 要启动的语言服务器列表
    - python                       # jedi-language-server
    - go                           # gopls
    - typescript                   # typescript-language-server
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | `bool` | `true` | 是否启用 LSP 服务 |
| `max_restarts` | `int` | `3` | 服务器崩溃后自动重启的最大次数 |
| `servers` | `list` | `[python]` | 语言列表，支持 40+ 种语言 |

> 服务器由 `src/services/lsp/LSPServerManager` 统一管理，采用三层架构（Manager → Instance → solidlsp）。
> 不支持的语言自动回退到 tree-sitter AST 分析（46+ 语言）。

---

## 5.5 mcp_servers — MCP 外部工具集成

配置全局 MCP (Model Context Protocol) Client，连接外部 MCP Server 动态加载工具。详见 [MCP 配置文档](mcp_config.md)。

```yaml
# 全局 MCP 配置（所有 Agent 共享）
mcp_servers: "config/.mcp.json"

# 高级选项
# mcp_servers:
#   path: "config/.mcp.json"
#   timeout: 30
#   tool_timeout: 60
#   tool_name_prefix: true
```

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `mcp_servers` | `str` / `list` / `dict` | 无 | 指向 `.mcp.json` 文件路径，路径从 `agent_root` 解析 |

`.mcp.json` 使用标准 MCP 格式（`{"mcpServers": {...}}`），支持 `stdio`、`sse`、`http` 三种传输方式。

Agent YAML 中的 `mcp_servers` 会与此全局配置合并（同名 Server 以 Agent 为准）。

---

## 6. execution_env — 执行环境配置

决定 Python 代码与 Shell 指令在何种计算节点中运行。这是最重要的安全与环境隔离配置。

> ⚠️ **模式限制**：整个 `execution_env` 配置仅适用于 `tool_call_type: "code_act"` 模式的 Agent。在 `tool_call` 模式下，`executor_type` 和 `executor_kwargs` 会被静默忽略，因为 `ToolCallingAgentV2` 使用结构化工具调用而非代码执行。
运行时 `executor_type` / `executor_kwargs` 由 Agent YAML 内的 `execution_env` 归一化得到；如果 Agent YAML 未配置该字段，会回退到 `local` + `{}`。Shell 路径通过智能检测链自动确定（参见下方 5.2 节）。

**YAML 路径**：`execution_env.*`

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `execution_env.type` | `str` | `"local"` | ❌ 否 | 执行环境类型。**仅**允许：`"local"`, `"docker"`, `"e2b"`, `"wasm"`，其他任何值都会报错 |
| `execution_env.executor_kwargs` | `dict` | `{}` | ❌ 否 | 透传给执行器构造函数的额外参数，具体接受的 key 取决于 `type` 选择的执行器 |

> ⚠️ `type` 的值不区分大小写（框架会自动 `.strip().lower()`），但**必须**是上述 4 个值之一。传入不支持的值（如 `"host"`、`"ssh"` 等）会报错：
> ```
> execution_env.type must be one of ['local', 'e2b', 'docker', 'wasm'], current value: host
> ```

### 5.1 execution_env.type 总览

| 值 | 底层执行器类 | 说明 | 安全性 | 适用场景 |
|----|-------------|------|--------|----------|
| `"local"` | `LocalPythonExecutor` | 在宿主机环境直接运行 Python 代码 | ⚠️ 低（可修改宿主机文件系统） | 开发调试、可信环境 |
| `"docker"` | `DockerExecutor` | 在 Docker 容器中启动 Jupyter Kernel 运行 | ✅ 高（与宿主机隔离） | 生产环境、不可信代码 |
| `"e2b"` | `E2BExecutor` | 部署到 [E2B](https://e2b.dev/) 云端沙箱运行 | ✅ 高（云端隔离） | 云端部署、SaaS 产品 |
| `"wasm"` | `WasmExecutor` | 通过 Deno + Pyodide 在本地 WebAssembly 沙箱运行 | ✅ 高（进程级隔离） | 轻量级本地隔离 |

### 5.2 Shell 路径自动检测

Shell 路径通过智能检测链自动确定，无需手动配置。仅支持 **bash** 和 **zsh**（其他 shell 如 sh、fish、csh 等因语法差异不兼容而被排除）。

检测链：

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1️⃣ | 环境变量 `$SHELL` (Unix) / `$COMSPEC` (Windows) | **仅当**路径指向 bash/zsh **且**通过可执行性验证时才使用；否则跳过 |
| 2️⃣ | `shutil.which("zsh")` / `shutil.which("bash")` | 自动搜索 PATH，顺序根据 `$SHELL` 偏好自动调整 |
| 3️⃣ | 硬编码路径扫描 | `/bin`、`/usr/bin`、`/usr/local/bin`、`/opt/homebrew/bin` × [bash, zsh] |
| 4️⃣ | 全部失败 | 抛出 `FileNotFoundError`，给出明确提示 |

**可执行性验证（两层检查）**：
- **第 1 层**：`os.access(path, os.X_OK)` — 快速权限位检测
- **第 2 层**（fallback）：实际执行 `<shell> --version` — 兼容 Nix 等权限位不可靠的环境

**偏好排序**：如果 `$SHELL` 含 `bash` 则 bash 优先于 zsh；否则 zsh 优先。

**子进程环境安全**：所有 shell 子进程自动过滤敏感环境变量（API keys、云凭证、CI tokens 等），并注入保护性变量（`GIT_EDITOR=true` 防止交互式编辑器弹出）。支持精确匹配和前缀匹配两种过滤策略。

**执行架构**：采用无状态子进程模型——每条命令启动独立 `subprocess.Popen`，通过环境快照（snapshot）和会话状态文件保持上下文连续性。此架构避免了 PTY 长连接的脆性问题（如缓冲区溢出、交互式命令挂死等）。

**环境快照**：Agent 会话初始化时自动捕获用户 shell 环境（函数定义、别名、shell 选项、PATH），保存为 `snapshot.sh`。后续每条命令执行前自动 source 该快照，恢复完整的用户环境。快照与登录 shell (`-l`) 互斥：快照可用时跳过 `-l` 标志和 `.bashrc` 加载，避免每次命令的双重初始化开销。快照还注入 extglob 保护（`shopt -u extglob` / `setopt NO_EXTENDED_GLOB`）防止 TOCTOU 攻击。

**CWD 追踪**：通过带外文件追踪（`pwd -P >| cwd_file`），不在 stdout 中嵌入控制字符。每条命令执行后自动读取追踪文件更新工作目录状态。

**环境变量**：`export` 语句是临时的——仅在当前命令中有效，不会跨命令持久化。PATH 通过快照机制在会话初始化时保存，后续命令自动继承。

**进程树管理**：子进程使用 `start_new_session=True` 创建独立进程组。超时或异常时通过 `os.killpg()` 发送 SIGTERM → SIGKILL 清理整个进程树，防止孤儿进程。

**大小看门狗**：后台监控输出文件大小，超过 100MB 自动 SIGKILL 终止进程，防止磁盘写满。

**后台任务管理**：超时的命令不会被直接杀死，而是自动提升为后台任务继续运行。Agent 可通过 `check_background_task(task_id)` 查看状态和最近输出、`kill_background_task(task_id)` 终止任务、`list_background_tasks()` 列出所有后台任务。也可通过 `shell_tool(command, run_in_background=True)` 显式启动后台任务。后台任务由 `BackgroundTaskRegistry` 单例管理，支持最大并发数限制（默认 10）。

**停滞检测**：后台任务运行期间，Stall Watchdog 每 5 秒轮询输出文件增长。如果输出 45 秒无增长且末尾匹配交互式提示模式（如 `(y/n)`、`Continue?`、`Press Enter` 等），系统将标记该任务为停滞并通知 Agent。

**前台停滞检测与自动终止**：前台命令执行期间，主线程采用 1 秒轮询循环（而非阻塞等待）。StallWatchdog 同步监控输出文件，一旦检测到交互式提示导致的停滞（45 秒无输出增长 + 末尾匹配提示模式），主线程在下一个轮询周期（最多 1 秒延迟）自动终止该进程并返回明确的停滞警告信息。部分输出（停滞前产生的内容）被保留在结果中。这样避免了命令因等待用户输入而卡死 120 秒的问题。

**管道重定向规范化**：对包含管道（`|`）的命令，自动将 `< /dev/null` 移至管道第一个命令之后（如 `rg foo | wc -l` → `rg foo < /dev/null | wc -l`），防止 `rg` 等工具因等待 stdin 而挂起。遇到 `$()` 、反引号、控制结构等复杂语法时保守跳过。

**环境噪音过滤**：环境变量差量追踪自动过滤 ~25 个系统级动态变量（如 `RANDOM`、`LINENO`、`PIPESTATUS`、`BASH_COMMAND` 等），确保 `session_env.sh` 只保留用户显式导出的变量。

后台任务配置：
```yaml
shell_settings:
  background_tasks:
    enabled: true                  # 是否启用后台任务
    max_concurrent: 10             # 最大并发后台任务数
    auto_background_on_timeout: true  # 超时时自动转后台
    max_output_bytes: 104857600    # 单个任务最大输出（100MB）
    stall_detection: true          # 是否启用停滞检测
    stall_threshold_seconds: 45    # 停滞检测阈值（秒）
```

> 检测结果在进程生命周期内缓存，不会每次命令都重新检测。

### 5.3 `local` — 本地执行器

在宿主机环境中直接运行 AI 生成的 Python 代码。这是默认模式，无需任何额外依赖。

**底层类**：`smolagents.local_python_executor.LocalPythonExecutor`

#### executor_kwargs 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `max_print_outputs_length` | `int` | `50000` | 单次代码执行中 `print()` 输出的最大字符数。超出部分会被截断 |

> `additional_functions` 由框架根据 `code_agent.additional_functions` 配置自动注入，无需在 `executor_kwargs` 中手动指定。

#### 配置示例

```yaml
# 最简配置（推荐开发环境使用）
execution_env:
  type: "local"
```

```yaml
# 限制输出长度
execution_env:
  type: "local"
  executor_kwargs:
    max_print_outputs_length: 100000
```

### 5.4 `docker` — Docker 容器执行器

在 Docker 容器内启动 Jupyter Kernel，通过 HTTP 通信执行代码。代码运行在隔离的容器文件系统中，无法直接修改宿主机。

**底层类**：`smolagents.remote_executors.DockerExecutor`

#### 前置要求

- 安装扩展依赖：`pip install 'smolagents[docker]'`
- Docker daemon 已启动并可用

#### executor_kwargs 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `host` | `str` | `"127.0.0.1"` | Docker 容器绑定的主机地址 |
| `port` | `int` | `8888` | Docker 容器绑定的端口号 |
| `image_name` | `str` | `"jupyter-kernel"` | 使用的 Docker 镜像名称 |
| `build_new_image` | `bool` | `true` | 是否在启动时强制重新构建 Docker 镜像。设为 `false` 可复用已有镜像加快启动 |
| `container_run_kwargs` | `dict` | `{}` | 透传给 `docker.containers.run()` 的额外参数（如 `mem_limit`、`network` 等） |

> ⚠️ Docker 执行器**不支持** `additional_functions` 注入（框架会自动跳过）。
> ⚠️ `code_agent.additional_authorized_imports` 中的通配符 `"*"` 会被自动剥离，仅保留显式列出的模块。

#### 配置示例

```yaml
# 基础 Docker 配置
execution_env:
  type: "docker"
  executor_kwargs:
    image_name: "my-jupyter-kernel:latest"
```

```yaml
# 完整 Docker 配置（复用已有镜像 + 自定义端口 + 内存限制）
execution_env:
  type: "docker"
  executor_kwargs:
    host: "127.0.0.1"
    port: 9999
    image_name: "agentloom-smolagents-jupyter-kernel:local"
    build_new_image: false
    container_run_kwargs:
      mem_limit: "2g"
      network: "host"
```

### 5.5 `e2b` — E2B 云端沙箱执行器

将代码执行托管到 [E2B](https://e2b.dev/) 云端沙箱。适合 SaaS 产品或需要完全隔离的生产环境。

**底层类**：`smolagents.remote_executors.E2BExecutor`

#### 前置要求

- 安装扩展依赖：`pip install 'smolagents[e2b]'`
- 设置环境变量 `E2B_API_KEY`（从 [E2B Dashboard](https://e2b.dev/dashboard) 获取）

#### executor_kwargs 参数

`executor_kwargs` 中的所有参数会**直接透传**给 `e2b_code_interpreter.Sandbox` 构造函数。常用参数包括：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `timeout` | `int` | — | 沙箱超时时间（秒） |

> 完整参数列表参见 [E2B 官方文档](https://e2b.dev/docs)。
> ⚠️ 与 Docker 执行器类似，`additional_functions` 不会注入，通配符 `"*"` 会被自动剥离。

#### 配置示例

```yaml
# 基础 E2B 配置
execution_env:
  type: "e2b"
  executor_kwargs:
    timeout: 300
```

```yaml
# E2B 沙箱 + 长任务超时
execution_env:
  type: "e2b"
  executor_kwargs:
    timeout: 600
```

### 5.6 `wasm` — WebAssembly 本地沙箱执行器

通过 Deno 运行 Pyodide（Python 的 WebAssembly 编译版本），在本地提供进程级隔离的 Python 执行环境。无需 Docker，也无需云端 API。

**底层类**：`smolagents.remote_executors.WasmExecutor`

#### 前置要求

- 安装 [Deno](https://deno.land/)（`curl -fsSL https://deno.land/install.sh | sh`）

#### executor_kwargs 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `deno_path` | `str` | `"deno"` | Deno 可执行文件路径。默认从 `$PATH` 中查找 |
| `deno_permissions` | `list[str]` | *(见下方)* | Deno 运行时权限标志列表 |
| `timeout` | `int` | `60` | 单次代码执行的超时时间（秒） |

**`deno_permissions` 默认值**（允许 Pyodide 从 CDN 下载包和使用本地缓存）：

```
--allow-net=0.0.0.0:8000,cdn.jsdelivr.net:443,pypi.org:443,files.pythonhosted.org:443
--allow-read=~/.cache/deno
--allow-write=~/.cache/deno
```

> ⚠️ 与 Docker/E2B 执行器不同，Wasm 执行器目前尚未被限制，`additional_functions` 会被注入。但通配符 `"*"` 依然会被自动剥离。

#### 配置示例

```yaml
# 基础 WASM 配置
execution_env:
  type: "wasm"
```

```yaml
# 自定义 Deno 路径 + 延长超时
execution_env:
  type: "wasm"
  executor_kwargs:
    deno_path: "/usr/local/bin/deno"
    timeout: 120
```

```yaml
# 完整 WASM 配置（限制网络权限）
execution_env:
  type: "wasm"
  executor_kwargs:
    deno_path: "/usr/local/bin/deno"
    timeout: 90
    deno_permissions:
      - "--allow-net=cdn.jsdelivr.net:443,pypi.org:443"
      - "--allow-read=~/.cache/deno"
      - "--allow-write=~/.cache/deno"
```

### 5.7 远程执行器的通用行为

当 `execution_env.type` 为 `"docker"`、`"e2b"` 或 `"wasm"` 时，框架会自动执行以下安全调整：

| 行为 | 说明 |
|------|------|
| **剥离 `additional_functions`** | 远程执行器的构造函数不支持该参数，框架会自动跳过注入 |
| **剥离通配符 `"*"` import** | `code_agent.additional_authorized_imports` 中的 `"*"` 会被移除，仅保留显式列出的模块名 |

这意味着在远程执行器中，建议显式列出所需的导入模块：

```yaml
# ❌ 不推荐：通配符在远程执行器中会被自动剥离
code_agent:
  additional_authorized_imports: "*"

# ✅ 推荐：显式列出需要的模块
code_agent:
  additional_authorized_imports:
    - "json"
    - "re"
    - "math"
    - "datetime"
    - "pandas"
    - "numpy"
```

---

## 6. code_agent — CodeAgent 代码执行权限

控制 AI 自动生成并运行的 Python 代码的可用边界。仅在 `tool_call_type: "code_act"` 模式下生效。

**YAML 路径**：`code_agent.*`

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `code_agent.additional_authorized_imports` | `str` \| `list[str]` | `[]`（框架 fallback） / `"*"`（仓库示例） | ❌ 否 | 允许 AI 代码导入的 Python 模块白名单 |
| `code_agent.additional_functions` | `str` \| `list[str]` | `[]`（框架输入 fallback） / `"*"`（仓库示例） | ❌ 否 | 允许 AI 代码调用的 Python 内置函数白名单 |

### 6.1 additional_authorized_imports

| 配置值 | 行为 |
|--------|------|
| `"*"` 或 `["*"]` | 允许导入环境内所有模块（**最高权限**） |
| `["json", "re", "os"]` | 仅允许白名单中的模块，其他 import 会报错 |

### 6.2 additional_functions

| 配置值 | 行为 |
|--------|------|
| `"*"` 或 `["*"]` | 允许调用所有 `builtins` 中的可调用对象（包括 `open`, `exec`, `eval` 等高危函数） |
| `["print", "len", "range"]` | 仅允许白名单中的内置函数 |

> ⚠️ 如果指定了不存在的内置函数名，会抛出 `AttributeError`：`'xxx' is not a valid Python built-in function.`

### 6.3 通配符在远程执行器中的行为

当 `execution_env.type` 为 `"docker"`, `"e2b"` 或 `"wasm"` 时，通配符 `"*"` 会在运行时被剥离（防止远程环境的副作用），仅保留显式列出的条目。

**安全配置建议**：

```yaml
# 本地开发（可信环境）
code_agent:
  additional_authorized_imports: "*"
  additional_functions: "*"

# 生产环境（收紧权限）
code_agent:
  additional_authorized_imports:
    - "json"
    - "re"
    - "math"
    - "datetime"
    - "collections"
  additional_functions:
    - "print"
    - "len"
    - "range"
    - "sorted"
    - "enumerate"
    - "zip"
    - "map"
    - "filter"
```

---

## 7. logging — 日志配置

控制系统日志的详细程度和输出位置。日志公共入口统一为 `src.lib.logging`，按"每个 Python 进程生命周期一个日志文件"管理。

**YAML 路径**：`logging.*`

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `logging.enabled` | `bool` | `true` | ❌ 否 | 是否开启日志系统 |
| `logging.level` | `str` | `"INFO"` | ❌ 否 | 日志过滤等级（不区分大小写） |
| `logging.file_path` | `str` \| `null` | `null` | ❌ 否 | 指定日志文件路径。`null` 时自动生成 |
| `logging.dir` | `str` | `".logs"` | ❌ 否 | 自动生成日志时的根目录 |

### 7.1 logging.level 详解

框架使用 `LogLevelParser` 读取 `logging.level`，支持标准 `logging` 级别以及特殊值 `OFF`：

| 值 | 含义 | 适用场景 |
|----|------|----------|
| `"DEBUG"` | 放行所有框架底层流转细节 | 排查错误、查看底层请求 |
| `"INFO"` | **(默认)** 显示核心运行状态和 Step 提示 | 日常使用 |
| `"WARNING"` / `"WARN"` | 屏蔽普通流转，仅暴露危险项与告警 | 简化输出 |
| `"ERROR"` | 仅捕捉异常和错误 | 生产环境 |
| `"CRITICAL"` | 仅捕捉严重异常 | 极度精简 |
| `"OFF"` | 彻底关闭日志（实际设置为 `CRITICAL + 10`） | 静默模式 |

此外也支持**整数值**（如 `10` = DEBUG, `20` = INFO, `30` = WARNING）。

### 7.2 logging.file_path 与 logging.dir

- 当 `file_path` 指定了具体路径时，当前进程全程写入该文件
- 当 `file_path` 为 `null` 时，框架自动在 `dir` 目录下生成日志文件，命名规则为：
  - `{logging.dir}/{app_name}/{timestamp}/{app_name}.log`
  - 同一运行的 Shell 审计日志也会放在同一时间戳目录下：`{logging.dir}/{app_name}/{timestamp}/shell_audit.log`
  - 若同一秒内启动多次，时间戳目录名会自动追加后缀 `_1`、`_2` 以避免覆盖

**示例**：

```yaml
# 默认配置（自动日志文件）
logging:
  enabled: true
  level: "INFO"
  file_path: null
  dir: ".logs"

# 调试模式（指定日志文件）
logging:
  enabled: true
  level: "DEBUG"
  file_path: "/tmp/agent_loom_debug.log"

# 静默模式
logging:
  enabled: true
  level: "OFF"
```

> **注意**：由于安全和配置隔离机制，在 Agent YAML 或应用级的 `system.yaml` 中配置 `logging` 都是无效的（会被系统过滤忽略）。所有的日志策略必须在顶层全局的 `config/system.yaml` 中统一配置。

---

## 8. tools — 工具系统配置

控制 Agent 初始化时拥有的工具列表，以及 Shell 命令执行的安全策略。

**YAML 路径**：`tools.*`

### 8.1 default_loaded_tools — 默认工具列表

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `default_loaded_tools` | `list[str]` | `[]` | ❌ 否 | 系统启动时所有 Agent 默认加载的工具名称列表 |

工具名称必须是框架预定义工具（与 `src/tools/__init__.py::_TOOLS_MAP` 完全一致）。完整预定义工具列表：

| 工具名 | 功能 |
|--------|------|
| `search_keyword_in_directory` | 目录内关键词搜索 |
| `search_keyword_with_context` | 搜索关键词 + 返回上下文 |
| `list_files_glob` | Glob 模式搜索文件 |
| `ripgrep_search_directory` | 高性能 ripgrep 搜索 |
| `write_file` | 创建新文件或覆盖已有文件 |
| `read_file` | 读取文件内容（支持 offset/limit 分段读取） |
| `edit_file` | 编辑文件（查找替换） |
| `get_file_outline` | 获取代码大纲（函数/类/结构体） |
| `browse_directory` | 浏览目录结构 |
| `delete_file` | 删除文件 |
| `move_file` | 移动文件 |
| `rename_file` | 重命名文件 |
| `copy_file` | 复制文件 |
| `search_files` | 文件搜索 |
| `code_search` | 代码搜索 |
| `code_replace` | 代码替换 |
| `code_edit` | 代码编辑 |
| `search_and_replace` | 文件内搜索替换 |
| `write_whole_file` | 整文件写入 |
| `git_commit_files` | 提交指定文件到 Git |
| `git_auto_commit` | 自动生成 Git 提交 |
| `git_check_dirty` | 检查 Git 工作区是否脏 |
| `ast_grep_search_file` | AST 模式搜索 |
| `get_git_diff_content` | 获取 Git diff |
| `git_grep_files` | Git grep 搜索 |
| `is_path_in_repo` | 检查路径是否在 Git 仓库内 |
| `load_skill` | 加载指定 Skill |
| `list_skills` | 列出可用 Skills |
| `shell_tool` | 执行 Shell 命令（受白名单限制） |
| `check_background_task` | 检查后台任务状态和最近输出 |
| `kill_background_task` | 终止运行中的后台任务 |
| `list_background_tasks` | 列出所有后台任务 |
| `write_markdown_file` | 写入 Markdown 文件 |
| `write_markdown_file_raw` | 原样写入 Markdown 内容 |
| `append_markdown_sections` | 追加 Markdown 章节 |

**示例**：

```yaml
default_loaded_tools:
  - "load_skill"
  - "list_skills"
  - "shell_tool"
  - "read_file"
  - "grep_search"
  - "glob_search"
  - "edit_file"
  - "write_markdown_file"
```

### 8.2 shell_settings — Shell 工具安全策略

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `shell_settings.allowed_commands` | `str` \| `list[str]` | `"*"` | ❌ 否 | 允许执行的 Shell 命令白名单 |
| `shell_settings.allowed_operators` | `str` \| `list[str]` | `"*"` | ❌ 否 | 允许使用的 Shell 操作符白名单 |

**allowed_commands 配置**：

| 配置值 | 行为 |
|--------|------|
| `"*"` 或 `["*"]` | 关闭命令白名单防御，放行所有命令 |
| `["ls", "pwd", "cat", "grep", "echo"]` | 仅允许白名单内的命令 |

**allowed_operators 配置**：

| 配置值 | 行为 |
|--------|------|
| `"*"` 或 `["*"]` | 允许所有 Shell 操作符 |
| `["\|"]` | 仅允许管道，禁止重定向（防止写入文件） |

**有效的 Shell 操作符**：`|`, `||`, `&&`, `>`, `>>`, `<`, `;`

**安全配置示例**：

```yaml
# 最高权限（开发环境）
shell_settings:
  allowed_commands: "*"
  allowed_operators: "*"

# 只读审计模式（防止修改文件系统）
shell_settings:
  allowed_commands:
    - "ls"
    - "pwd"
    - "cat"
    - "grep"
    - "echo"
    - "find"
    - "wc"
    - "head"
    - "tail"
  allowed_operators:
    - "|"
```

#### security_checks — 命令安全检查

每项检查可单独开关（默认全部启用）：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `security_checks.command_substitution` | `true` | 拦截 `$()` 和反引号命令替换 |
| `security_checks.process_substitution` | `true` | 拦截 `<()` / `>()` 进程替换 |
| `security_checks.env_injection` | `true` | 拦截危险环境变量 (LD_PRELOAD, PATH 等) |
| `security_checks.ifs_injection` | `true` | 拦截 IFS 变量操纵 |
| `security_checks.control_characters` | `true` | 拦截控制字符 |
| `security_checks.incomplete_commands` | `true` | 拦截不完整的命令片段 |
| `security_checks.dangerous_shell_prefix` | `true` | 拦截 bash -c / sudo / env 等危险前缀 |
| `security_checks.zsh_dangerous_commands` | `true` | 拦截 Zsh 危险内建命令 |
| `security_checks.parameter_expansion` | `true` | 拦截 `${}` 参数展开 |
| `security_checks.destructive_patterns` | `true` | 拦截 rm -rf / 、git reset --hard 等破坏性命令 |

#### 危险路径与安全机制

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `shell_settings.dangerous_paths` | `list[str]` | `["/", "/etc", "/usr", ...]` | 禁止 rm/rmdir 操作的危险路径 |
| `shell_settings.block_destructive` | `bool` | `true` | 是否启用危险路径拦截 |

> **路径边界**由 `tool_access_control.include_paths`（§9）统一管理，所有工具（包括 shell_tool）共享同一份配置。
> Shell 特有的 `dangerous_paths` 仅控制 `rm`/`rmdir` 等破坏性操作的拦截，不影响读取权限。
> `dangerous_paths` 的优先级高于 `include_paths`，即使 include 了 `/etc`，`rm -rf /etc` 仍会被阻止。

**安全机制说明**：

- **cd 边界校验**：`cd` 命令的目标路径必须在 workspace 或 `tool_access_control.include_paths` 范围内，
  防止 shell session 通过 `cd` 逃逸到 workspace 外部后利用相对路径绕过安全限制。
- **CWD 同步**：路径校验使用 shell session 的实际工作目录（而非 Python 进程 CWD），
  确保持久化 shell session 中的 `cd` 命令被正确追踪。
- **复合命令追踪**：`cd src && cd ../tests && ls` 这类复合命令会逐段追踪 CWD 变化，
  确保每个 `cd` 目标都在允许范围内。
- **符号链接解析**：路径校验通过 `realpath` 解析符号链接到最终目标，防止通过符号链接绕过 workspace 边界。

#### 安全策略透明化

框架自动将安全策略配置暴露给 LLM，避免 AI 在不知道限制的情况下反复尝试被拦截的操作。

**工作原理**：
- **shell_tool 描述动态注入**：Agent 初始化时，`shell_tool` 的工具描述会被自动替换为基于当前配置生成的安全策略摘要，包括允许目录列表、活跃安全检查列表及拒绝行为规则。
- **环境提示词安全段落**：Agent 的 environment prompt 自动包含安全行为指导，教导 AI 在工具调用被拦截后应如何响应（不要重试、使用替代工具等）。
- **丰富的拒绝消息**：安全拦截的错误消息不仅包含拒绝原因，还包含建议的替代操作（如"使用 edit_file 替代 heredoc"），帮助 AI 快速选择正确的替代方案。
- **单一数据源**：策略摘要从与执行强制相同的配置源读取（`get_allowed_directories()` 和 `security_checks`），确保 prompt 与执行始终一致。

**效果**：
- LLM 第一次就知道哪些操作受限，减少无效尝试
- 路径违规错误包含 `Use paths within allowed directories, or use read_file/grep_search tools instead.` 指导
- 命令安全拦截错误包含 `Suggested alternative:` 建议（如 `Use write_markdown_file or edit_file tool for multi-line content`）

> 此功能无需额外配置，基于已有的 `security_checks` 和 `tool_access_control` 配置自动生效。

#### sandbox — 沙箱模式

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `shell_settings.sandbox.enabled` | `bool` | `false` | 是否启用 OS 级别沙箱隔离 |
| `shell_settings.sandbox.mode` | `str` | `"bwrap"` | 沙箱后端：`bwrap`（bubblewrap）/ `docker` / `none` |
| `shell_settings.sandbox.allow_write` | `list[str]` | `[".", "/tmp"]` | 沙箱内可写路径 |
| `shell_settings.sandbox.deny_write` | `list[str]` | `["/etc", "/usr"]` | 沙箱内禁止写入的路径 |
| `shell_settings.sandbox.network_isolation` | `bool` | `false` | 是否隔离网络访问 |
| `shell_settings.sandbox.excluded_commands` | `list[str]` | `[]` | 不走沙箱的命令模式 |

**完整配置示例**：

```yaml
shell_settings:
  allowed_commands: "*"
  allowed_operators: "*"
  security_checks:
    command_substitution: true
    env_injection: true
    control_characters: true
    dangerous_shell_prefix: true
    destructive_patterns: true
  dangerous_paths: ["/", "/etc", "/usr", "/var", "/boot", "/sys", "/proc"]
  block_destructive: true
  sandbox:
    enabled: false
    mode: "bwrap"
    allow_write: [".", "/tmp"]
    deny_write: ["/etc", "/usr"]
    network_isolation: false
    excluded_commands: []
```

### 8.3 tools_mapping — 工具别名映射与 legacy 兼容

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `tools_mapping` | `dict[str, dict[str, str]]` | `{}` | ❌ 否 | 按平台分组的工具名别名映射 |

用于将 Skill 文件中声明的简短工具别名（如 `Read`, `Write`, `Bash`）映射到框架真实的工具名称。当 Skill 的 `platform` 为 `"Claude"` 时，会查找 `tools_mapping.Claude` 下的映射。

> **Legacy 兼容**：如果 `tools_mapping.Claude` 为空或未设置，框架会回退读取 `tools.mapping` 作为 `Claude` 分组的 legacy 兼容映射；如果 `tools_mapping.Claude` 中存在任何配置，则 `tools.mapping` 会被完全忽略。

**示例**：

```yaml
tools_mapping:
  Claude:
    Read: "read_file"
    Write: "write_markdown_file"
    Bash: "shell_tool"
    Glob: "glob_search"
    Grep: "grep_search"
    Edit: "edit_file"
```

---

## 9. tool_access_control — 工具访问控制

定义工具的文件路径访问规则。默认情况下工具只能访问 workspace 内文件。所有路径访问控制统一通过 `path_validation` 规则配置。

**YAML 路径**：`tool_access_control.*`
**Pydantic 模型**：`ToolAccessControlSettings`

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `tool_access_control.path_validation` | `list[dict]` | `[]` | ❌ 否 | 工具访问控制规则列表。只有列出的工具才做路径校验，空列表=全部放行 |

> **Note**: workspace root 始终等于项目根目录（`agent_root`，即包含 `pyproject.toml` 的目录），自动检测，不可通过配置覆盖。

### 9.1 path_validation 规则字段

每条 `path_validation` 规则定义一组工具的路径访问规则：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `tools` | `list[str]` | — | 需要做路径校验的工具名列表。`"*"` 匹配所有工具 |
| `exclude_paths` | `list[str]` | `[]` | 禁止访问的目录。支持 `~` 展开、glob 模式（`fnmatch`）、`"*"`（拒绝所有路径） |
| `include_paths` | `list[str]` | `[]` | 额外允许访问的 workspace 外目录。支持 `~` 展开、glob 模式（`fnmatch`）、`"*"`（允许所有路径） |
| `path_param_patterns` | `list[str]` | `[]`（代码 fallback 到内置默认列表） | 工具参数名匹配模式，匹配到的参数被识别为文件路径 |

### 9.2 通配符与 glob 匹配

`include_paths` 和 `exclude_paths` 支持以下模式：

| 模式 | 说明 | 示例 |
|------|------|------|
| `"*"` | **通配符**：匹配所有路径。`include_paths: ["*"]` = 允许全部；`exclude_paths: ["*"]` = 拒绝全部 | `include_paths: ["*"]` |
| `~` | **Tilde 展开**：展开为用户 home 目录 | `include_paths: ["~/libs"]` → `/home/user/libs` |
| Glob 模式 | **fnmatch 匹配**：支持 `*`、`?`、`[seq]` 等通配符 | `include_paths: ["/home/*/code"]` 匹配 `/home/lin/code` |
| 精确路径 | **前缀匹配**：绝对路径或相对路径做前缀检查 | `exclude_paths: ["secrets", "/opt/data"]` |

### 9.3 冲突解决规则

- **exclude 优先于 include**（安全第一）：路径同时匹配 `include_paths` 和 `exclude_paths` 时，**拒绝访问**
- 工具不在任何规则中 → 不做路径校验，直接放行
- 一个工具出现在多条规则中 → `include_paths` / `exclude_paths` 取所有匹配规则的**并集**
- `tools: ["*"]` 匹配所有工具（类似全局规则）

### 9.4 验证逻辑（两层架构）

**Layer 1 — Hook 层（所有工具自动生效）**：
1. 工具不在任何规则中 → 直接放行
2. 工具在某规则中 → 提取路径参数
3. 对每个路径检查：
   - UNC / Windows 特殊路径 → 阻止
   - 波浪号 `~` 展开
   - 符号链接链追踪（检查每个中间路径）
   - 路径是否匹配 `exclude_paths`（含 glob） → 是则阻止（**exclude 优先**）
   - 路径是否在 workspace 或匹配 `include_paths`（含 glob） → 否则阻止

**Layer 2 — 搜索结果过滤（仅搜索工具）**：

`grep_search` 和 `glob_search` 额外遵守 `exclude_paths` 的搜索结果过滤。当搜索根目录本身是合法的（如 `path="src/"`），但其子目录在 `exclude_paths` 中时，搜索结果中该子目录的匹配会被自动隐藏。文件操作工具（`read_file`、`edit_file` 等）只需要 Layer 1 即可。

**示例**：
```yaml
tool_access_control:
  path_validation:
    # shell 和文件工具允许访问外部目录，排除敏感目录
    - tools: ["shell_tool", "read_file", "edit_file", "grep_search"]
      include_paths: ["~/shared-libs", "/home/*/code"]
      exclude_paths: ["secrets", ".env"]

    # 移动/复制工具排除构建目录
    - tools: ["move_file", "copy_file"]
      exclude_paths: ["build", "dist"]
      path_param_patterns: ["source", "destination"]

    # 不限制的工具（允许所有路径）
    - tools: ["some_unrestricted_tool"]
      include_paths: ["*"]

    # 完全锁定的工具（拒绝所有路径）
    - tools: ["some_locked_tool"]
      exclude_paths: ["*"]
```

---

## 10. tool_metadata — 工具元数据配置

为每个工具声明运行时元数据（截断阈值、并发安全性、分类等）。Agent YAML 中可按需覆盖。

**YAML 路径**：`tool_metadata.*`

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `tool_metadata.<tool_name>.max_result_chars` | `int \| null` | `20000` | 工具输出超过此字符数时，框架将完整结果持久化到磁盘，并向 LLM 发送预览 + 文件路径。设为 `null` 表示不截断 |
| `tool_metadata.<tool_name>.is_concurrency_safe` | `bool` | `true` | 工具是否可并行调用（用于 Agent 并发调度） |
| `tool_metadata.<tool_name>.category` | `str` | `"general"` | 工具分类（search / file_ops / shell / git / general） |
| `tool_metadata.<tool_name>.disable_type_coercion` | `bool` | `false` | 禁用该工具的 LLM 参数类型自动强转 |
| `tool_metadata.default.*` | — | — | 未单独配置的工具使用此段作为兑底默认值 |

**示例**：
```yaml
tool_metadata:
  grep_search:
    max_result_chars: 20000
    is_concurrency_safe: true
    category: search
  shell_tool:
    max_result_chars: 5000
    is_concurrency_safe: false
    category: shell
  read_file:
    max_result_chars: null
    category: file_ops
  default:
    max_result_chars: 20000
    is_concurrency_safe: true
    category: general
```

**Agent YAML 覆盖**：
```yaml
# 在 Agent YAML 中按需覆盖单个工具的元数据
tools:
  - name: grep_search
    max_result_chars: 10000  # 此 Agent 使用更小的截断阈值
```

---

## 11. tool_output_limits — 工具输出限制

覆盖上下文压缩层（Layer 2）中的硬编码工具输出字符数限制。与 `tool_metadata.max_result_chars`（即时截断）独立，本段控制的是压缩阶段的字符数上限。

**YAML 路径**：`tool_output_limits.*`

**示例**：
```yaml
tool_output_limits:
  grep_search: 3000
  shell_tool: 2000
  read_file: null   # 跳过截断，使用 dedup 层处理
  default: 3000
```

> 当前此段为预留配置，默认注释状态。启用后将覆盖 `context_compression.py` 中的 `TOOL_MAX_RETAIN_CHARS` 字典。

---

## 10. checkpoint — 断点续跑与心跳配置

控制 Agent 任务的**断点续跑**、**心跳监控**与**崩溃检测**能力。框架默认全局开启，任何基于 AgentLoom 创建的应用均自动享有该能力，无需额外配置。

**YAML 路径**：`checkpoint.*`

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `checkpoint.enabled` | `bool` | `true` | ❌ 否 | 全局开关。设为 `false` 时禁用所有 checkpoint/heartbeat 功能 |
| `checkpoint.cleanup_on_success` | `bool` | `true` | ❌ 否 | 任务成功完成后自动删除 checkpoint 目录。生产环境建议 `true`；调试时可设 `false` 保留现场 |
| `checkpoint.max_resume_age` | `int`（秒） | `604800` | ❌ 否 | checkpoint 最大保留时长（7 天）。超过该时长的 checkpoint 被视为过期，不可恢复 |
| `checkpoint.heartbeat_interval` | `int`（秒） | `5` | ❌ 否 | 心跳文件写入频率。框架守护线程每隔该间隔将进程状态（PID、步骤数、时间戳）写入磁盘 |

### 10.1 运行时目录结构

checkpoint 数据写入每次运行的时间戳日志目录下，与运行日志共存：

```
.logs/{supervisor_name}/{timestamp}/
├── {supervisor_name}.log     # 运行日志
└── checkpoints/{task_id}/
    ├── task_tree.json        # 任务元数据（状态、worker 调用记录、创建时间）
    ├── checkpoint.json       # Supervisor Agent 的 memory steps 快照
    ├── heartbeat.json        # Supervisor 心跳（PID、时间戳、步骤数、状态）
    └── workers/{worker_name}/
        ├── checkpoint.json   # Worker Agent 每次调用的 memory 快照
        └── heartbeat.json    # Worker 心跳（聚合所有并发调用的状态）
```

此外，在 agent 根目录下维护一个轻量索引文件 `.task_index.json`，记录 `task_id → timestamp` 的映射关系，便于 `--resume` 时快速定位 checkpoint 所在的时间戳目录。

### 10.2 心跳机制与崩溃检测

框架维护两级心跳：

| 级别 | 文件位置 | Payload 字段 |
|------|----------|--------------|
| **Supervisor 心跳** | `{task_id}/heartbeat.json` | `pid`, `timestamp`, `timestamp_iso`, `status`, `step`, `agent_name` |
| **Worker 心跳** | `{task_id}/workers/{name}/heartbeat.json` | `agent_name`, `pid`, `timestamp`, `calls`（各并发调用的 `status`、`step`、`started_at`、`finished_at`） |

**崩溃检测逻辑**（`HEARTBEAT_STALE_THRESHOLD = 30` 秒）：

1. 心跳文件不存在 → `crashed`
2. 心跳 `status` 为 `stopped` 或 `exited` → `crashed`
3. PID 已不存在（`os.kill(pid, 0)` 失败）→ `crashed`
4. 心跳时间戳超过 30 秒未刷新 → `crashed`
5. 以上均不满足 → `running`

### 10.3 断点续跑流程

当检测到某 `task_id` 的 checkpoint 未超过 `max_resume_age`，且状态为非正常结束（`crashed`/`interrupted`）时，框架自动：

1. 从 `checkpoint.json` 恢复 Supervisor 的 memory steps（跳过已完成的思考步骤）
2. 对每个 Worker 调用，检查 `task_tree.json` 中的 `input_hash` 缓存——若输入一致且已完成，直接返回缓存结果（跳过重复执行）
3. 从中断点继续运行，直到任务完成

### 10.4 配置示例

```yaml
# 调试场景：保留 checkpoint、缩短 resume 窗口
checkpoint:
  enabled: true
  cleanup_on_success: false   # 保留产物，便于复盘
  max_resume_age: 86400       # 仅保留 1 天
  heartbeat_interval: 5

# 生产场景（使用框架默认值，可完全省略此段）
checkpoint:
  enabled: true
  cleanup_on_success: true
  max_resume_age: 604800
  heartbeat_interval: 5
```

---

## 附录 A：Pydantic 模型对照表

框架使用 Pydantic 对系统配置进行校验。以下是配置字段与 Pydantic 模型的映射关系：

| 配置段 | Pydantic 模型 | 源文件 |
|--------|--------------|--------|
| 根配置 | `RootSettings` | `src/lib/config/config_validation.py` |
| `system.*` | `SystemSettings` | `src/lib/config/config_validation.py` |
| `tool_access_control.*` | `ToolAccessControlSettings` | `src/lib/config/config_validation.py` |

**`RootSettings` 完整字段定义**：

| 字段 | 类型 | 默认值 |
|------|------|--------|
| `system` | `SystemSettings` | `SystemSettings()` |
| `tool_access_control` | `ToolAccessControlSettings` | `ToolAccessControlSettings()` |
| `smart_summary` | `bool` | `True` |
| `model` | `dict[str, Any]` | `{}` |
| `execution_env` | `dict[str, Any]` | `{}` |
| `code_agent` | `dict[str, Any]` | `{}` |
| `tools` | `dict[str, Any]` | `{}` |
| `tool_metadata` | `dict[str, Any]` | `{}` |
| `tool_output_limits` | `dict[str, Any]` | `{}` |

> 所有模型均设置 `extra="allow"`，允许扩展字段。`prompt` 就是这类顶层 extra key 之一，会通过 overlay 合并透传到最终配置，但不作为 `RootSettings` 的显式字段。

**容错解析工具集**：

| 解析器 | 用途 | 位于 |
|--------|------|------|
| `BoolParser` | 兼容布尔输入归一化，实际用于 `logging.enabled`、Skill 的 `invocation-control` 解析、以及部分 LLM 配置开关 | `config_validation.py` / `src/lib/logging/logger_manager.py` / `src/lib/smolagents/skills/parser.py` / `src/lib/config/llm_config.py` |
| `IntParser` | 兼容整数与旁路字符串输入，实际用于模型配置里的 `max_tokens`（支持 `"max"`） | `config_validation.py` / `src/lib/config/llm_config.py` |
| `FloatParser` | 兼容浮点与整数字符串输入，实际用于模型配置里的 `temperature`、`retry_delay`、`max_retry_delay` | `config_validation.py` / `src/lib/config/llm_config.py` |
| `EnumParser` | 通用枚举归一化辅助函数，当前未在 system.yaml 主链路中直接消费 | `config_validation.py` |
| `LogLevelParser` | 解析 `logging.level`，支持标准 `logging` 级别与 `OFF` | `config_validation.py` / `src/lib/logging/logger_manager.py` |

---

## 附录 B：应用级覆盖与目录结构

### Application 标准目录结构

每个 Application **必须**包含 `workflows/` 目录，框架以此作为应用的标志。`config/` 和 `sysprompt/` 目录是可选的：

```
<app>/
├── workflows/          ← 必须（应用的标志，框架据此定位 app_root）
│   ├── xxx_agent.yaml
│   └── worker_agents/
├── config/             ← 可选（有则叠加覆盖 config/system.yaml）
│   └── system.yaml
└── sysprompt/          ← 可选（自定义 prompt 模板，参考 .example.yaml）
    └── my_prompt.yaml
```

### 应用级覆盖发现机制

框架从 Agent YAML 文件路径开始**向上查找** `workflows/` 目录，其父目录即为 `app_root`。然后检查 `app_root/config/system.yaml` 是否存在：

- **存在** → 自动叠加为应用级覆盖
- **不存在** → 跳过，直接使用全局配置

> 无论使用 `loom run` 还是 `python xxx_demo.py`，发现机制都一致可靠。

### 嵌套应用

嵌套应用（如 `my_app/sub_module`）会命中**最近的** `workflows/` 目录，天然隔离：

```
applications/my_app/
├── config/system.yaml              ← my_app 自身的覆盖
├── workflows/my_app_agent.yaml     ← 命中 my_app
└── sub_module/
    ├── config/system.yaml          ← sub_module 独立覆盖
    └── workflows/agent.yaml        ← 命中 sub_module（不会跳到 my_app）
```

### 覆盖示例

#### 配置键覆盖级别参考表

下表展示各配置键在不同级别的支持情况：

| 配置键 | 全局 system.yaml | 应用级 system.yaml | Agent YAML |
|--------|----------------|-----------------|-----------|
| `system` | ✅ 支持 | ✅ 支持 | ✅ 支持 |
| `smart_summary` | ✅ 支持 | ✅ 支持 | ✅ 支持 |
| `skills` | ✅ 支持 | ✅ 支持 | ✅ 支持 (Agent私有) |
| `logging` | ✅ 支持 | ❌ 忽略 | ❌ 忽略 |
| `tool_access_control` | ✅ 支持 | ✅ 支持 | ✅ 支持 |
| `execution_env` | ✅ 支持 | ✅ 支持 | ✅ 支持 |
| `code_agent` | ✅ 支持 | ✅ 支持 | ✅ 支持 |
| `tools` | ✅ 支持 | ✅ 支持 | ✅ 支持 (覆盖字典) |
| `prompt` | ✅ 支持 | ✅ 支持 | ✅ 支持 |

```yaml
# applications/my_app/config/system.yaml
# 只需写需要覆盖的字段，其余自动继承全局配置

tool_access_control:
  exclude_paths:
    - ".git"
  tool_access_control:
    - tools: ["read_file", "edit_file"]
      exclude_paths: ["build"]
```

**合并效果**：

- `tool_access_control.exclude_paths`：`[]` → `[".git"]` ✅ 被整体替换
- `tool_access_control.path_validation`：`[]` → 应用级规则 ✅ 被整体替换
- `system.name`：保持 `"AgentLoom"` ✅ 继承全局
- `logging.level`：保持 `"INFO"` ✅ 继承全局
- 所有其他字段：保持全局配置值 ✅

---

## 12. checkpoint — 断点续跑与心跳配置

控制任务的断点续跑和心跳监控行为。详细说明请参考 [checkpoint.md](checkpoint.md)。

**YAML 路径**：`checkpoint.*`

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `checkpoint.enabled` | `bool` | `true` | ❌ 否 | 全局开关：是否启用断点续跑 |
| `checkpoint.cleanup_on_success` | `bool` | `true` | ❌ 否 | 任务成功完成后自动删除 checkpoint 目录 |
| `checkpoint.max_resume_age` | `int` | `604800` | ❌ 否 | checkpoint 最大保留时长（秒），默认 7 天 |
| `checkpoint.heartbeat_interval` | `float` | `5.0` | ❌ 否 | 心跳写入频率（秒），用于崩溃检测 |

**示例**：

```yaml
checkpoint:
  enabled: true
  cleanup_on_success: true
  max_resume_age: 604800
  heartbeat_interval: 5
```
