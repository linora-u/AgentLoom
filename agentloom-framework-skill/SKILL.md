---
name: agentloom-framework-skill
description: "当用户需要理解、开发、扩展或验证 AgentLoom 框架能力时使用。覆盖创建/扩展 Application、设计单 Agent 或多 Agent、编写 Agent/Worker YAML、实现 Tool、创建私有 Skill 或 Hook、更新 README、验证 YAML/结构/运行结果。也适用于用户问“这个功能用 AgentLoom 怎么实现”。"
allowed-tools: "Read, Write, Edit, Bash, Grep, Glob"
argument-hint: "<AgentLoom task or application path>"
---

# AgentLoom Framework Skill

本 Skill 是 AgentLoom 仓库的框架级入口，封装开发 AgentLoom 应用和扩展框架能力所需的上下文。一个入口 Skill 负责路由，细节放到按需 reference；不要把规则拆回多个互相竞争的 Skill。

## 前置条件

- 先进入 AgentLoom 根目录。运行时代码用 `pyproject.toml` 中 `[project].name == "AgentLoom"` 发现项目根；本 Skill 的操作前置检查额外要求 `config/llm.yaml` 存在，因为它是被忽略的本地模型配置，也是生成/验证 Application 前必须确认的环境条件。不要只用 `config/system.yaml` 判定环境可用。
- 新建 worktree 或干净 checkout 后先检查 `config/llm.yaml`；该文件通常被 `.gitignore` 忽略，不会随 worktree 自动生成。缺失时从同机可信工作区复制，或让用户提供本地配置；不要提交该文件，也不要凭空生成模型配置。
- 当前本地环境可能没有 `uv`；验证优先用 `.venv/bin/python` 和 `.venv/bin/loom`。
- 写 Application 前先读真实仓库结构与 `config/llm.yaml`，`model_type` 只能来自项目配置。
- 如果用户目标不清晰，先问清“功能目标、输入、输出、验收标准”；不要为了显得完整而发明需求。

## 快速决策

- 用户说“帮我实现一个功能 / 用框架做一个功能 / 创建一个应用 / 扩展框架能力”：先读 [`references/function-routing.md`](references/function-routing.md)，判断是新建 Application、扩展现有 Application、加 Worker、加 Tool、加私有 Skill、加 Hook，还是只改文档。
- 用户说“有哪些配置 / 这个配置能不能写 / skill 漏了配置 / system.yaml、llm.yaml、Agent YAML 怎么配”：先读 [`references/configuration-surface.md`](references/configuration-surface.md)，再按需要读 `docs/en/*.md` 和对应代码交叉验证。
- 用户说“shell 权限怎么配 / audit log 看什么 / 安全策略有没有生效 / sandbox 怎么验证”：先读 [`references/shell-security-audit.md`](references/shell-security-audit.md)，再按需要读 [`references/configuration-surface.md`](references/configuration-surface.md) 和 [`references/validation-and-review.md`](references/validation-and-review.md)。
- 需要生成或修改 `applications/<app_name>/`：读 [`references/application-generation.md`](references/application-generation.md)。
- 需要写 Agent YAML / Worker YAML / `agent_function_schema` / `worker_agents`：读 [`references/yaml-contract.md`](references/yaml-contract.md)。
- 需要为 Application 配置或创建私有 Skill：读 [`references/configuration-surface.md`](references/configuration-surface.md) 的 Skills/Hook 配置，再读 [`references/application-generation.md`](references/application-generation.md) 的目录规范。
- 需要验证是否真是多 Agent、是否能运行、问题怎么记录：读 [`references/validation-and-review.md`](references/validation-and-review.md)。
- 修改 checkpoint、resume、日志/维测、并发 Worker、文件回滚、`loom list-tasks` 或 `loom clean-tasks` 这类框架运行时能力：读 [`references/validation-and-review.md`](references/validation-and-review.md) 的“框架运行时功能验证”，并用真实 Application 跑功能路径。
- 需要写 README 或验证记录：读 [`references/readme-template.md`](references/readme-template.md)。
- 需要看一个按本 Skill 创建的简单多 Agent 示例：参考 `applications/feature_planner_demo/README.md`。
- 只有当规则会跨多个 Application 复用、需要 Hook、或必须长期注入领域协议时，才创建新的私有 Skill；否则不要创建 Skill。

## 模块地图

| 模块 | 处理什么问题 | 必读 reference | 输出 |
|---|---|---|---|
| 需求路由 | 用户只说一个功能，判断 AgentLoom 应该怎么落地 | `function-routing.md` | 实现路径、需要问的问题、应用边界 |
| 配置面 | 判断 system/llm/Agent/Skill/Hook/MCP/checkpoint 等配置写在哪里、是否可覆盖 | `configuration-surface.md` | 配置位置、字段、覆盖层级、验证来源 |
| Application 生成 | 新建/扩展 `applications/<app_name>` | `application-generation.md` | 目录、入口脚本、workflow、README |
| YAML 契约 | Supervisor/Worker/Tool/Skill 配置 | `yaml-contract.md` | 合法 Agent YAML 与 Worker schema |
| 验证评审 | 结构扫描、YAML 校验、运行边界、checkpoint/resume 功能、架构风险 | `validation-and-review.md` | 验证命令、结果、问题清单 |
| README 交付 | 给用户和后续 Agent 看的使用说明 | `readme-template.md` | 可运行说明、验证记录、已知问题 |

## 执行协议

1. 先判断任务类型。目标清晰但路径绕远时，直接建议更短路径。
2. 先做确定性扫描，再设计 Agent。不要凭印象写路径、工具名、模型名或配置字段。
3. 能用 Python 确定性完成的放进 `agent_tools/*.py`；需要推理、判断、写作的放进 Agent workflow。
4. 单职责用单 Agent；多个可独立验收阶段才用 Supervisor + N Worker。
5. Application 内容必须落到 `applications/<app_name>/`；框架级 Skill/Hook 扩展按真实加载路径落盘；README 必须同步写验证记录。
6. 写配置前先确认配置归属：LLM 参数只进 `config/llm.yaml`；应用行为优先用 `applications/<app>/config/system.yaml` 或 Agent YAML 白名单字段；Skill hooks 只写在 `SKILL.md` frontmatter。
7. Shell 权限不确定时，先跑真实 workflow 生成 `shell_audit.log`，review `POLICY_SNAPSHOT` 与拦截事件，再按需收敛 `allowed_commands`、`allowed_operators`、路径规则或 sandbox。
8. 最后必须运行验证命令。能跑到哪一步就记录到哪一步，不把“未执行”说成“通过”。

## 命令速查

```bash
# 根目录校验
pwd
test -f config/llm.yaml
# 新 worktree 缺失时，先确认它是否为被忽略的本地配置
git check-ignore -v config/llm.yaml || true

# YAML 契约校验
.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py \
  --app-root applications/<app_name>

# Application 结构扫描
.venv/bin/python -c "
import sys
sys.path.insert(0, 'agentloom-framework-skill')
from scripts.scan_tools import scan_app_structure
print(scan_app_structure('applications/<app_name>'))
"

# Python 编译校验
.venv/bin/python -m py_compile applications/<app_name>/<app_name>_app.py

# 直接运行 YAML（首选运行入口；会触发真实模型调用和外部工具）
.venv/bin/loom run applications/<app_name>/workflows/<app_name>_agent.yaml

# 入口脚本生成链路校验
.venv/bin/loom create applications/<app_name>/workflows/<app_name>_agent.yaml \
  -o /tmp/<app_name>_generated_app.py
```

## 安全与边界

- 不要修改用户已有未相关变更，不要为了合并框架 Skill 回滚业务文件。
- `loom run` 会触发真实模型调用和外部工具；执行前确认成本/权限/输入是否安全。不能执行时，把原因写进 README 的验证记录。
- 删除、覆盖、迁移旧目录前先确认当前目标是否要求这么做；合并 Skill 时可以删除旧重复 Skill，但不要删除业务应用。

## 输出规范

最终给用户的回复只保留决策相关事实：

- 创建/修改了哪些文件。
- 为什么这样拆 Agent/Tool。
- 验证命令和结果。
- 仍未验证或失败的部分，以及根因。
