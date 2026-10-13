# DBOS 接入合同复核

2026-10-14。只读核对历史比较程序、DBOS 官方文档及固定版本源码，写入本研究；未修改旧 ADR、spec、产品、依赖或数据库，未运行新探针、模型或搜索调用。

**历史比较应保留，但不足以据此决定自研持久执行引擎。** DBOS 的公开 Queue、Client、workflow 和 datasource 已覆盖大部分通用能力。应优先评估一个本地 DBOS executor、多个 CLI producer，以及业务提交回执的薄适配。原生业务连接与执行账本必须同事务，是此前选型添加的强约束，不能把它直接当成用户的最终目标。

## 历史比较测到了什么

程序使用公开 SDK，未把 DBOS 私有 system tables 当作自研 Store。[比较程序](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v1/durable-live-root-ownership/tools/durable_backend_comparison.py:86) 中的观测仍有效，但各项结论需要保持原有范围。

| 历史项目 | 可保留的结论 | 应收紧的选型解释 |
| --- | --- | --- |
| 两 executor 共用 SQLite，默认容量出现 3／4，显式事务配置后为 2 | 这些是所测环境的真实结果 | 不能据此淘汰单 executor、本地 `worker_concurrency` 的接入方式 |
| 取消后旧子进程还活着，峰值 3 | 同步 step 的取消不会立即杀掉外部子进程 | 程序额外启动了 follower executor，不能据此认定单 executor 提前释放本地运行计数 |
| 普通 step 内原生 `sqlite3` 提交，kill 后重放两次 | 任意业务副作用与 step checkpoint 间存在崩溃窗口 | 该项未采用 DBOS 正式 datasource 事务，不能概括为 DBOS 业务事务不可靠 |
| datasource transaction 提交后 kill／resume，写入一次 | 正式公开事务接线在该控制项有效 | 应与自研同事务方案比较，而非只突出普通 step 的失败 |
| 粗粒度 step 恢复后读取新配置 | 未完成 step 会再次执行其中的配置读取 | 配置冻结的位置和 step 粒度需要明确，不能当作引擎缺少快照能力 |
| 非空旧账本迁移被拒绝 | 当前原型没有实现状态翻译 | 原型显式遇到旧记录就抛 `MigrationUnsupported`，这不是 DBOS 不可迁移的证据 |

事务控制项正确使用 `SQLAlchemyDatasource.create()`、`@datasource.transaction()`、`sql_session()`，并由 workflow 直接调用事务函数。[本地接线](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v1/durable-live-root-ownership/tools/durable_backend_comparison.py:154) 与 [官方 datasource 教程][datasource-doc] 一致。迁移拒绝的实现与限制也写在[原型代码](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v1/durable-live-root-ownership/tools/durable_backend_migration.py:214) 中。

## 本地容量与多进程边界

官方文档定义 `worker_concurrency` 为每个 DBOS 进程、每个 Queue 的运行上限，`global_concurrency` 为跨全部 DBOS 进程的上限；SQLite 默认用于本地，明确不能跨多台服务器。[Queue 合同][queue-doc]、[数据库合同][database-doc] 没有明确把同机多 executor 的 SQLite 全局容量排除在外。不过，官方多实例共享容量测试显式跳过 SQLite，跳过理由只写了数据库类型，未说明能力承诺。[多实例测试][multi-executor-test]、[skip fixture][skip-fixture] 因此不能单凭通用文档或这个 skip 宣称该路径已经验证，或宣称它正式不受支持。

单 executor 更符合先缩小接入面的方向：按初筛、精筛、校验注册三个 Queue，使用 `worker_concurrency=20/30/20`；其他 CLI 通过公开 `DBOSClient.enqueue()` 提交任务，保持一个 SDK executor 消费这些队列。官方支持多个命名 Queue 和 Client producer。[队列教程][queue-tutorial]、[Client 合同][client-doc] Client 直接连接 system database，多个 producer 仍访问同一份执行数据库。Conductor 是额外管理服务，本地入队无需依赖它。

这些 Queue 的运行计数分别按 queue name 维护，底层仍使用 DBOS 共用的线程池，不能描述成三个独立物理线程池。[计数源码][active-source]、[队列领取源码][queue-source] 业务要的是三个阶段各自的容量，不必把物理线程池布局设为验收条件。

取消的计数也需看实现。`cancel_workflows()` 更新持久状态，不直接清除本地 active entry。同步 workflow 在进入执行函数后登记 entry，通常到执行收尾才释放，所以尚未返回的同步 step 会继续占据本地计数。[取消源码][cancel-source]、[执行登记与释放][execution-source] 历史脚本的峰值 3 来自另一个 executor 取得已在数据库中释放的全局名额，不能直接外推到这一单 executor 接线。

**该计数不是所有未完成 Future 的计数。** 同步执行先提交 Future，entry 在执行线程内才登记；异步代码还明确注明注册存在滞后。失去所有权或取消后等待持久结果的执行会先释放 entry。[dispatch 源码][dispatch-source]、[park 源码][park-source] 因此尚不能承诺排队 Future、异步注册和所有异常收尾路径都满足严格物理容量。后续验收应只检验选定的官方接入模式，特别是取消期间同步 Agent 及其资源仍未退出的窗口；本次没有新增测试。

## 业务事务可以与执行账本分开

DBOS datasource 在业务数据库内把业务变更与 `datasource_outputs` checkpoint 放入同一个 SQLAlchemy 事务；恢复读取该 checkpoint，而非要求业务库与 system database 是同一个数据库。源码在提交成功路径检查 workflow 所有权。[事务源码][transaction-source] 这已经提供了另一条防止已提交业务重复执行的路线。现有自研方案的[原生连接校验](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v1/durable-live-root-ownership/src/execution/durable/sqlite_store.py:116) 则要求两者使用同一个实际数据库事务，接线成本更低，却把队列、资源回执和业务写入都耦合到同一 SQLite 文件。

NewsAgent 可保留业务规则，评估两种公开接入：业务提交移入 workflow 直接调用的 datasource transaction，或保留原生 Repository 并在业务事务内保存稳定的幂等提交回执，由恢复逻辑对账。要比较的是重复业务写入、提交成功但回执丢失、旧执行恢复后的行为，不应先要求所有候选复制现有原生连接接口。

接线位置很关键。源码的 `run_tx_step()` 只在 `ctx.is_workflow()` 时记录持久 checkpoint；`is_workflow()` 明确排除 step 上下文。[事务条件][transaction-source]、[上下文定义][context-source] 如果把整个 Agent 当作一个 `@DBOS.step()`，其内部 Tool 再套 datasource transaction，不能自动获得 workflow 直接调用事务时的持久 checkpoint。应明确选择提交的 workflow 边界或应用幂等回执，避免把一个装饰器当作修复。

取消同样只覆盖引擎的执行边界。官方说明同步执行在下一 step 开始时中断；`preemptible=True` 针对 async step，仍不能代替 Pi／MCP 子进程或同步取消回调的实际收尾。[取消文档][management-doc] 这些资源属于 Agent Runtime 生命周期。薄适配需要把公开 Stop／close／join 的结果接到引擎结束流程。executor 重启恢复前，也须清理或拒绝未知的旧本机资源，否则单 executor 的本地计数无法覆盖仍存活的孤儿进程。这项接入约束尚未验证；不能为此又增加一套任务领取和自动重试权威。

## 当前版本没有替代旧故障诊断

本次读取时，最新正式 GitHub release 为 [3.2.0][release]，与历史比较使用的版本相同；main 固定到 `61426cb95bcd749dd18137b4eb483d02b9975d7d`，比 release 多 9 个提交。[版本差异][version-diff]

main 的 [PR #874][pr-874] 减少分区队列容量已耗尽后的无效遍历，不能解释为默认 SQLite 的读前事务问题已修复。[PR #888][pr-888] 给 datasource 失败结果增加所有权检查，针对的是旧执行记录错误结果的问题，也不解决本次原生锁失败。默认 SQLite engine 在连接事件中设置 `isolation_level="IMMEDIATE"`；`start_queued_workflows()` 的容量 SELECT 仍发生在首个写语句前，未看到 SQLAlchemy begin event 显式开始事务。[SQLite engine][sqlite-engine]、[领取事务][claim-source]

SQLAlchemy 官方说明，Python sqlite3 的 legacy transaction mode 不会为 SELECT 自动 BEGIN。[事务控制说明][sqlalchemy-doc] 历史自定义 `system_database_engine` 和 begin event 属于公开扩展方式；其容量通过证据应保留，作为配置候选的证据。它增加了事务串行约束，不能据此宣布旧 SQLite 锁根因已解决。

DBOS 当前默认本地 SQLite，远程可用 PostgreSQL URL。本次没有找到 SDK 自动启动 embedded PostgreSQL 的正式默认合同；自动打包本地 PostgreSQL 可以作为另一个部署候选，但要另行核对启动、退出和升级成本，不能写成已经自带。用户无需手动另起服务，与后台自动启动本地引擎，是两个可分别评估的要求。

建议先把 DBOS 公共引擎恢复为主要候选，与其他成熟项目按新的业务目标比较。优先缩小到单 executor、公开 Queue／Client、明确业务提交回执和 Runtime 收尾的薄适配，暂不实施自建 owner／RPC。原始 308／309 SQLite RED、一次有针对性的 247／248 RED，以及历史 522 均继续保留；新架构通过验收也不构成旧故障的因果结论。

官方源码和文档已缓存到 `.agentloom/validation/dbos-contract-reassessment-20261014-v1`，manifest 保存 URL、版本与内容 SHA。本次未运行其示例或测试。

[datasource-doc]: https://github.com/dbos-inc/dbos-docs/blob/6ffca1d4fc6c4aeaf6cd3634bb58fb8eaab9fa16/docs/python/tutorials/transaction-tutorial.md
[queue-doc]: https://github.com/dbos-inc/dbos-docs/blob/6ffca1d4fc6c4aeaf6cd3634bb58fb8eaab9fa16/docs/python/reference/queues.md#register_queue
[database-doc]: https://github.com/dbos-inc/dbos-docs/blob/6ffca1d4fc6c4aeaf6cd3634bb58fb8eaab9fa16/docs/python/tutorials/database-connection.md
[multi-executor-test]: https://github.com/dbos-inc/dbos-transact-py/blob/3.2.0/tests/test_queue.py#L1133-L1157
[skip-fixture]: https://github.com/dbos-inc/dbos-transact-py/blob/3.2.0/tests/conftest.py#L72-L75
[queue-tutorial]: https://github.com/dbos-inc/dbos-docs/blob/6ffca1d4fc6c4aeaf6cd3634bb58fb8eaab9fa16/docs/python/tutorials/queue-tutorial.md
[client-doc]: https://github.com/dbos-inc/dbos-docs/blob/6ffca1d4fc6c4aeaf6cd3634bb58fb8eaab9fa16/docs/python/reference/client.md
[active-source]: https://github.com/dbos-inc/dbos-transact-py/blob/3.2.0/dbos/_core.py#L905-L977
[queue-source]: https://github.com/dbos-inc/dbos-transact-py/blob/3.2.0/dbos/_queue.py#L728-L746
[cancel-source]: https://github.com/dbos-inc/dbos-transact-py/blob/3.2.0/dbos/_dbos.py#L2110-L2128
[execution-source]: https://github.com/dbos-inc/dbos-transact-py/blob/3.2.0/dbos/_core.py#L1090-L1179
[dispatch-source]: https://github.com/dbos-inc/dbos-transact-py/blob/3.2.0/dbos/_core.py#L1335-L1366
[park-source]: https://github.com/dbos-inc/dbos-transact-py/blob/3.2.0/dbos/_core.py#L822-L847
[transaction-source]: https://github.com/dbos-inc/dbos-transact-py/blob/3.2.0/dbos/_datasource.py#L843-L954
[context-source]: https://github.com/dbos-inc/dbos-transact-py/blob/3.2.0/dbos/_context.py#L288-L296
[management-doc]: https://github.com/dbos-inc/dbos-docs/blob/6ffca1d4fc6c4aeaf6cd3634bb58fb8eaab9fa16/docs/python/tutorials/workflow-management.md#cancelling-workflows
[release]: https://github.com/dbos-inc/dbos-transact-py/releases/tag/3.2.0
[version-diff]: https://github.com/dbos-inc/dbos-transact-py/compare/3.2.0...61426cb95bcd749dd18137b4eb483d02b9975d7d
[pr-874]: https://github.com/dbos-inc/dbos-transact-py/pull/874
[pr-888]: https://github.com/dbos-inc/dbos-transact-py/pull/888
[sqlite-engine]: https://github.com/dbos-inc/dbos-transact-py/blob/61426cb95bcd749dd18137b4eb483d02b9975d7d/dbos/_sys_db_sqlite.py#L20-L52
[claim-source]: https://github.com/dbos-inc/dbos-transact-py/blob/3.2.0/dbos/_sys_db.py#L4717-L4860
[sqlalchemy-doc]: https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#enabling-non-legacy-sqlite-transactional-modes-with-the-sqlite3-or-aiosqlite-driver
