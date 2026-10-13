# DBOS 产品接入状态与一手来源

2026-10-14。worktree 已有 DBOS 3.2.0 产品入口和 News 三阶段接线，有本机局部验证证据。2025-09-01 首轮真实全日退出 1／RED；绑定修复的单项真实 Pi 测试转为 GREEN，第二轮全日运行中。尚未合入 main。本文核对当前接入及已有证据，不重新比较全部引擎。[当前规格](../specs/durable-worker-open-source-engine-integration-v2.md)、[选型记录](durable-open-source-selection-20261014.md)

## 固定上游与产品依赖

[durable extra](../../pyproject.toml) 固定 `dbos==3.2.0`。官方 tag commit 为 `d324156616281d7d4923bc60851617bdbb70ea5e`；本轮直接从官方仓库取得 `_client.py`、`_dbos.py`、`_core.py`、`_serialization.py`、`_queue.py`、`_recovery.py`，六份源码均与当前安装包逐字一致。URL、SHA-256 和相同判断保存在 [上游 manifest](../../.agentloom/validation/dbos-product-documentation-source-review-20261014-v1/UPSTREAM-MANIFEST.json)。产品调用公开 API，未 patch SDK 或查询 DBOS 私有 system tables。[官方 Client 文档](https://docs.dbos.dev/python/reference/client)

## 公开 API 对应当前产品

| 职责 | 固定官方来源 | 当前产品入口 |
| --- | --- | --- |
| 持久队列声明、读取实际容量 | [Client register_queue／retrieve_queue][queue] | [DurableWorker](../../src/execution/durable/client.py) 使用 `never_update` 并显式核对配置；不同容量拒绝 |
| 有限批次原子入队 | [enqueue_in_transaction][enqueue] | 先核对已有 immutable input，再预验证新任务；使用上游事务 API 和 `workflow_id_reuse_policy="reject"`，不自建领取表 |
| workflow 执行、阶段容量与恢复 | [launch][launch]、[listen_queues][listen]、[官方 recovery][recovery] | [DurableExecutor](../../src/execution/durable/executor.py) 完成本地旧 owner／OS scope 检查后 launch；三个 Worker 各一个官方 queue |
| 取消及自动 retry | [run_step_async][step]、[官方 step 执行][step-core] | 外层 preemptible async step 等 OS Adapter 收尾；retry 是否允许与次数由可信 Host 显式提供，调度由 DBOS 执行 |
| terminal 恢复 | [Client resume／rewind][resume] | 先拒绝未退出的旧 scope；Cancelled 使用 resume，Error 使用 rewind，均保留原阶段 queue；先对账业务 receipt |
| 可移植成功／失败回执 | [官方 portable error 序列化][portable] | 完整 `WorkerOutcome`、原错误、cleanup 诊断和 usage 放入公开 exception `data`，由 SDK 持久化和返回 |

`DBOSClient` 提供外部 producer API，直接连接 system database。本地默认 DBOS SQLite 与 News 业务 SQLite 分文件，入队事务仅提交 DBOS workflow，不声称跨库原子提交。相同 root 的存储目标变化会拒绝，URL query 的目标覆盖也会在迁移前拒绝；允许的连接选项可旋转，marker 不保存凭据。PostgreSQL 的公开配置入口可用，但本项目尚未连接实际 PostgreSQL 验证。[公开入口独立复核](../../.agentloom/validation/durable-public-interface-independent-review-20261014-v1/REPORT.md)

## OS Adapter 与业务授权各有具体职责

[LaunchdJobs](../../src/execution/durable/os_job.py) 是本项目针对一次 Application 的 OS Adapter：公开 `launchctl bootstrap/bootout` 启停 unique label，`KeepAlive=false`，`AbandonProcessGroup=false`，`ExitTimeOut=5`。Apple 手册规定同组清理和 TERM／KILL 等待；本机进程身份与组退出另行核对，`launchctl print` 文本及命令成功不充当物理退出证明。主动 detached 的第三方资源不在普通进程组合同内，历史独立 session 负例仍保留。[固定 Apple 手册][apple]、[独立 OS 复核](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-product-os-review-20261014-v1/REPORT.md)

namespace 锁保护本地 executor 身份，scope 锁保护一个已执行 invocation 的资源退出。OS description／identity／result 文件保存恢复所需证据；它们不领取待办、不续租、不持久化阶段 slot，也不运行自己的 retry 调度器。`DBOS.destroy()` 返回仍不证明同步 Worker 或本机资源已退出，官方取消／恢复边界见 [固定源码研究](dbos-product-lifecycle-source-review-20261014.md)。

[NewsHost](../../applications/news_agent/runtime/durable_host.py) 在执行进程内重建 Application 和可信业务绑定，调用 `BoundWorker.invoke()`。Store 的 grant 绑定 task／revision／invocation；安装、撤销、工具提交与 finalize 在业务事务内校验，grant 没有到期续租或执行容量语义。finalize 将 completed 状态和包含 business_results 的 receipt 在同一业务事务提交；恢复先读 receipt，业务已提交而 SDK ACK 丢失时不重新跑模型。失败重算及累计预算由 News 原业务规则决定，实际 retry 仍交给 DBOS。[Store／StageLedger](../../applications/news_agent/runtime/store.py)、[业务／Host 复核](../../.agentloom/validation/news-business-host-20261014-v1/REPORT.md)

[News controller](../../applications/news_agent/runtime/supervisor.py) 的日期锁、producer 用户引用和 executor 状态属于应用生命周期。它们不 claim 任务；[publication](../../applications/news_agent/runtime/execution.py) 把已有业务计划映射成稳定 task／revision 输入，重复提交由公开 DBOS 接口和输入冲突校验处理。旧 running 资源未确认退出时拒绝自动迁移；旧 completed 的结果可显式迁移 receipt，未知历史 usage 保留未知。[独立 controller 复核](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/news-controller-independent-review-20261014-v1/REPORT.md)

## 局部验收与独立复核

| 证据 | 已观察到的结果 | 范围 |
| --- | --- | --- |
| [框架产品 v6](../../.agentloom/validation/durable-product-20261014-v6.log) | 18 passed：4 submission、7 executor、7 单次 invocation | macOS、真实官方 SQLite DBOS、受控无模型 Runtime；含取消、SIGKILL 恢复、receipt 与同 ID scope 拒绝 |
| [提交／业务／Host v2](../../.agentloom/validation/news-dbos-business-final-20261014-v2.log) | 21 passed：4 submission、10 business、7 Host | grant 撤销竞争、stale callback、receipt、重算 provenance、失败 usage、实际 SQLite 原异常保留 |
| [公开入口独立复核](../../.agentloom/validation/durable-public-interface-independent-review-20261014-v1/REPORT.md) | 3 个 P2 修复后原样复验关闭 | 完整 portable failure receipt、schema 更新后 completed resubmit、URL 目标覆盖；PG 部分只解析连接参数并 stub 迁移 |
| [prepared Host binding 复核](../../.agentloom/validation/prepared-host-binding-independent-review-20261014-v1/REPORT.md) | 新 rebind 丢失 parsed MCP capture 的 P2 修复并独立复验关闭 | 纯配置、无模型／MCP 连接；保留原 capture 和 strict marker，来源变更明确拒绝，不能外推真实 validation 已通过 |
| [OS 独立复核](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-product-os-review-20261014-v1/REPORT.md) | cleanup／loaded job／历史组归属及 shutdown 窗口分别保留 RED 和修复 GREEN | 结果各绑定固定 source hash；不冒称在最新 hash 下重跑所有故障 |
| [controller 独立复核](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/news-controller-independent-review-20261014-v1/REPORT.md) | SDK ACK 等待、真实 stop、恢复、两个 producer 共用正式空载 executor；公开读取三个 20／30／20 queue | 空载 queue 声明不是三个阶段满载；启动崩溃窗口未穷尽 |

两组 pytest 有 4 项 submission 重叠，不能相加为 39 个不同测试。这些记录先于首轮真实全日暴露的 trusted factory 绑定缺口，不能用来宣称该修复候选已通过。早期官方 152 个同步无模型任务达到 20／30／20 的证据继续保留，其负载和代码状态与正式 News 全日不同。[早期共享容量实测](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-official-single-executor-go-no-go-20261014-v1/REPORT.md)

## 全日首轮 RED 保留，第二轮运行中

2025-09-01 的正式 `news_agent.service run-day` 首轮使用 DeepSeek v4.1／AnySearch 和 20／30／20，[退出 1／RED](../../.agentloom/validation/durable-news-dbos-full-2025-09-01-v1/cli-exit-v1.json)。当前定位为 prepared snapshot 快返回绕过 News trusted factory 的新 fixed_args 和 Stop hooks，造成 Worker 假完成。修复在 [可信 worker 绑定](../../applications/news_agent/runtime/worker.py) 中重新绑定 captured layers，在全部绑定后才保存 snapshot；[配置重绑定](../../src/config/config.py) 保留已捕获的 LLM 配置。

新增 [真实 public factory 测试](../../applications/news_agent/tests/test_stage_pi.py) 从 RED 转为 GREEN，`1 passed in 5.21s`：覆盖 Stop block 后补齐、隐藏 session 参数、分类 schema、factory 返回后 prompt／YAML 修改不使快照漂移。该项通过只支持所测绑定路径，不代表全日通过。20 个初筛任务已通过官方 retry 进入 revision 2，累计 attempts 40；[第二轮 manifest](../../.agentloom/validation/durable-news-dbos-full-2025-09-01-v1/run-manifest-v2.json) 在推理前保存，第二轮全日仍运行中。

第二轮运行期间，新增 rebind 的独立复核又发现 parsed MCP capture 丢失，已在纯配置探针中修复复验。其后新 job 会加载更新后的 helper；全日证据须按源码变更时间和对应阶段记录，不能宣称整日绑定单一 source hash。[绑定修复的原／新源码 manifest](../../.agentloom/validation/prepared-host-binding-independent-review-20261014-v1/FIXED-SOURCE-MANIFEST.json)

推理前 [启动 manifest](../../.agentloom/validation/durable-news-dbos-full-2025-09-01-v1/run-manifest-v1.json) 保存了源码／输入 hash、模型／容量及 Application、业务、OS 与 observer trace 位置。[首轮原日志](../../.agentloom/validation/durable-news-dbos-full-2025-09-01-v1/run-day-v1.log) 和失败证据保留在同一目录。本文记录该失败及修复候选，不宣称完成、封存成功、合入或推送。

Linux、实际 PostgreSQL 和多机执行未验证；单本地 executor 的 owner 锁不能外推成分布式所有权。Provider 的远端推理是否停止也不能由本机进程退出推断。历史 `SQLITE_BUSY`、`522/SHORT_READ` 和索引异常的根因记录继续保留，新的接入或全日通过均不能倒推旧故障已修复。

[queue]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_client.py#L368-L522
[enqueue]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_client.py#L346-L366
[launch]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_dbos.py#L600
[listen]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_dbos.py#L3831
[recovery]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_recovery.py
[step]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_dbos.py#L1441-L1484
[step-core]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_core.py#L2121-L2388
[resume]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_client.py#L831-L908
[portable]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_serialization.py#L359-L437
[apple]: https://github.com/apple-oss-distributions/launchd/blob/d448a1c8f70a61202f8705f94337f686b87c30c4/man/launchd.plist.5
