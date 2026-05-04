# MCP (Model Context Protocol) 客户端配置

> AgentLoom 支持作为 MCP Client 连接外部 MCP Server，动态发现并加载工具。配置格式采用标准 `.mcp.json` 格式。

---

## 目录

1. [概述](#1-概述)
2. [配置文件格式](#2-配置文件格式)
   - 2.1 [`.mcp.json` 文件格式](#21-mcpjson-文件格式)
   - 2.2 [YAML `mcp_servers` 字段](#22-yaml-mcp_servers-字段)
3. [路径解析规则](#3-路径解析规则)
4. [多级配置合并](#4-多级配置合并)
5. [工具命名约定](#5-工具命名约定)
6. [支持的传输类型](#6-支持的传输类型)
7. [错误处理策略](#7-错误处理策略)
8. [日志](#8-日志)
9. [完整示例](#9-完整示例)
10. [集成测试](#10-集成测试)

---

## 1. 概述

MCP (Model Context Protocol) 是一个开放标准，允许 AI 应用通过统一协议连接外部工具服务器。AgentLoom 的 MCP Client 功能实现了：

- **动态工具发现**：Agent 启动时连接 MCP Server，自动获取其提供的工具
- **生态互通**：可直接使用 MCP 社区中 100+ 的 Server（数据库、API、搜索引擎等）
- **配置驱动**：通过编辑 `.mcp.json` 文件添加外部工具，无需修改代码
- **标准格式兼容**：`.mcp.json` 格式与 MCP 生态标准一致，可直接复用其他工具的配置

**已满足依赖**：`smolagents[mcp]==1.21.1`（包含 `mcp` SDK 和 `mcpadapt` 适配器），无需额外安装。

---

## 2. 配置文件格式

### 2.1 `.mcp.json` 文件格式

`.mcp.json` 是一个标准 JSON 文件，使用 MCP 标准格式定义 MCP Server：

```json
{
  "mcpServers": {
    "filesystem": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"],
      "env": {
        "NODE_ENV": "production"
      }
    },
    "database": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "mcp_server_sqlite", "--db", "data.db"]
    },
    "web-search": {
      "type": "sse",
      "url": "http://localhost:8080/mcp",
      "headers": {
        "Authorization": "Bearer token123"
      }
    },
    "remote-api": {
      "type": "http",
      "url": "https://api.example.com/mcp"
    }
  }
}
```

**字段说明**：

| 字段 | 必填 | 说明 |
|------|------|------|
| `type` | 否 | 传输类型，默认 `"stdio"`。支持 `"stdio"` / `"sse"` / `"http"` |
| `command` | `stdio` 必填 | 启动子进程的命令 |
| `args` | 否 | 命令行参数 |
| `env` | 否 | 环境变量 (stdio) |
| `url` | `sse`/`http` 必填 | 远程 Server URL |
| `headers` | 否 | HTTP 请求头 (sse/http) |

### 2.2 YAML `mcp_servers` 字段

在 `config/system.yaml`（全局）或 Agent YAML（每个 Agent）中通过 `mcp_servers` 字段引用 `.mcp.json` 文件：

```yaml
# 方式 1：单个文件路径（字符串）
mcp_servers: "config/.mcp.json"

# 方式 2：多个文件路径（列表）
mcp_servers:
  - "config/.mcp.json"
  - "config/extra-mcp.json"

# 方式 3：带选项的字典
mcp_servers:
  path: "config/.mcp.json"            # 或 paths: ["a.json", "b.json"]
  timeout: 30                          # 连接超时（秒）
  tool_timeout: 60                     # 工具调用超时（秒）
  tool_name_prefix: true               # 是否添加 mcp__{server}__ 前缀（默认 true）
```

---

## 3. 路径解析规则

与 `prompt.path` 一致的路径解析规则：

| 路径类型 | 解析规则 |
|----------|----------|
| 相对路径 | `agent_root / 相对路径` |
| 绝对路径 | 直接使用 |
| `~` 开头 | 展开为用户主目录 |

---

## 4. 多级配置合并

```
config/system.yaml  →  mcp_servers: "config/.mcp.json"      (全局)
        ↓ 合并
Agent YAML          →  mcp_servers: "apps/my_app/.mcp.json"  (单 Agent)
```

**合并规则**：
1. 先加载全局 JSON 文件 → 全局 Server 列表
2. 再加载 Agent YAML 中的 JSON 文件 → Agent 级别 Server 列表
3. 同名 Server：Agent 级别覆盖全局
4. Agent 级别的新 Server 名称追加到列表
5. 选项（`timeout` 等）以 Agent 级别为准

---

## 5. 工具命名约定

默认启用 `tool_name_prefix: true`，MCP 工具名格式为：

```
mcp__{server_name}__{tool_name}
```

示例：
- `"filesystem"` Server 的 `"read_file"` 工具 → `mcp__filesystem__read_file`
- `"web-search"` Server 的 `"query"` 工具 → `mcp__web_search__query`

**Server 名称清理规则**：非字母数字字符替换为 `_`。

**关闭前缀**：`tool_name_prefix: false` 时保留原始工具名。注意：关闭前缀可能导致与本地工具同名冲突。

---

## 6. 支持的传输类型

| 类型 | 说明 | 配置 |
|------|------|------|
| `stdio` | 本地子进程（最常用） | `command` + `args` + `env` |
| `sse` | HTTP + Server-Sent Events（旧式远程） | `url` + `headers` |
| `http` | Streamable HTTP（新式远程，自动映射为 `streamable-http`） | `url` + `headers` |

**暂不支持**：WebSocket (`ws`)、OAuth 认证。

---

## 7. 错误处理策略

AgentLoom 采用**优雅降级**策略——MCP 连接失败不会阻止 Agent 启动：

| 场景 | 行为 |
|------|------|
| `.mcp.json` 文件不存在 | WARNING 日志，跳过 |
| JSON 格式错误 | WARNING 日志，跳过该文件 |
| 缺少 `mcpServers` 键 | WARNING 日志，视为空文件 |
| 无效 Server 配置 | 跳过该 Server，WARNING 日志 |
| 连接超时 | 跳过该 Server，WARNING 日志 |
| 所有 Server 连接失败 | Agent 仅使用本地工具运行 |
| `mcp` 包未安装 | WARNING 日志，跳过 MCP |
| 工具名与本地冲突 | 前缀（默认开启）避免冲突 |
| `disconnect_all()` 多次调用 | 幂等，安全 |

---

## 8. 日志

| 事件 | 级别 | 消息 |
|------|------|------|
| JSON 文件加载成功 | INFO | `[MCP] Loaded config from '{path}': {n} servers` |
| Server 连接成功 | INFO | `[MCP] Connected to '{name}': {n} tools` |
| 连接失败 | WARNING | `[MCP] Failed to connect to '{name}': {error}` |
| 文件未找到 | WARNING | `[MCP] Config file not found: {path}` |
| JSON 解析错误 | WARNING | `[MCP] Invalid JSON in {path}: {error}` |
| Server 断开连接 | INFO | `[MCP] Disconnected from '{name}'` |
| 无 MCP 配置 | DEBUG | `[MCP] No MCP servers configured` |

---

## 9. 完整示例

### 文件结构

```
my_app/
├── config/
│   └── .mcp.json          # MCP Server 定义
└── workflows/
    └── agent.yaml         # Agent 配置
```

### `.mcp.json`

```json
{
  "mcpServers": {
    "MiniMax": {
      "command": "uvx",
      "args": ["minimax-coding-plan-mcp"],
      "env": {
        "MINIMAX_API_KEY": "your-api-key",
        "MINIMAX_API_HOST": "https://api.minimaxi.com"
      }
    }
  }
}
```

### `agent.yaml`

```yaml
name: "search_agent"
description: |
  Use web_search to find information.
model_type: "powerful"
tool_call_type: "code_act"

mcp_servers: "my_app/config/.mcp.json"

tools:
  - name: "read_file"

workflow: |
  You have access to web_search from MCP.
  Use mcp__MiniMax__web_search to search for information.

max_steps: 10
```

### 运行

```bash
loom run my_app/workflows/agent.yaml
```

---

## 10. 集成测试

项目提供了一个 MiniMax MCP 集成测试模板：

- **MCP 配置**：`applications/test_demo/config/.mcp.json`
- **Agent YAML**：`applications/test_demo/workflows/test_mcp_agent.yaml`

运行前需设置 `MINIMAX_API_KEY` 环境变量或直接编辑 `.mcp.json` 文件。

```bash
# 编辑 .mcp.json 填入 API Key
# 然后运行：
loom run applications/test_demo/workflows/test_mcp_agent.yaml
```

**验证标准**：
- 日志显示 `[MCP] Connected to 'MiniMax': N tools`
- LLM 发现并调用 `web_search` 工具
- 工具返回真实搜索结果（非空内容）
- 最终输出包含有意义的搜索结果摘要
