# AgentLoom Application 配置快速参考

> 本文档供 Agent 和用户在配置过程中快速查阅。完整详情请参阅项目内的配置文档。
>
> **路径约定**：所有相对路径均基于项目根目录（即包含 `config/llm.yaml` 的目录）。`config/llm.yaml` 仅存在于项目根目录，不会出现在 Application 级子目录中，因此不会产生歧义。
>
> **自定义 Tool**：纯 Python 函数即可——无需 `@tool` 装饰器。框架通过 YAML 中的 `module` + `function` 配置动态导入 Tool。

---

## 1. 全量预定义 Tool

| Tool 名称 | 描述 | 常见用途 |
|-----------|------|----------|
| **文件读取** | | |
| `read_file` | 读取文件内容（支持 offset/limit 分段读取） | 代码分析、文件检查 |
| `get_file_outline` | 获取代码大纲（函数/类/结构体） | 结构分析 |
| **文件写入** ¹ | | |
| `write_file` | 创建新文件或覆盖已有文件 | 报告生成、文件创建 |
| `write_whole_file` | 写入整个文件 | 大文件写入 |
| `edit_file` | 编辑文件（查找 & 替换） | 代码修改 |
| `write_markdown_file` | 写入 Markdown 文件 | 文档/报告生成 |
| `write_markdown_file_raw` | 写入原始 Markdown | 非转义 Markdown |
| `append_markdown_sections` | 追加 Markdown 段落 | 增量报告生成 |
| **文件操作** | | |
| `move_file` | 移动文件 | 文件整理 |
| `rename_file` | 重命名文件 | 文件重命名 |
| `copy_file` | 复制文件 | 文件备份 |

> ¹ **文件历史备份**: 当 checkpoint 启用时，文件写入工具（`edit_file`、`write_file`、`create_file`、`write_markdown_file`、`move_file`、`copy_file`）会通过 FileHistoryHook 自动创建编辑前备份。备份存储在 `.logs/{agent}/checkpoints/{task_id}/file-history/` 下，可用于恢复时回滚文件状态。
| `delete_file` | 删除文件 | 清理临时文件 |
| **目录与搜索** | | |
| `browse_directory` | 浏览目录结构 | 项目结构分析 |
| `list_files_glob` | 使用 Glob 模式搜索文件 | 批量文件定位 |
| `search_keyword_in_directory` | 在目录中搜索关键词 | 关键词定位 |
| `search_keyword_with_context` | 带上下文搜索关键词 | 代码引用分析 |
| `ripgrep_search_directory` | 高性能 ripgrep 搜索 | 大型仓库搜索 |
| `search_files` | 搜索文件 | 文件名搜索 |
| `code_search` | 代码搜索 | 语义级代码搜索 |
| `search_and_replace` | 文件中搜索替换 | 批量修改 |
| `code_replace` | 代码替换 | 代码级替换 |
| `code_edit` | 代码编辑 | 智能代码编辑 |
| `ast_grep_search_file` | AST 模式搜索 | 语法级搜索 |
| **Git 操作** | | |
| `get_git_diff_content` | 获取 Git 差异 | PR 审查、变更分析 |
| `git_grep_files` | Git grep 搜索 | 在 Git 仓库中搜索 |
| `git_commit_files` | Git 提交指定文件 | 自动提交 |
| `git_auto_commit` | Git 自动提交 | 自动保存 |
| `git_check_dirty` | 检查未提交的变更 | 状态检查 |
| `is_path_in_repo` | 检查路径是否在 Git 仓库中 | 路径校验 |
| **Shell 与 Skill** | | |
| `shell_tool` | 执行 Shell 命令（白名单限制） | 构建、测试 |
| `load_skill` | 加载指定 Skill | 按需加载 Skill |
| `list_skills` | 列出可用 Skill | 查看可用 Skill |

---

## 2. model_type 选型规则（动态发现）

`model_type` 不是固定枚举——应从项目的 `config/llm.yaml` 中动态读取：

1. 读取 `model.default_model_type` 作为默认类型。
2. 读取 `model` 下的其余键作为可用类型（排除保留键 `default_model_type` 和非 dict 值）。
3. 在交互场景中，向用户确认：使用默认 / 明确指定 / 自定义。
4. 自定义值必须已在 `config/llm.yaml` 中定义，否则运行时会报错。

快速检查当前项目可用的 `model_type` 值（在项目根目录运行）：

```python
import yaml
from pathlib import Path

cfg = yaml.safe_load(Path("config/llm.yaml").read_text(encoding="utf-8")) or {}
model_cfg = cfg.get("model", {}) if isinstance(cfg, dict) else {}
default_type = model_cfg.get("default_model_type")
types = [k for k in model_cfg if k != "default_model_type" and isinstance(model_cfg[k], dict)]
print("default_model_type:", default_type)
print("available_model_types:", types)
```

---

## 3. execution_env.type 可选值

| 值 | 描述 | 安全性 | 适用场景 |
|----|------|--------|----------|
| `local` | 直接在本地宿主机运行 | ⚠️ 低 | 开发/调试、可信环境 |
| `docker` | 在 Docker 容器中隔离运行 | ✅ 高 | 生产环境 |
| `e2b` | E2B 云端沙箱 | ✅ 高 | SaaS 产品 |
| `wasm` | WebAssembly 本地沙箱 | ✅ 高 | 轻量级隔离 |

---

## 4. tool_call_type 对比

| 值 | Agent 类型 | 调用方式 | 灵活性 | 推荐场景 |
|----|-----------|----------|--------|----------|
| `code_act` | `CodeAgentV2` | 编写 Python 代码调用 | 高（循环、条件、多步编排） | **推荐用于 Supervisor**、复杂 Worker |
| `tool_call` | `ToolCallingAgentV2` | 结构化 tool_call 消息 | 低（每次只能调用一个 Tool） | 简单 Worker |

---

## 5. agent_function_schema.inputs 常用命名

| 参数名 | 适用场景 | 示例值 |
|--------|----------|--------|
| `query` | 通用任务描述 | `"分析 src/ 目录的代码结构"` |
| `file_path` | 单文件操作 | `"src/main.py"` |
| `file_paths` | 多文件操作 | `"src/a.py, src/b.py"` |
| `module_name` | 模块级操作 | `"authentication"` |
| `context` | 上游 Worker 传递的上下文 | 上一阶段的分析结果文本 |
| `config` | 配置信息 | JSON 格式的参数集 |
| `target_dir` | 目录级操作 | `"src/lib/"` |

---

## 6. 系统级 Checkpoint 配置

AgentLoom 提供**内置断点续跑/心跳监控系统**，任何应用自动继承，无需应用层代码。配置在 `config/system.yaml` 的 `checkpoint.*` 下：

| 字段 | 类型 | 默认值 | 说明 |
|-------|------|---------|------|
| `checkpoint.enabled` | `bool` | `true` | 全局开关。仅对暂时性脚本设 `false` |
| `checkpoint.cleanup_on_success` | `bool` | `true` | 成功后自动删除 checkpoint 目录。调试时可设 `false` 保留现场 |
| `checkpoint.max_resume_age` | `int`（秒） | `604800` | 7 天有效期。超期的 checkpoint 不可恢复 |
| `checkpoint.heartbeat_interval` | `int`（秒） | `5` | 心跳写入频率，用于崩溃检测 |

> **注意**：`checkpoint.*` 是系统级配置，**不**在 Agent YAML 覆盖白名单中。如需按应用调整，将 `config/system.yaml` 放在应用目录下即可。

---

## 7. 覆盖白名单字段（Agent YAML 可覆盖系统配置）

仅以下 7 个顶层字段可以覆盖 `config/system.yaml`：

| 字段 | 类型 | 描述 |
|------|------|------|
| `system` | `dict` | 系统元数据 |
| `smart_summary` | `any` | 上下文压缩策略 |
| `tool_access_control` | `dict` | 工作目录和路径过滤 |
| `execution_env` | `dict` | 执行环境 |
| `code_agent` | `dict` | 代码执行权限 |
| `tools` | `dict` | Tool 配置（注意：这是 dict，与 Agent 的 tools 列表不同） |
| `prompt` | `str/dict` | 系统 Prompt 模板路径 |

---

## 8. 关键约束清单

| # | 约束 | 说明 |
|---|------|------|
| 1 | **LLM 配置隔离** | Agent YAML 不得包含 `model`/`llm`/`langfuse` —— 这些仅属于 `config/llm.yaml` |
| 2 | **worker_agents 仅使用 path** | 禁止使用 `name` 字段 |
| 2a | **简写路径必须包含文件后缀** | 如 `scan.yaml`，不能写 `scan` |
| 2b | **Supervisor 在 `workflows/`，Worker 在 `workflows/worker_agents/`** | 框架会校验目录存在 |
| 3 | **Tool 是纯函数（无装饰器），描述来自 docstring** | YAML 中的 description 会被忽略 |
| 4 | **module/function 必须成对** | 自定义 Tool 必须同时配置两者 |
| 5 | **workflow 使用 `\|` 或非空 `list[str]`** | 单个工作流使用多行文本块；顺序工作流使用列表项，每项建议用多行文本块 |
| 6 | **列表是覆盖而非追加** | 覆盖 `default_loaded_tools` 需要完整列表 |
| 7 | **Worker 返回值为字符串** | `None` → `""`，其他 → `str()` |
| 8 | **inputs 键必须是合法标识符** | 必须满足 Python `isidentifier()` |

---

## 9. 目录结构与 `worker_agents.path` 解析规则（重要）

### 目录结构（强制）

Agent YAML 文件**必须**按以下目录结构放置，框架会校验目录是否存在：

```
applications/{app_name}/
└── workflows/                          ← Supervisor YAML 必须放在这里
    ├── {app_name}_agent.yaml
    └── worker_agents/                  ← Worker YAML 必须放在这里
        ├── worker_a.yaml
        ├── worker_b.yaml
        └── analysis/                   ← 允许创建子目录（无命名限制）
            └── deep_scan.yaml          ← 需使用完整相对路径引用
```

### 路径解析模式

`worker_agents.path` 并非“总是相对于项目根目录解析”——它遵循三种解析模式：

| 模式 | 示例 | 解析结果 |
|------|------|----------|
| 带后缀的文件名（不含 `/`） | `code_scan.yaml` | 相对于当前 Supervisor YAML 旁边的 `worker_agents/` 目录解析 |
| 含 `/` 的相对路径 | `applications/code_review/workflows/worker_agents/code_scan.yaml` | 相对于项目根目录解析 |
| 绝对路径 | `/home/user/project/applications/.../code_scan.yaml` | 原样使用 |

> **注意**：简写文件名**必须包含文件后缀**（`.yaml`、`.yml` 或 `.md`）。不带后缀的写法（如 `code_scan`）会直接报错。

> 最佳实践：Worker 在 `worker_agents/` 一级目录下时，使用简写文件名（如 `code_scan.yaml`）。跨 app 引用或引用子目录下的 Worker 时使用完整相对路径（如 `applications/my_app/workflows/worker_agents/analysis/deep_scan.yaml`）。
