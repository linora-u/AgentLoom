# Runtime 资源接线验证记录

2026-10-14。候选代码已提交到 `codex/durable-worker-v2-dbos-proof`，提交为 `64ad36ad`，Author 与 Committer 均为 linora-u。它只交付资源归属和关闭诊断接线，DBOS 产品入口、News 迁移及新的完整日期验收仍未完成。[当前方案](../specs/durable-worker-open-source-engine-integration-v2.md)

## 复用范围

Pi 保留公开 `BashOperations`、官方工具定义和回执。受管理分支使用 Node 官方 spawn，带固定 Pi 源码的等待函数与原 MIT 许可证；已删除会改变 stdin、环境和输出等待语义的 Execa 接线。MCP 只替换受管理 stdio 的字节传输，协议继续由官方 `ClientSession` 处理。smolagents 保留正式 Runtime 和前台转后台规则，复用框架现有的具体进程清理与 Run 资源所有者。

这些适配不承担入队、领取、租约或持久容量。上述职责继续由候选 DBOS 引擎提供；完整任务进程的外部所有者接入尚待交付。普通进程组不保证包含主动另建 session 的第三方程序，已有负例继续保留。

## 已完成的验证

| 验证 | 结果与范围 |
| --- | --- |
| Pi／MCP／Application 回归 | 166 项通过；这是最后的持续输出修复之前的结果 |
| 最后 Pi 修复及 OS 资源接线 | 13 项通过；覆盖正式 Pi CLI 和 launchd 管理的 Application 资源用例 |
| smolagents、Shell、hook 和 OS 资源接线 | 304 项通过、1 项跳过；包含正式 smol CLI 的前台、后台、超时、转后台和关闭失败，以及原异常对象、二次收尾验证 |
| 原 100 MB watchdog 故障脚本 | 输入与原脚本相同，限额仍为 100000000 字节、实际输出 100000001 字节；现在 CLI 返回 1、Run 为 failed，进程最终退出 |

各组存在重叠，不能相加为独立测试数量。全部使用本机真实 Runtime／进程与本地模型 fixture，没有请求远端模型或 AnySearch，没有运行新的 News 日期。

日志、XML、23 个代码文件的冻结哈希和临时用例完整副本保存在 [候选证据目录](../../.agentloom/validation/managed-runtime-resource-candidate-20261014-v3/)。原拒绝方案的 DBOS 直接包装探针已原样移出默认测试发现范围，原文件与 RED 记录仍保存；没有修改或放宽其退出断言。

## 两条评审轴分别复验

Standards 复评确认 watchdog 的原 OSError 对象到达资源所有者，实际关闭后第二次 close 成功、句柄清零；没有发现该修复新增的硬标准问题。[报告](../../.agentloom/validation/managed-smol-standards-review-20261014-v3/REPORT.md)

Spec 复评独立逐字重跑原 100 MB 正式 CLI 脚本，确认原问题闭合，未发现这次修复新增的具体缺失、扩张或实现错误。[报告](../../.agentloom/validation/managed-smol-spec-review-20261014-v3/REPORT.md)

这些结论只绑定本次资源适配。仍未证明所有异步中断窗口安全，也不能把工具流关闭、Future 终态或 Run 状态当成任意后代退出的证明。

## 剩余验收

继续完成 DBOS 公开 Client／executor 与 OS job 的产品入口。旧 job 核验必须先于 DBOS 恢复；随后验证稳定输入冲突、业务授权的原子撤销、结果与业务回执共同提交，以及提交后引擎确认丢失。通过后才迁移 News 并完整跑 2025-09-01，使用 DeepSeek v4.1、已授权的 AnySearch 自动切 key 和 20／30／20，推理前准备 trace。

历史 SQLite 522、后续 BUSY 和索引异常仍分别保留原因未闭合的记录。采用新执行引擎不能倒推历史故障已修复。

本次更新保留了测试数量、阶段范围、原始失败和未完成事项。中文机械扫描的半角标点、空泛元话语及机械编号标题均为 0；代码、链接、版本号和英文原文不在正文扫描范围。
