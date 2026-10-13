# 优先复用开源持久执行引擎

2026-10-14。采用 DBOS 3.2.0 单本地 executor 队列与 macOS launchd。产品接入和 2025-09-01 完整日期验收通过：三阶段 80 个当前 workflow 均 SUCCESS，封存与计分完成。原失败和修复边界见 [全日验收](../reviews/durable-worker-dbos-news-acceptance-20261014.md)。自研持久内核停止扩展；Prefect、loky 和历史失败保留为对照。[当前规格](../specs/durable-worker-open-source-engine-integration-v2.md)、[固定一手来源](dbos-product-integration-status-20261014.md)。

## 需求决定取舍

需要解决的是完整新闻任务可靠恢复、跨日期共享 20／30／20，以及旧本机执行失效后的退出和提交权限。默认本机 CLI 可运行，以 SQLite 等本地存储持久化；未来允许远程后端。新闻判断、阶段衔接、封存和计分规则继续由 NewsAgent 管理。

采用上游的价值应当能落实到删除自研入队、领取、租约、持久容量和恢复状态机。框架只适配自己的 Runtime 调用、可信业务依赖和回执。若接入后还要补一套持久调度器，这个候选就没有减少本项目的主要复杂度。

## 已核对的上游路线

各项部署、恢复、许可证判断均有下列研究中的官方文档与固定源码支持。推荐顺序是本项目的取舍，不是上游维护者的保证。

| 项目 | 本地部署事实 | 对当前需求的判断 |
| --- | --- | --- |
| DBOS 3.2.0 | 官方库内 executor、SQLite 默认、公开 Queue／Client | 当前产品接入；官方队列、业务回执与 OS 管理职责分开；20／30／20 实测，完整日期恢复后封存与计分通过 |
| Prefect 3.8.8 | 一个 API server，默认 SQLite；官方 Process worker | 保留对照；普通 Process worker 接线遗留 native，同环境安装与 OTel pin 冲突，不足以简化当前迁移 |
| Temporal | 官方本地 CLI 可持久化到 SQLite，但 start-dev 是开发模式 | 恢复语义与远程部署参照；不把开发服务或每 Worker 容量当作本次默认生产合同 |
| Hatchet | embedded 自动启动 sidecar 与 bundled PostgreSQL，官方定位开发／CI | 本地接入便利，但增加数据库与进程部署；需验证生命周期，暂不优先 |
| Restate | 单 binary、内建持久存储；官方支持单节点生产 | 部署有参考价值；Server 是 BSL source-available，所需 scope 并发功能尚属实验，不能当作默认 OSI 开源答案 |
| Huey 3.4.0 | SQLite 队列和进程消费者 | 在途任务没有完成 ACK，消费者崩溃可能丢任务；补恢复会重新引入拟删除的内核 |
| Procrastinate 3.10.0 | PostgreSQL 承担持久队列，无额外 broker | 保留为 PostgreSQL 候选；本地 Worker 容量与同 key 排他锁不能直接当作阶段共享 N 个额度 |
| Absurd | PostgreSQL、SQL 与 SDK，无额外 broker／编排服务 | 组件少，适合未来 PostgreSQL 路线；默认不满足 SQLite，claim 到期后的短暂执行重叠是明确合同 |
| Kitaru 0.27.2 | 当前 Python 本地模式是容器化 FastAPI 与 PostgreSQL | 当前部署成本高于首选；不能混用旧集成页的 flow 示例与新版 API |

DBOS 见 [接入复核](dbos-comparison-contract-reassessment-20261014.md)；Prefect、Huey、Procrastinate 见 [本地任务引擎](local-task-engines-prefect-huey-reassessment-20261014.md)；Temporal、Hatchet、Restate 见 [服务型引擎](durable-open-source-engine-reassessment-20261014.md)；Kitaru、Absurd 见 [补充研究](local-durable-engines-kitaru-absurd-supplement-20261014.md)。Celery 与 arq 的 broker 依赖和 ACK 取舍保留在 [早期比较](durable-agent-execution-open-source.md)，未因名称熟悉就直接替换。

LangGraph SQLite checkpointer 能保存 Agent 状态，但不是跨 CLI 阶段队列。Pydantic AI 的官方持久执行集成值得参考：Agent 调用与成熟引擎分别实现自己的职责。它不要求 AgentLoom 为了持久执行替换 Pi／smolagents。[接入研究](durable-agent-ecosystem-integration-reassessment-20261014.md)、[Pydantic AI 官方概览](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/)

## 修正此前比较的范围

此前 DBOS 比较使用了额外 follower executor；原生 SQLite 业务副作用与正式 datasource transaction 也采用不同提交合同，迁移拒绝来自原型中的显式未实现分支。这些实际结果应保留，但不足以决定必须自研。新的默认路线先用官方单 executor、多 CLI producer 和独立业务库验证。[逐项复核](dbos-comparison-contract-reassessment-20261014.md)

业务完成回执解决“业务已提交、引擎 ACK 丢失”的重复执行窗口。旧 Worker 的写权限仍需在业务事务内校验，不能先查远程引擎状态再无条件提交。分库减少共同事务依赖，不会自动解决新闻数据库自身的并发写问题。

## 已运行的上游证据

最初隔离环境运行官方 DBOS 3.2.0，没有改 SDK、SQLite 事务配置，当时未改变产品依赖：

- 两个独立 CLI、一个 executor，完成 152 个无模型任务。三个阶段同时达到并守住 20／30／20。
- 单额度同步任务取消后，下一任务仍等旧 body 与受控子进程退出后才开始。
- 默认 `destroy()` 在 body 和子进程仍活着时返回；之后旧 body 的 checkpoint 报错。这个返回值不能作为安全重启依据。

该轮三项保留完整时序和异常，程序退出 0 只表示观测断言完成。当时没有请求模型、AnySearch 或使用私有配置，也没有运行新的真实新闻日期；后续产品和全日状态见下文。[早期完整实测](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-official-single-executor-go-no-go-20261014-v1/REPORT.md)

三个队列拥有独立额度，底层共用 DBOS 的线程池。该轮同步实测不能外推到 async、关闭失败、信号打断、孤儿资源或同身份 resume；后续产品使用官方 preemptible async step，并另行验证这些具体边界。上游在 workflow 结束或停驻时释放运行计数，收尾失败仍不能当作资源退出。[早期退出补充核验](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-contract-reassessment-20261014-v1/NATIVE_EXIT_SUPPLEMENT.md)、[当前取消／恢复合同](dbos-product-lifecycle-source-review-20261014.md)

## 进程资源单独复用成熟机制

新增核对 AnyIO、Trio、loky、Supervisor、Huey 与 Prefect 的资源管理源码和测试，以及 launchd、systemd／cgroup 的系统合同。研究结论与引擎队列分开：队列恢复并不自动拥有 Agent 创建的全部本机进程。[进程研究](upstream-process-resource-ownership-20261014.md)

loky 的公开 executor 接真实 Application，关闭阻塞与关闭失败通过，根 Worker `os._exit(17)` 后仍遗留 native，结果 2／3。macOS launchd 的同组清理实验通过，独立 session 的负例仍失败。进一步 DBOS 加 launchd 接真实 Application，让受控资源加入任务组后，三项故障通过。此处只证明受控资源接线，不覆盖正式 Pi／MCP 或宿主重启。[loky 比较](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-loky-application-go-no-go-20261014-v1/REPORT.md)、[OS 组对照](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/launchd-process-group-go-no-go-20261014-v1/REPORT.md)、[真实 Application](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-launchd-application-go-no-go-20261014-v1/REPORT.md)

当前产品分工是 DBOS 调度、OS 管理一次完整 Application、业务库维护完成回执和写授权。Pi／smolagents／MCP 的 managed 资源接线已有正式 CLI 局部证据，普通独立 session 的历史负例继续保留。Linux systemd／cgroup 尚无实测；macOS 进程组不能覆盖任意第三方主动脱离行为。[Runtime 与 OS 证据](../specs/durable-worker-open-source-engine-integration-v2.md#runtime-适配已有局部验证)

## 当前产品验收与限制

公开 `Application.durable()`、官方事务入队、独立消费者 CLI 已接入产品，News 三阶段复用 `BoundWorker.invoke()`。框架未增加第二套 claim／lease／slot；业务 grant 不计时续租，receipt 不领取任务，OS 描述记录已进入引擎执行的资源。[产品状态](dbos-product-integration-status-20261014.md)

2025-09-01 全量 5,674 条准备后的新闻、115 个 ETF 行情文件使用 DeepSeek v4.1 和 AnySearch，三轮恢复后完成初筛 20、精筛 30、常规校验 20、行业净校验 10。80 个当前 workflow 均 SUCCESS，594 条候选完成，61 条信号完整计分；122 次 invocation 的退出及撤权证据保留。真实 Pi 绑定、AnySearch 协议错误与恢复、输出 cap、停止 admission 的回归及原失败均可追溯。[全日验收](../reviews/durable-worker-dbos-news-acceptance-20261014.md)

默认 macOS 本地 SQLite，无需另部署服务。Linux、实际 PostgreSQL 和多机执行未验证；若后续合同需要重建持久调度内核，重新评估接入路线。队列线程池、逻辑终态、本机资源退出和远端模型请求分别观察，不能相互替代。

旧 SQLite `BUSY`、`522/SHORT_READ` 与索引异常继续分别保留未闭合的原因记录。新的引擎接入通过，也不能倒推这些历史故障已经定案。
