---
name: agentloom-framework-skill
description: "当用户需要理解、开发、扩展或验证 AgentLoom 框架能力时使用。覆盖创建/扩展 Application、设计单 Agent 或多 Agent、编写 Agent/Worker YAML、实现 Tool、创建私有 Skill 或 Hook、更新 README、验证 YAML/结构/运行结果。也适用于用户问“这个功能用 AgentLoom 怎么实现”。"
version: "1.1.0"
allowed-tools: "Read, Write, Edit, Bash, Grep, Glob"
metadata:
  requires:
    bins: [".venv/bin/python", ".venv/bin/loom"]
  cliHelp: ".venv/bin/loom --help"
---

# AgentLoom Framework Skill

本 Skill 是 AgentLoom 仓库的框架级入口，封装开发 AgentLoom 应用和扩展框架能力所需的上下文。一个入口 Skill 负责路由，细节放到按需 reference；不要把规则拆回多个互相竞争的 Skill。

## 前置条件

- 先进入 AgentLoom 根目录，根目录判定只看 `config/llm.yaml`，不要用 `config/system.yaml`。
- 当前本地环境可能没有 `uv`；验证优先用 `.venv/bin/python` 和 `.venv/bin/loom`。
- 写 Application 前先读真实仓库结构与 `config/llm.yaml`，`model_type` 只能来自项目配置。
- 如果用户目标不清晰，先问清“功能目标、输入、输出、验收标准”；不要为了显得完整而发明需求。

## 快速决策

- 用户说“帮我实现一个功能 / 用框架做一个功能 / 创建一个应用 / 扩展框架能力”：先读 [`references/function-routing.md`](references/function-routing.md)，判断是新建 Application、扩展现有 Application、加 Worker、加 Tool、加私有 Skill、加 Hook，还是只改文档。
- 需要生成或修改 `applications/<app_name>/`：读 [`references/application-generation.md`](references/application-generation.md)。
- 需要写 Agent YAML / Worker YAML / `agent_function_schema` / `worker_agents`：读 [`references/yaml-contract.md`](references/yaml-contract.md)。
- 需要验证是否真是多 Agent、是否能运行、问题怎么记录：读 [`references/validation-and-review.md`](references/validation-and-review.md)。
- 需要写 README 或验证记录：读 [`references/readme-template.md`](references/readme-template.md)。
- 需要看一个按本 Skill 创建的简单多 Agent 示例：参考 `applications/feature_planner_demo/README.md`。
- 只有当规则会跨多个 Application 复用、需要 Hook、或必须长期注入领域协议时，才创建新的私有 Skill；否则不要创建 Skill。

## 模块地图

| 模块 | 处理什么问题 | 必读 reference | 输出 |
|---|---|---|---|
| 需求路由 | 用户只说一个功能，判断 AgentLoom 应该怎么落地 | `function-routing.md` | 实现路径、需要问的问题、应用边界 |
| Application 生成 | 新建/扩展 `applications/<app_name>` | `application-generation.md` | 目录、入口脚本、workflow、README |
| YAML 契约 | Supervisor/Worker/Tool/Skill 配置 | `yaml-contract.md` | 合法 Agent YAML 与 Worker schema |
| 验证评审 | 结构扫描、YAML 校验、运行边界、架构风险 | `validation-and-review.md` | 验证命令、结果、问题清单 |
| README 交付 | 给用户和后续 Agent 看的使用说明 | `readme-template.md` | 可运行说明、验证记录、已知问题 |

## 执行协议

1. 先判断任务类型。目标清晰但路径绕远时，直接建议更短路径。
2. 先做确定性扫描，再设计 Agent。不要凭印象写路径、工具名、模型名。
3. 能用 Python 确定性完成的放进 `agent_tools/*.py`；需要推理、判断、写作的放进 Agent workflow。
4. 单职责用单 Agent；多个可独立验收阶段才用 Supervisor + N Worker。
5. Application 内容必须落到 `applications/<app_name>/`；框架级 Skill/Hook 扩展按真实加载路径落盘；README 必须同步写验证记录。
6. 最后必须运行验证命令。能跑到哪一步就记录到哪一步，不把“未执行”说成“通过”。

## 命令速查

```bash
# 根目录校验
pwd
test -f config/llm.yaml

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
