---
name: create-skill
description: "Use when: creating a new AgentLoom Skill from scratch based on user requirements. Covers SKILL.md generation (frontmatter + markdown body), Hook scripts scaffolding, common.py utilities, registration to system.yaml or Agent YAML, and post-creation validation. DO NOT USE for modifying existing skills or non-AgentLoom project."
---

# 创建 AgentLoom Skill

AgentLoom Skill 创建技能。可由 **Copilot Chat / Copilot Codex / Claude Code / AgentLoom Agent** 调用，根据用户需求自动（或交互式地）生成符合 `docs/cn/skills_config.md` 规范的完整 Skill 目录结构。

> **📖 配套参考文档**（按需查阅）：
> - [references/skill-template.md](./references/skill-template.md) — SKILL.md 模板、YAML frontmatter 完整字段参考、Hook 事件速查
> - [references/hook-scripts-guide.md](./references/hook-scripts-guide.md) — 完整 Hook 脚本开发指南（环境变量、JSON 输出、退出码、common.py 模式）
>
> **📖 权威规范文档**（生成前必读）：
> - `docs/cn/skills_config.md` — AgentLoom Skills 配置完整参考（本 Skill 的规范来源）
>
> 参考路径均相对于当前 Skill 根目录。

## 适用场景

- 用户说"帮我创建一个新的 Skill"或"我想写一个 Skill 来做 XXX"
- 用户描述了需要在 Agent 生命周期中自动执行的逻辑（需要 Hook）
- 用户希望向 Agent 注入领域知识或操作规范（需要 LLM 指令）
- 用户想要一个可跨多个 Agent/工作流复用的能力扩展包

## 不适用场景

- 修改已有 Skill 的配置（直接编辑对应的 SKILL.md）
- 创建应用程序或 Agent 工作流（使用 `create-app` Skill）
- 仅需单个工具函数，无需 Skill 封装
- 非 AgentLoom 框架项目

## 执行策略

| 环境 | 策略 |
|------|------|
| **交互式** (VS Code Copilot Chat / 终端对话) | 先补全缺失信息，再确认方案，确认后生成文件 |
| **自主式** (Copilot Codex / Claude Code / 批量处理) | 从 Prompt 中提取信息；无法提问时，根据"可推断信息 + 默认策略"直接生成，附上"假设列表" |

> **核心原则**：遇到不明确或不确定的点，**直接询问用户** — 不要自行猜测。

## 执行前置条件（必须）

- **先导航到 AgentLoom 根目录**，再执行本 Skill 的任何操作（创建目录、生成文件、注册配置等）。
- **根目录识别标准**：存在 `config/llm.yaml` 文件。
  - ⚠️ 不要使用 `config/system.yaml` 进行识别，因为应用级目录也可能包含此文件（例如 `applications/ai_quality_analysis/config/system.yaml`），无法唯一标识项目根目录。
  - `config/llm.yaml` 是全局唯一的，仅存在于 AgentLoom 根目录。
- 所有路径均相对于 AgentLoom 根目录解析。

## 路径策略

- **Skill 目录位置**：默认为 `skills/<skill-name>/`（相对于 AgentLoom 项目根目录）
- 如果用户指定了其他路径（如 `applications/xxx/skills/`），按要求放置
- 所有文件路径均相对于项目根目录（包含 `config/llm.yaml` 的目录）解析

---

## 阶段一：需求收集

**在生成任何文件之前，必须完成需求收集。**

### 前置动作：阅读权威规范

在开始创建之前，**必须先阅读** `docs/cn/skills_config.md` 文档的关键章节，确保准确理解：
- SKILL.md 文件格式（第3章）
- YAML Frontmatter 所有字段（第4章）
- Hook 系统（第6章）
- Hook 脚本开发（第7章）

### 信息提取清单

从用户的 Prompt 或对话中提取以下信息。**加粗项为必填**；其余有默认值，可跳过：

| # | 信息项 | 必填 | 默认值 | 说明 |
|---|--------|------|--------|------|
| 1 | **Skill 名称** | ✅ | — | 小写+连字符，如 `task-logger`，用作目录名 |
| 2 | **一句话描述** | ✅ | — | 用于 SKILL.md 的 `description` 字段 |
| 3 | **Skill 类型** | ✅ | — | 强制注入 / 按需 / 隐藏（决定 `invocation-control.allow-model`） |
| 4 | **核心功能描述** | ✅ | — | Skill 做什么、何时使用、操作步骤是什么 |
| 5 | 是否需要 Hook | ❌ | 按需判断 | 如果逻辑需要在工具调用前/后或任务生命周期中执行，则需要 Hook |
| 6 | Hook 事件列表 | 使用 Hook 时 ✅ | — | 从 9 个事件中选择：TaskCreated、TaskCompleted、StopFailure、SubagentStart、SubagentStop、PreToolUse、PostToolUse、PostToolUseFailure、Stop |
| 7 | 要拦截的工具 | PreToolUse/PostToolUse/PostToolUseFailure 时 ✅ | `"*"` | 用于 `matcher`，如 `"Write\|Edit\|Bash"`。SubagentStart/SubagentStop 也可选择使用 matcher（匹配 Worker Agent 名称） |
| 8 | allowed-tools | ❌ | `null` | Skill 将使用的工具列表 |
| 9 | version | ❌ | `"1.0.0"` | 语义化版本号 |
| 10 | 放置路径 | ❌ | `skills/<name>/` | Skill 目录位置 |
| 11 | 注册位置 | ❌ | 创建后询问 | `config/system.yaml`（全局）或特定 Agent YAML（局部） |

### Skill 类型选择指南

帮助用户选择正确的 Skill 类型：

| 类型 | `allow-model` 值 | 适用场景 | 典型示例 |
|------|-----------------|---------|---------|
| **强制注入** | `"force-inject"` | 核心能力，LLM 必须始终遵守 | 记忆系统、安全规范、编码标准 |
| **按需** | `true`（默认） | 领域辅助，LLM 按需调用 | API 操作指南、特定工作流规范 |
| **隐藏** | `false` | 后台监控，LLM 无需感知 | 事件收集、可视化、日志记录 |

> **建议**：如果用户不确定，关键 Skill 推荐使用 `"force-inject"`（防止 LLM 忘记调用 `load_skill()`）。

> **⚠️ 远程环境注意事项（Docker / E2B）**：当 `execution_env.type` 为 `"docker"` 或 `"e2b"` 时，框架会**跳过加载所有默认工具**（包括 `load_skill` 和 `list_skills`）。这意味着远程环境中的 Agent **无法主动调用 `load_skill()`**。对于必须在远程环境中工作的 Skill，应使用 `"force-inject"` 将指令直接嵌入系统提示词，或在 Agent YAML 的 `tools:` 字段中显式声明 `load_skill`。

### Hook 需求评估指南

根据用户需求判断是否需要 Hook 以及使用哪些事件：

| 用户需求 | 所需 Hook | 说明 |
|---------|------------|------|
| 任务开始时初始化环境/文件 | `TaskCreated` | 创建目录、读取历史状态 |
| 任务完成时清理/通知 | `TaskCompleted` | 清理临时文件、发送通知 |
| 任务失败时记录/回滚 | `StopFailure` | 记录失败原因、回滚状态 |
| 工具调用前验证/修改输入 | `PreToolUse` | 路径校验、参数改写、权限检查 |
| 工具调用后处理输出/日志 | `PostToolUse` | 日志记录、输出过滤 |
| 处理工具执行异常 | `PostToolUseFailure` | 错误处理、回滚操作 |
| 跟踪子任务开始/完成 | `SubagentStart` / `SubagentStop` | 进度跟踪 |
| Agent 给出最终答案前检查 | `Stop` | 确保必要步骤已完成 |
| 只需要 LLM 指令，无需自动化 | 不需要 Hook | 仅编写 Markdown 正文 |

> **⚠️ `PostToolUse` 和 `PostToolUseFailure` 互斥**：同一次工具调用只会触发其中一个 —— 成功时触发 `PostToolUse`，异常时触发 `PostToolUseFailure`。两者不会对同一次调用同时触发。

### 各事件 tool_input 关键字段速查

编写 Hook 脚本时，需要了解不同事件的 `tool_input` 包含哪些关键字段：

| 事件 | `tool_input` 包含的关键字段 |
|------|---------------------------|
| `TaskCreated` | `task_id`、`cwd`、`task_text`（任务文本）、`agent_name`、`worker_agents`（Worker 名称列表） |
| `TaskCompleted` / `StopFailure` | `task_id`、`cwd`、`task_text`、`agent_name`；StopFailure 额外含 `error`、`error_type` |
| `SubagentStart` | `agent_name`（Worker Agent 名称）、`sub_task_id` |
| `SubagentStop` | `agent_name`、`sub_task_id`、`success`（布尔）；失败时额外含 `error` |
| `PreToolUse` / `PostToolUse` / `PostToolUseFailure` | 工具调用的完整输入参数（因工具而异） |
| `Stop` | `final_answer`（Agent 准备给出的最终答案） |

### 交互模式：必填信息缺失时

如果 Prompt 中缺少必填信息（#1-#4），向用户询问。**一次性询问所有缺失项**：

```
创建 Skill 需要以下信息：
1. Skill 名称？（建议：小写+连字符，如 task-logger）
2. 一句话功能描述？
3. Skill 类型？（强制注入 / 按需 / 隐藏）
4. 核心功能是什么？（做什么、何时使用、操作步骤）
5. 是否需要 Hook？如果需要，挂载到哪些事件上？
```

---

## 阶段二：方案确认

**在生成任何文件之前**，必须向用户展示完整的生成方案。

### 方案模板

```markdown
## Skill 生成方案

### 基本信息
- **名称**: {name}
- **描述**: {description}
- **版本**: {version}
- **类型**: {强制注入 / 按需 / 隐藏}
- **目录**: {path}

### 目录结构
{展示将要生成的完整目录树}

### SKILL.md Frontmatter
{展示关键 YAML frontmatter 配置}

### Hook 方案
{如果有 Hook，列出每个事件的脚本和行为}

### Markdown 正文摘要
{LLM 指令主要内容摘要}

### 注册方式
{如何注册到配置文件}

请确认是否继续生成？如需调整，请告诉我。
```

> **等待用户确认后再开始阶段三。** 自主式场景可跳过确认。

---

## 阶段三：文件生成

### 生成顺序

严格按照以下顺序生成文件：

1. **创建目录结构**
2. **生成 SKILL.md**（frontmatter + markdown 正文）
3. **生成 scripts/common.py**（如果有 Hook）
4. **生成各 Hook 脚本**（如果有 Hook）
5. **生成 templates/**（如果有模板需求）
6. **生成 references/**（如果需要参考文档）

### 3.1 SKILL.md 生成规范

#### Frontmatter 规范

```yaml
---
name: {skill-name}                    # 必须与目录名一致
description: "{清晰的功能描述}"  # LLM 根据此字段决定是否使用该 Skill
version: "1.0.0"
allowed-tools: "{工具列表}"            # 建议使用抽象名称：Read, Write, Edit, Bash, Glob, Grep
hooks:                                 # 如果有 Hook
  {EventName}:
    - matcher: "{pattern}"             # 工具事件必需，生命周期事件不需要
      hooks:
        - type: command
          command: python ./scripts/{script_name}.py
---
```

**Frontmatter 检查清单**：
- [ ] `name` 与目录名一致
- [ ] `description` 清晰且不超过 1024 个字符
- [ ] `description` 包含触发短语如 "Use when" / "Applicable for"
- [ ] `allowed-tools` 使用抽象工具名（如 `Read` 而非 `read_file`）
- [ ] Hook `matcher` 使用抽象工具名（加载时自动映射）
- [ ] 包含冒号的 YAML 值用引号包裹
- [ ] 缩进使用空格，非制表符
- [ ] **不要**在 frontmatter 中写 `platform` 或 `invocation-control` — 它们只在引用侧（system.yaml / Agent YAML）配置

#### Markdown 正文规范

Markdown 正文是 `load_skill()` 返回给 LLM 的内容，LLM 必须遵循。推荐结构：

```markdown
# {Skill 名称}

一句话描述该 Skill 的功能。

## 使用场景
- 何时使用
- 何时不使用

## 操作步骤
1. 第一步...
2. 第二步...
3. 第三步...

## 注意事项
- 重要的约束和规则
```

**正文检查清单**：
- [ ] 以清晰的功能描述开头
- [ ] 有明确的使用场景和不适用场景
- [ ] 有具体的操作步骤（避免模糊指令）
- [ ] 如果有运行时文件，指定文件路径和格式
- [ ] 语言与目标受众匹配（中文项目使用中文）

### 3.2 Hook 脚本生成规范

#### common.py 模板

每个包含 Hook 的 Skill 都应生成 `scripts/common.py`：

```python
# scripts/common.py
import json
import os
from pathlib import Path


def get_agent_name() -> str:
    """Get current Agent name from environment variable"""
    return os.environ.get("AGENT_NAME", "") or "default"


def get_tool_name() -> str:
    """Get current tool name from environment variable"""
    return os.environ.get("TOOL_NAME", "") or "unknown"


def get_task_id() -> str:
    """Get task ID from environment variable"""
    return os.environ.get("TASK_ID", "") or ""


def get_hook_event() -> str:
    """Get Hook event name from environment variable"""
    return os.environ.get("HOOK_EVENT", "") or "Unknown"


def get_hook_context() -> dict:
    """Parse HOOK_CONTEXT_JSON"""
    raw = os.environ.get("HOOK_CONTEXT_JSON", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}


def get_tool_input() -> dict:
    """Extract tool_input from Hook context"""
    ti = get_hook_context().get("tool_input")
    return ti if isinstance(ti, dict) else {}


def get_tool_response():
    """Extract tool_response from Hook context"""
    return get_hook_context().get("tool_response")


def output(result: dict) -> None:
    """Print JSON result to stdout"""
    print(json.dumps(result, ensure_ascii=False))


def _find_agent_loom_root() -> Path:
    """推导 AgentLoom 项目根目录。

    检测顺序：
    1. $AGENT_LOOM_RUNTIME_ROOT 环境变量（测试时使用临时目录）。
    2. 从当前文件逐层向上查找 config/llm.yaml
       —— AgentLoom 根目录的全局唯一标识文件。
    3. pyproject.toml 兜底（向后兼容）。
    4. cwd 兜底。
    """
    env_root = os.environ.get("AGENT_LOOM_RUNTIME_ROOT", "").strip()
    if env_root:
        return Path(env_root)

    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "config" / "llm.yaml").exists():
            return current
        current = current.parent

    candidate = Path(__file__).resolve().parent.parent.parent.parent
    if (candidate / "pyproject.toml").exists():
        return candidate

    return Path.cwd()


def runtime_dir(agent_name: str) -> Path:
    """Return <agent_loom_root>/.runtime/<agent_name> path"""
    return _find_agent_loom_root() / ".runtime" / agent_name
```

> **根目录检测优先级**：`$AGENT_LOOM_RUNTIME_ROOT` 环境变量 > 逐层向上查找 `config/llm.yaml`（全局唯一） > `pyproject.toml` 兆底 > 当前工作目录。
> 优先使用 `config/llm.yaml` 是因为它是 AgentLoom 项目的全局唯一标识文件。逐层向上查找确保无论 Skill 嵌套多深（如 `applications/xxx/skills/my-skill/`）都能正确检测。

#### Hook 脚本工作目录 (cwd)

执行 Hook 脚本时的 `cwd` 始终是 **Skill 目录**（包含 SKILL.md 的目录），而非项目根目录。
因此，`command: python ./scripts/on_task_start.py` 中的 `./scripts/` 路径是相对于 Skill 目录解析的。

#### Hook 脚本模板

每个 Hook 脚本遵循统一结构：

```python
# scripts/on_{event}.py
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from common import get_agent_name, get_hook_context, output


def main():
    agent = get_agent_name()
    context = get_hook_context()

    # ← 在此编写 Hook 逻辑

    output({
        "decision": "allow",          # allow / block / modify
        # "modified_input": {},        # PreToolUse 时可修改工具输入
        # "modified_response": {},     # PostToolUse 时可修改工具输出
        # "agent_context": "",         # 注入到 Agent 系统提示词
        # "user_message": "",          # 发送消息给用户
        # "reason": "",                # 原因描述
        # "telemetry": {},             # 自定义遥测数据
    })


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 避免脚本异常导致非零退出码 → 意外阻断
        output({"decision": "allow", "reason": f"Hook error (safe allow): {e}"})
```

> **最佳实践**：用 `try/except` 包裹 `main()` 调用，异常时输出 `allow` 并附带原因。
> 如果脚本以非零退出码退出，框架会**强制阻断**（即使 JSON 中写的是 `allow`），这通常不是期望的行为。

**`decision: "block"` 在不同事件中的实际效果**：

| 事件 | block 效果 |
|------|-----------|
| `PreToolUse` | ✅ 直接阻止工具执行 |
| `PostToolUse` / `PostToolUseFailure` | ⚠️ 不会撤销已完成的执行，只阻止结果继续传递 |
| `Stop` | 阻止 Agent 给出最终答复 |
| 生命周期事件 | 不中断任务主流程，只跳过后续 Hook |

> 如果目标是阻止操作发生，应在 `PreToolUse` 阶段拦截。

**Hook 脚本检查清单**：
- [ ] 以 `sys.path.insert(0, os.path.dirname(__file__))` 开头
- [ ] 从 `common` 导入工具函数
- [ ] 有 `main()` 函数和 `if __name__ == "__main__"` 入口
- [ ] 使用 `output()` 输出 JSON（不要直接 `print` 非 JSON 内容）
- [ ] 仅输出规范定义的 7 个字段（decision、modified_input、modified_response、agent_context、user_message、reason、telemetry）
- [ ] 异常处理防止脚本以非零退出码退出（除非故意阻断）— 推荐用 `try/except` 包裹 `main()`
- [ ] 注意 Hook 超时限制（默认 20 秒），可通过 `timeout` 字段自定义：

```yaml
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
          timeout: 60    # 单位：秒，默认 20
```

---

## 阶段四：注册配置

创建 Skill 文件后，询问用户如何注册：

### 提示模板

```
Skill 创建完成！请选择注册方式：

1. **全局注册**（所有 Agent 共享）：添加到 config/system.yaml
2. **Agent 级注册**（仅特定 Agent）：添加到 Agent YAML 的 skills 字段
3. **自动发现**（放在 skills/ 目录下已自动发现，无需额外注册）
4. **暂不注册**

如果选择 1 或 2，还需确认：
- invocation-control 配置？（force-inject / true / false）
- allow-hook？（true / false）
```

> **关于自动发现的重要说明**：
> - 自动发现**仅扫描** `AGENT_ROOT/skills/` 目录（即 AgentLoom 根目录下的 `skills/` 目录），递归搜索 `SKILL.md` / `skill.md` 文件。
> - `tools/skills/` 目录中的 Skill 是给 AI 助手使用的工具 Skill，**不在自动发现路径内**；不会被 AgentLoom Agent 自动加载。
> - 如果 Skill 放在非标准路径（如 `applications/xxx/skills/`），**必须手动注册**到 `system.yaml` 或 Agent YAML 中。

### 注册配置格式

**全局注册**（`config/system.yaml`）：

```yaml
skills:
  # ... 已有 skills ...
  - path: "skills/{skill-name}"
    invocation-control:
      allow-model: {true / false / "force-inject"}
      allow-hook: true
```

**Agent 级注册**（Agent YAML）：

```yaml
skills:
  - path: "skills/{skill-name}"
    platform: "Claude"
```

**字符串简写格式**（最简形式，默认 `allow-model: true, allow-hook: true`）：

```yaml
skills:
  - "skills/{skill-name}"
```

> **⚠️ 命名冲突警告**：如果已有同名的 Skill（从全局配置、自动发现或其他 Agent YAML 加载），后加载的会**静默覆盖**先加载的（并输出 warning 日志）。创建新 Skill 前，请通过 `list_skills()` 或查看 `config/system.yaml` 和 `skills/` 目录确认现有名称，避免意外覆盖。

> **注意**：如果 Skill 放在 `skills/` 目录（`AGENT_ROOT/skills/`，即 AgentLoom 根目录下的 `skills/` 目录），不需要在 `system.yaml` 或 Agent YAML 中显式注册路径 — 框架会自动扫描发现。但是，**`invocation-control` 参数仍需在引用侧配置**（默认为 `allow-model: true, allow-hook: true`）。
>
> ⚠️ 自动发现**不包括** `tools/skills/` 和 `applications/xxx/skills/` 等路径 — 这些位置的 Skill 必须手动注册。

---

## 阶段五：验证

### 生成后验证清单

- [ ] SKILL.md 存在且格式正确（frontmatter + markdown）
- [ ] `name` 字段与目录名一致
- [ ] `description` 非空且有意义
- [ ] 所有 Hook 脚本文件存在且可执行
- [ ] `common.py` 存在（如果有 Hook）
- [ ] 所有 Python 脚本语法正确（无 SyntaxError）
- [ ] Hook `matcher` 使用抽象工具名
- [ ] `allowed-tools` 使用抽象工具名
- [ ] 注册配置已添加（如果用户要求）
- [ ] 目录结构符合规范

### 验证命令

```bash
# 检查 SKILL.md 格式（将 <skill-path> 替换为实际 Skill 路径，如 skills/task-logger）
cd AgentLoom && .venv/bin/python -c "
from src.lib.smolagents.skills.parser import parse_skill_file
meta, content = parse_skill_file('<skill-path>/SKILL.md')
print(f'Name: {meta.name}')
print(f'Description: {meta.description[:80]}...')
print(f'Hooks: {list(meta.hooks.keys()) if meta.hooks else \"None\"}')
print(f'Allowed tools: {meta.allowed_tools}')
print('✅ SKILL.md parsed successfully')
"
```

```bash
# 检查 Hook 脚本语法（将 <skill-path> 替换为实际路径）
cd AgentLoom && .venv/bin/python -m py_compile <skill-path>/scripts/common.py
cd AgentLoom && .venv/bin/python -m py_compile <skill-path>/scripts/on_task_start.py
# ... 其他脚本
```

---

## 参考：现有 Skill 示例

### agent-recall-with-files（强制注入类型）

```
skills/agent-recall-with-files/
├── SKILL.md              # 6.0.0, 8 个 Hook, 强制注入
├── scripts/
│   ├── common.py
│   ├── on_task_start.py
│   ├── on_task_complete.py
│   ├── on_task_fail.py
│   ├── on_pre_tool_use.py
│   ├── on_post_tool_use.py
│   ├── on_subtask_start.py
│   ├── on_subtask_finish.py
│   └── on_stop.py
└── templates/
    ├── context.md
    └── trace.md
```

### agent-visualization（隐藏类型）

```
skills/agent-visualization/
├── SKILL.md              # 1.0.0, 8 个 Hook, allow-model: false
└── scripts/
    ├── common.py
    ├── on_task_start.py
    ├── on_task_complete.py
    ├── on_task_fail.py
    ├── on_subtask_start.py
    ├── on_subtask_finish.py
    ├── on_pre_tool_use.py
    ├── on_post_tool_use.py
    └── on_post_tool_error.py
```
