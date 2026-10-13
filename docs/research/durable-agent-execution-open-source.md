# 持久 Agent 执行：开源机制与 NewsAgent 合同映射

研究日期：2026-10-12（Asia/Shanghai）。AgentLoom 基线：`94491c2d`。状态：当日一手资料研究已完成；本文记录的研究阶段未安装这些系统、改产品或运行模型。用户已确认框架应默认提供本地持久执行，并预留可替换远程后端；[部署边界 ADR](../adr/0001-local-durable-execution-with-pluggable-store.md)已接受。

2026-10-14 已按用户要求停止扩展自研内核，重新核对上游公开接入，并完成 DBOS 官方 SDK 首轮无模型观测。后续判断与未通过项目以 [当前规格](../specs/durable-worker-open-source-engine-integration-v2.md) 和本目录的同日复评为准，本文早期比较不作为继续自研的选型结论。

## 要回答的问题

NewsAgent 需要持续领取新闻阶段任务、限制跨日期的在途数量、在退出或崩溃后恢复，并阻止失效尝试提交。问题不是“有了 map 是否还需要队列”，而是哪些责任具有跨应用价值，以及框架应提供多大的恢复合同。

当前 `BoundWorker.map()` 解决有限输入的执行和资源清理。NewsAgent 的 Store、Consumer、StageLedger 另行拥有持久任务、领取、业务工具提交和阶段封存。对这两层的区分与接入缺口，见 [本地接入审查](news-agent-concurrency-integration-review.md)。

本研究分别检查三种能力，避免把它们写成同一个保证：

1. **任务持久化**：任务是否入库，停止后是否还可领取。
2. **执行恢复**：从整条任务、已完成步骤、图节点或模型轮次中的哪一层继续。
3. **效果提交**：旧尝试是否还能写业务库，外部调用是否可能重复，物理资源是否已经退出。

## 证据版本与获取方式

全部外部资料于 2026-10-12 直接读取官方 URL；没有采用第三方评测或替代搜索服务。官方文档一般为滚动最新版，不是固定发行文档。下面固定源码版本用于复核关键实现；不能把未核实的最新文档 API 推断为较旧版本也支持。

| 项目 | 本轮核实版本 | 固定依据 |
| --- | --- | --- |
| Temporal Python SDK | `1.34.0`，2026-09-30 发布 | [发行页][T-release]；commit `a4c2bb38c3bad43498dfa6bd849e8d85bb684e94` |
| Temporal Server | `v1.32.1`，2026-10-08 发布 | [发行页][T-server-release]；commit `af08a5e5f2dd40f545f2afac7780daaa6987001e`；没有部署验证 |
| DBOS Python | `3.2.0`，2026-09-29 发布 | [发行页][D-release]；commit `d324156616281d7d4923bc60851617bdbb70ea5e` |
| Celery | `5.6.3` | [发行页][C-release]；本轮只作为消息确认语义的简短对照 |
| arq | `v0.28.0` | [发行页][A-release]；本轮只作为 Redis 任务消费者的简短对照 |
| Prefect | 官方当前文档；源码 HEAD `e9bc08239af12f2bbfccab7a8e15b9f0a0ec6865` | 后台 task worker 与 flow worker 分开核实，不当作固定发行验收 |
| LangGraph | 官方当前文档；源码 HEAD `799c2f04fd3a56186e24e9fb8bbf3568e543cfbe` | 新 fault-tolerance API 要求 `langgraph>=1.2`；OSS 库与 Agent Server 分开 |
| Huey | 源码 HEAD `7e937f9d741f4fbc6fa8a18e589826f5f61554aa` | 文档与 SQLite dequeue 源码同一固定快照 |

Python 兼容性另按发行包元数据核对：Temporal SDK `1.34.0` 与 DBOS `3.2.0` 均要求 Python `>=3.10`，均列出 Python `3.14` classifier。[T-pypi] [D-pypi] 这是维护者声明和发行证据，尚不是 AgentLoom + Pi/MCP/AnySearch 的安装及运行验收。

## Temporal 与 DBOS：执行机制

| 维度 | Temporal | DBOS Python |
| --- | --- | --- |
| 持久任务与消费者 | Workflow/Activity Task 持久保留，Worker 有空位才 poll；慢任务不会要求整批结束才派发下一条。[T-queue] | enqueue 写 system database，队列消费者持续领取符合容量条件的任务；源码在事务内执行 `ENQUEUED → PENDING` 并分配 ownership。[D-queue] [D-source-queue] [D-source-dequeue] |
| 是否必须引入完整编排 | 当前还有 **Standalone Activity**：Client 直接持久提交一个后台 job，不要求 Workflow。Python SDK 最低 `1.33.0`、CLI 最低 `1.9.1`；本轮未验证服务部署兼容性。[T-standalone] [T-standalone-python] | queue 的单位是 workflow；可以是包装单个 step 的普通 Python 函数，也可以编排多步，不强制先建复杂 DAG。[D-workflow] [D-step] |
| 恢复粒度 | Workflow 按事件历史 replay 编排；已记录完成的 Activity 不再因 replay 执行。未完成 Activity 的 retry 从函数顶部开始；heartbeat details 只是显式保存的进度，需 Activity 自己识别和跳过已做工作。[T-history] [T-activity] [T-standalone] | workflow 重执行代码，读取已持久记录的 step 结果，继续未完成 step；把整个 Agent 当一个 step 时，未完成的 Agent 会整次重跑，并不恢复函数栈或模型中间轮次。[D-workflow] [D-step] [D-source-step] |
| 失联检测与恢复 | Start-To-Close 限制一次 attempt；Schedule-To-Close 限制整体执行；heartbeat timeout 可触发下一 attempt。取消送达还依赖 Activity heartbeat，检测超时不等于旧代码已物理停止。[T-timeouts] | 单机重启恢复 `PENDING`；显式 executor ID 限定恢复归属。多机无 Conductor 时需自行组织失联判定与恢复；Conductor 默认断连 60 秒后触发别的 executor 恢复。[D-recovery] [D-conductor] |
| 旧尝试写入 | 不能用任务唯一 ID 推导外部调用只发生一次；Activity 可重复执行，需要下游幂等键。[T-activity] | checkpoint 写入同一事务检查 ownership；失效执行不能保存新 checkpoint，并等待胜出执行结果。官方仍明确可能存在 zombie 执行，step 是 at-least-once。[D-conflicts] [D-source-owner] |
| 容量作用域 | Python `max_concurrent_activities` 属一个 Worker；queue 全部 Workers 的 server-side 参数是**派发速率**，不是同名的全局在途上限。[T-source-worker] | 明确区分单进程 `worker_concurrency`、全应用同 queue `global_concurrency`、partition 上限以及启动速率。全局额度还被旧版本 `PENDING` 占用。[D-queue] [D-source-dequeue] |
| 默认重试 | Activity 默认附 Retry Policy；可配置 attempt 次数、退避、不可重试错误。把现有 whole-Agent 调用包成 Activity 时，要明确关闭或接管 News 的业务重试，避免两套 retry 叠加。[T-timeouts] | step retry 默认关闭，可配置异常筛选和退避；workflow 未捕获异常进入 ERROR，自动崩溃恢复不等于自动重试所有业务错误。[D-step] [D-workflow] |
| 定义版本 | Workflow 需要 replay-safe；Worker Versioning 提供 pinned/auto-upgrade，patch 用于变更步骤顺序。版本路由解决代码兼容，不自动冻结外部模型配置、提示词文件和 API 行为。[T-version] [T-worker-version] | application version 默认按 workflow 源码计算；恢复选择同版本，或用 patch 保持兼容。文档建议新旧进程同时运行直到旧任务完成；不能据此假定动态 YAML/模型配置都已纳入版本。[D-version] |
| 停止与物理退出 | 停 poll、graceful interval 后发 cancellation；Python Worker 仍等待 Activity 结束，任意同步代码不因此可强杀。[T-shutdown] [T-source-worker] | workflow 取消在 checkpoint/步骤边界生效；async step 可设置 timeout/preemptible，sync step 不支持此类抢占。业务取消状态不能证明同步 Agent/MCP 资源已退出。[D-workflow] [D-step] [D-source-step] |
| 单机部署成本 | 本地有 CLI dev server；持续负载需独立服务与持久存储，并管理 schema、服务版本和 Worker。官方 production 示例包含 PostgreSQL/Elasticsearch，也提供其他存储/二进制部署选项；不能说 ES 是所有部署必需。[T-deploy] | 库内执行加 system database，SQLite 默认可本地运行；官方推荐生产用 PostgreSQL，并明确本地 SQLite 文件不支持跨服务器部署。分布式自动恢复另有 Conductor/自管责任。[D-database] [D-recovery] |

### 哪些实现值得吸收

Temporal 将 **job queue 与复杂 Workflow 编排分开**，为框架提供可持久提交、持续消费和 attempt receipts 提供直接先例。[T-standalone] 这支持研究独立公共持久执行层，但不等于必须引入 Temporal 服务。

DBOS 的固定源码提供了比较接近当前单机形态的参照：有界 claim、持久 status、executor/version 归属、ownership 校验，以及恢复重新入队后再走正常 admission。其恢复代码不是跳过容量控制直接批量启动失联任务。[D-source-recovery] [D-source-dequeue] 这支持把通用队列/恢复责任提取到框架；若直接采用它，也会带来 workflow/step 注册、确定性、schema 与版本管理合同。

两者都提示应区分**恢复同一个执行**和**发起新的业务尝试**。News 当前 retry 清空该任务部分结果并用最新定义/模型整项重算；这与恢复时重用已完成 step 是不同产品行为，不能在换执行 backend 时静默交换。

## 消息队列的简短对照

| 系统 | 官方语义 | 对 NewsAgent 的含义 |
| --- | --- | --- |
| Celery `5.6.3` | message ack 后从队列移除；默认执行前 ack，幂等任务可设 `acks_late`。晚 ack 下 child 被终止仍可能 ack；`task_reject_on_worker_lost` 改变该行为。[C-tasks] | 框架自带持久队列是成熟模式，但“有队列就能恢复”并不成立：须明确丢失窗口、确认时点和重投策略。它不自动合并 News 的工具 finalize 与执行/清理终态。 |
| arq `v0.28.0` | pessimistic execution：退出时未完成 job 留队再次执行；官方明确可能执行多次，需事务或幂等键。依赖 Redis；`max_jobs` 是一个 Worker 的容量，`job_completion_wait` 是退出等待参数。[A-docs] | 适合观察整任务重投的简洁边界；不能把单 Worker 上限当跨进程 stage 上限。自定义 `job_id` 防重复入队到结果清除为止，不能去重远端模型/搜索效果。 |

## Prefect、LangGraph 与 Huey：不要混合不同执行层

这部分由独立只读研究汇入；源码版本见上表，没有运行相应系统。

| 对象 | 已确认能力 | 对 NewsAgent 的边界 |
| --- | --- | --- |
| Prefect 普通 task runner / flow Worker | task runner 执行本次 flow 的 `.submit()`/`.map()`；flow Worker 持续 poll work pool 并启动整个 flow。[P-runners] [P-workers] | flow 容量、flow 内 task 容量与跨日期 stage 容量不同。不要把 flow Worker 当成 News task consumer。 |
| Prefect 后台 task worker | `.delay()` / deferred map 可独立于 flow；`task_worker.serve()` 持续消费，默认 limit=10 属当前 Worker，需持久 API服务。[P-background] [P-task-worker] | 确有长期消费者；但 websocket ACK 是收到任务即 ACK，之后才等待执行容量。dispatch `TaskQueue` 是内存队列，数据库 run记录是另一层。[P-ack] [P-dispatch] 尚未证明已 ACK 的 running task被 SIGKILL 后可靠重派，须专门验证。 |
| Prefect 重试/缓存/共享容量 | 异常 retry 与 crash 状态处理分开；缓存需持久化结果，串行化同 key执行需锁；全局资源额度采用 timed lease。[P-engine] [P-cache] [P-global] | 资源 lease 续约失败默认可能继续执行，过期却释放 slot；strict/显式失败选项不能替代业务 owner fencing。缓存不会自动冻结新闻 YAML/模型配置，也不能撤销 HTTP副作用。 |
| OSS LangGraph + checkpointer | 保存 superstep checkpoints/node pending writes；恢复可复用成功节点，未完成 node/task 从头执行。durability 的 sync/async/exit 有不同崩溃窗口。[L-checkpoints] [L-functional] | 是图状态恢复库，不自动变成独立跨进程任务消费者；模型调用作为粗 node时仍会重复。SQLite可本地，持久 backend 与 caller的恢复驱动另需安排。 |
| OSS LangGraph 故障和版本 | `>=1.2` 的 drain等待当前 superstep、保存checkpoint；async node有timeout，不能强杀同步线程。runtime max_concurrency属当前执行器；checkpoint不固定旧图代码。[L-fault] [L-executor] [L-version] | 可参考状态机/细粒度进度；不因此满足 News三阶段共享额度、ownership/recovery服务。改变 task/node顺序或schema仍需兼容策略。 |
| LangSmith Agent Server | 另有 durable queue/lease；每 thread同时最多1run，默认 `N_JOBS_PER_WORKER=10`；background retry默认3，shutdown grace默认180秒。[LS-server] [LS-scale] [LS-env] | 不能把这个部署产品的队列能力归给纯 OSS LangGraph库。standalone要求 PostgreSQL、Redis和license key；其队列“exactly once”措辞不能扩展到模型/AnySearch效果。[LS-standalone] |
| Huey consumer | 有长期consumer、scheduler和worker，可用SQLite；但官方明确**不保证at-least-once、不做完成ACK**。SQLite dequeue在事务内select并delete；强杀在途可能丢失。[H-consumer] [H-storage] | 可借鉴轻量consumer/CLI/存储分离；不符合当前News租约恢复的等价替换。捕获普通task异常后的retry不补足强杀丢失窗口。[H-retry] |

Prefect本地可用 server + SQLite + 参数/结果存储 + task worker；完整Compose并不是所有场景的最低部署要求。[P-local] LangGraph OSS可本地，但选择Agent Server是另一个存储和许可决定。这些例子共同支持将**有限执行、持久消费、图恢复、共享资源额度**分成清晰接口，而非扩大一个 `map()` 词义。

## NewsAgent 合同映射与代价

| 当前需求 | 可以公共化的机制 | 留给应用的业务合同 / 代价 |
| --- | --- | --- |
| 跨日期持续补位，初筛/精筛/校验有独立容量 | 持久 queue + 明确 queue/worker 容量域；有空位才 claim，先完成清理再释放实际执行槽位 | 日期、阶段依赖、校验候选分组与 backlog 限制仍是业务规则。单进程容量与多进程同阶段总额度要分别选择。 |
| 重启后找回工作 | durable task ID、attempt ID、input/config revision、status、执行归属和 recovery API | 选择 whole task、已提交新闻条目、Runtime snapshot 还是模型/Tool turn 恢复。Temporal Activity 和 DBOS coarse step 不自动给出模型轮次恢复。 |
| 旧 attempt 不得污染新 attempt | ownership 条件更新、claim/recovery 事务、heartbeat、失效 attempt 的拒绝证据 | News 所有结果提交已在 SQLite 事务检查 owner/status/lease；迁移要保留这些提交条件，不能仅用框架 completed 替代。外部 Tool 还需自己接受幂等键/有效尝试凭据。 |
| 初筛分类合同、允许序号、动态 session | invocation 的可信宿主绑定接缝，让每个 attempt 独立绑定工具参数和资源 | 分类和候选 schema 仍归 News；不能把 owner、数据库路径交由模型自由填写。第三方任务库不会自动补齐 AgentLoom 的这项公开 seam。 |
| 工具已 finalize，但模型退出/清理随后失败 | 同时保存业务提交状态、invocation outcome、cleanup outcome | 若把 finalize 后的任何错误都视为自动 retry，可能重复计费、覆盖已完成判断或重复发送外部动作。必须定义该组合终态的策略。 |
| 最新配置应用于新业务尝试 | 分开 task identity、attempt identity、definition/config snapshot 与 replay version | pinned recovery 需要旧代码/定义可获取；latest retry 要新 attempt receipts。版本 hash 不是模型输出可复现性保证。 |
| stop-workers 后确认全部结束 | 停止 admission、可观察 drain、最终资源 proof；失联恢复与旧资源处理分开 | Temporal/DBOS 的 durable cancelled/terminated 都不能撤销外部效果，也不能替 AgentLoom 证明 Pi callback、MCP 和线程已退出。 |
| 业务完成、阶段封存和计分 | durable execution receipts、required commit seam 与可失败 observer 分开 | StageLedger finalized、stage complete、day seal/evaluate 保留。当前 map 的 event_sink 普通错误只记诊断，不能拿它承担必须成功的 ack。 |

本地对应：`applications/news_agent/runtime/pipeline.py:211,244,268`；`runtime/store.py:310,317,329,447,452,562,791`；`runtime/worker.py:90,117,141,187`；`src/app/application.py:425,511,616`；`src/execution/concurrency/batch_runner.py:155`。

### 本地故障实证：旧尝试可以重启新尝试

2026-10-12 的隔离故障注入使用真实 `NewsPipeline._execute` 和 SQLite Store，仅模型调用替换为可控异常，模型请求数为0。attempt 1失去租约；replacement attempt 3以永久错误失败，已到配置上限3。旧 attempt随后返回retryable错误：`fail(id, old_owner)` 没改新 owner的状态，但紧接着 `retry([id], strict=False)` 不检查旧 owner，把 attempt 3从failed改回pending，最终可claim出attempt 4。

已读回[探针结果](../../.agentloom/validation/news-concurrency-20261012/fault-injection/stale-attempt-retry-result.json)与[探针源码](../../.agentloom/validation/news-concurrency-20261012/fault-injection/stale-attempt-retry.py)。这不是上游行为推断，也不是模型并发故障；它证明 **retry/requeue也必须受attempt ownership与次数上限的同一事务条件约束**。只保护结果提交或接入外部queue，不会自动修复当前应用中的这个旁路。

### 逻辑额度不等于物理资源上限

如果租约到期后重派，但旧 Runtime仍运行，一个logical task只有一个有效owner，也可能有两个实际Agent同时发出LLM/AnySearch请求。DBOS明确承认zombie执行；Prefect资源lease也可能到期释放而原代码继续；Temporal超时后重新派发不等于已经强停原attempt。[D-conflicts] [P-global] [T-timeouts]

因此要先选容量承诺，再选恢复时点：

| 承诺 | 对失联/失租恢复的要求 | 代价 |
| --- | --- | --- |
| 严格物理在途上限 | 替换前确认旧进程/Runtime退出，或外部执行系统提供可验证的中止/资源fencing；仅把DB slot归还不足够 | 无法确认旧实例已退出时可能阻塞恢复；安全与可用性有实际取舍 |
| 逻辑owner/queue上限，允许恢复重叠 | 新attempt按有效ownership计额，同时单独观测和告警旧执行资源；拒绝旧提交 | 需接受短暂额外资源和计费；不能把逻辑peak报告为实际LLM/Agentpeak |

AgentLoom现有有限map在同进程正常drain时明确等到实例清理再释放槽位。增加持久恢复后必须扩展跨attempt资源合同，不能复用这个已有验收就宣称崩溃/网络分区时也满足严格物理上限。

## exactly-once 的证据边界

官方资料支持以下受限保证，不能扩写为“所有外部效果恰好一次”：

- Temporal 已记录完成的 Activity 不因 Workflow replay 再执行；但完成后未通知服务便崩溃，仍可能重试。去重应由接收外部请求的服务执行。[T-activity]
- DBOS 记录的 step/workflow 结果有 ownership 与唯一性保护；普通 step 调用完成后才写 checkpoint，仍存在外部动作成功而 checkpoint 缺失的窗口。固定源码明确先执行 `func()` 再保存结果。[D-conflicts] [D-source-step]
- DBOS datasource 的“事务 commit exactly once”要求业务变更和结果 checkpoint 在同一个数据库事务中；源码把用户函数与 `_record_result` 放入同一个 `session.begin()`。它不包含事务外的 LLM、AnySearch、文件、邮件或其他 HTTP 请求。[D-transactions] [D-source-transaction]
- job/workflow ID 去重防止重复**提交一个执行**，而 retry 防止执行丢失；同一执行内部再次调用远端系统是另一问题。持久任务最终 recorded SUCCESS 也不证明每个远端动作只执行过一次。[T-standalone] [D-conflicts] [A-docs]

因此，任何方案都要明确：可重复的只读搜索/模型请求是否允许重复计费；写入 Tool 使用何种业务幂等键；失效 attempt 在本库以外能否被拒绝；ambiguous result 如何处理。没有这些合同，不能宣称通用 exactly-once Agent。

## 已确认范围与待判定实现

已确认：持久任务和恢复进入框架，默认本地持久存储，SQLite为本地候选；公开可替换后端接口支持未来远程存储；首版单机多进程、共享阶段容量，失租时停止旧 Worker 并确认本机资源退出后恢复补位；有限map合同保留。远程存储不等于首版支持多机器分布式执行。把队列永久留在每个应用的旧提议不再作为同级实施推荐。仍需选择持久核心与恢复粒度：

| 候选 | 收益 | 要承担的合同 |
| --- | --- | --- |
| 旧提议：只补公开 invocation/绑定接缝，队列仍在应用 | 最小迁移，复用框架资源和 receipts | 未满足已确认的框架持久化方向；仅作为现状与迁移成本对照 |
| 框架提供单机持久执行层，业务 Store 保留 | 复用任务身份、持续 admission、attempt 与恢复管理；可保持 SQLite部署 | 需要原子提交/恢复、租约失效、queue容量、配置版本、schema migration 和 CLI运维；执行库与业务库之间的提交边界必须明确 |
| 可选成熟 backend，框架统一 Agent invocation 适配 | 复用外部库的队列、观测、恢复与多机机制 | 依赖/部署成本、编程与版本模型、默认 retry 差异；至少验证一个 backend 的真实 Agent/Tool/资源合同 |
| 框架实现通用步骤/模型轮次 durable replay | 更细恢复，少重做已完成工作 | 比持久队列更大：Runtime/Tool checkpoint、确定性、版本兼容、外部幂等、历史保留和秘密脱敏都进入核心合同 |

上游证据支持已确认的公共持久执行方向：队列、attempt、归属、恢复和持续 admission 可与新闻规则分离。证据尚不足以选择自研还是嵌入引擎，或决定把整个 Agent、每条新闻还是每个 Tool turn 作为恢复单位。完整 NewsAgent 真实验收与故障注入结果应影响后续实现范围。

### 三条主要实施路线的决策比较

| 决策因素 | 自建SQLite最小durable task核 | DBOS嵌入引擎候选 | Temporal未来可选外部engine |
| --- | --- | --- | --- |
| 本地CLI，无额外服务 | 可保持当前形态，但新核尚未实现 | Python库+SQLite已是现成路线；不需先引入独立队列服务。[D-database] | 需独立Temporal服务；CLI dev server只证明易于开发，不替代生产服务运维。[T-deploy] |
| Python3.14 | 可沿用当前项目支持版本；新代码须验收 | 3.2.0声明支持；本项目组合未运行。[D-pypi] | SDK1.34.0声明支持；本项目组合未运行。[T-pypi] |
| 与业务Store同事务 | **可设计**同一SQLite connection提交task转移与业务修改；若拆两个DB，仍需outbox/reconcile | datasource可原子提交业务修改+step checkpoint，但需使用其SQLAlchemy transaction。现有sqlite3 Store不会因同file自动加入；queue终态与业务finalize还要协调。[D-transactions] [D-source-transaction] | engine与业务SQLite分离；需幂等提交/业务receipt/reconcile，不能跨两者凭空得到原子事务。[T-activity] |
| 跨进程同stage额度 | 需事务内计数/claim、共享存储、失效恢复，验证多consumer竞争 | queue global_concurrency已提供；仍是logical PENDING额度，不能约束未退出zombie资源。[D-queue] [D-conflicts] | Worker并发各自独立；taskqueue全局派发rate不是在途cap，若要严格共享stage数还需额外容量合同。[T-source-worker] |
| 恢复/新配置 | 可直接表达News的latest retry与snapshot resume区别，但全部机制自管 | 同版本恢复、steps结果复用现成；外部prompt/model快照需适配；latest业务retry需另起attempt | Activity可以whole-attempt重跑，Workflow可细步replay；版本路由、外部配置快照与业务retry是分别的选择 |
| Agent可信工具binding | 三条路线都需补齐Application公开invocation binding，不能由队列替代 | 同左；decorator不自动绑定StageLedger/session | 同左；Activity执行宿主仍负责Agent工具与资源 |
| 主要维护成本 | CAS状态机、schema迁移、排队/退避/恢复/CLI/观测/故障矩阵都由AgentLoom承担 | 依赖与schema升级、workflow/step注册、确定性、executor/version运维、业务事务迁移 | 外部服务与存储升级、版本发布、观测运维、async SDK适配、业务与engine一致性 |

选择判据：按用户已确认的本地CLI、持久化、可替换后端边界，**自建最小核与DBOS应按同一验收矩阵比较**，不因DBOS支持SQLite就反过来默认选择自建。自建的价值只能由“更直接保留现有业务事务/配置/运维合同”的验证证明；DBOS的价值是复用已经有的队列、ownership和恢复，代价也需实测。Temporal保留为未来外部引擎参照，不是默认部署建议；只有跨机器恢复、复杂长期编排或成熟版本运维收益足够时才进一步评估其服务成本。

### 可替换后端的事务与执行边界

可替换接口应承诺持久enqueue、原子claim/ownership转移、attempt条件终态更新、retry/recovery与可观察状态等**语义**，不要求应用拿SQLite connection，更不能要求远程实现假装参加本地事务。下列能力要分开：

- **后端自身原子状态转移**：每个后端都必须兑现；旧owner的fail、retry、complete都应在同一条件提交中拒绝，不能先检查再无条件更新。
- **业务修改与执行记录共同提交**：只有实际支持共同事务的后端/业务存储组合才能声明。默认同SQLite connection可做优化；远程任务后端+本地NewsStore通常需要幂等写、transactional outbox或reconcile。简单先读远端owner再写本地结果仍有检查与写入之间的竞争窗口。
- **存储可替换与执行引擎可替换**：DBOS同时拥有queue consumer、workflow/step恢复，不是纯SQLite CRUD库。若采用DBOS，应适配公开durable execution语义，不把它的私有`_sys_db`当作稳定Store协议。Storage协议与完整engine适配应明确边界，避免重复维护两个owner和两个重试调度器。

这也决定了框架不能把“默认本地时能共同事务”承诺为“换远端后仍自动业务exactly-once”。可信工具binding、业务幂等与资源退出依然是独立合同。

至少要验证以下区分性场景：claim前/后kill；业务finalize后runtime失败；外部请求成功而receipt写入前kill；旧attempt晚到retry与提交；lease续约失败但Runtime继续；多consumer共享额度；不同代码/模型配置恢复；重启大量pending；drain超时后的业务状态与真实资源。正常mock结果、普通异常retry、仅查看queue状态都不能替代这些证据。

## 原始来源

下列文档均于 2026-10-12 获取；源码链接固定到上述 release commit。滚动文档与源码验证分开标注。

[T-release]: https://github.com/temporalio/sdk-python/releases/tag/1.34.0
[T-pypi]: https://pypi.org/pypi/temporalio/1.34.0/json
[T-server-release]: https://github.com/temporalio/temporal/releases/tag/v1.32.1
[T-queue]: https://docs.temporal.io/task-queue
[T-history]: https://docs.temporal.io/encyclopedia/event-history
[T-activity]: https://docs.temporal.io/activity-definition#idempotency
[T-timeouts]: https://docs.temporal.io/develop/python/activities/timeouts
[T-standalone]: https://docs.temporal.io/standalone-activity
[T-standalone-python]: https://docs.temporal.io/develop/python/activities/standalone-activities#prerequisites
[T-version]: https://docs.temporal.io/develop/python/workflows/versioning
[T-worker-version]: https://docs.temporal.io/worker-versioning
[T-deploy]: https://docs.temporal.io/self-hosted-guide/deployment
[T-shutdown]: https://docs.temporal.io/encyclopedia/workers/worker-shutdown
[T-source-worker]: https://github.com/temporalio/sdk-python/blob/a4c2bb38c3bad43498dfa6bd849e8d85bb684e94/temporalio/worker/_worker.py#L217
[D-release]: https://github.com/dbos-inc/dbos-transact-py/releases/tag/3.2.0
[D-pypi]: https://pypi.org/pypi/dbos/3.2.0/json
[D-workflow]: https://docs.dbos.dev/python/tutorials/workflow-tutorial
[D-step]: https://docs.dbos.dev/python/tutorials/step-tutorial
[D-queue]: https://docs.dbos.dev/python/tutorials/queue-tutorial
[D-recovery]: https://docs.dbos.dev/production/workflow-recovery
[D-conductor]: https://docs.dbos.dev/conductor/distributed-recovery
[D-version]: https://docs.dbos.dev/python/tutorials/upgrading-workflows
[D-conflicts]: https://docs.dbos.dev/explanations/concurrent-executions
[D-database]: https://docs.dbos.dev/python/tutorials/database-connection
[D-transactions]: https://docs.dbos.dev/python/tutorials/transaction-tutorial
[D-source-queue]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_queue.py#L692
[D-source-dequeue]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_sys_db.py#L4777
[D-source-owner]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_sys_db.py#L3209
[D-source-recovery]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_recovery.py#L14
[D-source-step]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_core.py#L2289
[D-source-transaction]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_datasource.py#L885
[C-release]: https://github.com/celery/celery/releases/tag/v5.6.3
[C-tasks]: https://docs.celeryq.dev/en/stable/userguide/tasks.html
[A-release]: https://github.com/python-arq/arq/releases/tag/v0.28.0
[A-docs]: https://arq-docs.helpmanual.io/
[P-runners]: https://docs.prefect.io/v3/concepts/task-runners
[P-workers]: https://docs.prefect.io/v3/concepts/workers
[P-background]: https://docs.prefect.io/v3/how-to-guides/workflows/run-background-tasks
[P-task-worker]: https://github.com/PrefectHQ/prefect/blob/e9bc08239af12f2bbfccab7a8e15b9f0a0ec6865/src/prefect/task_worker.py#L80
[P-ack]: https://github.com/PrefectHQ/prefect/blob/e9bc08239af12f2bbfccab7a8e15b9f0a0ec6865/src/prefect/client/subscriptions.py#L54
[P-dispatch]: https://github.com/PrefectHQ/prefect/blob/e9bc08239af12f2bbfccab7a8e15b9f0a0ec6865/src/prefect/server/task_queue.py#L1
[P-engine]: https://github.com/PrefectHQ/prefect/blob/e9bc08239af12f2bbfccab7a8e15b9f0a0ec6865/src/prefect/task_engine.py#L662
[P-cache]: https://docs.prefect.io/v3/advanced/caching
[P-global]: https://docs.prefect.io/v3/concepts/global-concurrency-limits#timed-leases
[P-local]: https://docs.prefect.io/v3/advanced/background-tasks
[L-checkpoints]: https://docs.langchain.com/oss/python/langgraph/checkpointers
[L-functional]: https://docs.langchain.com/oss/python/langgraph/functional-api#idempotency
[L-fault]: https://docs.langchain.com/oss/python/langgraph/fault-tolerance#graceful-shutdown
[L-executor]: https://github.com/langchain-ai/langgraph/blob/799c2f04fd3a56186e24e9fb8bbf3568e543cfbe/libs/langgraph/langgraph/pregel/_executor.py#L40
[L-version]: https://docs.langchain.com/oss/python/langgraph/backward-compatibility
[LS-server]: https://docs.langchain.com/langsmith/agent-server#runtime-architecture
[LS-scale]: https://docs.langchain.com/langsmith/agent-server-scale#request-vs-run-concurrency
[LS-env]: https://docs.langchain.com/langsmith/env-var-self-hosted#bg_job_max_retries
[LS-standalone]: https://docs.langchain.com/langsmith/deploy-standalone-server
[H-consumer]: https://github.com/coleifer/huey/blob/7e937f9d741f4fbc6fa8a18e589826f5f61554aa/docs/consumer.rst#L250
[H-storage]: https://github.com/coleifer/huey/blob/7e937f9d741f4fbc6fa8a18e589826f5f61554aa/huey/storage.py#L952
[H-retry]: https://github.com/coleifer/huey/blob/7e937f9d741f4fbc6fa8a18e589826f5f61554aa/docs/guide.rst#L177
