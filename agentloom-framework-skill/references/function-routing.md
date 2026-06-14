# AgentLoom 需求路由

当用户说“用 AgentLoom 实现/扩展某个能力”时，先把自然语言需求路由成框架动作。不要一上来写 YAML。

## 必问/必推断的四件事

| 信息 | 为什么需要 | 缺失时怎么处理 |
|---|---|---|
| 功能目标 | 决定 Application 或框架扩展边界 | 目标含糊就问用户 |
| 输入 | 决定入口脚本参数和 Tool | 可从文件/URL/目录推断时先验证 |
| 输出 | 决定 README、产物路径、最终回答 | 不清楚就问“最终要看到什么” |
| 验收标准 | 决定验证命令 | 没有验收标准就先定义最小可验证标准 |

## 路由表

| 用户诉求 | 框架动作 | 落点 |
|---|---|---|
| “做一个新功能/新工具/新流程” | 新建 Application | `applications/<app_name>/` |
| “在现有应用里加一步/加能力” | 扩展现有 Application | 现有 `workflows/`、`agent_tools/`、README |
| “这个阶段需要一个专家 Agent” | 新增 Worker | `workflows/worker_agents/<worker>.yaml` |
| “要读文件/查数据/写产物/调用 API” | 新增普通 Python Tool | `agent_tools/<module>.py` |
| “批量处理很多项，每项要 Agent 判断” | Python 包装 Agent Tool | `agent_tools/` 调 `YamlAgentFactory.create_agent_as_tool()` |
| “这个规则只服务当前应用/验证场景” | 应用私有 Skill | `applications/<app_name>/skills/<skill_name>/SKILL.md` |
| “这个规则以后很多应用都要用” | 全局 Skill | 确实通用再放全局 runtime skill |
| “要在工具调用/任务生命周期前后做统一处理” | 新增或扩展 Hook Skill | 优先应用私有 Skill，确实通用再全局 |
| “要改框架能力/运行时/工具加载机制” | 框架源码修改 | `src/`、`config/`、`docs/`，先定位真实调用链 |
| “只要说明怎么用/补文档” | README/docs 修改 | 不创建 Agent |
| “现有应用跑不通/架构不合理” | 验证评审 | 先扫描，再修根因 |

## 单 Agent / 多 Agent 判断

优先单 Agent：

- 只有一个职责。
- 不需要分阶段交付。
- 不需要不同工具/模型/权限。

使用 Supervisor + Worker：

- 需求天然有多个阶段，例如“需求分析 -> 实现方案 -> 审核报告”。
- 阶段之间输出可独立验收。
- Worker 需要不同工具、模型、并发或权限。
- 批处理需要同一个 Worker 被多次调用。

不要为了“像多 Agent”而拆。拆分后每个 Worker 都必须回答：输入是什么、输出是什么、失败后怎么定位。

## 功能落地顺序

1. 确认目标和验收标准。
2. 找相似现有 Application、Skill、Hook 或框架模块。
3. 画出职责边界：Python Tool 做确定性，Agent 做推理。
4. 选择单 Agent 或 Supervisor + Worker。
5. 写对应文件：Application、Tool、Skill、Hook、框架源码或文档。
6. 写 README/docs 的运行与验证记录。
7. 运行校验，失败则修根因。
