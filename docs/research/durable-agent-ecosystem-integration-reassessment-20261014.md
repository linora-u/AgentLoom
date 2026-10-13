# Agent 持久执行的接入方式

2026-10-14。一手资料补充，未运行引擎或模型。本文核对 LangGraph 与 Pydantic AI，其他引擎见本目录的三份独立复评。

## checkpoint 不能直接替代任务引擎

LangGraph 将 thread checkpoint 与业务 store 分开。官方将 `SqliteSaver` 定位为轻量同步场景，生产持久化示例优先使用 Postgres。固定源码中的 SQLite saver 持有一个 connection，用 `threading.Lock` 包住 cursor 操作。这个锁只属于该 Python 对象，不能据此推出跨 CLI 的阶段共享容量。[持久化说明][langgraph-persistence]、[固定源码][langgraph-source]

本轮固定到提交 `8a77332d0db75c49d296b1f3833a3a5ccd5f0dc8`，对应 checkpoint-sqlite 包版本为 `3.1.2`，项目许可证为 MIT。源码与 SHA 保存在 `.agentloom/validation/open-source-durable-reassessment-20261014-v1/langgraph-source/`。该提交是研究时的 main，并非本项目已验证的依赖版本。

LangGraph 有恢复价值，但将它当作默认持久队列仍需另行提供跨消费者容量、在途恢复和本机资源退出合同。它因此暂不列为本次替代内核的首选。这个判断针对所需任务执行能力，不意味着 checkpoint 功能无效。[checkpoint 指南][langgraph-checkpoints]

## 成熟 Agent 框架也把执行引擎单独接入

Pydantic AI 官方列出了 Temporal、DBOS、Prefect、Restate 等持久执行集成，并区分 conversation persistence 与跨崩溃的 durable run。它还提供 durable backend 扩展接口。这支持“Agent invocation 与持久引擎分别负责自己的职责”的接入方向。[执行概览][pydantic-overview]、[backend 接口][pydantic-backend]

这些集成不能直接当作 Pi 或 smolagents 的替代执行路径。AgentLoom 应复用自己的 Application invocation，由选定引擎调用它；不为持久队列更换模型框架。Pydantic AI 是接入方式的参考，本次不新增其依赖。

Kitaru 的官方适配也说明 checkpoint 粒度会影响恢复：默认按模型和工具调用记录，整个 Agent 若已处于外层 checkpoint，则不能再获得相同的嵌套细粒度行为。DBOS 的 workflow 与 step 事务条件同样需要正确接线。不能只添加装饰器就宣称所有业务副作用已得到恢复保证。[Kitaru 适配][kitaru]、[DBOS 复评](dbos-comparison-contract-reassessment-20261014.md)

## 对本项目的判断

优先验证已有持久引擎的公开 Queue、Client 和执行入口，删除 AgentLoom 自有的通用领取与恢复循环。新闻业务库保留提交校验及持久完成回执。引擎确认丢失时，恢复先查业务回执，避免重新执行已经完成的新闻任务。

仍须单独验证旧 Worker 的真实退出和业务写权限。引擎的取消状态、checkpoint 成功以及一条幂等回执，各自覆盖不同问题；其中任一项都不能替代其他项。[引擎复评](durable-open-source-engine-reassessment-20261014.md)、[本地任务引擎复评](local-task-engines-prefect-huey-reassessment-20261014.md)

[langgraph-persistence]: https://docs.langchain.com/oss/python/langgraph/persistence
[langgraph-checkpoints]: https://docs.langchain.com/oss/python/langgraph/checkpointers
[langgraph-source]: https://github.com/langchain-ai/langgraph/blob/8a77332d0db75c49d296b1f3833a3a5ccd5f0dc8/libs/checkpoint-sqlite/langgraph/checkpoint/sqlite/__init__.py
[pydantic-overview]: https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/
[pydantic-backend]: https://pydantic.dev/docs/ai/capabilities/durable_execution/backends/
[kitaru]: https://pydantic.dev/docs/ai/capabilities/durable_execution/kitaru/
