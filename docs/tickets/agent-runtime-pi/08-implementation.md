# 08 实施与验证记录

08 的平台工具解耦已实现；交付状态与最终测试结果见 [验证记录](08-validation.json)，逐工具边界见 [工具清单](08-tool-inventory.md)。本票不宣称 Pi 的生产工具回调已接通，该能力由 09 验收。

## 实现

- Goal 工具移除 smol decorator，使用普通类型函数和中立绑定；原根目标权限、完成证据、记忆范围与审核规则不变，不增加必填 YAML。
- MCP 发现直接产生中立 ToolBinding，保留完整 JSON Schema、提供方与可见名称。错误仍分类为 mcp_error，结构化结果和多内容协议块保留，不引入 smol 媒体类型。
- MCP 每个连接拥有 AnyIO portal；重复连接/关闭、重新连接、旧绑定失效、双实例隔离、启动超时、执行中取消均用真实 stdio 服务验证。取消会结束等待调用并关闭服务进程。
- LSP 从 Application 的无条件预热改为选定工具首次调用时启动。管理器绑定完整 RuntimeKey 与 Worker 实例，资源关闭捕获实际句柄；启动失败也释放句柄，重复语言配置在启动前拒绝。
- 修复 Worker 跟踪包装把实例 ID 替换成共享名称的问题；子任务上下文携带真实实例 ID，避免并行 Worker 的资源串用。
- SkillCatalog、激活、提案、memory/history 与 ContextRef 复用既有中立入口。专业 AST/LSP/大纲/Markdown 保留显式选择；smol 基础工具与 Todo 的迁移属于 04。

共享接线已在本票串行集成：factory 识别中立 MCP 名称并处理发现冲突清理；ToolBinding 增加可选 input_validator，在 Hook 修正后的最终解码阶段校验完整 MCP schema，先于 Guard、历史和输入记录；smol proxy 只补本地 SDK 展示所需类型，模型仍接收原始定义。04/05/07 已验收基线 48de1512 合入后，复核这些接线仍生效。没有复制另一套权限或记忆规则。

## 提交与环境

分支 codex/pi-t08-platform 起始于 9d5a88915efbd402d0f2697318125933fc1500e0。分阶段提交保留，未压成一个提交：

| 提交 | 内容 |
| --- | --- |
| 4d8063f1 | Goal 构造解耦 |
| 6cdfaec0 | 中立 MCP 与取消/连接生命周期 |
| 234b739b | 实例级 LSP 资源与 Worker 身份 |
| 105fb1a9 | 独立真实 Application 验证入口 |
| 2072720b | 完整 schema 校验顺序、LSP 初始化清理 |
| 8115d855 | 验证场景修正与真实引用 schema |
| cbea7fa9 | LSP 重复语言配置检查 |
| c9746f29 | 实际模型 profile 交叉验证 |
| f0087265 | 合入 04/05/07 的已验收代码 |
| 1df8a983 | 工具归属与 09 交接清单 |

独立 worktree 使用自己的 Python 3.12.13、锁定依赖和 Node 25.9.0。config/llm.yaml 从主工作区复制，权限 0600 且被 Git 忽略。所有真实应用使用新的私有工作目录，原始日志与凭证不进入本报告。Pi bridge 使用锁文件安装并通过 TypeScript build。

## 验收方法与范围

确定性验证通过实际 YAML / execute_app / Gateway 入口执行 Goal、Skill 提案、记忆、ContextRef、专业工具与多 Worker；模型执行边界使用中立 runtime fixture，MCP 使用真实 stdio 进程。测试检查工具实际输入、产物、隔离与清理，不只检查最终回答。LSP 的跨 Worker 生命周期测试控制语言服务边界；真实 provider 验证另行启动 Python Language Server 并断言输出来源为 LSP。

另外建立实际不安装 smol SDK 和其 instrumentation 的 Python 环境：平台/专业工具与中立 Application 定向验证 34 passed，4 项明确依赖 SDK 的投影兼容测试跳过。这证明本票执行路径不依赖 smol；不替代 13 的发行安装 profile 验收。

真实模型覆盖 22 种平台/专业工具应用场景，另复验合并后的 Pi 无工具 Application。全部尝试（包括失败）汇总在验证记录；原始证据保留于仓库外 AgentLoom-validation/t08。组合提交 1df8a983 上 8 个平台应用和 1 个 Pi 应用均通过。

**真实 provider 限制保留：** skill_manage、ContextRef 检索和原始 Markdown 写入曾连续漏传可选参数，在两个模型 profile 下复现。前两项也有真实成功记录；raw Markdown 的真实模型场景尚无成功记录，中立 Application 的实际写入验证已通过。HTTP 边界取证显示请求包含完整 content 字段，响应已经缺失这些参数，LiteLLM 和 Gateway 没有在回传中删字段。具体上游原因未确定；没有为得到绿灯而填造参数、改写生产 schema 或把失败记为跳过。初轮另有 3 个验证 Agent 与工具同名的配置冲突，修正验证名称后通过。

本票验收的是可移植工具定义、执行与资源边界，不保证模型总能产生正确参数。09 应继续在真实 Pi 工具回调中复验这些场景；不得把这里的 native fixture 或真实 smol 结果当成 Pi 工具验收。

类型检查：本票 MCP/Goal/LSP 资源/smol proxy 的 10 个源文件通过。早期对 6 个共享模块做过与基线的对照，12 个既有错误未增加；不宣称仓库全量类型检查通过。最终组合完整回归 **4329 passed、1 skipped**；组合定向回归 **198 passed**。具体命令和日志位置见机器记录。

## Standards

原 2 项发现（MCP 校验过晚、重复 LSP 配置可能覆盖句柄）已修复。合并 04/05 后复核校验顺序与 Worker 资源身份，无新增问题。

## Spec

原 2 项发现（smol 对引用/组合 schema 的兼容、LSP 启动失败资源清理）已修复。独立审查确认可选参数未在本地 schema 投影或返回解析中丢失；真实 provider 限制按上文保留。

两个审查维度各 2 项发现，均已关闭；不把 provider 限制冒充已解决。

## 交付

经验证的代码交付主工作区 main 的未提交、未暂存 Changes；不新增 main 提交、不推送。冻结入口为 refs/agentloom/ticket08-frozen，组合基线也由 codex/pi-integration 指向。交付时校验 main 原有差异和 index 未被覆盖，再删除本次 08 worktree；实现分支、分阶段提交及外部验收记录保留。实际候选 SHA、文件映射和清理状态见 08-validation.json / 仓库外 delivery 验证记录。
