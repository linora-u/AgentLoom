# 本地持久执行补查：Kitaru、Absurd

2026-10-14，只补查会改变选型的事实。Pydantic AI当前确实列出这两项，但归于外部SDK集成；进入名单不代表满足AgentLoom的本地部署、共享阶段容量或本机退出合同。[官方清单](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/)

**两者都不改变当前SQLite默认的选型顺序。Absurd值得纳入“若接受PostgreSQL”的简洁候选；Kitaru暂不作为当前新闻执行层的优先替代。** 不是因为需要为它们补一个自研调度器，而是因为核实后的默认部署和恢复语义与已接受的合同不同。本轮不设计这些补丁。

| 核实项 | Kitaru | Absurd |
| --- | --- | --- |
| 官方仓库、许可证 | [zenml-io/kitaru](https://github.com/zenml-io/kitaru)，[Apache-2.0](https://github.com/zenml-io/kitaru/blob/ae0c4f372dcb0b78a23f51a72d187ea288de824f/LICENSE) | [earendil-works/absurd](https://github.com/earendil-works/absurd)，[Apache-2.0](https://github.com/earendil-works/absurd/blob/5b97022ac6d1523cda92470db3b6731909cae571/LICENSE) |
| 本轮版本绑定 | Python `0.27.2`，commit `ae0c4f3…` | latest release `0.5.0`；当前main源码另固定 `5b97022…` |
| 当前真实本地方式 | Docker/Podman Compose管理FastAPI server和PostgreSQL，另有worker | PostgreSQL、单份SQL schema和SDK worker；无需额外broker或API server |
| 本地SQLite默认 | 没有；不能沿用旧资料中的ZenML/SQLite判断 | 没有 |
| 核实到的concurrency | 单worker当前running tasks的上限 | 单worker thread pool/async执行数量的上限 |
| 恢复与取消 | 心跳失联后server可重新派发；正常worker停止先drain | claim到期可重新领取，官方明确可能重叠执行；cancel在checkpoint/heartbeat边界被察觉 |

## Kitaru：当前部署与接口已不同于早期资料

GitHub的`releases/latest`目前指向TypeScript `v0.6.0`，Python最新发布实际是`python/kitaru/v0.27.2`。官方本地安装会通过Docker/Podman启动FastAPI和PostgreSQL；不是嵌入式SQLite，也不只是安装一个Python包。当前主线是记录session、replay、evaluation，worker在子进程中执行agent/evaluator/importer任务。它确实有现成的进程执行和远程API，但没有比已研究的SQLite本地路线更少的部署组件。[Python release](https://github.com/zenml-io/kitaru/releases/tag/python/kitaru/v0.27.2)、[本地server](https://docs.zenml.io/kitaru/getting-started/installation#the-local-server)、[当前架构](https://docs.zenml.io/kitaru/core-concepts/under-the-hood)

`KITARU_WORKER_CONCURRENCY`默认10；固定源码按该worker的`concurrency - len(running)`决定claim数。按kind/label拆三个worker可以区分阶段，但这个参数不能当成多个CLI合计20/30/20的共享额度。官方恢复按worker heartbeat是否仍有效重新派发，不等于先确认旧机器上的所有子进程退出。进程runner已有kill-tree和有界等待，这是比纯线程取消更明确的上游能力；仍不能据此把server失联恢复也解释成物理退出证明。[worker合同](https://docs.zenml.io/kitaru/running-in-production/workers)、[本地容量源码](https://github.com/zenml-io/kitaru/blob/ae0c4f372dcb0b78a23f51a72d187ea288de824f/src/kitaru/worker/worker.py#L312)、[进程收尾](https://github.com/zenml-io/kitaru/blob/ae0c4f372dcb0b78a23f51a72d187ea288de824f/src/kitaru/worker/process.py#L179)

另一个应影响评估的版本风险：Pydantic AI页面仍展示`kitaru.adapters.pydantic_ai`、`kitaru.flow`和`local` extra；当前0.27.2项目的主包和安装指南已采用独立adapter packages与server/worker结构。这个差异需要版本对齐，不能把老示例自动当成当前SDK的可运行入口，也不能仅凭差异断言所有新adapter不兼容。[Pydantic AI集成页](https://pydantic.dev/docs/ai/capabilities/durable_execution/kitaru/)、[固定pyproject](https://github.com/zenml-io/kitaru/blob/ae0c4f372dcb0b78a23f51a72d187ea288de824f/pyproject.toml)、[当前安装指南](https://docs.zenml.io/kitaru/getting-started/installation)

判断：保留为agent replay/eval工具及未来完整平台候选；当前不提高为News持久队列默认。决定性原因是PostgreSQL与容器部署、容量参数的本地范围，以及并未提供所需的旧本机资源退出后重派保证。

## Absurd：组件确实少，但上游明确接受重叠执行

Absurd把任务、checkpoint、retry、sleep/event推进放进PostgreSQL stored procedures；只需要Postgres和`absurd.sql`，没有额外broker、协调server或scheduler服务。它有官方Python SDK；相比需要独立API server的路线，这是会改变部署成本判断的优点。但需要真实Postgres，不能把“本机Postgres”写成“SQLite默认”。[官方概览](https://earendil-works.github.io/absurd/)、[SQL核](https://github.com/earendil-works/absurd/blob/5b97022ac6d1523cda92470db3b6731909cae571/sql/absurd.sql)

官方概念文档直接写明：claim lease在写checkpoint时延长；worker失效或没有及时进展、lease到期后，另一worker可接手，因此短暂重叠执行是允许的。cancel修改任务状态，运行中handler在下一个checkpoint write或heartbeat察觉；不是立即停止模型请求和MCP。固定SQL的claim路径按过期时间失败旧run，再领取pending run；cancel也直接清除claimed_by/claim_expires_at，未查询本机PID退出。[worker与retry语义](https://earendil-works.github.io/absurd/concepts/#workers)、[取消语义](https://earendil-works.github.io/absurd/concepts/#cancellation)、[claim源码](https://github.com/earendil-works/absurd/blob/5b97022ac6d1523cda92470db3b6731909cae571/sql/absurd.sql#L908)、[cancel源码](https://github.com/earendil-works/absurd/blob/5b97022ac6d1523cda92470db3b6731909cae571/sql/absurd.sql#L1976)

不同queue可独立消费，但Python `start_worker(concurrency=N)`是该worker的thread pool/async上限；不能直接推导多个CLI共用阶段N槽。它也不是默认每Attempt独立进程。[Python worker指南](https://earendil-works.github.io/absurd/sdks/python/#workers)、[固定worker实现](https://github.com/earendil-works/absurd/blob/5b97022ac6d1523cda92470db3b6731909cae571/sdks/python/src/absurd_sdk/__init__.py#L1626)

判断：纳入Postgres可选路线，理由是实际组件少、SQL核与SDK职责清晰；排除当前严格SQLite默认，理由是存储要求和上游允许重叠的恢复合同。本轮不为它另加资源slot、lease或重派调度。Checkpoint重放也不能替代独立News业务SQLite的完成回执及事务内写权限，这部分仍属于业务接入合同。

证据缓存在 `.agentloom/validation/kitaru-absurd-primary-sources-20261014-v1/`，包括版本/commit、许可证、官方docs与源码SHA。没有安装引擎、执行模型或修改现有比较及产品。
