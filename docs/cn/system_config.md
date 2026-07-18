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
- [1.5 model_request_headers — 模型请求头隐私配置](#15-model_request_headers--模型请求头隐私配置)
- [2. smart_summary — 上下文压缩策略](#2-smart_summary--上下文压缩策略)
- [3. prompt — 顶层 System Prompt 覆盖](#3-prompt--顶层-system-prompt-覆盖)
- [4. skills — 全局 Skills 配置](#4-skills--全局-skills-配置)
- [5. lsp_servers — LSP 语言服务器配置](#5-lsp_servers--lsp-语言服务器配置)
- [5.5 mcp_servers — MCP 外部工具集成](#55-mcp_servers--mcp-外部工具集成)
- [6. execution_env — 执行环境配置](#6-execution_env--执行环境配置)
- [6. code_agent — CodeAgent 代码执行权限](#6-code_agent--codeagent-代码执行权限)
- [7. runtime 与 logging — 运行时存储与日志](#7-runtime-与-logging--运行时存储与日志)
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
# 模型请求头隐私配置
# ============================================
model_request_headers:
  profile: "opencode"  # agentloom | none | kimicode | openclaw | opencode
  headers: {}

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
# 运行时存储与保留策略
# ============================================
runtime:
  root_dir: ".agentloom"
  successful_run_retention_days: 7
  failed_run_retention_days: 30
  artifact_retention_days: 3
  cleanup_interval_hours: 24

# ============================================
# 日志配置
# ============================================
logging:
  level: "INFO"
  console_enabled: true
  file_enabled: true
  max_file_bytes: 26214400
  backup_count: 3

# ============================================
# 默认加载 Toolsets
# ============================================
default_toolsets:
  - "core_shell"
  - "core_file"
  - "core_search"
  - "context"
  - "skills"

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
    Write: "write_file"
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

控制系统的基本身份标识，用于日志；当 `model_request_headers.profile: "agentloom"` 时，也用于模型请求的 `User-Agent`。

**YAML 路径**：`system.*`
**Pydantic 模型**：`SystemSettings`

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `system.name` | `str` | `"AgentLoom"` | ❌ 否 | 系统名称，用于日志标识和 User-Agent 构建 |
| `system.version` | `str` | `"1.0.1"` | ❌ 否 | 系统版本号（信息性字段） |
| `system.user_agent` | `str` | `"AgentLoom/1.0.1"` | ❌ 否 | `agentloom` 请求头 profile 使用的 AgentLoom 身份字符串 |

**示例**：

```yaml
system:
  name: "my-project-agents"
  version: "2.0.0"
  user_agent: "my-project-agents/2.0.0"
```

---

## 1.5 model_request_headers — 模型请求头隐私配置

配置所有模型 API 出站请求的默认 HTTP headers，用于避免 AgentLoom 默认身份直接暴露给模型供应商。全局隐私默认值放这里；某个模型或供应商需要单独覆盖时，仍放在 `config/llm.yaml` 的 `model.<type>.extra_headers`。

当前推荐配置为 `opencode`，该 profile 已用当前 `llm.yaml` 的 Ark OpenAI-compatible endpoint 做过真实工具对比：

```yaml
model_request_headers:
  profile: "opencode"
  headers: {}
```

内置 profile：

| profile | 状态 | 说明 |
|------|------|------|
| `opencode` | 已用真实 OpenCode 验证 | 适用于当前 OpenAI-compatible `llm.yaml`；发送 OpenCode 当前版本的 `User-Agent` 和 session headers |
| `cline` | 已用真实 Cline CLI 验证 | 适用于当前 OpenAI-compatible `llm.yaml`；发送 Cline CLI 当前 OpenAI-compatible runtime 的 `User-Agent` |
| `kimicode` | 已用真实 Kimi Code 验证 | 适用于当前 OpenAI-compatible `llm.yaml`；发送 Kimi Code 当前版本的 `User-Agent` 和 JS SDK headers |
| `openclaw` | 已用真实 OpenClaw 验证 | 适用于当前 OpenAI-compatible `llm.yaml`；发送 OpenClaw 当前 direct runtime 的 OpenAI JS SDK headers |
| `roo` | 已用 Roo Code OpenAI provider 源码验证 | 适用于当前 OpenAI-compatible `llm.yaml`；发送 Roo Code 当前默认 `HTTP-Referer`、`X-Title` 和 `User-Agent` |
| `agentloom` | 显式使用 AgentLoom 身份 | 使用 `system.user_agent` |
| `none` | 不加系统默认身份 header | 只保留模型 SDK 自带 headers 和显式配置的 headers |

Claude Code 当前走 Anthropic-compatible plan/coding 协议，不能用仓库当前
OpenAI-compatible `/api/v3` + `ep-...` 配置证明协议级一致，因此不作为内置 profile。
如确实要实验 Claude Code 风格 header，可在 `model_request_headers.profiles` 里显式
自定义。

Roo Code 当前公开 npm 中没有可直接运行的官方 Roo CLI，仓库内 CLI 也不能用命令行
配置 OpenAI-compatible base URL；因此 `roo` 的验收边界是 Roo Code `3.53.0`
源码中的 `OpenAiHandler` provider 真实请求验证，不是完整 VS Code 扩展宿主验证。

自定义 profile 示例：

```yaml
model_request_headers:
  profile: "codex"
  profiles:
    codex:
      headers:
        User-Agent: "configured-agent/1.0"
        X-Client-Profile: "codex"
```

合并顺序：

1. 内置 profile，或 `model_request_headers.profiles.<name>`
2. `model_request_headers.headers`
3. `config/llm.yaml` 的 `model.<type>.extra_headers`

后面的层级按 header 名大小写不敏感覆盖前面的层级。注意：这个功能只控制 AgentLoom 管理的 HTTP headers，不能保证 TLS 指纹、请求体 schema、header 顺序等与真实客户端完全相同；要证明某个真实客户端版本完全一致，需要用同一捕获端点抓真实客户端请求后做差异比对。当前验证边界见 [模型请求头伪装验证记录](model_request_header_tool_parity.md)。

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `model_request_headers.profile` | `str` | `"agentloom"` | ❌ 否 | 选择内置或自定义请求头 profile；仓库默认示例使用 `opencode` |
| `model_request_headers.profiles` | `dict` | `{}` | ❌ 否 | 本地自定义或覆盖的命名请求头 profile |
| `model_request_headers.headers` | `dict` | `{}` | ❌ 否 | 应用于每个模型请求的系统级额外 header |

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

定义所有 Agent 默认继承的全局 Skill 包。Skills 是可复用的 Claude 风格 `SKILL.md` 包。加载策略由 `load-mode` 控制：`on-demand`（只放 catalogue）或 `eager`（完整正文注入 system prompt）。

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
| `load-mode` | `str` | `on-demand` | ❌ 否 | `on-demand` catalogue 加载，或 `eager` 完整正文注入 |
| `allow-scripts` | `bool` | `true` | ❌ 否 | 设为 `false` 时阻断 `run_skill_script` |
| `allow-network` | `bool` | `true` | ❌ 否 | 设为 `false` 时阻断 `run_skill_script` 中常见网络命令 |

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
    load-mode: "eager"

  - path: "skills/agent-visualization"
    allow-scripts: false

  # 简写格式（默认 load-mode=on-demand，允许脚本和网络）
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
| `timeout_seconds` | `int \| null` | `30` | 单个生成 Python 代码块的最长墙钟执行秒数。同步调用 Worker Agent 或其他长耗时工具时可调大 |

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
    timeout_seconds: 120
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

## 7. runtime 与 logging — 运行时存储与日志

`runtime` 定义框架唯一的存储根目录和有界保留策略；`logging` 控制控制台与 run-scoped 文件 backend。Logger 状态绑定当前 run，不再复用进程全局输出路径。

这两个配置段只能写在全局 `config/system.yaml`。Application 级 `config/system.yaml` 或 Agent YAML 一旦包含任一配置段就会校验失败，避免错误位置的配置被静默忽略，让用户误以为任务发现已经移动到另一个 runtime root。

隔离子进程或验证任务可设置 `AGENTLOOM_RUNTIME_ROOT`；它覆盖的是整个 canonical runtime home（runs、checkpoints、sessions、learning 与 `self_learning.db` 一起移动），不存在 self-learning 专用 root 覆盖。

**YAML 路径**：`runtime.*`、`logging.*`

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `runtime.root_dir` | `str` | `".agentloom"` | ❌ 否 | 框架运行时唯一根目录；相对路径从项目根解析，也支持绝对路径 |
| `runtime.successful_run_retention_days` | `int` | `7` | ❌ 否 | 成功 run 目录保留天数 |
| `runtime.failed_run_retention_days` | `int` | `30` | ❌ 否 | 失败/中断 run 目录保留天数 |
| `runtime.artifact_retention_days` | `int` | `3` | ❌ 否 | 已保留 run 内原始 `artifacts/` 的保留天数 |
| `runtime.cleanup_interval_hours` | `int` | `24` | ❌ 否 | 两次自动清理之间的最小间隔 |
| `logging.level` | `str` \| `int` | `"INFO"` | ❌ 否 | 日志过滤等级（不区分大小写） |
| `logging.console_enabled` | `bool` | `true` | ❌ 否 | 是否向控制台输出格式化运行日志 |
| `logging.file_enabled` | `bool` | `true` | ❌ 否 | 是否把当前 attempt 写入 `logs/runtime.log` |
| `logging.max_file_bytes` | `int` | `26214400` | ❌ 否 | 每个 runtime log 分段的最大字节数（25 MiB） |
| `logging.backup_count` | `int` | `3` | ❌ 否 | runtime log 轮转备份数 |

### 7.1 Canonical 路径与生命周期

每个 attempt 写入 `.agentloom/runs/<application_id>/<run_id>/`：

```text
manifest.json
logs/runtime.log[.1-.3]
audit/shell.jsonl[.1-.2]
audit/task_tree.json
audit/task_events.jsonl
artifacts/result.txt
artifacts/{shell,background,skills}/
```

任务树、任务事件与结果文件仅在对应证据存在时写入；成功清理 checkpoint 前，它们的路径会先记录到 `manifest.json`，因此 Run 详情不依赖仍然存活的 checkpoint。Shell audit 独立按每段 10 MiB、2 个备份轮转。Resume 为同一任务创建新的 `run_id` 和 run 目录，同时继续使用原 `task_id` 与 `.agentloom/checkpoints/<application_id>/<task_id>/`。可以只关闭文件日志，而不关闭 checkpoint 或 Shell audit：

```bash
loom run applications/<app>/workflows/<agent>.yaml --no-file-log
```

框架不再保留 `--log-to-file`、`logging.enabled`、`logging.dir` 或 `logging.file_path` 兼容路径。

### 7.2 保留策略与存储边界

自动清理最多按配置间隔执行一次；`loom clean-runtime` 可显式应用同一策略。它只删除符合条件的 run 目录或其中的 raw artifacts，永不遍历 checkpoints、`.agentloom/legacy/`、`.agentloom/workspaces/` 或 Application 自有 output 目录。

Agent 的 recall/todo 文件统一位于 `.agentloom/workspaces/agents/<application_id>/<agent_path>/`。`loom migrate-runtime --dry-run` 会预览旧 checkpoint 候选和未分域的 `.runtime`；`loom migrate-runtime --apply` 会迁移 checkpoints、把 `.logs` 归档到 `.agentloom/legacy/`，并将缺少 Application/task 来源信息的 `.runtime` 原子归档到 `.agentloom/workspaces/legacy-unscoped/`。

验证真实 attempt 时必须读取 `manifest.json` 及其引用的日志、审计与产物，不能只看退出码。

---

## 8. tools — 工具系统配置

控制 Agent 初始化时拥有的工具列表，以及 Shell 命令执行的安全策略。

**YAML 路径**：`tools.*`

### 8.1 default_toolsets — 默认 Toolsets

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `default_toolsets` | `list[str]` | `[]` | ❌ 否 | 系统启动时所有 Agent 默认加载的 toolset 名称列表 |

Agent YAML 中的 `toolsets:` 会整体替换全局默认；`toolsets: []` 表示不加载任何内置工具。完整 registry toolsets：

| Toolset | 工具 |
|--------|------|
| `core_shell` | `shell_tool`, `check_background_task`, `kill_background_task`, `list_background_tasks` |
| `core_file` | `read_file`, `edit_file`, `write_file`, `list_directory` |
| `core_search` | `grep_search`, `glob_search` |
| `context` | `loom_retrieve_context` |
| `skills` | `load_skill`, `list_skills` |
| `markdown_report` | `write_markdown_file`, `write_markdown_file_raw`, `append_markdown_sections` |
| `code_nav` | `get_file_outline`, `ast_grep_search_file`, `lsp_find_definition`, `lsp_find_references`, `lsp_get_document_symbols`, `lsp_hover`, `lsp_get_workspace_symbols` |

完整预定义工具列表：

| 工具名 | 功能 |
|--------|------|
| `write_file` | 创建新文件或覆盖已有文件 |
| `read_file` | 读取文件内容（支持 offset/limit 分段读取） |
| `edit_file` | 应用一个或多个唯一文本编辑 |
| `get_file_outline` | 获取代码大纲（函数/类/结构体） |
| `list_directory` | 列出目录结构 |
| `grep_search` | 正则搜索文件内容 |
| `glob_search` | 按 glob 查找文件 |
| `ast_grep_search_file` | AST 模式搜索 |
| `lsp_find_definition` | 查找符号定义 |
| `lsp_find_references` | 查找符号引用 |
| `lsp_get_document_symbols` | 列出文档符号 |
| `lsp_hover` | 查看 hover/type 信息 |
| `lsp_get_workspace_symbols` | 搜索工作区符号 |
| `loom_retrieve_context` | 读取压缩上下文引用 |
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
default_toolsets:
  - "core_shell"
  - "core_file"
  - "core_search"
  - "context"
  - "skills"
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
- 命令安全拦截错误包含 `Suggested alternative:` 建议（如 `Use write_file or edit_file tool for multi-line content`）

> 此功能无需额外配置，基于已有的 `security_checks` 和 `tool_access_control` 配置自动生效。

#### audit_log — Shell 审计日志

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `shell_settings.audit_log.enabled` | `bool` | `true` | 是否启用每个 agent 独立的 Shell 审计日志 |
| `shell_settings.audit_log.log_policy_snapshot` | `bool` | `true` | 每次运行写入一条 `POLICY_SNAPSHOT`，记录有效 Shell 策略，包括 `allowed_commands: "*"` 这类全放行默认值 |
| `shell_settings.audit_log.log_success` | `bool` | `false` | 是否把成功命令也记录为 `COMMAND_SUCCESS` |

`POLICY_SNAPSHOT` 会在第一次 Shell 命令执行前写入，因此即使是完全放行、没有任何拦截的运行，也能审计到命令/操作符白名单检查是被显式关闭的。

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
  audit_log:
    enabled: true
    log_policy_snapshot: true
    log_success: false
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
    Write: "write_file"
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
    - tools: ["", ""]
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
    # shell_tool 自身会对大输出做截断并写入完整输出路径。
    # 外层 shim 阈值需高于该预览，避免隐藏完整输出提示。
    max_result_chars: 40000
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

## 12. checkpoint — 断点续跑与心跳配置

控制 Agent 任务的**断点续跑**、**心跳监控**与**崩溃检测**能力。框架默认全局开启，任何基于 AgentLoom 创建的应用均自动享有该能力，无需额外配置。

**YAML 路径**：`checkpoint.*`

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `checkpoint.enabled` | `bool` | `true` | ❌ 否 | 全局开关。设为 `false` 时禁用所有 checkpoint/heartbeat 功能 |
| `checkpoint.cleanup_on_success` | `bool` | `true` | ❌ 否 | 任务成功完成后自动删除 checkpoint 目录。生产环境建议 `true`；调试时可设 `false` 保留现场 |
| `checkpoint.max_resume_age` | `int`（秒） | `604800` | ❌ 否 | checkpoint 最大保留时长（7 天）。超过该时长的 checkpoint 被视为过期，不可恢复 |
| `checkpoint.heartbeat_interval` | `int`（秒） | `5` | ❌ 否 | 心跳文件写入频率。框架守护线程每隔该间隔将进程状态（PID、步骤数、时间戳）写入磁盘 |

### 12.1 运行时目录结构

Run 证据与 task 恢复状态在同一个 runtime root 下保持独立生命周期：

```text
.agentloom/
├── runs/<application_id>/<run_id>/
│   ├── manifest.json
│   ├── logs/runtime.log[.1-.3]
│   ├── audit/shell.jsonl[.1-.2]
│   └── artifacts/{shell,background,skills}/
└── checkpoints/<application_id>/<task_id>/
    ├── task_events.jsonl
    ├── task_tree.json
    ├── checkpoint.json
    ├── heartbeat.json
    ├── workers/<worker_name>/calls/<call_index>/checkpoint.json
    ├── context_store/
    └── file-history/
```

关键设计：

- 每个 attempt 都更换 `run_id`，resume 时 `task_id` 保持不变
- Run manifest 记录 `task_id`；checkpoint run event 和 heartbeat 记录当前 `run_id`
- Checkpoint 直接按 Application/task canonical 路径定位，不依赖日志、`.task_index.json` 或 legacy 扫描
- 日志关闭、轮转和 runtime retention 不会删除 checkpoint
- Agent workspace 与 run artifacts 保持独立；Application `output_dir` 仍由 Application 管理

### 12.2 心跳机制与崩溃检测

框架维护两级心跳：

| 级别 | 文件位置 | Payload 字段 |
|------|----------|--------------|
| **Supervisor 心跳** | `{task_id}/heartbeat.json` | `pid`, `run_id`, `timestamp`, `timestamp_iso`, `status`, `step`, `agent_name` |
| **Worker 心跳** | `{task_id}/workers/{name}/heartbeat.json` | `agent_name`, `run_id`, `pid`, `timestamp`, `calls`（各并发调用的 `status`、`step`、`started_at`、`finished_at`） |

**崩溃检测逻辑**（`HEARTBEAT_STALE_THRESHOLD = 30` 秒）：

1. 心跳文件不存在 → `crashed`
2. 心跳 `status` 为 `stopped` 或 `exited` → `crashed`
3. PID 已不存在（`os.kill(pid, 0)` 失败）→ `crashed`
4. 心跳时间戳超过 30 秒未刷新 → `crashed`
5. 以上均不满足 → `running`

### 12.3 断点续跑流程

当检测到某 `task_id` 的 checkpoint 未超过 `max_resume_age`，且状态为非正常结束（`crashed`/`interrupted`）时，框架自动：

1. 从 `checkpoint.json` 恢复 Supervisor 的 memory steps（跳过已完成的思考步骤）
2. 恢复 task-scoped ContextStore 与 file-history index
3. 对每个 Worker 调用，在原 `call_index` 下恢复未完成 memory；已完成且 `input_hash` 一致时直接返回缓存结果
4. 创建新 run 目录，使用新的 `run_id` 从中断点继续执行

### 12.4 配置示例

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
| `model_request_headers.*` | `ModelRequestHeadersSettings` | `src/lib/config/config_validation.py` |
| `tool_access_control.*` | `ToolAccessControlSettings` | `src/lib/config/config_validation.py` |
| `runtime.*` | `RuntimeSettings` | `src/lib/config/config_validation.py` |
| `logging.*` | `LoggingSettings` | `src/lib/config/config_validation.py` |

**`RootSettings` 完整字段定义**：

| 字段 | 类型 | 默认值 |
|------|------|--------|
| `system` | `SystemSettings` | `SystemSettings()` |
| `model_request_headers` | `ModelRequestHeadersSettings` | `ModelRequestHeadersSettings()` |
| `tool_access_control` | `ToolAccessControlSettings` | `ToolAccessControlSettings()` |
| `runtime` | `RuntimeSettings` | `RuntimeSettings()` |
| `logging` | `LoggingSettings` | `LoggingSettings()` |
| `smart_summary` | `bool` | `True` |
| `model` | `dict[str, Any]` | `{}` |
| `execution_env` | `dict[str, Any]` | `{}` |
| `code_agent` | `dict[str, Any]` | `{}` |
| `tools` | `dict[str, Any]` | `{}` |
| `tool_metadata` | `dict[str, Any]` | `{}` |
| `tool_output_limits` | `dict[str, Any]` | `{}` |

> `RootSettings` 允许扩展字段，因此 `prompt` 等顶层字段仍可参与 overlay 合并。`RuntimeSettings` 与 `LoggingSettings` 刻意使用 `extra="forbid"`；已删除的 runtime/logging key 会直接校验失败，不会静默启用第二套存储路径。

**容错解析工具集**：

| 解析器 | 用途 | 位于 |
|--------|------|------|
| `BoolParser` | 兼容布尔输入归一化，实际用于 `logging.console_enabled` / `logging.file_enabled`、Skill 的 `allow-scripts` / `allow-network` 解析，以及部分 LLM 配置开关 | `config_validation.py` / `src/lib/logging/logger_manager.py` / `src/lib/smolagents/agent/base_agent.py` / `src/lib/config/llm_config.py` |
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
| `runtime` | ✅ 支持 | ❌ 拒绝 | ❌ 拒绝 |
| `logging` | ✅ 支持 | ❌ 拒绝 | ❌ 拒绝 |
| `checkpoint` | ✅ 支持 | ✅ 支持 | ❌ 忽略 |
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
