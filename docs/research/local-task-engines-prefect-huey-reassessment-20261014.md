# 本地任务引擎复评：Prefect、Huey、Procrastinate

2026-10-14。目标是用成熟引擎减少 AgentLoom 自有的执行状态、并发和资源生命周期逻辑，保留 News 的筛选、校验、计分及失败恢复规则。本轮只查官方文档、固定版本源码和现有接入代码，没有安装引擎、运行原型或修改产品。

**建议把 Prefect 的完整执行引擎路线列为这三者中优先验证的候选。它能提供本地 SQLite server、公开任务状态 API、跨客户端容量和独立进程执行；但现成的租约与取消状态不满足“证明旧本机资源退出后才能补位”。因此当前证据支持继续评估，尚不支持直接宣布替换完成。Huey 的 SQLite 部署更轻，却缺少在途任务确认及崩溃恢复；补齐这些能力会重新制造持久执行核。Procrastinate 的 PostgreSQL 事务队列成熟，但默认不是 SQLite，也没有现成的跨 worker N 槽容量及本机退出证明。** 下文把官方能力、源码观察和接入判断分开。

## 版本与判定条件

官方 release/tag 核验版本为 Prefect **3.8.8**（commit `b73b09dc3699489c799dbb40ac5ed2da459e391c`）、Huey **3.4.0**（`6e8acd022346eeb1b09f46c7fa2612f760ee194b`）、Procrastinate **3.10.0**（`8b6398329d915b7299c0d936e567facaebea8903`）。文档是检索当日官方页面，关键代码固定到上述 commit。来源：[Prefect release](https://github.com/PrefectHQ/prefect/releases/tag/3.8.8)、[Huey release](https://github.com/coleifer/huey/releases/tag/3.4.0)、[Procrastinate release](https://github.com/procrastinate-org/procrastinate/releases/tag/3.10.0)。

以下约束来自已经接受的本地持久执行方案，而非新添加的引擎要求：

- 同 root 同阶段跨日期、跨 CLI 共用初筛20、精筛30、校验20的容量。
- 停止接收新任务和实际退出分别记录；lease、失去心跳、终态标记都不能代替旧本机 Runtime、回调、MCP及归属子进程退出。
- 业务完成与执行、清理状态分开；业务提交后框架确认前崩溃，不重新计算已完成业务。
- 分库要有持久业务回执及恢复协调；提交时还必须原子校验业务写入权限。先查询远端 owner 再无条件写 SQLite 不成立。

依据：[既有 spec](../specs/durable-worker-execution-news-integration-v1.md) 第19–27、33–37条；[ADR-0001](../adr/0001-local-durable-execution-with-pluggable-store.md)。

## 能力对照

| 决定性合同 | Prefect 3.8.8 | Huey 3.4.0 | Procrastinate 3.10.0 |
| --- | --- | --- | --- |
| 默认本地持久化，无 Redis/Postgres | 单实例 local server默认SQLite；另有worker进程 | SqliteHuey；consumer进程 | 必须PostgreSQL；不提供SQLite默认 |
| Worker通过公开接口，不直接访问执行SQLite | 客户端通过server API；接线最接近目标 | producer/consumer直接使用storage；不是API server边界 | producer/worker经PostgreSQL connector；不是SQLite边界 |
| 跨CLI共享20/30/20 | server work queue/pool或命名global limits有现成共享权威 | 单个受管理consumer的worker数可限；多个consumer会叠加 | worker concurrency是本地值；DB lock提供同key互斥 |
| 已领取后崩溃的持久状态 | 有flow/task状态、取消和重试 | 无完成ack，在途任务可丢失 | doing状态与worker heartbeat保留，可显式恢复 |
| 旧本机资源退出后补位 | 默认lease到期可释放，取消状态也可超时完成；不满足严格证明合同 | revoke不停止已运行任务；缺少在途恢复和资源证明 | abort是协作/async取消，stalled按heartbeat判定；没有本机资源证明 |
| 每个重试Attempt独立进程 | Process worker隔离flow run；flow内重试可在同process；ProcessPool复用进程 | process consumer是持久fork worker，不是每任务新process | sync/async job执行，不是默认每job独立process |
| 独立News业务SQLite的receipt/reconcile | 要由业务adapter完成 | 要由业务adapter完成 | 要由业务adapter完成；同PostgreSQL事务的公开能力不能用于SQLite分库 |

表中每项由下列官方页面和固定源码支持；表内“接近目标/不满足”是与本项目合同比较后的判断，不是对开源项目通用可靠性的评价。

## Prefect：有成熟控制面，退出与重试边界仍需核验

**本地部署与数据边界。** Prefect server默认使用SQLite；客户端与任务执行通过server API，能避免每个Agent直接创建执行库连接。建议评估时让所有CLI连接同一个受管理的本地server地址。不要让每次CLI各自启动一个临时server，再把它当成共享控制面。单个server内部仍有连接/会话管理，不能称为“唯一一条SQLite connection”。官方把SQLite定位于轻量单server；多API worker模式要求PostgreSQL与Redis。这限制的是server横向扩容，不能误读成“SQLite不能连接多个任务worker”。来源：[server概念](https://docs.prefect.io/v3/concepts/server)、[本地server及多API worker要求](https://docs.prefect.io/v3/how-to-guides/self-hosted/server-cli)、[固定database实现](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/server/database/configurations.py)。

**容量。** Work pool/queue支持共享concurrency；命名global concurrency limit还能围住普通Python代码，适合不同日期引用相同阶段名称。Task runner的`max_workers`只是该runner的本地容量，两个CLI各设30不会自动合计为30。新接入应选择一个官方共享权威，不再同时保留AgentLoom的独立claim/slot/retry循环。来源：[work queues](https://docs.prefect.io/v3/concepts/work-pools#work-queues)、[global limit用法](https://docs.prefect.io/v3/how-to-guides/workflows/global-concurrency-limits)、[task runners](https://docs.prefect.io/v3/concepts/task-runners)。

**global concurrency不是本机退出证明。** 官方明确slot有定时lease，未续约到期会释放；默认续约失败只告警并继续执行，`strict=True`会在续约失败时抛错，但并不在服务端添加旧进程退出检查。固定版本的Repossessor按到期lease扣减active slots，路径没有本机PID或callback检查。默认memory lease storage也不是SQLite中的持久本机资源日志。因此不能把strict模式或更长lease当成严格恢复的修复。来源：[lease语义](https://docs.prefect.io/v3/concepts/global-concurrency-limits#timed-leases)、[Repossessor源码](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/server/services/repossessor.py#L28)、[memory lease storage](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/server/concurrency/lease_storage/memory.py#L16)。

**取消状态不是drained回执。** 正常部署取消由worker先请求停止，再在宽限期后kill基础设施；这是已有成熟机制。但官方也写明：缺少基础设施PID时可以标记Cancelled，CANCELLING超过默认300秒可由server转Cancelled。3.8.8新的ProcessManager在hard-kill分支发送信号后返回，CancellationManager随后继续hooks和取消状态；该函数路径没有hard-kill后的最终exit等待。这不足以支持本项目“Cancelled即可释放所有归属本机资源”的推论。后续必须核验完整executor/process scope，不能只看这一个函数就断言所有Prefect取消都泄漏。来源：[取消文档](https://docs.prefect.io/v3/advanced/cancel-workflows)、[server取消超时](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/server/services/cancellation_cleanup.py#L194)、[ProcessManager.kill](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/runner/_process_manager.py#L398)、[CancellationManager](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/runner/_cancellation_manager.py#L155)。

**独立进程的粒度。** Process work pool让每个flow run进入自己的subprocess。ProcessPoolTaskRunner使用spawn与cloudpickle，进程池会复用worker，`cancel_all`设置取消Event并`shutdown(wait=True)`，并不等于逐Attempt可强制停止的独立进程。Flow engine也可以在同一engine/process内把Scheduled retry重新置Running，再次调用flow function。所以不能只换ProcessPoolTaskRunner就声称消除了root/callback共享。若选独立process作为Attempt隔离边界，应明确一次独立部署运行与一个Attempt的对应关系，检查引擎retry是否改变该对应关系。来源：[local process部署](https://docs.prefect.io/v3/how-to-guides/deployment_infra/run-flows-in-local-processes)、[ProcessPool实现](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/task_runners.py#L749)、[取消实现](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/task_runners.py#L1263)、[flow异常重试](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/flow_engine.py#L829)、[同engine执行循环](https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/flow_engine.py#L2092)。

**能做薄adapter的部分**是任务描述与官方flow/job的映射、在子进程内重建应用绑定、转换明确执行状态、读取业务回执。若为了保留严格退出合同，另外增加自己的持久slots、lease、claim和重试权威，就又变成第二个调度器。公开配置或扩展点是否能在引擎自己的容量释放位置实施本机exit gate，需要下一轮证明；当前源码检查尚未证明存在完整的现成接口。不能用关闭自动回收、无限延长lease或进度hook承担必需确认来回避该问题。

## Huey：SQLite很轻，可靠恢复是决定性缺口

SqliteHuey支持本地持久队列，官方consumer支持线程、进程和greenlet。若每阶段只有一个受管理consumer，分别配置20/30/20，可让多个CLI只负责提交任务；如果每个CLI都另外启动consumer，worker数会叠加。固定3.4.0 process environment使用fork和daemon worker，worker会持续处理多个任务；这与spawn的每Attempt独立进程不同。来源：[consumer文档](https://huey.readthedocs.io/en/stable/consumer.html)、[ProcessEnvironment](https://github.com/coleifer/huey/blob/6e8acd022346eeb1b09f46c7fa2612f760ee194b/huey/consumer.py#L250)、[consumer worker创建](https://github.com/coleifer/huey/blob/6e8acd022346eeb1b09f46c7fa2612f760ee194b/huey/consumer.py#L294)。

关键限制是官方明确采用at-most-once且没有completed-task acknowledgement：消费者突然退出时在途任务丢失。SQLite dequeue也在执行任务前删掉queue row。SIGINT可等待运行任务完成；SIGTERM/关闭超时可中断任务，但Signal回调重新enqueue不是持久in-flight确认协议，SIGKILL也不会让Python handler运行。revoke只阻止尚未开始的任务，不能直接停止已运行的模型/MCP动作。来源：[shutdown与无ack合同](https://huey.readthedocs.io/en/stable/consumer.html#consumer-shutdown)、[SQLite dequeue](https://github.com/coleifer/huey/blob/6e8acd022346eeb1b09f46c7fa2612f760ee194b/huey/storage.py#L952)、[revoke范围](https://huey.readthedocs.io/en/stable/guide.html#canceling-or-pausing-tasks)、[SIGNAL_INTERRUPTED](https://huey.readthedocs.io/en/stable/signals.html#using-signal-interrupted)。

判断：Huey适合可容忍任务丢失、或由业务账本自行发现并重发的轻量后台任务。本项目要求完整新闻恢复、stale Attempt拒写和旧资源退出后补位；为Huey加持久in-flight journal、代次fence、退出证明和重新领取，会重建目前想删除的主要复杂度。它可作轻量方案的边界参照，不建议作为当前严格合同的默认替代。

## Procrastinate：PostgreSQL队列成熟，默认与容量合同不吻合

本轮选择Procrastinate作为第二个深入对照；Dramatiq的官方生产broker是RabbitMQ/Redis，StubBroker用于测试，离默认SQLite要求更远。Procrastinate用PostgreSQL承担队列、事务、locks和通知，不需另加消息broker。如果后续接受PostgreSQL，它值得保留为可选后端候选；当前零外部数据库服务的默认不能直接满足。来源：[Procrastinate quickstart](https://procrastinate.readthedocs.io/en/stable/quickstart.html)、[架构讨论](https://procrastinate.readthedocs.io/en/stable/discussions.html)、[Dramatiq broker合同](https://dramatiq.io/guide.html#message-brokers)。

固定版本使用`FOR UPDATE SKIP LOCKED`领取job，doing状态保留；同lock key的doing job有唯一约束。worker的concurrency却是本地asyncio.Semaphore，SQL lock是同key互斥，不是阶段N槽全局限额。单个受管理worker可以限制本地数量，多个CLI各起worker仍不能把各自30当成总30。若改私有fetch SQL再加slot表，就不是现成引擎的薄接入。来源：[锁文档](https://procrastinate.readthedocs.io/en/stable/howto/advanced/locks.html)、[worker本地concurrency](https://github.com/procrastinate-org/procrastinate/blob/8b6398329d915b7299c0d936e567facaebea8903/procrastinate/worker.py#L97)、[doing唯一lock与fetch](https://github.com/procrastinate-org/procrastinate/blob/8b6398329d915b7299c0d936e567facaebea8903/procrastinate/sql/schema.sql#L99)。

正在运行的同步任务由任务自己检查abort，异步任务通过asyncio取消；它没有默认给每job启动可强杀的新process。突然终止的job保留doing，可根据worker heartbeat通过公开get_stalled_jobs/retry恢复；默认不是一到超时就无条件自动重放。但是heartbeat失联也可能来自仍活的worker，文档给出的周期retry示例没有本机PID/创建身份和MCP descendants退出检查。直接照抄不能满足本项目的恢复合同。来源：[取消](https://procrastinate.readthedocs.io/en/stable/howto/advanced/cancellation.html)、[stalled jobs恢复](https://procrastinate.readthedocs.io/en/stable/howto/production/retry_stalled_jobs.html)、[worker abort实现](https://github.com/procrastinate-org/procrastinate/blob/8b6398329d915b7299c0d936e567facaebea8903/procrastinate/worker.py#L527)。

它支持在外部PostgreSQL connection上把业务写入和defer/cancel/status放进共同事务，这是真正有用的公开能力。但这不覆盖独立News SQLite，也不能自动推导所有finish/business-fence都在同事务。当前finish SQL以job id和todo/doing状态为条件，没有本项目Attempt的generation/owner条件；不能将它当成News stale-write防护。来源：[external connection](https://procrastinate.readthedocs.io/en/stable/howto/production/external_connection.html)、[finish函数](https://github.com/procrastinate-org/procrastinate/blob/8b6398329d915b7299c0d936e567facaebea8903/procrastinate/sql/schema.sql#L264)。

## 冻结候选的 News 接入范围

冻结的 `51903c0f` 候选（含保留的未提交修正）中，`NewsExecution`直接在业务文件上创建SQLiteDurableStore；begin_attempt、publish、retry及StageLedger.finalize通过`connection=db`共同验证和提交。它不是已完成的跨库adapter。把执行库迁给现成引擎后，这些调用不能简单换一个HTTP proxy继续接收sqlite3.Connection。来源：[NewsExecution](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v1/durable-live-root-ownership/applications/news_agent/runtime/durable.py)、[StageLedger业务事务与finalize](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v1/durable-live-root-ownership/applications/news_agent/runtime/store.py)。

独立业务receipt/reconcile能解决“业务已提交、执行确认未写”的窗口：新运行先核对持久、带业务身份/revision的已完成回执，再决定是否还要执行模型；不能只相信进程stdout或模型说completed。**它本身不能解决stale writer。** 当前同事务的owner/Attempt校验必须转化为业务库内可原子执行的权限合同，或使用后端正式支持的共同事务；未提供时必须明确拒绝相应保证，而不是先查引擎状态再无条件写业务库。依据：[现有跨库spec第37条](../specs/durable-worker-execution-news-integration-v1.md)、[当前_owned与finalize](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v1/durable-live-root-ownership/applications/news_agent/runtime/store.py)。

`AttemptBinding`包含context manager、factory、skill catalog、finalize和failure classifier；`AttemptContext`包含当前Store、Run与threading.Event。News binder还构造局部NewsWorkerAgent类、捕获StageLedger/AnySearch sessions的closure和lambda，以及Run下的session/Stop文件。换进程后应传稳定任务描述，在child admission之后重建这些对象；不能把现有活对象或原Application根句柄透明搬过去。公开binder的“宿主可信绑定”职责可保留，但“任意父进程closure/原句柄都兼容”不能承诺。来源：[binding类型](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v1/durable-live-root-ownership/src/execution/durable/binding.py)、[bind_stage_attempt](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v1/durable-live-root-ownership/applications/news_agent/runtime/worker.py)。

每Attempt独立process确实能减少父Application与取消callback共享root的复杂度，但需要该Attempt的Runtime、MCP、业务句柄和Application全部在child内创建。仅把模型Runner放进process、仍让父进程运行binder/finalize/close，不能消除已发现的共享root问题。成本包括child启动与import、多个SDK/MCP后代、定义与配置冻结、IPC错误/Stop/回执传输、进程创建身份和orphan回收。Prefect已经提供local flow process，但这些应用与资源归属边界仍需核验；本轮没有测吞吐、内存或物理退出时序。

## 下一步应回答的三项问题

1. **Prefect能否用公开接口承载严格exit gate，并让它属于引擎自己的唯一容量/重试权威？** 必须覆盖worker/API失联、server重启、CANCELLING超时、hard kill后实际退出和MCP后代；若答案需要第二套持久slot/lease/claim，则应报告接入过重，不继续称薄adapter。
2. **单个Attempt的child入口能否在内部重建现有可信binding，且完全不调用另一套durable领取循环？** 需要明确common invocation的公开接入点、fresh配置和业务回执验证；重用当前`service.run_until_idle()`会把第二调度器带回去。
3. **独立News业务SQLite的权限与完成回执合同怎样成立？** 先确认有效业务写权限在实际事务中的校验，再讨论receipt/outbox/reconcile。任务引擎的Succeeded/Cancelled状态不能替代业务完成和本机清理证明。

这三项是后续选型验证条件，本轮没有启动对应测试。旧SQLite自然RED、callback-start Standards RED、真实新闻输出与trace均保持独立，不以开源文档证明这些问题已经修复。

本轮源证据保存在 `.agentloom/validation/local-task-engine-sources-20261014-v1/`：release-pins、官方source/docs快照及SHA、all-source-index。GitHub匿名API被限流、几个候选URL为404的记录保留，未作为结论依据；release/tag经GitHub连接器重新核实，官方Markdown页面通过直接只读GET获取。
