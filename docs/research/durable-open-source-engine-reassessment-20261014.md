# 持久执行引擎重评

研究日期：2026-10-14。范围：Temporal、Restate，辅查 Hatchet、Prefect。只读官方文档、固定版本源码和官方发布元数据；未安装或启动执行引擎，未修改产品、业务数据库或既有 spec，模型请求数为 0。

本文保留该组候选的独立研究建议。结合其他项目和首轮 SDK 实测后的实施顺序，以 [统一选型结论](durable-open-source-selection-20261014.md) 为准：先验证 DBOS，Prefect 做进程隔离对照。以下研究建议不代表产品已选用 Restate 或 Hatchet。

## 当前没有可直接宣布达标的候选

建议停止扩展当前 SQLite durable 内核，先用 **Restate 做一个独立接入验证**。它最接近“本地 CLI 自动启动持久引擎、无外配数据库、业务库和执行库分离”的部署合同；其官方单节点部署支持生产工作负载，其 scope 并发规则也能直接表达三个阶段额度。不过，Restate Server 是 **BSL 1.1 的 source-available 软件**，许可证明确说明它不是 Open Source license；所需的 scope 并发目前又是实验功能。若“优秀开源项目”要求 OSI 开源许可证，Restate 不应被当作默认开源答案。[R-host][R-flow][R-license]

**OSI 开源前提下，优先验证 Hatchet 的单本地 engine 路线，Temporal 留作稳定语义和未来远程部署的对照。** Hatchet 已有自动启动 sidecar 和 bundled Postgres 的 Python embedded 模式，不需要用户另配 Postgres 或 Docker；其 engine 原生支持跨 Worker 的阶段并发规则。但官方把 embedded 模式定位为 local development/CI，不能把这种便利直接提升为长期生产部署承诺。Temporal 本地持久服务也已经存在；它的 CLI `start-dev` 明确不用于 production，且每个 Worker 的并发上限不等于 Task Queue 的全局在途上限。[H-embedded][H-local][H-concurrency][T-cli][T-worker]

**四个候选都没有直接兑现“旧本机 Worker 真正退出以后才补位”的完整合同。** 超时、取消、重放和任务终态描述的是引擎执行状态；它们不能证明 Pi 回调、MCP 子进程、阻塞线程以及 Application root 资源已经退出。采用上游的价值是删除自研 owner/retry/lease/queue 状态机，剩下一个复用 Application invocation 的宿主适配，不应在上游之外再造第二套 owner、retry、fence 或 semaphore 引擎。[T-shutdown][T-cancel][R-cancel][R-python-run][H-cancel-source][P-concurrency]

这里的“优先验证”是明确的实施建议，不是宣布已经选型。当前资料足以排除不成立的保证；尚无运行证据证明任何组合在本仓库真实 Pi/smolagents invocation 和故障情况下达标。

## 以合同决定选型

| 合同 | Temporal | Restate | Hatchet | Prefect |
| --- | --- | --- | --- | --- |
| 用户不另配 Redis/Postgres | CLI 单 binary、SQLite 文件可持久化；官方 dev-only | 单 binary、内建持久 log/state store；单节点可用于生产 | embedded 自动启动 engine + Postgres，无 Docker；官方 dev/CI 定位 | 单 server 默认 SQLite，无额外配置 |
| 首版单机、多日期和消费者共享 20/30/20 | Task Queue 多 Worker 可消费；`max_concurrent_activities` 每 Worker，queue rate 不是 cap | 同一 stage scope 原生共享；协议 v7/vqueues 实验 | 原生 concurrency expression/static key；tenant-scoped 可跨 workflow；所有消费者必须连接同一 engine | Global concurrency limit 原生共享 slots；带 timed lease |
| 失效或取消后旧本机执行退出才能补位 | Heartbeat/timeout/cancel 不证明进程退出；Python Worker shutdown 等待 activities，但不替宿主证明子资源退出 | cooperative cancel；run 中的外部调用结束后才抛 cancel；abort/kill 不是 OS process join | engine release 先于 Worker 取消；Python sync thread 可继续，仍清理 run bookkeeping | lease 到期释放；默认续约失败仍继续执行，strict 抛错也不是进程退出证明 |
| 业务提交后 engine ACK 丢失，不重做模型 | Activity 起始查业务持久 receipt；Activity 完成事件仍有丢 ACK 窗口 | run closure 起始查业务持久 receipt；journal 与业务 DB 不是同一事务 | task 起始查业务持久 receipt；at-least-once 需幂等 | 业务 receipt 仍必要；缓存/result persistence 不替代业务事务 |
| 业务库与执行库低耦合 | Activity 内调业务 repo；engine DB 独立 | `ctx.run_typed` 内调业务 repo；内建 log 与业务库独立 | Postgres 执行库与业务 SQLite 独立 | server DB 与业务 repo 可独立 |
| 未来远程引擎/存储 | Client endpoint 可换；生产 service 另有运维要求 | 注册 endpoint/远程 ingress；引擎管理自身 log/state，不提供任意 Store 插槽 | client endpoint 可换；engine 依赖 Postgres | remote API URL 可换；多 server 需 Postgres/Redis |

本表的上游事实来自各节引用；Hatchet 官方明确声明 at-least-once task execution，调用代码必须幂等。[H-guarantees] 业务 receipt、宿主退出验收、接缝选择是针对本项目合同的设计推论。**引擎可替换**不等于**引擎内部存储任意可替换**。把 Restate/Temporal 的私有存储接口包装成 `DurableStore`，会错过它们真正提供的执行恢复能力，也容易保留两套调度器。

## 版本和许可证要分别核对

下列是研究日的官方 latest/stable 发布，不代表本项目已验证依赖组合。发布为正式版本与具体功能为 stable 是两件事。

| 项目 | 本次核对版本 | 许可证 | Python/本地依赖事实 | 稳定度限制 |
| --- | --- | --- | --- | --- |
| Temporal CLI / Server / Python SDK | CLI 1.9.1（2026-09-14）；Server 1.32.1（2026-10-08）；SDK 1.34.0（2026-09-30） | 三者 MIT | SDK ≥3.10，明确列 3.14；Rust/Core extension，基本包还依赖 nexus-rpc、protobuf、typing-extensions 等 | CLI 1.9.1 的 `go.mod` 内嵌 Server 1.32.0，不能把最新 Server 1.32.1 当成本地 CLI 内嵌版本；`start-dev` 官方 dev-only |
| Restate Server / Python SDK | Server 1.7.13（2026-10-01）；SDK 1.0.5（2026-09-02） | Server BSL 1.1，四年后转 Apache-2.0；SDK MIT | SDK packaging ≥3.10，官方滚动 services 文档写 ≥3.11；发布有 CPython 3.14 wheel；SDK 含 Rust extension，基本包无 Python runtime dependencies，ASGI host 需另装 | scope/limit-key 需 Server 1.7；flow control disabled by default，配置/接口可能变；1.7.13 修复过 VQueues replica state 一致性问题 |
| Hatchet engine / embedded / Python SDK | engine 0.110.5、embedded 0.110.5（2026-10-06）；SDK 1.42.1（2026-10-09） | MIT | SDK ≥3.10,<4；gRPC/protobuf 6、Pydantic 2、aiohttp 等；embedded 首次下载 sidecar 和 Postgres | embedded dev/CI 定位；engine 的 0.x 版本号本身不足以推导生产成熟度或兼容承诺；需要固定 SDK 与 sidecar 版本 |
| Prefect | 3.8.8（2026-10-06） | Apache-2.0 | ≥3.10,<3.15；默认 SQLite；依赖 SQLAlchemy/aiosqlite/asyncpg、ASGI 和较多 Python 包 | 单 server 可默认 SQLite；多 API/background server 同步要求 Postgres ≥14.9 和 Redis |

来源：[Temporal releases][T-releases]、[CLI release][T-cli-release]、[CLI go.mod][T-cli-go]、[SDK package][T-package]、[Server license][T-license]、[CLI license][T-cli-license]；[Restate release][R-release]、[SDK release][R-sdk-release]、[SDK package][R-package]、[SDK compatibility/source][R-sdk-readme]、[Server license][R-license]、[SDK license][R-sdk-license]、[services][R-services]；[Hatchet engine release][H-release]、[embedded release][H-embedded-release]、[SDK package][H-package]、[MIT source][H-license]；[Prefect release][P-release]、[package][P-package]、[license][P-license]。

Restate 的当前 Additional Use Grant 明确允许组织自己的 production workflows、内部平台，以及经自有抽象提供的 workflow 平台；它限制的是让第三方直接访问 Restate APIs、注册自己的 endpoints 并调用的托管 Restate 平台。AgentLoom 的本地使用与当前已描述业务没有落入该禁用情形的证据。这不改变 BSL 并非 OSI 开源这一分类，也不能预先替未来对外托管产品作许可证决定。[R-license]

## Temporal 的可靠执行能力可复用

`temporal server start-dev --db-filename <path>` 保存跨重启的 workflow executions；不传路径则默认 server 结束时丢失执行数据。CLI 可关闭 UI、固定 loopback endpoint、传 SQLite pragma。多个 Python Worker 通过 gRPC 消费任务，业务进程无需直接开 engine SQLite connection。由此可以避免当前执行元数据与业务写入挤在同一个 SQLite 文件，但**不能推导业务 SQLite 的并发写问题已经解决**。[T-cli][T-queue]

Workflow 代码必须确定性执行，不能直接调用模型、业务 DB、随机数、线程或普通外部服务。完整 Application invocation 应放进普通 Activity；workflow 保存输入、阶段编排和 Activity 结果。已记录完成的 Activity 不因 workflow replay 重跑；如果业务动作成功、Activity 结果还没报给 service 就崩溃，Activity 仍会重试。官方明确要求由被调用业务服务的幂等键解决这个窗口。[T-python-readme][T-activity]

普通 Activity 必须设置 heartbeat timeout 并实际 heartbeat 才能接收取消请求；heartbeat details 可供下一次重试恢复进度。`start_to_close_timeout` 管单次 Activity，`schedule_to_close_timeout` 管包括重试的整体期限。heartbeat 丢失可以触发重试，**即使旧 Activity 的外部动作尚未结束**。因此 heartbeat 证明的是 service 近期收到进展，不是退出证据。[T-cancel][T-timeouts][T-python-readme]

Python SDK 的 `Worker.shutdown()` 会等待 Activity 完成；`graceful_shutdown_timeout` 过后发取消，并不保证 deadline 到了就已停。Activity 不配合取消，shutdown 可以永远等待。`WAIT_CANCELLATION_COMPLETED` 让 workflow 等待 Activity 的取消完成响应，比默认 try-cancel 更符合正常清理顺序；它仍不能覆盖 heartbeat 超时、进程失联或 Activity 内脱离宿主的线程/子进程。[T-shutdown][T-python-readme][T-cancellation-type]

`max_concurrent_activities=30` 是一个 Worker 的在途额度。两个同 Task Queue 的 Worker 各设 30，不能宣称全局还是 30。`max_task_queue_activities_per_second` 是 service-side 派发速率；其单位是每秒，多个不同值的 poller 还会由最后一次 poll 设置覆盖。用它代替 shared stage cap 是语义错误。[T-worker]

首版若采用固定的单机 stage Worker fleet，可由启动配置使各 Worker 的额度总和固定为 20/30/20，所有日期只提交到这三个队列。这是有限部署策略，不是 Task Queue 原生全局 cap；新增 CLI 自动起第二组 Worker、旧 fleet 尚未结束就起 replacement，会打破这个策略。若必须支持消费者任意加入又始终共享总额度，不应另写一个 Temporal semaphore workflow 来弥补本次要求，应该重新选择有 native cap 的引擎。

## Restate 最贴近单本地引擎

Restate 的完整 server 是一个自包含 binary。单节点把 `restate-data` 存在持久磁盘；官方支持可接受短暂重启停机的 production workloads。log 是首要 durability layer，partition 的 materialized state 用 RocksDB；单节点对已提交数据 fsync 后才确认消息。无需 SQLite、Redis、Postgres 或外部 broker。将来多节点有不同磁盘、snapshot/object-store 与运维要求，不能把本地零配置等同于远程零运维。[R-install][R-host]

Python handler 通过 ASGI endpoint 提供给 server；server 调 endpoint，不是 Worker 从 Task Queue poll。`restate.Service` 是独立请求；Virtual Object 是同 key 单 writer；Workflow 的主 handler 同 workflow ID 只运行一次。多 ASGI 进程可承载同一 deployment；并发调度由 Restate server 持有，不必让每个业务消费者自己抢业务 SQLite lease。[R-services][R-serving]

非确定性的模型/数据库/HTTP调用必须包进 `ctx.run_typed`。已记录的结果在恢复中重放；源码只在 `run.replayed` 为 false 时调 closure。closure 先执行，成功后才 `propose_run_completion_success`，所以业务提交与 Restate journal ACK 之间仍有失败窗口。SDK 对同步 closure 使用 `loop.run_in_executor`；attempt 结束取消 outstanding futures，不执行 OS process kill/join。因此直接把同步 Application invocation 塞进 run closure，仍可能遗留线程。[R-steps][R-python-run]

取消是非阻塞操作，需要 deployment 可达；SDK 在 durable await 点抛 `TerminalError`。取消发生在 run closure 中时，官方示例明确等 closure 结束以后才抛错。one-way send 和 delayed send 脱离调用树，不随父 invocation cancel/kill；宿主资源不能放进这种调用并期待父任务取消自动收回。[R-cancel]

inactivity timeout 默认为 1 分钟，它观察 journal 进展；到时要求 handler suspend。abort timeout 默认再等 10 分钟，然后中断 invocation 通信。长模型调用须据实配置，调大 timeout 只是避免错误恢复，不能构成进程退出保障。Restate 的 kill 是调用树的逻辑终止，不能翻译为已经杀掉 Python PID 或 Pi child。[R-config][R-cancel][R-python-run]

### 原生阶段额度存在，仍属实验功能

Server 1.7 的 flow control 使用 scope 和 rule book。同一 scope 的 invocation 共享预算，跨日期/输入不再各拿一份额度。应让 scope 表达阶段：`initial`、`refined`、`validation`；日期放输入或 idempotency key。若 scope 按日期构造，就会把 20/30/20 乘上日期数量。通配规则 `* --concurrency 30` 也是每 scope 各 30，不是所有 scope 合计 30。[R-flow]

示例使用真实公开 CLI/HTTP：

```toml
# restate.toml
experimental-enable-protocol-v7 = true
experimental-enable-vqueues = true
```

```sh
restate-server --config-file restate.toml
restate deployments register http://localhost:9080
restate rules set initial --concurrency 20
restate rules set refined --concurrency 30
restate rules set validation --concurrency 20
curl http://localhost:8080/restate/scope/refined/send/AgentWork/invoke \
  -H 'Idempotency-Key: news-2026-10-14-refined-item-17-r1' \
  --json '{"stage":"refined","work_id":"item-17","business_revision":"r1"}'
```

走非 scoped endpoint 的调用不受这些规则约束。scope 也是 idempotency/Virtual Object identity 的一部分；同幂等键换 scope 会产生另一项 invocation。scope 数量很少会集中 scheduling partitions，当前 70 个阶段并发是否形成瓶颈需要实测，不能靠宣传吞吐断言。[R-flow][R-http]

flow control 官方写明 opt-in、默认关闭、配置与接口可能变化，启用要求 experimental protocol-v7/vqueues。Server 1.7.13 的发布说明还记录了 VQueues 下 Admin state 更新在 replica 间不一致的修复。该 bug 的指定条件是 replication>1 且用 state API，不能推导本地单节点已经有此 bug；它提醒我们对实验路径固定版本、验证恢复和升级。[R-flow][R-release][R-upgrade]

## 业务 receipt 解决跨库 ACK 窗口

共同原则是：**业务数据和 receipt 在业务库同一事务提交；每次上游调用到达真实 invocation 之前，先读 receipt。** receipt 的身份应由业务任务/阶段与业务 revision 决定，不能用易变的 Worker PID、Activity attempt 或新 invocation ID。它保存能返回给 engine 的业务完成结果，保证 engine ACK 丢失后不再跑模型。[T-activity][R-db]

查 receipt 是一项业务幂等操作，不应再持有上游第二份 attempt owner、租约或 retry 状态。跨库原子事务不在承诺内；允许 engine 短暂显示待恢复、业务已完成，下一次执行通过 receipt 收敛。若 receipt 永久保存，engine 24h 默认 idempotency retention 结束后也不会重做业务。Restate 的 ingress `Idempotency-Key` 去重的是请求，不能替代持久业务 receipt。[R-http][R-config]

此外，普通“先查不存在、然后跑模型、最后写 receipt”只消除了**已提交后的重做**，不自动消除同一任务的在途重复、旧 attempt 晚提交或两次模型计费。业务提交必须仍原子满足现有新闻规则，且资源退出问题须独立解决。禁止用跨库“先查 engine owner，再写业务 DB”作为 atomic fence；两次操作之间依然有竞争窗口。这里不提出第二套 owner engine。

当前候选 WT 的新闻适配在业务事务中调用 execution store `validate_attempt(..., connection=db)`，说明既有实现要求执行存储参与业务 SQLite 事务。采用远程或独立引擎后，这个调用不能直接保留为共用 connection 合同；应把“新闻合法提交 + 幂等 receipt”留在业务 repo，把 engine 的 retry/ownership 从业务事务移出。[L-news]

## 最小接入使用真实公开接口

下面只说明可用的上游接口及 Application 复用位置，未运行，不是可上线的宿主生命期实现。例子没有自己实现 claim、owner、heartbeat store、retry scheduler 或 fence。

候选 WT 已公开 `Application.open`、`Application.worker`、`BoundWorker.map`、`WorkItem` 和 `BatchOutcome.to_dict`；单条调用可复用同一 Application invocation 链。模型 provider 配置继续由应用定义处理，Temporal/Restate 不需要另外实现 DeepSeek v4.1、Pi 或 smolagents 模型调用。下面省略 News 的可信工具 binding 和业务 repo；在新闻业务适配里，必须继续调用现有真实 invocation 和提交规则，不可用以下通用例子替代初筛/精筛/校验行为。[L-app][L-items]

```python
from agentloom.app import Application
from agentloom.execution.concurrency import WorkItem


def invoke_application_once(req: dict) -> dict:
    # Public facade in the inspected candidate WT.
    with Application.open(req["application_path"], project_root=req["project_root"]) as app:
        batch = app.worker(req["worker_path"]).map(
            [WorkItem(id=req["work_id"], input=req["input"])],
            concurrency=1,
        )
        return batch.to_dict()
```

该函数返回 execution receipt，**不等于新闻业务已提交**。真实适配还要在调用前查业务 receipt，在 tool finalize 事务内保存业务结果与 receipt，并在返回前等宿主资源结束。`asyncio.to_thread` 或 SDK 默认 executor 不能因为 coroutine 已取消就被视为完成这个等待。

### Temporal Activity 与 Worker

真实接口来自 SDK 1.34.0 的官方源码和文档。`invoke_once` 是适配传入的 async callable，须自己兑现上述 receipt 和资源退出合同，不能把 Application 的同步方法假写成原生 async。[T-python-readme][T-worker][T-timeouts][T-cancellation-type]

```python
from datetime import timedelta
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Worker


def make_activity(invoke_once):
    @activity.defn(name="application-invoke")
    async def invoke(req: dict) -> dict:
        # The host adapter must heartbeat during execution, including cleanup.
        # A heartbeat_timeout alone does not send a heartbeat.
        return await invoke_once(req)
    return invoke


@workflow.defn
class Work:
    @workflow.run
    async def run(self, req: dict) -> dict:
        return await workflow.execute_activity(
            "application-invoke", req,
            task_queue="news-" + req["stage"],
            start_to_close_timeout=timedelta(minutes=30),
            heartbeat_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=3),
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        )


async def run_refined_worker(invoke_once):
    client = await Client.connect("127.0.0.1:7233")
    worker = Worker(client, task_queue="news-refined",
                    activities=[make_activity(invoke_once)],
                    max_concurrent_activities=30,
                    graceful_shutdown_timeout=timedelta(seconds=30))
    await worker.run()
```

上例的 30 只适用于一个 Worker。workflow 另外注册在负责 workflow tasks 的 Worker，提交用 `client.start_workflow(Work.run, req, id=<stable work ID>, task_queue=<workflow queue>)`；取消用 `handle.cancel()`，正常 shutdown 用 `await worker.shutdown()`。workflow/Activity retries 由 Temporal 管，不在 adapter 再包 `retry()` 循环。各 timeout 数值只是示例，不能作为本业务 SLA。[T-python-readme][T-worker]

### Restate handler 与 durable run

`Service`、`InvocationRetryPolicy`、`Context.run_typed`、`restate.app` 均是 SDK 1.0.5 的真实公开接口。默认 Server retry policy 与 run retry policy是不同层次；最小接入让 Server 管 invocation retries，避免再加入应用循环。生产适配需要明确把可重试故障、永久业务错误、已提交后 cleanup 错误区分出来，不能统一无限重做模型。[R-sdk-source][R-sdk-context][R-sdk-retry][R-config][R-errors]

```python
from datetime import timedelta
import restate


def make_restate_app(invoke_once):
    service = restate.Service(
        "AgentWork",
        inactivity_timeout=timedelta(minutes=30),
        abort_timeout=timedelta(minutes=5),
        invocation_retry_policy=restate.InvocationRetryPolicy(
            max_attempts=3, on_max_attempts="pause"
        ),
    )

    @service.handler()
    async def invoke(ctx: restate.Context, req: dict) -> dict:
        return await ctx.run_typed("application-invocation", invoke_once, req=req)

    return restate.app(services=[service])
```

ASGI host 可用官方支持的 Hypercorn，多进程例子使用 `workers=8`；注册同一 endpoint 后，多个日期从同 Restate ingress 提交，scope 额度由 server 控制。上述 timeout 避免默认 1 分钟误判长模型调用，但不是清理证明。不要把同步 `invoke_application_once` 直接传给 `run_typed` 就宣布支持安全取消，因为 SDK 会把它放进线程 executor。[R-serving][R-python-run][R-config]

### Hatchet 不应再因外配 Postgres 被淘汰

以下公开接口已经存在，可用于比较候选成本；需要一个负责 engine 的本地进程，其他独立 CLI 消费者连接它。每个 CLI 都 `Hatchet.from_embedded()` 会起不同 engine，不共享阶段额度。[H-embedded][H-local-instructions][H-sdk]

```python
from hatchet_sdk import Hatchet
from hatchet_sdk.config import ClientConfig, EmbeddedHatchetConfig
from hatchet_sdk.types import ConcurrencyExpression, ConcurrencyLimitStrategy

hatchet = Hatchet.from_embedded(
    ClientConfig(embedded=EmbeddedHatchetConfig(version="v0.110.5"))
)
refined = hatchet.workflow(
    name="news-refined",
    concurrency=ConcurrencyExpression(
        expression="'shared-stage'",
        max_runs=30,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)
# Register task(s) on this workflow, then hatchet.worker(..., workflows=[refined]).start().
# Stop the managed engine with hatchet.stop_embedded(); it waits for engine + Postgres exit.
```

`stop_embedded()` 等待的是 engine + bundled Postgres，并非所有任务代码或 Pi/MCP 资源。源码 engine `handleTaskCancelled` 先调用 `CancelTasks` 得到 `ReleasedTasks`，再把 cancellation 发给 dispatcher并通知后续队列；Python `handle_cancel_action` 发 flag、取消 coroutine，sync thread 仍在运行时仅 warning，最后照样 `cleanup_run_id`。所谓 force-kill-sync-thread 是 `PyThreadState_SetAsyncExc(SystemExit)`，不是 OS process kill/join。当前 PyPI 1.42.1 wheel 中也保留此取消逻辑；不能把“Python Worker 使用 subprocess”理解为每个业务任务都被独立进程可靠杀停。[H-controller-source][H-cancel-source][H-package]

这使 Hatchet 能成为原生 logical cap 的候选，却不能直接满足严格物理 cap。选它之前还需证明长期本地 engine 的持久恢复和多 CLI连接方式；不能把官方 embedded 开发例子中的 `os._exit(0)` 复制进产品作为资源清理答案。[H-example][H-local-instructions]

## Prefect 可本地运行，但没有解决退出合同

`prefect server start` 默认 `~/.prefect/prefect.db`，无额外数据库配置。用户不另配外部服务的单 server 条件可满足，多个业务执行进程可连接一个 server；不能用多 server生产拓扑要求 Postgres/Redis反过来否定单 server。若将来运行多个 API/background server，同步要求则确实变成 Postgres 和 Redis。[P-local][P-scale]

`prefect.concurrency.asyncio.concurrency("news-refined", occupy=1, strict=True)` 可占用共享 global limit。官方 timed lease 到期就放回 slot；默认续约失败只 warning、继续跑，`strict=True`/`raise_on_lease_renewal_failure=True` 会抛错停止受控执行。strict 的错误传播仍不是原生线程/PID join。它不比当前要求更强，且还需要核对中断 task 的后台任务派发与恢复语义，因此本次不将 Prefect 排在 Restate/Hatchet之前。[P-concurrency][P-gcl-api]

## 应删除的复杂度与保留的业务合同

采用上游后，enqueue、持久任务身份、attempt/retry policy、replay/history、排队和 engine 状态查询归上游。业务日期、初筛分类、精筛候选、校验分组、允许序号、可信工具绑定、finalize、封存和计分仍归 NewsAgent。不能以 engine `completed` 替代业务 completed，也不能让模型填写 owner、receipt ID 或 DB path。

业务 repo 与 execution adapter 的接缝使用稳定 WorkItem input、业务 revision、持久业务 receipt 和可观察结果；不要求业务 repo接收 engine SQLite connection。engine adapter负责真实 Application invocation 与 engine completion间的翻译。未来更换引擎只换 adapter，不承诺旧引擎的 execution history可无损迁移到新引擎；未来更换业务存储则换业务 repo。

本机资源管理必须限制在真正需要的宿主生命期：启动进程、传输入、停止/清理、等待退出并返回证据。**如果这个 adapter开始自己调度下一次 attempt、接管lease、维护 durable owner 或实现 semaphore，这条路线已经重新造了执行引擎，应停止。** 上游对失联恢复的决定与宿主退出顺序若无法协调，不能用另一个 fence 表把合同缺口藏起来。

更换执行引擎可消除“执行库与业务库同 SQLite 文件、多进程直接写执行状态”的结构耦合；它不证明两次 `SQLITE_BUSY` 的根因已经全找出，更不能解释历史 WAL 522。当前实证和未知原因分开保留，本研究没有作新的数据库因果结论。

## 下一步只验证会改变决定的场景

第一轮不跑模型：固定上游版本，在独立目录启动 engine，用同一个真实 Application invocation接缝，但替换低成本受控 handler。记录 OS PID/thread/child tree，不以 queue status充当实际资源计数。

- 两个日期、两个独立 CLI提交同一阶段：实际同时运行的宿主不超过阶段总额度；重启消费者不启动第二 engine或第二容量域。
- 业务提交事务成功、返回 engine前 kill：恢复读取持久业务 receipt，模型/handler invocation次数不增加。
- disconnect、heartbeat/inactivity timeout、cancel发生时旧调用仍阻塞：确认 replacement开始时旧 PID/thread/child是否已经退出；超时释放 logical slot不能算通过。
- Pi callback失败、MCP teardown卡住、Application root关闭、SIGTERM发生在 spawn/join附近：完成状态、业务 receipt、实际资源退出分别记录。
- engine crash/restart与宿主进程 crash/restart分别验证；pending仍存在，完成receipt不重做，永久错误不会无限重试。

前两项通过、第三项失败时，应如实报告“上游 logical持久执行可用，严格物理 cap尚不满足”。只有宿主接入验证通过以后，再把 DeepSeek v4.1/Pi/smolagents真实 invocation接入；同一套 adapter复用现有新闻规则，不开启另一轮自研 durable内核。

## 已读一手来源

滚动官方文档均于研究日读取；源码链接固定至所核对版本。Hatchet 1.42.1 wheel另从官方 PyPI发布下载检查，没有运行其中代码。以下来源支撑本文事实；本地路径仅指已检查的候选 WT。

[T-releases]: https://github.com/temporalio/temporal/releases/tag/v1.32.1
[T-cli-release]: https://github.com/temporalio/cli/releases/tag/v1.9.1
[T-cli-go]: https://github.com/temporalio/cli/blob/v1.9.1/go.mod
[T-package]: https://pypi.org/pypi/temporalio/1.34.0/json
[T-license]: https://github.com/temporalio/temporal/blob/v1.32.1/LICENSE
[T-cli-license]: https://github.com/temporalio/cli/blob/v1.9.1/LICENSE
[T-cli]: https://docs.temporal.io/cli/command-reference/server
[T-queue]: https://docs.temporal.io/task-queue
[T-worker]: https://github.com/temporalio/sdk-python/blob/1.34.0/temporalio/worker/_worker.py#L217
[T-python-readme]: https://github.com/temporalio/sdk-python/blob/1.34.0/README.md
[T-activity]: https://docs.temporal.io/activity-definition#idempotency
[T-timeouts]: https://docs.temporal.io/develop/python/activities/timeouts
[T-cancel]: https://docs.temporal.io/develop/python/workflows/cancellation
[T-shutdown]: https://docs.temporal.io/encyclopedia/workers/worker-shutdown
[T-cancellation-type]: https://github.com/temporalio/sdk-python/blob/1.34.0/temporalio/workflow/_activities.py#L72
[R-release]: https://github.com/restatedev/restate/releases/tag/v1.7.13
[R-sdk-release]: https://github.com/restatedev/sdk-python/releases/tag/v1.0.5
[R-package]: https://pypi.org/pypi/restate-sdk/1.0.5/json
[R-license]: https://github.com/restatedev/restate/blob/v1.7.13/LICENSE
[R-sdk-license]: https://github.com/restatedev/sdk-python/blob/v1.0.5/LICENSE
[R-sdk-readme]: https://github.com/restatedev/sdk-python/blob/v1.0.5/README.md
[R-install]: https://docs.restate.dev/installation
[R-host]: https://docs.restate.dev/server/overview
[R-services]: https://docs.restate.dev/develop/python/services
[R-serving]: https://docs.restate.dev/develop/python/serving
[R-steps]: https://docs.restate.dev/develop/python/durable-steps
[R-python-run]: https://github.com/restatedev/sdk-python/blob/v1.0.5/python/restate/server_context.py#L641
[R-flow]: https://docs.restate.dev/services/flow-control
[R-config]: https://docs.restate.dev/services/configuration
[R-cancel]: https://docs.restate.dev/services/invocation/managing-invocations
[R-http]: https://docs.restate.dev/services/invocation/http
[R-db]: https://docs.restate.dev/guides/databases
[R-errors]: https://docs.restate.dev/guides/error-handling
[R-upgrade]: https://docs.restate.dev/server/upgrading
[R-sdk-source]: https://github.com/restatedev/sdk-python/blob/v1.0.5/python/restate/service.py
[R-sdk-context]: https://github.com/restatedev/sdk-python/blob/v1.0.5/python/restate/context.py#L548
[R-sdk-retry]: https://github.com/restatedev/sdk-python/blob/v1.0.5/python/restate/retry_policy.py#L24
[H-release]: https://github.com/hatchet-dev/hatchet/releases/tag/v0.110.5
[H-embedded-release]: https://github.com/hatchet-dev/hatchet-embedded/releases/tag/v0.110.5
[H-package]: https://pypi.org/pypi/hatchet-sdk/1.42.1/json
[H-license]: https://github.com/hatchet-dev/hatchet/blob/v0.110.5/LICENSE
[H-embedded]: https://docs.hatchet.run/v1/embedded
[H-local]: https://docs.hatchet.run/v1/running-locally
[H-local-instructions]: https://docs.hatchet.run/agent-instructions/local-dev-embedded
[H-concurrency]: https://docs.hatchet.run/v1/concurrency
[H-guarantees]: https://docs.hatchet.run/v1/architecture-and-guarantees
[H-sdk]: https://github.com/hatchet-dev/hatchet/blob/v0.110.5/sdks/python/hatchet_sdk/hatchet.py#L100
[H-controller-source]: https://github.com/hatchet-dev/hatchet/blob/v0.110.5/internal/services/controllers/task/controller.go#L885
[H-cancel-source]: https://github.com/hatchet-dev/hatchet/blob/v0.110.5/sdks/python/hatchet_sdk/worker/runner/runner.py#L894
[H-example]: https://github.com/hatchet-dev/hatchet-embedded/blob/v0.110.5/examples/python/main.py
[P-release]: https://github.com/PrefectHQ/prefect/releases/tag/3.8.8
[P-package]: https://pypi.org/pypi/prefect/3.8.8/json
[P-license]: https://github.com/PrefectHQ/prefect/blob/3.8.8/LICENSE
[P-local]: https://docs.prefect.io/v3/how-to-guides/self-hosted/server-cli
[P-scale]: https://docs.prefect.io/v3/advanced/self-hosted
[P-concurrency]: https://docs.prefect.io/v3/concepts/global-concurrency-limits
[P-gcl-api]: https://docs.prefect.io/v3/how-to-guides/workflows/global-concurrency-limits
[L-app]: /Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v1/durable-owned-callback-root-drain/src/app/application.py
[L-items]: /Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v1/durable-owned-callback-root-drain/src/execution/concurrency/batch_models.py
[L-news]: /Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v1/durable-owned-callback-root-drain/applications/news_agent/runtime/durable.py
