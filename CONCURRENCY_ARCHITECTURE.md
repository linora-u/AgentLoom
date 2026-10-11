# AgentLoom Worker 并发方案

状态：设计提案，产品代码尚未实现。更新：2026-10-11。源码基线：`bc5cff6e`。

本轮已与用户确认：`concurrency` 是**单次 `map()` 的上限**。保留一行调用和
`system 配置 < Supervisor binding < 本次代码参数` 的覆盖关系。

源码与开源项目依据见[原研究记录](docs/research/agentloom-concurrency-architecture-research.md)和
[执行合同复核](docs/research/agentloom-worker-execution-contracts.md)。原研究中的备选 Interface
保留作比较，当前设计以本文件为准。

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

`app.worker()`、`BoundWorker`、`Application.open()` 都是待实现的 Interface。当前
`agentloom.app` 尚未导出这些能力，不能把示例当作现有可运行代码。

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

## NewsAgent 分两步接入

有限计划先走上述 `map()`；修改并发值只改变同时执行数，不修改 WorkPlan。任务大小仍在
NewsAgent 的 `config/settings.yaml` 中由 token 上限、相关报道和完整分段规则决定。
不引入未经评测的 `target_items_per_task: 200`。

持久任务继续由 NewsAgent Store/Consumer 负责注册、claim、lease、renew、recover、
StageLedger 和阶段 seal。首版不把一次 `map()` 伪装成持久后台消费者，也不先发布通用
`WorkerRunner.serve()`。以后以 NewsAgent 专用 Adapter 复用执行内核，claim 数只能匹配空闲槽位，
不预领无法立即执行的 lease。

目前的本库结果提交已经在同一个 SQLite transaction 内检查 owner、running 状态和 lease；
这能够拒绝旧 attempt 的本库写入，不必为此一律重造单调 fencing token。继续验证所有
renew/commit/settle 的检查都覆盖当前 attempt。对外部副作用，仍需下游支持的幂等键或 fencing；
一个本地 owner 检查无法替外部系统保证 exactly-once。

持久调用按 at-least-once 看待。现有恢复可能使用当前模型配置，尚未实现冻结 execution
snapshot 的恢复。迁移时必须明确区分“重试当前配置”和“恢复原 snapshot”，不能悄悄承诺后者。

NewsAgent 当前分片使用 `initial_workers/refined_workers/validation_workers`，应先拆掉
任务大小与执行容量的耦合。框架批处理验收与持久迁移分别交付，避免为了替换线程池先重写数据库。

## 实施顺序和验收

| 交付 | 内容 | 必须通过的验证 |
| --- | --- | --- |
| 配置与预检 | typed worker_execution、路径身份、来源链、冻结记录；去掉 concurrency → parallel_tools 的错误映射 | 严格整数、各 system 层与代码覆盖、非法低层不被掩盖、未知字段拒绝 |
| 生命周期与有限 map | Application facade、BoundWorker、输入/Outcome、有界窗口、stop/drain | 接入现有 invocation 和资源归属，不重复根 Run，不泄漏 sibling 资源 |
| Supervisor 默认值 | 显式 binding Adapter 和 execution schema | system < Supervisor < 本次参数；as_tool 不被误说成受 map 上限约束 |
| NewsAgent 迁移 | Planner 与容量分离，之后用专用 Consumer 复用内核 | 固定输入下计划不随容量变化；claim/renew/迟到提交/恢复各有证据 |

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

本轮只更新方案和研究。上述验收是后续实现要求，尚未作为已实现能力通过。
