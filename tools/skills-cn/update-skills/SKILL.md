---
name: update-skills
description: "当 docs/cn/ 文档、框架源码（src/）或核心配置文件（config/system.yaml、config/llm.yaml）发生变更时，检测变更范围并同步更新 tools/skills-cn/ 下所有 Skills 的 SKILL.md 和 references/*.md，确保 Skills 内容与最新文档和代码保持一致。"
---

# 更新 Skills

当 AgentLoom 项目文档（`docs/cn/`）、框架源代码（`src/`）或核心配置文件（`config/system.yaml`、`config/llm.yaml`）发生变更后，使用本 Skill 自动检测变更内容，定位受影响的 Skills，并逐一更新 `tools/skills-cn/` 下的 SKILL.md 和 references/*.md 文件。

## 前提条件（必须满足）

- **先导航到 AgentLoom 根目录**，再执行本 Skill 的任何操作。
- **根目录识别标准**：当前目录存在 `<project_root>/config/llm.yaml`（即当前路径下可直接访问 `config/llm.yaml`）。
  - ⚠️ 不要使用 `config/system.yaml` 进行识别，因为应用级目录也可能包含此文件（例如 `applications/ai_quality_analysis/config/system.yaml`），无法唯一标识项目根目录。
  - `config/llm.yaml` 是全局唯一的，仅存在于 AgentLoom 根目录。
- 项目路径（`docs/*`、`src/*`、`config/*`）均相对于 AgentLoom 根目录解析。
- Skill 内置引用路径（`./references/*`）相对于当前 Skill 根目录解析。
- `tools/skills-cn/update-skills/` 仅作为更新规则来源，**不是更新目标**。

> **📖 辅助参考文档**（按需查阅）：
> - [references/doc-skill-mapping.md](./references/doc-skill-mapping.md) — 文档/代码 → Skill 影响映射表
> - [references/update-checklist.md](./references/update-checklist.md) — 逐 Skill 更新检查清单
>
> **📖 权威规范文档**（更新时必读）：
> - `docs/cn/agent_config.md` — Agent YAML 配置完整参考
> - `docs/cn/skills_config.md` — Skills 配置完整参考
> - `docs/cn/system_config.md` — 系统配置完整参考
> - `docs/cn/llm_config.md` — LLM 配置完整参考
> - `docs/cn/config-overview.md` — 配置系统概述
>
> 路径基准说明：
> - `./references/*` 相对于当前 Skill 根目录。
> - `docs/*`、`src/*`、`config/*` 相对于 AgentLoom 根目录。

## 适用场景

- `docs/cn/` 下的文档发生了变更（新增、修改或删除），且 tools/skills-cn 下引用了这些文档的 Skills 需要同步更新
- `src/` 下的框架源代码发生了变更（配置解析、Skills 加载、工具系统等），可能影响 Skills 中描述的行为或约束
- `config/system.yaml` 或 `config/llm.yaml` 发生了变更，可能影响工具列表、覆盖规则、model_type 选择等配置语义
- 用户说"更新 skills"、"同步 skills 和文档"、"文档改了，skills 需要跟上"
- 新增了 `docs/cn/` 文档，需要在相关 Skills 中添加引用

## 不适用场景

- 创建全新的 Skill（请使用 `create-skill` Skill）
- 创建新的应用程序（请使用 `create-app` Skill）
- 审查 Workflow 质量（请使用 `workflow-review` Skill）
- 不基于 AgentLoom 框架的项目

## 执行策略

| 环境 | 策略 |
|------|------|
| **交互式**（VS Code Copilot Chat / 终端对话） | 先展示变更影响分析，确认后逐个更新 Skills，每个 Skill 更新后展示 diff 摘要 |
| **自动化**（Copilot Codex / Claude Code / 批量处理） | 自动完成全部四个阶段，附带变更摘要 |

> **核心原则**：
> - 遇到不明确或不确定的情况时，**直接询问用户**。
> - 更新时保留 Skill 的原有风格和结构；仅修改与变更相关的内容。
> - 不编造信息；所有更新必须有文档或代码作为依据。

---

## 阶段 1：变更检测

**目标**：识别映射表支持的来源（`docs/cn/`、`src/` 下的关键目录、`config/system.yaml`、`config/llm.yaml`）中的变更。

> **一致性规则**：阶段 1 的变更来源 = `references/doc-skill-mapping.md` 支持的来源。两者必须保持同步，以防遗漏检测或更新。

### 1.0 根目录校验（Fail-Fast，先做）

在进入阶段 1 的任何检测命令前，先校验当前目录是否为 AgentLoom 根目录：

```bash
# 在当前目录执行（不要先 cd 到其他目录）
pwd
test -f config/llm.yaml
```

- 如果 `test -f config/llm.yaml` 失败：**立即停止**，先 `cd <project_root>` 到包含 `config/llm.yaml` 的目录，再继续阶段 1。
- 通过后再执行后续检测命令，避免在子目录误检。

### 1.1 通过 Git 检测文档变更

执行以下命令获取变更文件列表和 diff 摘要：

```bash
# 在项目根目录下执行
cd <project_root>

# 查看 docs/cn/ 的最近变更历史
git log --oneline -20 -- docs/cn/

# 查看 docs/cn/ 自上次更新以来的 diff（基于 commit 或 tag）
# 方法 1：与特定 commit 比较
git diff <base_commit>..HEAD -- docs/cn/

# 方法 2：与上次已知更新点比较
git diff HEAD~N..HEAD -- docs/cn/

# 方法 3：查看哪些文件发生了变更
git diff --name-only <base_commit>..HEAD -- docs/cn/
```

### 1.2 通过 Git 检测代码变更

```bash
# 查看与 Skills 相关的代码和配置变更
git diff --name-only <base_commit>..HEAD -- \
  src/lib/config/ \
  src/lib/smolagents/skills/ \
  src/lib/smolagents/hooks/ \
  src/lib/smolagents/agent/ \
  src/lib/smolagents/models/ \
  src/tools/ \
  config/system.yaml \
  config/llm.yaml
```

### 1.3 确定变更基线

变更基线选择策略（按优先级排序）：

1. **用户指定的 commit / tag**：用户说"从 xxx 以来的变更"
2. **上次更新 Skills 的 commit**：通过 `git log --oneline -5 -- tools/skills-cn/` 查找上次修改点
3. **最近 N 个 commits**：当用户说"最近的变更"时，默认比较最近 3-5 个 commits

### 1.4 输出：变更文件列表

```markdown
## 变更检测结果

### 文档变更
| 文件 | 变更类型 | 关键变更摘要 |
|------|----------|-------------|
| docs/cn/agent_config.md | 修改 | 重构为完整参考，新增字段速查表和配置覆盖关系 |
| docs/cn/skills_config.md | 新增 | 全新的 Skills 配置完整参考文档 |
| ... | ... | ... |

### 代码变更
| 文件 | 变更类型 | 影响的功能 |
|------|----------|-----------|
| src/lib/config/config.py | 修改 | 配置加载逻辑、覆盖规则 |
| ... | ... | ... |
```

---

## 阶段 2：影响分析

**目标**：根据变更定位受影响的 Skills 和具体文件。

### 2.1 加载映射表

读取 `references/doc-skill-mapping.md`，获取完整的"文档/代码 → Skill 文件"映射。

### 2.2 匹配受影响的 Skills

对于阶段 1 中检测到的每个变更文件，在映射表中查找受影响的 Skills 和具体文件：

- 默认范围：`tools/skills-cn/` 下的所有 Skills（包括未来新增的 Skills）
- 例外：`tools/skills-cn/update-skills/` 不是更新目标

```markdown
## 影响分析结果

### 受影响的 Skills

| Skill | 受影响的文件 | 影响来源 | 预估影响范围 |
|-------|-------------|---------|-------------|
| create-app | SKILL.md | docs/cn/agent_config.md 重构 | 模板和字段描述需要更新 |
| create-app | references/quick-reference.md | docs/cn/system_config.md 变更 | 工具列表/约束需要同步 |
| create-skill | SKILL.md | docs/cn/skills_config.md 新增 | 需要验证引用的章节编号 |
| ... | ... | ... | ... |
```

### 2.3 交互确认（交互模式）

向用户展示影响分析结果，确认更新范围：

```
以下 Skills 需要更新，是否确认？
1. create-app — SKILL.md, references/quick-reference.md, references/templates.md
2. create-skill — SKILL.md, references/skill-template.md
3. workflow-review — references/system-tools.md

[全部更新 / 选择性更新 / 取消]
```

---

## 阶段 3：逐 Skill 更新

**目标**：对每个受影响的 Skill 文件执行更新。

### 3.1 更新工作流（逐文件）

```
对每个受影响的文件：
  0. 如果文件位于 tools/skills-cn/update-skills/ 下，则跳过（该目录不进行写回）
  1. 读取 Skill 文件的当前内容
  2. 读取对应的最新 docs/cn/ 文档内容（变更后版本）
  3. 识别 Skill 文件中需要更新的具体章节
  4. 执行更新：
     - 仅修改与变更相关的内容
     - 保留文件的原有结构和风格
     - 不改动未受影响的章节
  5. 记录更新摘要
```

### 3.2 SKILL.md 更新策略

| 更新项 | 检查内容 | 操作 |
|--------|---------|------|
| frontmatter.description | 文档中的功能描述是否变更 | 同步更新描述文本 |
| 适用/不适用场景 | 文档中是否新增/删除了功能 | 增加/删除对应条目 |
| 信息提取检查清单 | 文档中是否新增/修改了配置字段 | 同步更新检查清单项 |
| 解决方案模板 | 文档中模板格式/字段是否变更 | 同步更新 YAML 模板 |
| 文档引用路径 | 文档文件名/位置是否变更 | 修正引用链接 |
| 章节编号引用 | 文档章节是否重新编号 | 修正章节编号 |

### 3.3 references/*.md 更新策略

| 文件类型 | 更新重点 |
|---------|---------|
| quick-reference.md | 工具列表、model_type 规则、execution_env 选项、约束检查清单、覆盖允许列表 |
| templates.md | YAML 模板中的字段名、默认值和注释说明 |
| troubleshooting.md | 错误消息文本、排障步骤、验证脚本 |
| skill-template.md | SKILL.md frontmatter 字段、Hook 事件列表、invocation-control 配置 |
| hook-scripts-guide.md | 环境变量列表、输出 JSON 格式、决策值、退出码规则 |
| system-tools.md | 工具发现工作流、配置字段路径 |
| review-checklist.md | 审查维度和检查清单项 |
| best-practices.md | 设计模式和最佳实践 |

### 3.4 更新原则

1. **最小变更**：仅修改与文档/代码变更直接相关的内容；不进行无关的重构
2. **保持一致性**：使用与原文件相同的术语、格式风格和标题层级
3. **有据可依**：每个修改都必须能追溯到具体的文档/代码变更
4. **不遗漏引用**：如果某个概念在 Skill 的多处被引用，所有引用都必须更新
5. **保留上下文**：更新表格、代码块等结构时，保持上下文完整性

---

## 阶段 4：一致性验证与汇总

### 4.1 更新后验证

对每个已更新的文件执行检查：

```markdown
### 验证检查清单
- [ ] 更新目标仅包含 tools/skills-cn/ 下的业务 Skills（排除 tools/skills-cn/update-skills/）
- [ ] SKILL.md 中引用的 docs/cn/ 章节编号正确
- [ ] references/*.md 中的配置字段名与最新文档一致
- [ ] YAML 模板中的字段和默认值与最新规范一致
- [ ] 工具列表反映了最新的 config/system.yaml
- [ ] Hook 事件名称/数量与最新的 skills_config.md 一致
- [ ] 错误消息和排障步骤反映了最新的代码行为
- [ ] 所有内部交叉引用（Skills 之间、references 之间）为有效链接
- [ ] 默认验证命令 `./run_tests.sh tests/skills_test` 已执行并通过
```

### 4.2 测试验证范围（默认）

- 默认仅执行 Skills 模块测试：`./run_tests.sh tests/skills_test`
- 仅当用户明确要求时，才执行全量测试：`./run_tests.sh`

### 4.3 输出更新报告

```markdown
# Skills 更新报告

## 变更基线
- 基线 commit: <base_commit_hash>
- 当前 commit: <head_commit_hash>
- 检测到文档变更: N 个文件
- 检测到代码变更: M 个文件

## 更新摘要

### create-app
| 文件 | 更新内容 | 状态 |
|------|---------|------|
| SKILL.md | 更新了 XXX 章节 | ✅ 完成 |
| references/quick-reference.md | 同步了工具列表 | ✅ 完成 |

### create-skill
| 文件 | 更新内容 | 状态 |
|------|---------|------|
| SKILL.md | 更新了信息提取检查清单 | ✅ 完成 |

### workflow-review
| 文件 | 更新内容 | 状态 |
|------|---------|------|
| references/system-tools.md | 更新了配置路径 | ✅ 完成 |

## 一致性验证
- 全部通过 / X 项需要人工确认

## 备注
- <任何需要用户注意的特殊情况>
```

---

## 附录：快速使用示例

### 示例 1：文档变更后更新

```
用户：docs/cn 的文档改了，帮我更新 tools/skills-cn 下的所有 skills
AI：（加载 update-skills）→ 检测变更 → 分析影响 → 逐个更新 → 输出报告
```

### 示例 2：定向更新

```
用户：agent_config.md 重写了，只更新 create-app skill
AI：（加载 update-skills）→ 仅检测 agent_config.md → 仅更新 create-app → 输出报告
```

### 示例 3：代码变更后更新

```
用户：src/lib/config/ 的配置逻辑改了，skills 需要同步
AI：（加载 update-skills）→ 检测代码变更 → 分析影响（覆盖规则等）→ 更新相关 references → 输出报告
```
