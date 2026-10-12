---
name: agentloom-framework-skill
description: "当用户需要理解、开发、扩展或验证 AgentLoom 框架能力时使用。覆盖创建/扩展 Application、设计单 Agent 或多 Agent、编写 Agent/Worker YAML、实现 Tool、创建私有 Skill 或 Hook、更新 README、验证 YAML/结构/运行结果。也适用于用户问“这个功能用 AgentLoom 怎么实现”。"
---

# AgentLoom Framework Skill

本 Skill 是 AgentLoom 仓库的框架级入口，封装开发 AgentLoom 应用和扩展框架能力所需的上下文。一个入口 Skill 负责路由，细节放到按需 reference；不要把规则拆回多个互相竞争的 Skill。

## 前置条件

- 先进入 AgentLoom 根目录。运行时代码用 `pyproject.toml` 中 `[project].name == "AgentLoom"` 发现项目根。模型来自项目及 Agent 所属应用的 `config/model.yaml`；项目模型文件可省略，但有效模型目录必须包含实际所需类型。
- 新建 worktree 或干净 checkout 后检查实际使用的模型配置。含凭据的本地文件可能被 `.gitignore` 忽略；需要项目级模型时从同机可信工作区复制，或让用户提供配置，不要提交密钥或凭空生成凭据。
- 当前本地环境可能没有 `uv`；验证优先用 `.venv/bin/python` 和 `.venv/bin/loom`。
- 框架 Python 导入统一使用 `agentloom.*`；职责模块的物理源码直接位于 `src/app/`、`src/execution/`、`src/runtimes/`、`src/integrations/` 等目录，由安装配置映射为 `agentloom`。不要新增 `src.*` 导入或旧路径转发。
- 写 Application 前先读真实仓库结构、项目和本应用的 `config/model.yaml`；`model_type` 必须来自框架合并后的模型目录，同名应用模型整条覆盖项目模型。
- 如果用户目标不清晰，先问清“功能目标、输入、输出、验收标准”；不要为了显得完整而发明需求。

## 快速决策

- 用户说“帮我实现一个功能 / 用框架做一个功能 / 创建一个应用 / 扩展框架能力”：先读 [`references/function-routing.md`](references/function-routing.md)，判断是新建 Application、扩展现有 Application、加 Worker、加 Tool、加私有 Skill、加 Hook，还是只改文档。
- 用户说“有哪些配置 / 这个配置能不能写 / skill 漏了配置 / system.yaml、model.yaml、Agent YAML 怎么配”：先读 [`references/configuration-surface.md`](references/configuration-surface.md)，再按需要读 `docs/en/*.md` 和对应代码交叉验证。
- 需要生成或修改 `applications/<app_name>/`：读 [`references/application-generation.md`](references/application-generation.md)。
- 需要写 Agent YAML / Worker YAML / `input_schema` / `output_schema` / `worker_agents`：读 [`references/yaml-contract.md`](references/yaml-contract.md)。
- 需要配置或实现 Goal Mode、长期 continuation 或 checkpoint resume：读 [`references/yaml-contract.md`](references/yaml-contract.md)
  的 Goal 契约和 [`references/validation-and-review.md`](references/validation-and-review.md)
  的真实 Goal 验证矩阵；用户语义以 `docs/en/goal_mode.md` 为准。
- 需要为 Application 配置私有 Skill 或独立 Hook Bundle：读 [`references/configuration-surface.md`](references/configuration-surface.md) 的 Skills/Hook 配置，再读 [`references/application-generation.md`](references/application-generation.md) 的目录规范。
- 需要配置或验证 shell 权限、allowlist、audit log、sandbox、路径安全、后台任务或 stall 检测：先读 [`references/shell-security-audit.md`](references/shell-security-audit.md)，再按需要读配置面和验证评审。
- 需要验证是否真是多 Agent、是否能运行、问题怎么记录：读 [`references/validation-and-review.md`](references/validation-and-review.md)。
- 修改 ContextEngine/压缩、checkpoint、resume、run-scoped 日志/维测、并发 Worker、文件回滚、`loom list-tasks`、`loom clean-tasks` 或 `loom clean-runtime` 这类框架运行时能力：读 [`references/validation-and-review.md`](references/validation-and-review.md) 的“框架运行时功能验证”，并用真实 Application 跑功能路径。
- 需要写 README 或验证记录：读 [`references/readme-template.md`](references/readme-template.md)。
- 需要看一个按本 Skill 创建的简单多 Agent 示例：参考 `applications/feature_planner_demo/README.md`。
- 只有当规则必须长期注入领域协议时才创建 Skill；确定性事件行为应创建独立 Hook Bundle，不要用 Skill 承载 Hook。

## 模块地图

| 模块 | 处理什么问题 | 必读 reference | 输出 |
|---|---|---|---|
| 需求路由 | 用户只说一个功能，判断 AgentLoom 应该怎么落地 | `function-routing.md` | 实现路径、需要问的问题、应用边界 |
| 配置面 | 判断 system/llm/Agent/Skill/Hook/MCP/checkpoint 等配置写在哪里、是否可覆盖 | `configuration-surface.md` | 配置位置、字段、覆盖层级、验证来源 |
| Application 生成 | 新建/扩展 `applications/<app_name>` | `application-generation.md` | 目录、入口脚本、workflow、README |
| YAML 契约 | Supervisor/Worker/Goal/Tool/Skill 配置 | `yaml-contract.md` | 合法 Agent YAML、Goal 生命周期与 Worker schema |
| 验证评审 | 结构扫描、YAML 校验、运行边界、checkpoint/resume 功能、架构风险 | `validation-and-review.md` | 验证命令、结果、问题清单 |
| README 交付 | 给用户和后续 Agent 看的使用说明 | `readme-template.md` | 可运行说明、验证记录、已知问题 |

## 执行协议

1. 先判断任务类型。目标清晰但路径绕远时，直接建议更短路径。
2. 先做确定性扫描，再设计 Agent。不要凭印象写路径、工具名、模型名或配置字段。
3. 能用 Python 确定性完成的放进 `agent_tools/*.py`；需要推理、判断、写作的放进 Agent workflow。
4. 单职责用单 Agent；多个可独立验收阶段才用 Supervisor + N Worker。
5. Application 内容必须落到 `applications/<app_name>/`；框架级 Skill/Hook 扩展按真实加载路径落盘；README 必须同步写验证记录。
6. 写配置前先确认配置归属：LLM 参数只进项目或所属应用的 `config/model.yaml`；应用行为优先用 `applications/<app>/config/system.yaml` 或 Agent YAML 白名单字段；Skill 不得声明 Hook，Hook 只通过顶层 `hooks:` 或显式 `HOOK.yaml` Bundle 接入。
7. 新增或改变任何可配置字段时，必须同步更新 `references/configuration-surface.md` 和相关 YAML 契约；优先精简配置，只暴露用户确实需要调的字段，不为了“完整”增加开关。
8. 框架功能不要新增兼容桥、旧字段回退或第二套路径；如果契约需要变化，直接改主路径、配置白名单、文档和验证。
9. 设计或修改框架功能时必须先写验证矩阵：单测证明局部规则，真实 Application 证明完整运行链路；涉及上下文压缩时必须验证压缩后能按 `ContextRef` retrieve 原文，涉及 resume 时必须验证恢复后旧 ref 仍可 retrieve。
10. 代码编写完成后不能只跑单测或 YAML 校验。必须按改动风险选择多条真实 Application 跑功能验证；现有 Application 覆盖不足时，新增最小验证 Application，保留在 `applications/<validation_app>/`，让后续 Agent 可复跑。
11. 每个真实 Application 跑完后都要读 `.agentloom/runs/<application_id>/<run_id>/manifest.json`、`logs/runtime.log`、`audit/shell.jsonl` 和关键产物，再判断是否符合预期；不要只看进程退出码或 LLM final answer。验证记录至少包含：命令、退出码、`task_id`/`run_id`、final answer、关键 tool/worker 调用、日志错误/警告、产物或 checkpoint/context/audit 证据。
12. 如果用户不确定 shell 权限怎么配，先用隔离 runtime 跑真实 workflow，读取当前 run 的 `audit/shell.jsonl` 里的 `[POLICY_SNAPSHOT]` 和拦截事件，再收敛 `allowed_commands`、`allowed_operators`、路径规则或 sandbox。
13. 最后必须运行验证命令。能跑到哪一步就记录到哪一步，不把“未执行”说成“通过”。

## 静态预检

Application 定义检查和 Run 准备共用 `agentloom.app.definition` 的完整拓扑预检。
它解析 Supervisor 与所有 Worker 的 Skill 目录、校验 frontmatter 和同层重名，
在分配 Run 前拒绝静态错误。运行复用已解析的 Skill 正文；新的检查或调用重新读取。
静态检查不创建模型、连接 MCP、执行 Hook 或加载工具实现。

## 定义发现

结构扫描和 YAML 校验共用 `agentloom.app.definition` 的递归发现规则：
`workflows/` 下所有 YAML Agent 定义都会被发现；相对路径中包含
`worker_agents` 目录段的定义按 Worker 处理，其余按 Supervisor 处理。
发现过程不跟随 symlink。YAML 校验随后复用共享定义预检，覆盖嵌套 Supervisor、
未引用 Worker 与完整 Worker 引用图。

## 命令速查

```bash
# 根目录校验
pwd
test -f pyproject.toml
# 需要项目模型时检查被忽略的本地配置；应用可仅使用自己的 config/model.yaml
git check-ignore -v config/model.yaml || true

# 共享定义与 Effective Config 预检（含 Worker 引用图）
.venv/bin/python agentloom-framework-skill/scripts/validate_application_yaml.py \
  --app-root applications/<app_name>

# Application 结构扫描（复用共享 YAML 定义解析器）
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

# 查看真实 run 证据；file logging 默认开启，单次关闭用 --no-file-log
manifest=$(find .agentloom/runs -name manifest.json -type f -print | sort | tail -1)
run_dir=$(dirname "$manifest")
sed -n '1,160p' "$manifest"
tail -n 100 "$run_dir/logs/runtime.log"
tail -n 100 "$run_dir/audit/shell.jsonl"

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
