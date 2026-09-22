# 04 / 05 集成交付

04 自研 smol 边界与 05 native 读取治理已合入同一代码提交 `6560bf9a45977e9fc1c2ada98ce114c1929318bd`。04 分支保留 6 个阶段提交，冻结为 `49eba895ba333c68cafa0f25ded3ac64ed1dbe22`；05 保持 `196ece736925f14070a8207fd5062acaea331356`。没有 squash 原实现历史。

完整集成回归 **4292 passed、1 skipped**。另有 **10 个真实模型 Application 全部通过**：8 个外部 executor fixture 检查真实读取、Hook 改写/拒绝、排除、参数错误、缺文件和取消，2 个真实 smol 文件读取。它们验证 04+05 组合，不作为生产 Pi 验收。04 独立验收的 17 组真实场景及 Anthropic 未运行项见 [04-validation.json](04-validation.json)。

main HEAD 保持 `9d5a88915efbd402d0f2697318125933fc1500e0`；04/05 成果在未提交、未暂存 Changes。原有票据修改、05 交付、索引、stash 和参考仓库保留。t04 worktree 已删除，分支和证据保留；本轮只清理 t04；07 已由自己的 session 清理，08 保留。

main 的发布源码清单测试会因索引仍登记四个已迁走的模板而拒绝生成清单；这是保留未暂存删除时的验收条件。该失败原样保留，单项及整组测试均在干净集成提交通过，没有修改测试或清单逻辑。直接在 main 重跑同项时需要考虑这一交付状态。

外部证据：`/Users/bytedance/code/data_clear/AgentLoom-worktrees/evidence/t04`。包含实际产物、checkpoint、原失败尝试、29 个验收应用文件归档、组合验证和来源清单；私有配置与日志不进入 Git。机器可读结果见 [04-05-integration.json](04-05-integration.json)。

**06 可启动，与 08 并行；07 已交付。** 从 `refs/agentloom/ticket04-05-integrated` 解析准确 SHA，此引用在上述受检代码之上纳入最新票据修订；先接收 [04 保护调用点](04-implementation.md) 与 [05 prepare/settle 接口](05-implementation.md)。09 继续等待 08。此次没有开发 06 或其他票据。

收尾期间 07 并行交付，最终代码集成为 `da4824f052bd8edb89370ef554b57ae629769e84`。唯一交叉点是公共 runtime_options：保留 04 的 smol 选项下沉，同时接入 07 的 Pi 专属参数校验，与 main 内容一致。该组合另通过 **181 项接线回归**及 **1 个真实 Pi Application**；4292 项完整回归对应前述 04+05 提交，未将其冒充最终组合的全量结果。冻结入口在最终组合上只追加票据/证据文档。
