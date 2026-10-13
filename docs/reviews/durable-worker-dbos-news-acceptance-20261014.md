# 开源执行与全日新闻验收

2026-10-14。本机验收通过。采用官方 DBOS 3.2.0 队列与恢复机制，macOS launchd 管理每次完整 Application；NewsAgent 的筛选、来源校验、行业净方向、封存和计分规则保留。产品源码为 `4be4da8569d32297e3c168d6fb85f1f3b8336d05`，测试迁移为 `2912c3dc`。合入与发布结果另记在交付记录中。

## 选型有上游依据和实测

[选型记录](../research/durable-open-source-selection-20261014.md) 比较 DBOS、Prefect、Temporal、Hatchet、Restate、Huey、Procrastinate、Absurd 和 Kitaru；各项指向官方文档、固定版本源码与本地实验。LangGraph checkpoint 和 Pydantic AI 持久集成另外核对。DBOS 的六份官方源码与安装的 SDK 逐字一致，未修改 SDK。[固定上游核对](../../.agentloom/validation/dbos-product-documentation-source-review-20261014-v1/UPSTREAM-MANIFEST.json)

采用 DBOS，因为它默认支持 SQLite，公开 Client 可以原子入队，官方队列负责容量、执行与恢复，能删除自研领取、任务租约、持久 slot 和重试调度器。Prefect 的官方 Process worker、loky executor 和直接包装 DBOS 的真实 Application 对照均发现子进程遗留；这些失败保留。进程资源另交给系统管理，框架仅适配自己的调用与回执。[进程研究](../research/upstream-process-resource-ownership-20261014.md)

同一 root 的多个日期共用一个本地 executor。初筛、精筛、校验各自使用 20／30／20 队列额度；DBOS 底层线程池共用，每次模型任务进入独立 OS 管理的进程。正式 `run-day` 自动衔接初筛、精筛和校验，单独 `submit-initial` 只提交初筛。业务与执行状态使用不同数据库，业务权限与完成回执在业务事务内确认。

## 完整真实日期已完成

正式 `news_agent.service run-day --day 2025-09-01 --stop-started-workers` 使用 DeepSeek v4.1 和配置的 AnySearch，未经抽样：原始 6,438 条，prepare 后 5,674 条，115 个 ETF 行情文件。每轮推理前记录源码与输入 hash、模型、容量和 trace 位置。[启动记录](../../.agentloom/validation/durable-news-dbos-full-2025-09-01-v1/run-manifest-v3.json)

| 核验项 | 实际结果 |
| --- | --- |
| 三阶段业务任务 | 初筛 20、精筛 30、常规校验 20、行业净校验 10，全部 completed |
| 引擎最终确认 | 当前 revision 的 80 个 workflow 均 SUCCESS，80 份业务完成回执 |
| 候选与封存 | 594 条候选 completed，日期 sealed，CLI exit 0 |
| 并发观测 | 每 10 秒采样的本机任务峰值为 20／30／20；该采样不证明任意瞬间的远端 HTTP 数量 |
| 运行收尾 | 122 个 OS invocation 都有撤权、卸载和物理退出回执；无仍活着的具体任务根进程或 loaded label，executor 已退出 |
| SQLite 诊断 | 381 份日志，2,288 次业务 begin／commit，无记录到的 SQLite 错误 |
| 官方 evaluate | exit 0，61 条最终信号全部计分，无缺行情排除项 |

计分为 5 条命中、5 条弱命中、51 条未命中，分数 `0.12295081967213115`。这是业务输出，流程完成不代表预测有效。[机器核验](../../.agentloom/validation/durable-news-dbos-full-2025-09-01-v1/acceptance-audit-v1.json)、[evaluate 原输出](../../.agentloom/validation/durable-news-dbos-full-2025-09-01-v1/evaluate-v1.log)

## 失败、修复和恢复均保留

首轮 exit 1：prepared snapshot 快返回丢失 trusted factory 新增的 fixed_args 和 Stop hook，造成假完成。修复重新绑定已捕获配置层，并保留 parsed MCP capture。真实 Pi 回归验证未 finalize 时 Stop 阻断，补齐后才能完成；后续不读取变化后的 Prompt／YAML 来污染已捕获配置。

第二轮 exit 1：AnySearch `extract` 对 DuckDuckGo SERP URL 返回的内容违反声明的 output schema，官方 MCP SDK 拒绝；旧逻辑把这类公共协议错误当作 key 失败，误报“全部 key 耗尽”。修复保留 SDK 校验，将输入／schema／未知协议错误返回模型调整请求，仅明确服务／传输／超时错误进入 failover。单 key 真实诊断及本地 Pi recovery 回归保留，不声称七个账户都发生了额度耗尽。[根因](../research/anysearch-output-schema-root-cause-20261014.md)、[MCP 错误合同](../research/anysearch-mcp-error-contract-20261014.md)

第二轮两个初筛尝试耗尽显式 32,768 输出预算，自动重试成功；20 批初筛仍在该预算下完成。经用户授权，本地 profile 后来调整到 65,536，记录变更，后续校验的 HTTP 请求确认使用该值。不能宣称整日都使用 65,536，也不将本地调参当作框架默认 cap 的修复证明。

第三轮通过官方 retry 恢复原日期，最终 exit 0。三轮共 122 次业务尝试：80 success、4 failed、38 revoked。84 次有 measured usage，38 次被撤销的用量未知；已测总 token 为 54,602,566，reasoning 属于 output 的子集，cached input 属于 input 的子集，未知部分不能补零或据此精确计费。

第三轮推理前 manifest 的 406 个运行源码文件与 `4be4da85` 全部一致。第二轮期间有源码更新，保留其时间和版本边界，不能描述成全日由一个冻结版本重新完成。首轮 observer 将 `attempts` 写成 `attempt`，22 次只读观测报错保留；第二、三轮已修正，业务执行未受影响。最终 audit 首次因这 22 个旧观测错误拒绝通过，原日志保留；复核明确区分产品错误与观察器笔误后通过。

## 回归测试覆盖公开调用

News 测试通过真实 NewsHost／Application／BoundWorker／Stop 运行，保留业务断言，受控 Runtime 不请求远端服务。全量 **431 passed**；最后 discovery 修复另以新进程运行 **5 passed**。全量测试进程早于最后 discovery 改动，不能把两组证据写成同一次最终版本全量运行。[测试迁移报告](../../.agentloom/validation/news-public-seam-tests-20261014-v1/REPORT.md)

输出预算合同的独立 **294 passed** 覆盖 Pi／smol 的真实本地 HTTP：省略不注入上限，显式正数保留，null 解除继承，输入预留独立，必需 cap 的协议在能力未知时提前报错。[预算报告](../../.agentloom/validation/output-cap-contract-port-20261014-v1/REPORT.md)

提交与 executor 在停止修复后 **11 passed**；新增停止 admission 的三项测试验证预先设置 Event、已有 stop 文件、恢复期间收到 stop。其余早期测试与独立复核按原记录保留，重叠计数不相加。[产品状态与复核](../research/dbos-product-integration-status-20261014.md)

## 交付范围明确

当前仅支持已验证的 macOS 本机执行。Linux OS adapter、实际 PostgreSQL 与多机器执行未验证。数据库连接入口可替换不等于多机所有权已解决；本机进程退出也不能证明 Provider 远端推理已经停止。主动脱离任务进程组的第三方资源仍超出当前合同。

历史 `SQLITE_BUSY`、`522/SHORT_READ` 和索引异常的原始证据保留，本次成功未证明旧根因已修复。归档复制保留原路径与源码 provenance；path-bound namespace 不因此成为可移植恢复目录。日志、真实日期输出和旧失败实验在删除 worktree 前保留到主工作区，均不将私有配置或运行数据发布到公共仓库。
