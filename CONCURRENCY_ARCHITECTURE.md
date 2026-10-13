# AgentLoom Worker 并发方案

状态：有限批次与 macOS 本地持久执行均已实现并验收。路线更新：2026-10-14。原研究基线：`94491c2d`。

持久执行已接入 DBOS 3.2.0 的公开队列、事务提交和恢复接口，自研内核停止扩展。当前方案见 [开源接入规格](docs/specs/durable-worker-open-source-engine-integration-v2.md)。本机框架故障、controller 和 NewsAgent 全日验收通过：2025-09-01 完成封存与计分，失败与恢复见 [验收报告](docs/reviews/durable-worker-dbos-news-acceptance-20261014.md)。

本轮已与用户确认：`concurrency` 是**单次 `map()` 的上限**。保留一行调用和
`system 配置 < Supervisor binding < 本次代码参数` 的覆盖关系。

源码与开源项目依据见[原研究记录](docs/research/agentloom-concurrency-architecture-research.md)和
[执行合同复核](docs/research/agentloom-worker-execution-contracts.md)。原研究中的备选 Interface
保留作比较，当前设计以本文件为准。

有限批次的交付证据见 [Worker batch 验收](docs/reviews/worker-batch-execution-v1-acceptance.md)。持久执行的上游选择、产品入口和逐项证据见 [DBOS 接入状态](docs/research/dbos-product-integration-status-20261014.md)，部署范围遵循 [本地默认与可替换存储边界](docs/adr/0001-local-durable-execution-with-pluggable-store.md)。

## 开发者只交任务，框架负责执行

业务代码先决定任务，再调用框架：

```python
plan = initial_planner.plan(request)
outcome = app.worker("workflows/initial.yaml").map(plan.items)
initial_reducer.finalize(plan, outcome)
```

临时调整本次批次：

```python
outcome = app.worker("workflows/initial.yaml").map(plan.items, concurrency=4)
```

开发者不创建线程池、读取并发配置、操作 Future，也不逐项关闭 Runtime。
Planner 和 Reducer 仍属于 Application；框架无法仅凭“20 并发”决定新闻该怎样拆批。

完整链路：

```text
完整业务请求
  → Application Planner：生成稳定的 WorkItem
  → BoundWorker.map：解析本次配置，按空闲槽位执行
  → Application Reducer：检查完整性，汇总，规划下一阶段
```

`app.worker()`、`BoundWorker`、`Application.open()` 已由 `agentloom.app` 导出。NewsAgent 已迁移到框架持久入口；有限 `map()` 与持久队列各自保持原容量作用域。

## 并发上限只约束本次批次

`map(items, concurrency=20)` 表示：本批次由 Runner 直接启动、且尚未完成清理的
顶层 Worker invocation 最多 20 个。完成一个并释放其资源，才启动下一个。

| 场景 | 首版行为 |
| --- | --- |
| 同一个批次有 37 项 | 最多同时执行 20 项，完成后继续补位 |
| 批次只有 7 项 | 只执行 7 项，任务不会为了填满槽位而重新切分 |
| 同一 Worker 的两个 `map()` 同时执行，各设 20 | 两个独立批次，合计最多 40 个顶层 invocation |
| 本次传 4，下次不传 | 本次使用 4，下次重新采用该 binding 的默认值 |
| Worker 内又调用子 Agent | 子调用不计入这次 `map()` 的顶层槽位 |
| 模型一次返回多个 Tool call | 由 Pi/smolagents Runtime 自己调度 |

因此，20 不是整个 Run 的 Runtime、线程、进程或模型请求总数。首版不跨批次共享容量，
不承诺跨批次公平性。Agent-as-Tool 保留现有单次调用路径，也不受 `map()` 上限约束。

每个批次拥有独立的有界提交窗口；线程池只是内部 Implementation。不要把池挂在
process-global 单例或可反复调用的 `BoundWorker` 上，避免同步子调用排队回父池而死锁。
未来若需要跨批次/Agent-as-Tool 共享额度，再设计独立 admission 和父子 lineage 合同。

## 配置跟随调用方，代码参数最后覆盖

NewsAgent 没有 Supervisor Agent，由 NewsPipeline / NewsService / CLI 编排。
具体 Worker 的默认并发放在 Application 的系统配置中：

```yaml
# applications/news_agent/config/system.yaml
worker_execution:
  workers:
    workflows/initial.yaml:
      concurrency: 20
    workflows/refined.yaml:
      concurrency: 30
    workflows/validation.yaml:
      concurrency: 20
```

key 是相对 Application 根目录的 Worker YAML 路径。`initial` 这类业务阶段名无法唯一标识
Worker。Worker YAML 继续描述 Prompt、模型、Tool 和一次输入输出合同。

如果所有 Worker 需要共同默认值，可以使用：

```yaml
# config/system.yaml：项目共同默认值，可省略
worker_execution:
  defaults:
    concurrency: 1
```

Application 也可以设置 `worker_execution.defaults.concurrency`。项目根只放共同默认值，
不列 NewsAgent 私有路径。完整优先级为：

```text
框架默认值 1
  < 项目 system 默认值
  < Application system 默认值
  < Application system 中对应 Worker 的值
  < 当前 Supervisor 的 Worker binding
  < 本次 map 的 concurrency 参数
```

`system` 各层属于用户所说的 setting 配置。配置值采用标量替换；显式代码参数直接覆盖
本次批次的较低层值，不做 `min()`、相加或按 RPM 推算，不回写文件或修改 binding。
省略或传 `None` 表示不覆盖。

有 Supervisor 的 Application，目标 schema 可在调用方的引用处写默认值：

```yaml
worker_agents:
  - path: workflows/worker_agents/research.yaml
    execution:
      concurrency: 4
```

这里是**属于该 Supervisor 的批量调用默认值**。需要由声明式 binding Adapter 把它交给
同一个解析器；`app.worker(path)` 不得按路径猜测其他 Supervisor 的策略。它不会将多个
独立的 Agent-as-Tool call 自动变成一个批次，也不限制模型单回合发出多少个 call。

首个交付先支持 system 和代码覆盖；Supervisor binding 支持作为独立后续交付，覆盖顺序
保持上述合同。支持前，预检必须明确拒绝 `worker_agents[].execution`，不能接受后忽略。

### 配置错误必须在启动 Worker 前暴露

- 每个已提供的并发值都必须是严格正整数，拒绝 `true`、字符串、0、负数和 `auto`；
- 较低层的非法值即使被较高层覆盖也要报错，不能悄悄回退；
- `worker_execution` 子 schema 拒绝拼错的未知字段；
- Worker key 按现有 definition 路径规则验证，禁止越界、符号链接和重复规范身份；
- Application 配置表中的路径必须存在并能通过 Worker 合同预检；
- 某个合法 Worker 没有单独配置时允许使用默认值；
- 其他合法 Worker 未参与本次调用，不构成配置错误。

NewsAgent 可以在自己的组合预检中要求三个阶段都有明确配置，这属于它的完整性要求。
通用 Runner 不强迫每个 Worker 都配置一次。

当前 `RootSettings` 使用 `extra="allow"`，未知 `worker_execution` 会进入 extra，
且 0、`true`、`auto` 都可被接受。接受 YAML 不代表具备执行能力；实现时必须新增类型校验、
保留未合并来源，并接入现有配置快照。不能只从裸字典临时取值。

首版不引入 `process_hard_limit`。旧提案中的 `resolved_concurrency <= 64` 只检查单次值，
无法约束多个批次的进程总量；把它命名成全进程上限会给出错误保证。跨批次资源预算需另行设计。

## 固定配置与输入，记录到本次批次

冻结分两次发生：

- Application 执行作用域固定 project/Application system snapshot；首次绑定 Worker 时固定
  definition 和有效模型配置，同一作用域再次绑定同路径不重新读取；
- 每次 `map()` 在启动任何 Worker 前，复制并校验全部输入，解析本次覆盖，保存 execution record。

本次代码参数到调用时才出现，因此最终并发值在**本次批次启动前**冻结，无需在整个
Application Run 启动前预测所有调用。新 Application 执行作用域才重新读取默认配置。

每次批次生成独立 `batch_id`，记录至少包含：

```python
ResolvedWorkerExecution(
    concurrency=4,
    source="call_override",
    chain=(
        ("framework_default", 1),
        ("application_worker", 20),
        ("call_override", 4),
    ),
)
```

同一记录还保存父 Run identity、规范 Worker ID、definition/config revision、输入指纹和
本次 failure 策略。只记录解析并发字段所需的来源信息，不能把凭据写入记录。

每个批次写自己的执行记录；路径分配通过 `RuntimeContext` 扩展完成，根 manifest 由
Application 生命周期统一汇总批次引用。禁止 sibling Worker 并发读改写同一个根 manifest。
当前 `RuntimeContext.update_manifest()` 的 read-modify-write 会丢失并发更新，原子替换文件
只能保证文件完整，无法保证两次更新都被保留。

首版内存 `map()` 不提供崩溃恢复或自动重放。记录用于审计，不能据此声称已有恢复能力。
之后如增加恢复，必须显式保存并选择 definition/config/execution snapshot；输入关联稳定
也不代表真实模型输出可复现。

## 公开 Interface 保持小

```python
class Application:
    def worker(self, path: str) -> BoundWorker: ...


class BoundWorker:
    @property
    def definition_id(self) -> str: ...

    def run(self, input: Mapping[str, JSONValue]) -> WorkerOutcome: ...

    def map(
        self,
        inputs: Sequence[WorkItem],
        *,
        concurrency: int | None = None,
        failure: Literal["collect", "fail_fast"] = "collect",
        stop: StopToken | None = None,
        event_sink: WorkerEventSink | None = None,
    ) -> BatchOutcome: ...

    def as_tool(self) -> ToolBinding: ...
```

`run()` 是一个输入的便利入口；`as_tool()` 接入现有 nested invocation 路径。二者都不
继承另一次 `map()` 的临时覆盖。内部 Runner 接收已解析的 binding 和 execution snapshot，
无需再实现一套优先级。

Application facade 必须接入现有 Run 生命周期。Python/CLI 编排者的目标形式是：

```python
with Application.open(APP_ROOT) as app:
    plan = initial_planner.plan(request)
    outcome = app.worker("workflows/initial.yaml").map(plan.items)
    initial_reducer.finalize(plan, outcome)
```

已有 Application Run 内的 facade 复用它的 config、RuntimeContext、trace 和 resource owner；
独立打开时，通过共享的生命周期 Module 分配和结束 Run。不得复制一份 runner cleanup，
也不得为了 NewsAgent 凭空创建 Supervisor Agent。

批次在 admission 前登记到 Application owner。作用域退出时拒绝新批次、请求已登记批次停止
admission，并等待它们清理后才关闭根资源；退出后的旧 `BoundWorker` 立即报作用域已关闭。
owner 的登记与退出转换必须同步，防止最后一个批次漏登记。Worker/observer 不能同步关闭
自己所属的 Application 并等待自身结束，遇到这种调用应立即报错。

路径预检复用 `app/definition.py` 的 snapshot 和 Worker 验证。NewsAgent 的 Worker 不在
`worker_agents/` 目录下，不能仅凭目录名决定角色；直接 Worker 入口须明确使用 Worker 预检。

首版 `map()` 是同步阻塞 Interface。async 调用方通过明确的 Adapter offload，Runner
内部不猜测 event loop、不偷偷调用 `asyncio.run()`；`amap()` 留待真实调用方需要时设计。

### 输入和结果合同

| 对象 | 合同 |
| --- | --- |
| `WorkItem` | batch 内唯一且非空的 ID，JSON input 的自有不可变快照 |
| `WorkPlan` | 业务生成的稳定计划 ID、items 和输入指纹；无需通用 Planner 基类 |
| `WorkerOutcome` | 输入 ID/index、可选 invocation identity、成功/失败/未开始即取消、output 或结构化 error、usage 和清理信息；未开始项的 invocation identity 为空 |
| `BatchOutcome` | batch identity、终态、按输入顺序排列的完整 Outcomes、execution record 和 observer diagnostics |

终态按优先级确定：基础设施或清理一致性错误抛 `WorkerBatchError`；有 item 失败则为
`failed`；没有失败且执行期间观察到显式 stop 则为 `stopped`；其余为 `completed`。
`fail_fast` 返回完整 Outcomes 并标记批次 `failed`，未开始项记录 fail-fast 取消原因。

`frozen=True` 仅冻结 dataclass 字段，不冻结嵌套 dict/list。`WorkItem` 需要深度复制并冻结
JSON；每次 Runtime 调用再得到自己的可变输入副本。运行中改变调用者原始字典不得改变任务。

首版只接受有限 `Sequence[WorkItem]`，不接 generator。启动前验证全部 ID 和输入 schema；
空输入返回空的已完成批次；重复 ID 或非法输入令整个调用预检失败，零 Worker 启动。

结果始终按输入 index 返回；成功 output 为 `None` 仍是成功，不能用真假值判断状态。
进度在完成时上报，不等待前面较慢的输入。controller 串行投递事件，并分配 `event_seq`；
顺序表示接收顺序，不承诺同时完成的线程之间存在精确物理时间全序。

执行窗口为 O(concurrency)；复制全部输入和保存全部结果仍为 O(item 数及 payload 大小)。
不声称整个批次内存只随并发数增长。

## Runner 隐藏完整 invocation 生命周期

Runner 的内部循环是“补空闲槽位 → 等任意完成 → 收集结果 → 再补位”。
项目最低支持 Python 3.12，而 `Executor.map(buffersize=...)` 是 3.14 新增，首版采用显式
`submit/wait(FIRST_COMPLETED)` 窗口，不依赖新版参数，也不因保序结果而阻塞补位。

每个 item：

1. 在执行前再次检查 stop，生成 batch/item/invocation identity；
2. 独立 capture/rebind Context，为该项创建 fresh Agent；
3. 复用 `AgentInvocation` 执行、Hook、checkpoint、usage 和资源关闭路径；
4. 等 Runtime 和实例资源清理后，生成 Outcome，再释放该项槽位。

不能只调用底层 `runtime.run()`，绕过现有生命周期；也不能为每个 item 重复建立根 Run。
所有创建和关闭失败都保留原始原因。关闭失败令批次停止补位，抛带 phase 和清理证据的
`WorkerBatchError`；无法确认已关闭的资源仍交 owner 管理，不能先释放槽位再继续创建。
现有调用清理入口需要保留结构化 phase，不能靠异常字符串识别 cleanup failure，
也不能在关闭失败后声称已经完整 drain。

`copy_context()` 复制 binding，不复制 binding 中的对象。至少审计以下状态：

| 状态 | 处理方式 |
| --- | --- |
| Run identity、已冻结的配置、root memory snapshot | 有明确并发合同后共享 |
| agent/subtask identity、Agent state、runtime、tool host 和实例资源 | 每项派生或独立创建 |
| checkpoint coordinator、Hook/trace writer、usage、provider budget、workspace | 逐项核实同步和归属，不能因已复制 Context 就认定安全 |

每次提交使用不同的 `Context` 对象。一个 item 结束只关闭自己的实例资源，不能关闭其他
batch 或 sibling 的资源；根资源在全部批次 drain 后由 Application 生命周期关闭。

### 失败和停止必须反映实际状态

| 情况 | 行为 |
| --- | --- |
| 普通 item/Runtime 失败，`collect` | 保存失败，其他 item 继续 |
| 首个失败被观察到，`fail_fast` | 停止新 admission，取消尚未开始的工作，等待已开始工作清理 |
| `StopToken` | 同样停止 admission 并 drain，未开始项记录取消原因，已开始项记录真实终态 |
| executor、批次记录持久化或一致性机制故障 | 停止批次并清理，抛携带部分 Outcomes 和 cleanup errors 的 `WorkerBatchError` |
| 进度 observer 抛普通异常 | 记录 diagnostics，后续停用该 observer，Worker 结果不变 |
| `KeyboardInterrupt` 等控制异常 | 停止 admission、drain 后传播，附带已完成结果和清理证据 |

若一次收集到多个完成项，先处理它们的失败和 stop 状态，再补位。线程包装入口需检查 stop，
以覆盖“已提交但尚未开始”的竞争；已经跨过启动判定的 item 属于在途工作，必须等待它结束。

首版只承诺 stop-admission-and-drain。`Future.cancel()` 无法停止已经运行的线程。
Pi 终止 Node 进程也不等于停止 Python Tool callback；其 transport 会等待这些 callback。
因此首版没有固定的停止时限，也不增加虚假的线程硬超时。

Runner 不默认重试整个 Worker，不提供默认 circuit breaker。单次调用可能已经写入结果或
产生外部副作用；仅有 `retryable=True` 不足以证明可以安全重放。重试由 Application 的业务
幂等和持久 task attempt 合同决定。

## 持久执行进入框架，NewsAgent 保留业务编排

用户已确认保持新闻筛选、校验、计分和失败恢复业务规则，评估统一执行层；持久任务和恢复纳入框架。默认本地持久化，首版单机多进程，存储接口可替换为未来远程后端。一次有限 `map()` 的合同保持不变，持久消费者有独立入口与容量域。

以下执行机制由选定的上游引擎承担，AgentLoom 提供公开接入与 Runtime 生命周期合同：

1. 持久任务身份、输入与 attempt 记录、入队和可观察状态。
2. 单机多进程共享 queue 容量，有空位才原子领取，单项完成后持续补位。
3. 上游 workflow 恢复和有次数／类别限制的重试；业务旧 attempt 条件提交由业务库授权校验。
4. 每个 attempt 的公开宿主准备接口，绑定独立 session、工具固定参数及资源；它与有限 map 共用 invocation 内核。
5. 停止新 invocation 启动、停止旧 Worker、确认本机资源退出后恢复补位。退出不明时保留可观察的阻塞状态，不仅归还数据库额度。
6. 分别记录业务提交、运行结果和资源清理回执，必需提交接口与可失败 event observer 分开。

NewsAgent 继续拥有完整新闻分组、分类与判断合同、三阶段及行业净校验衔接、候选替换、封存与计分。现有 planner 使用目标容量及输入预算分组；本轮不把 `map(concurrency)` 当成分组算法，也不借执行迁移修改业务判断规则。

当前 Store 已保护旧 owner 的工具写入，但隔离故障注入复现了无 owner 的 retry 旁路：旧 attempt 1 晚到的异常可把 attempt 3 的失败重新置为 pending，最终 claim 出第 4 次尝试。框架的 fail/retry/recovery 必须在同一 attempt 条件下原子转移，不能只保护结果写入。

持久任务执行按可能重复尝试看待；外部 LLM/AnySearch 请求不承诺恰好一次。News 新业务尝试继续按当前定义与模型重算整项，并保存本次配置来源；恢复旧执行快照是另一合同，不能由后端 replay 隐式改变现有行为。

本地接入采用 DBOS 3.2.0、一个 executor 和多个 CLI producer；Prefect 等对照的取舍见 [开源选择](docs/research/durable-open-source-selection-20261014.md)。业务库与执行库分别使用 SQLite，通过业务库内的完成回执和原子写权限协调；不保留自研领取循环，不承诺跨库原子提交。

开发者通过稳定 namespace 提交明确业务 revision：

```python
with Application.open(application, project_root=root) as app:
    with app.durable(root=namespace_root) as execution:
        handles = execution.worker("workflows/initial.yaml").submit_many(items, revision="1")
```

独立消费者使用 `loom durable serve APPLICATION --worker workflows/initial.yaml --root NAMESPACE_ROOT`。任务身份跨 Application Run 稳定；相同任务及 revision 的不同输入会拒绝。取消和恢复通过同一公开执行对象调用，恢复先核对旧本机资源，再进入原 Worker 队列。当前 OS Adapter 为 macOS launchd；Linux、真实 PostgreSQL 和多机执行仍未验证。

## 输出预算缺陷独立修复

完整实跑复现非 Codex 的未配置预算被框架默填 16,384，四个初筛批次因此耗尽输出，且 Provider 的 incomplete reason=length 被 Pi 归为普通错误。这个问题先按独立合同修复，不能靠提高并发或持久重投掩盖。

用户已确认：未配置不额外向 Provider 注入输出 cap，显式数字才限制，配置覆盖中的 null 可解除继承；输入预留独立。必须提供 cap 的协议使用已验证能力或要求显式配置，未知时提前报错。见 [预算 ADR](docs/adr/0002-explicit-output-cap-with-independent-input-reserve.md) 和 [完整根因分析](docs/research/model-output-budget-default-review.md)。

只删 Python 默认值不够：离线真实 SDK 探针复现 Pi 会从 context metadata 补出 495,897 的请求上限。产品已贯通配置、Pi 最终 payload、smol/LiteLLM、内部摘要预算和实际 HTTP 合同，相关独立回归 294 项通过。[预算修复证据](.agentloom/validation/output-cap-contract-port-20261014-v1/REPORT.md)。本次全日初筛使用显式 32,768，后续校验经授权调为 65,536；本地调参不替代默认值的 HTTP 合同验证。

## 实施顺序和验收

| 交付 | 内容 | 必须通过的验证 |
| --- | --- | --- |
| 配置与预检 | typed worker_execution、路径身份、来源链、冻结记录；去掉 concurrency → parallel_tools 的错误映射 | 严格整数、各 system 层与代码覆盖、非法低层不被掩盖、未知字段拒绝 |
| 生命周期与有限 map | Application facade、BoundWorker、输入/Outcome、有界窗口、stop/drain | 接入现有 invocation 和资源归属，不重复根 Run，不泄漏 sibling 资源 |
| Supervisor 默认值 | 显式 binding Adapter 和 execution schema | system < Supervisor < 本次参数；as_tool 不被误说成受 map 上限约束 |
| 输出预算语义修复 | 未指定、显式 cap、null 解除、输入预留独立；Responses length 归类 | 真实 SDK HTTP payload 合同、Pi/smol Application runner、完整 NewsAgent；不能只删默认常量 |
| 持久执行后端比较 | 成熟开源引擎公开接入；首轮 DBOS，Prefect 对照；停止自研内核扩展 | 多进程提交共享额度、kill/recovery、旧资源实际退出、业务权限原子撤销、schema/version、提交后 ACK 丢失 |
| NewsAgent 迁移 | 业务 planner/ledger/阶段与计分规则保留，迁移通用消费和恢复机制 | 完整单日、真实模型与 AnySearch；全量工具覆盖、封存计分、恢复与资源退出均有证据 |

`.batch()` 与 `run_agents_parallel()` 在迁移期转调新内核，并明确结果顺序和失败策略的变化；
删除 `auto` 前清点实际调用者，给出迁移错误。Runtime 继续自己处理 Tool calls，
`is_concurrency_safe` 的 smolagents 修复单独验收，不作为建立新 Worker Scheduler 的理由。

首版合同至少覆盖：

| 场景 | 预期证据 |
| --- | --- |
| 足够多的受控任务，容量 1/2/20/32 | 峰值不超过 N，任务具备并行条件时能达到 N，32 不被隐藏截断为 20 |
| 同一路径两个并行批次，各 4 | 各自峰值不超过 4，可合计达到 8，记录互不覆盖 |
| 连续覆盖 4、默认 20、覆盖 2 | binding 不变，三份 execution record 能解释最终值 |
| 调用后修改原始嵌套 input | 实际执行 payload 和输入指纹保持不变 |
| 首项慢、后项快 | 后项完成和补位不等待首项，最终结果仍保序 |
| collect/fail_fast/stop/observer 故障 | Outcome 与实际是否启动对应，控制异常和基础设施错误不被吞掉 |
| Runtime 创建/关闭失败 | 保存主错误及 cleanup errors，释放实例归属，不宣告伪成功 |
| 多轮同 Worker、两个 batch、嵌套子 Agent | Context/Hook/checkpoint/resource owner 不串，子调用不回到父池等待 |
| Pi 与 smolagents 真实 Application runner/CLI | 同一外部合同成立，返回后无遗留 invocation 或所拥有资源 |
| NewsAgent 固定输入，容量 1 与 20 | WorkPlan ID、payload、任务数量相同；stub 结果一致，真实模型验证覆盖率和阶段完整性 |

并发测试使用 Event/Barrier 和 deadline，不能仅靠 sleep 判断正确。真实模型不要求逐字相同，
并发数改变不得改变模型得到的任务合同。持久迁移另做崩溃、lease 失效、旧 owner 迟到写和
CLI 重启验证；单元测试不能替代真实 Application 和 NewsAgent runner/CLI 证据。

有限批次相关验收已经交付，见前述验收记录。持久框架扩展仍为后续要求。本轮完整真实 NewsAgent 验收使用 DeepSeek v4.1 与 AnySearch，首轮因框架隐式 16,384 输出预算失败；用户授权将本地预算提高为 32,768 后，官方 run-day 与 evaluate 均退出 0 并封存。初筛 20 批/1,426 条、精筛 30 批/447 条、校验 23 项/189 候选（含 3 项行业净方向补充）全部完成；Store、runtime scope 和客户端 HTTP 边界的阶段观测峰值均为 20/30/20，供应商内部执行峰值未知。原 16 批完成记录未重做，累计采样 83 个归属进程最终存活 0。详见 [完整验收记录](.agentloom/validation/news-concurrency-20261012/full-2025-08-24-20261012T083423Z/campaign/full-day-acceptance.md)。产品代码未修改。
