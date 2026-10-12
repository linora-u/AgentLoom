# AgentLoom 并发架构研究

首次研究：2026-10-10。方案复核：2026-10-11。

> 设计决策更新：本文件保留源码事实、开源项目证据和备选方案。最终方案以项目根目录
> [CONCURRENCY_ARCHITECTURE.md](../../CONCURRENCY_ARCHITECTURE.md) 为准。复核后决定：
> 多个 Tool call 的调度继续由 Pi 或 smolagents Runtime 自己处理；AgentLoom core
> 只提供 Tool schema、metadata、权限和结果提交契约，不新增通用 Tool Scheduler。
> 本轮框架改造集中解决 Runtime 外层的 Worker 并发。
> NewsAgent 没有 Supervisor Agent；其并发策略由现有 NewsPipeline / NewsService / CLI
> 从 Application `config/system.yaml` 按规范 Worker 路径加载并构造成 WorkerBinding。
> Supervisor YAML binding 只适用于
> 原本就有 Supervisor Agent 的其他 Application，不是 NewsAgent 的前置结构。
> 对 Application 开发者，目标调用面收敛为
> `app.worker("workflows/initial.yaml").map(plan.items)`；框架负责 definition/config
> 绑定和并发执行，开发者仍负责业务 Planner 和 Reducer。
> 2026-10-11 用户进一步确认：并发上限只约束单次 `map()`，允许本次代码覆盖；
> system 默认值 < Supervisor binding < call override。首版不共享 Agent-as-Tool 容量，
> 不引入全进程 `process_hard_limit`，也不默认重试整个 Worker。
> 本文四至七节及后面的早期 Interface 是研究历史，不作为实施接口：旧 `max_in_flight`、
> 通用 `serve()`、共享容量、统一 Tool Scheduler 和“必须使用单调 fencing token”已收缩或改判。
> 最新合同和新一手证据见[执行合同复核](agentloom-worker-execution-contracts.md)。

## 结论

当前实现把三种不同问题混成了一个 `concurrency`：

1. **Worker fan-out**：同一个 Worker Agent 对多份输入并发运行；
2. **Tool fan-out**：一次模型响应产生多个 tool call 后，runtime 如何执行；
3. **Provider admission control**：模型请求的同时在途数、RPM、429 退避和排队。

这三者的生命周期、失败域和资源单位不同，不能共用一个字段，也不应该复用同一个线程池。

建议：

- AgentLoom 新建一个深的 **Worker Runner Module**，拥有跨 Pi/smolagents 的 Worker fan-out；
- Pi 和 smolagents 继续各自实现一次 Agent run 内的 Tool Dispatcher，不把其线程池上提；
- Agent-as-Tool 只是 `Worker` 的一个 **Adapter**，不再是批量并发的 Interface；
- 删除 `auto`，单次批处理使用明确的正整数 `concurrency`；
- provider 的并发上限/RPM 归模型调用 **Seam**，与 Worker 数分开；
- NewsAgent 保留任务切分、阶段依赖、SQLite 租约和业务提交；有限批处理先走 Worker Runner，
  持久 claim → invoke → settle 循环后续通过 NewsAgent 专用 Adapter 验证。

删除测试也支持这个判断：删除现有 `ParallelAgentExecutor`，NewsAgent 的线程池、认领、租约、恢复、停止和重试仍全部存在；现有 Module 没有为这类应用创造足够 **Leverage** 和 **Locality**。

## 一、当前架构事实

### 1. Agent-as-Tool 同时承担了两个不相干的 Interface

`YamlConfiguredAgent.agent_as_tool()`：

- 单次调用时为每次调用创建新的 Agent，避免 `memory`/`state` 串扰；
- 又在返回的 Python 函数上动态挂载 `.batch()`；
- `.batch()` 再创建 `ParallelAgentExecutor`。

见：

- [`src/app/factory.py`](../../src/app/factory.py) 的 `agent_as_tool()`；
- [`src/app/factory.py`](../../src/app/factory.py) 的 `run_agents_parallel()`；
- [`src/execution/concurrency/parallel_executor.py`](../../src/execution/concurrency/parallel_executor.py)。

“把 Agent 暴露成模型可调用的 Tool”和“由应用批量执行 Worker”是两个不同的 Seam。前者关心 schema、父子运行、usage、checkpoint 和工具结果；后者关心 fan-out、背压、取消、失败和结果顺序。把 `.batch()` 动态挂在函数对象上会让 Interface 变浅：类型无法表达、IDE 不可发现、任意 callable 与 Worker 的差异靠私有属性判断。

### 2. `concurrency` 同时泄漏到 runtime capability 判断

Agent YAML 的 `concurrency` 本来被文档定义为批量 Worker 数，但
`AgentConfigNormalizer.runtime_requirements()` 又根据它生成
`RuntimeRequirements.parallel_tools`。

见 [`src/app/validation.py`](../../src/app/validation.py)。

因此同一配置被解释成：

- Worker 批量调用的 `max_workers`；
- runtime 是否必须支持一次模型回合中的多个 tool call。

这是语义错误，不是命名问题。即使 Worker fan-out 为 1，模型也可能在单次 Agent run 中发出多个 tool call；反过来，Worker fan-out 为 20 也不代表每个 Worker 必须并行执行内部工具。

### 3. 当前 `ParallelAgentExecutor` 的 Interface 没有兑现直觉承诺

当前实现有这些行为：

- 默认 `max_pending=20`，所以 `max_workers > 20` 时实际在途数仍不超过 20；
- 修改调用者传入的 task dict，写入 `_start_time`；
- 返回顺序是完成顺序，不是输入顺序；
- 默认连续失败 5 次后跳过尚未提交的输入，但“连续”取决于并发完成时序；
- 没有运行级取消 token；
- 没有 per-item timeout；
- `on_progress` 运行于收集线程且异常会改变整个 batch；
- `auto` 用裸 `model_type` 查 limiter，但实际 LiteLLM limiter key 包含配置哈希。

见 [`src/execution/concurrency/parallel_executor.py`](../../src/execution/concurrency/parallel_executor.py) 和
[`src/integrations/litellm/model_binding.py`](../../src/integrations/litellm/model_binding.py)。

`auto = min(RPM, 10)` 也没有第一性原理基础：RPM 是启动速率，Worker 数是同时在途的 Agent run 数。一个 Agent run 还可能产生多个模型请求。Little's Law 下，合理在途数至少还依赖平均延迟和一次 Worker 的请求次数，单靠 RPM 不能推出。

### 4. Pi 和 smolagents 已经有的是 Tool fan-out，不是 Worker fan-out

#### Pi

Pi 根据 `tool_metadata.is_concurrency_safe` 把工具映射为 `parallel` 或
`sequential` execution mode。只要一个响应批次含 serial tool，当前
`BridgeToolCoordinator` 会延迟该批次的统一准备，之后由 SDK 按 tool execution mode 执行。

见：

- [`src/runtimes/pi/runtime.py`](../../src/runtimes/pi/runtime.py)；
- [`src/runtimes/pi/bridge/tool-coordinator.ts`](../../src/runtimes/pi/bridge/tool-coordinator.ts)；
- [`src/runtimes/pi/bridge/tools.ts`](../../src/runtimes/pi/bridge/tools.ts)。

Pi 的模型参数可以允许 provider 产生多个 tool call，但 AgentLoom 没有把 Worker fan-out 的 `concurrency` 传给这个执行器。Pi 文档也明确说 `requests_per_minute` 没有 Pi SDK 对应实现。

#### smolagents

`ToolCallingAgentV2.process_tool_calls()` 在模型一次发出多个 tool call 时，
用 `ThreadPoolExecutor(self.max_tool_threads)` 并行执行，然后按模型发出顺序
组装 `tool_results` 和 observation。

见 [`src/runtimes/smolagents/agents.py`](../../src/runtimes/smolagents/agents.py)。

当前 AgentLoom 没有把 `is_concurrency_safe` 应用到 smolagents 的这个路径：
只要一次响应有多个 call，它们都会进入线程池。`max_tool_threads` 也没有暴露在
AgentLoom 的 smolagents runtime options 中。

因此两个 runtime 的 Tool fan-out 语义目前不等价：

- Pi 能区分 serial/parallel tool；
- smolagents 默认全部并行；
- 两者都没有与 YAML `concurrency` 形成可靠的数量关系。

### 5. NewsAgent 自己实现并发有合理部分，也有应下沉部分

NewsAgent 的 SQLite Store 持有业务事实：

- 阶段依赖和任务切分；
- candidate/record 的业务状态；
- task owner、lease、renew、recover；
- 幂等结果提交和阶段 seal。

这些属于 NewsAgent Module，不应搬进 AgentLoom。

但 [`applications/news_agent/runtime/pipeline.py`](../../applications/news_agent/runtime/pipeline.py)
中的以下循环是通用执行机制：

- 保持 `workers` 个 future；
- 从 source claim；
- 复制执行上下文；
- 调用一个 Worker；
- heartbeat/renew；
- 根据 retryable error settle；
- 停止领取、等待在途任务、汇总结果。

这部分适合进入 Worker Runner。当前 `initial_workers/refined_workers/validation_workers`
还参与任务分片，因此“资源并发数”会改变 Agent 输入粒度和结果质量；任务大小应由
`max_input_tokens`、新闻亲和关系和完整分段规则决定，`max_in_flight` 只控制执行槽位。
固定的 `target_items_per_task` 没有足够证据，不进入首版设计。

## 二、成熟开源项目的一手证据

以下均为官方仓库或官方文档；研究时源码 HEAD 分别为：

- LangGraph `b4991f1ba36a2449ab59c38ab71828ee865bff84`
- PydanticAI `69ea1e57201ed59e7d30fbe4a7590d193acecce0`
- OpenAI Agents SDK Python `125efa029b4bfd84238bd2c4fd69c3406f802663`

### LangGraph：fan-out 属于图执行器

- `Send` 的官方源码把它定义为向指定 node 发送自定义 state 的消息，并明确以
  map-reduce 为例：同一个 node 用不同 state 并行运行，之后再聚合。
  [官方源码](https://github.com/langchain-ai/langgraph/blob/b4991f1ba36a2449ab59c38ab71828ee865bff84/libs/langgraph/langgraph/types.py#L738-L810)
- `max_concurrency` 的含义是“并发 graph steps 的最大数，也适用于 parallelized steps”，
  由 Pregel 执行器用 semaphore 实现，而不是由 node 自己决定。
  [配置源码](https://github.com/langchain-ai/langgraph/blob/b4991f1ba36a2449ab59c38ab71828ee865bff84/libs/langgraph/langgraph/_internal/_config.py#L194-L232)
  [执行器源码](https://github.com/langchain-ai/langgraph/blob/b4991f1ba36a2449ab59c38ab71828ee865bff84/libs/langgraph/langgraph/pregel/_executor.py#L122-L217)
- graph step 失败时，runner 取消仍在运行的 sibling，再传播异常。
  [失败/取消源码](https://github.com/langchain-ai/langgraph/blob/b4991f1ba36a2449ab59c38ab71828ee865bff84/libs/langgraph/langgraph/pregel/_runner.py#L652-L699)
- ToolNode 是另一层并发：同步路径使用 configured executor，异步路径当前直接
  `asyncio.gather`。这恰好说明“图 step 并发”和“同一步中的工具并发”不能因字段同名
  就假定语义相同。
  [ToolNode 源码](https://github.com/langchain-ai/langgraph/blob/b4991f1ba36a2449ab59c38ab71828ee865bff84/libs/prebuilt/langgraph/prebuilt/tool_node.py#L801-L868)

可借鉴：fan-out/fan-in、并发上限、失败传播放在执行器；node/agent 只实现一次调用。

不应照抄：AgentLoom 当前没有完整图状态 reducer，没必要为 Worker batch 引入整套 Pregel。

### PydanticAI：明确拆分 Agent run、Tool call 和 Model request 并发

- 官方多 Agent 文档把 delegation 定义为父 Agent 通过 tool 调用子 Agent；每次 delegation
  有独立 run 和 message history。若要复杂控制流，文档另指向 graph。
  [官方文档源码](https://github.com/pydantic/pydantic-ai/blob/69ea1e57201ed59e7d30fbe4a7590d193acecce0/docs/multi-agent-applications.md#L5-L34)
- PydanticAI 的通用 limiter Interface 是 `acquire(source)` / `release()`，支持
  `max_running`、`max_queued`、等待可观测性和分布式 Adapter；取消等待时会正确减少排队计数。
  [Limiter 源码](https://github.com/pydantic/pydantic-ai/blob/69ea1e57201ed59e7d30fbe4a7590d193acecce0/pydantic_ai_slim/pydantic_ai/concurrency.py#L34-L267)
- `ConcurrencyLimitedModel` 在 model request Seam 获取槽位，可以跨多个模型共享 limiter，
  并显式检测嵌套复用同一个 limiter 导致的死锁。
  [模型并发源码](https://github.com/pydantic/pydantic-ai/blob/69ea1e57201ed59e7d30fbe4a7590d193acecce0/pydantic_ai_slim/pydantic_ai/models/concurrency.py#L41-L158)
- Tool Dispatcher 有 `parallel`、`sequential`、`parallel_ordered_events` 三种模式；
  单个 `sequential=True` tool 是 barrier，而不是让整个 batch 永久串行。
  [ToolManager 源码](https://github.com/pydantic/pydantic-ai/blob/69ea1e57201ed59e7d30fbe4a7590d193acecce0/pydantic_ai_slim/pydantic_ai/tool_manager.py#L193-L293)
- 工具执行实现按 barrier 分 segment；同 segment 并行。失败或外层取消时取消并 drain sibling，
  同时把已经完成的结果按 call index 稳定写回。
  [工具执行源码](https://github.com/pydantic/pydantic-ai/blob/69ea1e57201ed59e7d30fbe4a7590d193acecce0/pydantic_ai_slim/pydantic_ai/_tool_execution.py#L285-L349)
  [取消与顺序源码](https://github.com/pydantic/pydantic-ai/blob/69ea1e57201ed59e7d30fbe4a7590d193acecce0/pydantic_ai_slim/pydantic_ai/_tool_execution.py#L840-L930)
- 其内部 `gather` 用 AnyIO task group；一个失败会取消 siblings，但最终结果仍按输入顺序返回。
  [gather 源码](https://github.com/pydantic/pydantic-ai/blob/69ea1e57201ed59e7d30fbe4a7590d193acecce0/pydantic_ai_slim/pydantic_ai/_utils.py#L280-L333)

可借鉴：不同资源层有不同 limiter；barrier 是 Tool Dispatcher 语义；取消必须 cancel-and-drain；
完成事件顺序与最终结果顺序可以分开。

### OpenAI Agents SDK Python：Agent-as-Tool 不是 batch scheduler

- 官方编排文档把 Agent-as-Tool 定义为 manager 保持会话控制权的 delegation；代码式并行
  则直接建议外层使用 `asyncio.gather`。
  [官方文档源码](https://github.com/openai/openai-agents-python/blob/125efa029b4bfd84238bd2c4fd69c3406f802663/docs/multi_agent.md#L1-L52)
- `Agent.as_tool()` 返回一个 FunctionTool；每次调用在内部启动独立 `Runner.run()`，
  并允许配置 nested run 的 `max_turns`、run config、session、approval 和输出提取。
  [Agent-as-Tool 源码](https://github.com/openai/openai-agents-python/blob/125efa029b4bfd84238bd2c4fd69c3406f802663/src/agents/agent.py#L606-L790)
- provider 是否可以产生多个 tool call (`parallel_tool_calls`) 与 SDK 本地执行这些 call 的
  数量上限 (`max_function_tool_concurrency`) 是两个独立配置。
  [官方文档源码](https://github.com/openai/openai-agents-python/blob/125efa029b4bfd84238bd2c4fd69c3406f802663/docs/running_agents.md#L175-L196)
  [配置源码](https://github.com/openai/openai-agents-python/blob/125efa029b4bfd84238bd2c4fd69c3406f802663/src/agents/run_config.py#L142-L165)
- Tool Dispatcher 用 pending queue + task slots 实现明确上限；失败后取消并 drain siblings。
  [执行源码](https://github.com/openai/openai-agents-python/blob/125efa029b4bfd84238bd2c4fd69c3406f802663/src/agents/run_internal/tool_execution.py#L1591-L1699)
- SDK 的 `gather_with_cancel` 在任何异常/取消时取消并 drain children。
  [取消源码](https://github.com/openai/openai-agents-python/blob/125efa029b4bfd84238bd2c4fd69c3406f802663/src/agents/util/_asyncio_tasks.py#L93-L117)
- streaming run 区分 immediate cancel 与 after-turn graceful cancel。
  [取消 Interface](https://github.com/openai/openai-agents-python/blob/125efa029b4bfd84238bd2c4fd69c3406f802663/src/agents/result.py#L933-L975)

可借鉴：Agent-as-Tool 只做 nested run Adapter；provider 产出能力与本地 dispatch 上限分开；
取消模式必须成为 Interface 的一部分。

## 三、设计判断

### 判断 1：应改 AgentLoom，而不是只改 NewsAgent

NewsAgent 重复实现通用 Worker Scheduler 是症状。根因是 AgentLoom 只有“一次 Agent run”
和“Agent-as-Tool”两个可用 Seam，没有一个可靠的多 Worker 执行 Interface。

### 判断 2：不能复用 Pi/smolagents 的内部并发实现

可以复用**语义**，不能复用**执行器实例**：

| 层 | 资源单位 | 生命周期 | 正确拥有者 |
|---|---|---|---|
| Worker fan-out | 完整 Agent run | 多个独立输入直到 batch/pool 结束 | AgentLoom Worker Runner |
| Tool fan-out | 一次模型响应中的 tool calls | 一个 Agent step | Pi/smolagents runtime Adapter |
| Provider admission | 一次模型请求 | 请求/stream 完成 | Model/Provider Adapter |

把 Pi/smolagents 的 Tool Dispatcher 直接用于 Worker，会绕过 Worker 的 RuntimeDefinition 构造、
fresh instance、checkpoint、Subagent hooks、usage、workspace 和关闭逻辑。反过来，用 Worker Runner
执行单回合 tool calls 又会丢失 call ID、barrier、provider message pairing 和 runtime 原生取消。

### 判断 3：Agent-as-Tool 仍合理，但 `.batch()` 不合理

合理部分：每次 Tool 调用创建 fresh Worker Agent，父 Agent 保持控制权。

不合理部分：让模型可调用的 function 同时假装 batch controller。

目标形态应是：

```python
worker = application.worker("workflows/worker_agents/research.yaml")

one = worker.run({"topic": "..."})
many = runner.map(worker, jobs, max_in_flight=8)
tool = worker.as_tool()
```

三条路径共享同一个 `Worker.run`，而不是共享同一个 function 上的隐藏属性。

## 四、方案 A：最小 Interface，最大化 Leverage

### 1. 外部 Interface：三个入口

```python
class Worker:
    def run(
        self,
        input: Mapping[str, JSONValue],
        *,
        cancellation: CancellationToken | None = None,
    ) -> WorkerOutcome: ...

    def as_tool(self) -> ToolBinding: ...


class WorkerRunner:
    def map(
        self,
        worker: Worker,
        inputs: Iterable[WorkItem | Mapping[str, JSONValue]],
        *,
        max_in_flight: int,
        failure: Literal["collect", "fail_fast"] = "collect",
        cancellation: CancellationToken | None = None,
        event_sink: Callable[[WorkerEvent], None] | None = None,
    ) -> BatchOutcome: ...

    def serve(
        self,
        worker: Worker,
        source: WorkSource,
        *,
        max_in_flight: int,
        cancellation: CancellationToken,
        event_sink: Callable[[WorkerEvent], None] | None = None,
    ) -> RunSummary: ...
```

这里实际只有三种用户动作：`run`、`map`、`serve`；`as_tool` 是把 `run` 投影到已有 Tool
Seam 的 Adapter 工厂，不承担调度。

如果首版只服务有限 batch，可以只交付 `run`、`map`、`as_tool`；NewsAgent 接入时再让
`serve` 替换 `map`，但底层 scheduler 从第一天就按 source 驱动实现，避免二次重写。

### 2. 值对象

```python
@dataclass(frozen=True)
class WorkItem:
    id: str
    input: Mapping[str, JSONValue]

@dataclass(frozen=True)
class WorkerOutcome:
    item_id: str
    state: Literal["succeeded", "failed", "cancelled"]
    output: JSONValue | None
    error: WorkerError | None
    usage: RuntimeUsage
    duration: float

@dataclass(frozen=True)
class BatchOutcome:
    items: tuple[WorkerOutcome, ...]  # 永远按输入顺序
    cancelled: bool
```

不要继续使用自由字符串 `TaskResult.status`，也不要暴露 traceback 字符串作为稳定 Interface。
`WorkerError` 应保留 runtime 的 `category`、`retryable`、message 和 cause（cause 不序列化）。

### 3. 固定语义，避免配置膨胀

#### 输入和顺序

- `max_in_flight` 只接受正整数；没有 `auto`；
- `map` 惰性消费 iterable，最多只预取 `max_in_flight`，因此不需要另一个隐藏
  `max_pending`；
- 不修改输入；
- `BatchOutcome.items` 按输入顺序；
- `event_sink` 按实际发生顺序收到 started/completed/failed/cancelled；
- event sink 失败只记录 observer error，不改变 Worker 结果。

#### 错误

- `collect`：一个 item 失败不影响 siblings，所有输入都有 Outcome；
- `fail_fast`：第一个未隔离异常触发停止取新任务，取消未开始任务，对在途任务发合作式取消，
  drain 后返回/抛出一个包含已完成 Outcomes 的 `WorkerBatchError`；
- 配置错误（非法 schema、`max_in_flight <= 0`）在启动前直接抛出，不变成 item failure；
- retry 不放进默认 `map` 策略。一次 Worker 的 provider/tool retry 由 runtime 处理；业务 task retry
  由 durable WorkSource 的 settle/requeue policy 决定，避免双重重试。

#### 取消

- `CancellationToken.cancel("reason")` 后不再 claim/submit；
- 未开始 item 标记 cancelled；
- 在途 item 通过 `AgentRuntimeRequest.cancellation` 传给 runtime Adapter；
- runtime 必须合作式停止并释放 Tool、MCP、transport、workspace 等资源；
- Runner 必须 drain 所有已启动执行后才返回；
- durable `serve` 默认是 graceful：停止 claim，等待当前 Agent run settle；
- 若以后需要 immediate/after-current 两种模式，新增明确 enum，不能靠 `KeyboardInterrupt`
  或 stop file 推断。

当前 `AgentRuntimeRequest` 没有 cancellation，这是一项必要的 runtime-neutral contract 扩展。
Pi Adapter 可映射到 transport/SDK abort；smolagents Adapter 应映射到其 run/task 的合作式取消。
在该扩展完成前，AgentLoom 只能诚实承诺“停止提交并等待在途任务”，不能宣称 immediate cancel。

### 4. 隐藏实现

Worker Runner Module 内部隐藏：

1. 验证 `max_in_flight`；
2. 把 list/iterable 适配成 `IterableWorkSource`；
3. 每个 slot claim 一个 item；
4. 捕获并传播 `ExplicitExecutionContext`；
5. 调 `Worker.run`，由 Worker 为每次调用构建 fresh Agent/runtime；
6. 将 runtime state/usage 标准化成 `WorkerOutcome`；
7. settle source；
8. 发有序生命周期事件；
9. 处理 cancel-and-drain；
10. 聚合稳定输入顺序的 `BatchOutcome`。

实现可先使用有界 `ThreadPoolExecutor`，以后换 AnyIO 或进程/远程执行而不改变外部 Interface。
Depth 来自隐藏完整生命周期，而不是来自线程池代码量。

### 5. 真实的内部 Seam 和 Adapter

#### WorkSource Seam

```python
class WorkSource(Protocol):
    def claim(self, limit: int) -> Sequence[WorkLease]: ...
    def settle(self, lease: WorkLease, outcome: WorkerOutcome) -> None: ...
    def exhausted(self) -> bool: ...
```

至少有两个真实 Adapter，因此这个 Seam 成立：

- `IterableWorkSource`：有限 list/generator，内存顺序；
- `LeaseWorkSource`：NewsAgent SQLite task/owner/lease/renew/recover。

NewsAgent Adapter 决定 task 如何 requeue、什么时候失败、怎样 heartbeat；Runner 只保证调用时机和
每个 lease 恰好 settle 一次。业务 StageLedger 仍留在 NewsAgent。

#### Worker invocation Seam

已有 `AgentRuntime` 是正确的深 Interface：Pi 和 smolagents 是两个真实 Adapter。
Worker Runner 不新增“Pi executor/smol executor”分支；它只调用 runtime-neutral `Worker.run`。

#### Provider limiter Seam

新增或重构为 `AdmissionLimiter.acquire(resource_key)` / `release()`，至少有：

- in-process semaphore Adapter；
- 测试 Adapter；若确有跨进程需求，再增加 Redis Adapter。

RPM token bucket 和 max-in-flight semaphore 是两种独立 policy，可以组合，不能互相推导。
Pi、smolagents/LiteLLM 都必须在各自 provider Adapter 发请求的位置过同一个语义 Seam。

### 6. Runtime Tool Dispatcher 的统一语义

不要把 Tool fan-out 并入 Worker Runner；只统一它的 contract：

```yaml
runtime_options:
  tool_execution:
    mode: parallel        # parallel | sequential
    max_in_flight: 4      # 可选；与 Worker 数无关
```

并保留 per-tool `is_concurrency_safe`：

- safe tools 在一个 segment 内并行；
- unsafe tool 是 barrier：前面的先完成，它单独执行，后面的再开始；
- 最终 Tool results 按模型 call 顺序提交；
- 一个 infrastructure exception 取消并 drain siblings；
- 普通 Tool failure 是否投影为 model-visible error，继续由 Tool Gateway policy 决定。

这套语义接近 PydanticAI 的 barrier 模型。Pi 已基本具备；smolagents 需要补齐 barrier 和配置映射。

`RuntimeRequirements.parallel_tools` 应只由明确的 Tool execution policy/模型能力需求产生，
不得再读取 Worker `max_in_flight`。

## 五、迁移顺序

### Phase 1：纠正 Interface

1. 引入 `Worker` / `WorkerRunner.map` 和严格的 Outcome；
2. 仅支持明确正整数 `max_in_flight`，删除 `auto`；
3. `.batch()` 进入 deprecation，内部转发到 `runner.map`，不再读取模型 RPM；
4. 修复结果输入顺序、输入不可变、observer 隔离；
5. `RuntimeRequirements.parallel_tools` 与 Worker 并发解耦。

### Phase 2：统一 runtime 内 Tool execution 语义

1. 加 `runtime_options.tool_execution`；
2. Pi 映射到 native execution mode；
3. smolagents 增加 per-tool barrier 和明确 `max_in_flight`；
4. 两个 runtime 做相同 contract tests：顺序、barrier、失败、取消、结果 pairing。

### Phase 3：取消与 provider admission

1. 给 `AgentRuntimeRequest` 增加 cancellation；
2. Pi/smolagents Adapter 做合作式 cancel-and-drain；
3. provider 层分别实现 max-in-flight、RPM、429 backoff；
4. 用实际 profile identity 作为 limiter key，不用裸 `model_type`。

### Phase 4：NewsAgent 复用

1. 写 `NewsTaskSource` Adapter 包装现有 Store；
2. 用 `runner.serve` 替换 `worker_pool` 的 future 管理；
3. 保留 StageLedger、lease SQL、stage seal 和业务 retry；
4. 将 `refined_workers/validation_workers` 从任务切分参数中移除；
5. 让 token budget 和业务亲和规则独立于 Worker 并发；只有评测证明条目数本身影响质量时，
   再增加 `max_items_per_task`；
6. 用真实 Application/CLI 验证启动、并发上限、停止、崩溃恢复和阶段 seal。

## 六、取舍

- **为什么不直接让 Supervisor 一次发出 N 个 Agent tool call？**
  模型决定 fan-out 会让数量、输入切分、失败恢复和成本不可预测。适合开放式 delegation，不适合
  NewsAgent 的确定性批处理。
- **为什么不引入完整 LangGraph？**
  AgentLoom 已有 Application、checkpoint、hooks、runtime Adapter；当前缺口是一个深的 Worker
  execution Module，不是另一套总编排框架。
- **为什么 `map` 默认 collect？**
  独立批任务通常需要完整审计和部分成功；fail-fast 必须显式，因为它改变 sibling 生命周期。
- **为什么结果固定输入顺序？**
  这让调用方无需自己重建映射，测试稳定；实时进度仍通过 completion-order events 提供。
- **为什么不自动计算并发数？**
  框架不知道应用延迟目标、一次 Worker 的模型请求数、provider burst、机器资源和下游连接池。
  一个错误的自动值比显式配置更危险。未来可以做观测和建议，但不应静默决策。

## 七、验收标准

实现不能只验证线程池单测，还应通过两个 runtime 的真实 Application：

1. `max_in_flight=N` 实测峰值严格为 N，N>20 不被隐藏截断；
2. map 返回输入顺序，event 是完成顺序；
3. 输入对象未被修改；
4. collect/fail-fast 语义稳定；
5. graceful cancellation 停止 claim 并 drain；
6. Pi 与 smolagents 的 unsafe Tool 都形成 barrier；
7. Worker `max_in_flight=1` 不会错误禁止 runtime 内安全 Tool 并行；
8. provider limiter 同时约束两个 Worker/两个模型 profile 的真实请求；
9. NewsAgent CLI 能停止、重启、lease recover，且任务粒度不随 `max_in_flight` 改变。

## 八、一个“总任务”如何进入并发执行器

### 1. 先区分“拆任务”和“跑任务”

线程池不会理解业务，也不会把“筛完一天新闻”自动拆成 20 个正确的输入。完整流程一定有
两个不同步骤：

```text
业务输入
  └─ Application 规划：按 token、亲和关系、业务原子性拆成 WorkItem
       └─ AgentLoom 执行：最多同时运行 N 个 WorkItem
            └─ Application 聚合：检查完整性并发布阶段结果
```

职责应固定为：

| 问题 | 正确拥有者 |
|---|---|
| 哪些新闻必须放在同一批、单批 token 上限是多少 | NewsAgent |
| 一共生成多少个 `WorkItem`，每个 item 的稳定 ID 和输入是什么 | NewsAgent |
| 如何提交 item、最多同时运行多少个、如何停止并等待在途任务 | AgentLoom Worker Runner |
| 崩溃后由谁重新领取、租约何时续期、结果如何原子提交 | durable `WorkSource` Adapter；NewsAgent 首版继续使用现有 SQLite Store |
| 所有 item 完成后如何合并成初筛/精筛/校验结果 | NewsAgent |

因此 `concurrency=20` 的准确含义只能是“最多同时执行 20 个已经规划好的
`WorkItem`”，不能同时表示“把总任务恰好切成 20 份”。这两个参数偶然相等也不应绑定。

### 2. 成熟实现如何分配责任

研究时源码 HEAD：

- CPython `5564790598b8bcc58c00df31a62ca56745961911`
- LangGraph `b4991f1ba36a2449ab59c38ab71828ee865bff84`
- Celery `ed354d9100dbdaf7b867f103593b4dea4148670a`
- Temporal Python SDK `fc5299b9ddab38d6a9064a23295bf36412f4b487`
- Temporal Python samples `b0e4d843ed49a647b5fcd1e18453ab1d048ba857`

#### Python `Executor.map`：调用方提供 item，执行器不理解业务

`Executor.map(fn, *iterables)` 由调用方提供 iterable；执行器只是逐项 `submit`。
`buffersize` 限制“已提交但尚未 yield 的结果数”，缓冲满后暂停消费输入，这是有限 batch
的背压。最终 iterator 仍按输入位置 yield，尽管调用可以乱序完成。
[官方源码](https://github.com/python/cpython/blob/5564790598b8bcc58c00df31a62ca56745961911/Lib/concurrent/futures/_base.py#L603-L689)

`chunksize` 只对 `ProcessPoolExecutor` 生效：它用 `itertools.batched` 做纯机械分块；
`ThreadPoolExecutor` 忽略它。即使这里存在 chunking，也只是按固定数量切 iterable，不理解
token、新闻亲和关系或业务原子性。
[官方源码](https://github.com/python/cpython/blob/5564790598b8bcc58c00df31a62ca56745961911/Lib/concurrent/futures/process.py#L940-L974)

可借鉴：

- `map` 接收已拆好的输入；
- 背压是执行器消费 iterable 的策略；
- 最终结果顺序与实际完成顺序分开。

#### LangGraph `Send`：业务节点产生 fan-out，图执行器负责调度

LangGraph 的 `Send(node, arg)` 由 conditional edge 返回。官方 map-reduce 示例中，
`continue_to_jokes` 遍历业务态里的 `subjects`，为每个 subject 创建一个 `Send`；
图执行器随后并行调用同一 node，`Annotated[..., operator.add]` reducer 聚合结果。
[官方源码](https://github.com/langchain-ai/langgraph/blob/b4991f1ba36a2449ab59c38ab71828ee865bff84/libs/langgraph/langgraph/types.py#L738-L810)

`max_concurrency` 是图执行器对并发 graph step 的上限，并由执行器 semaphore 落实，
不是 node 用它反推需要创建多少个 `Send`。
[配置源码](https://github.com/langchain-ai/langgraph/blob/b4991f1ba36a2449ab59c38ab71828ee865bff84/libs/langgraph/langgraph/_internal/_config.py#L194-L232)
[执行器源码](https://github.com/langchain-ai/langgraph/blob/b4991f1ba36a2449ab59c38ab71828ee865bff84/libs/langgraph/langgraph/pregel/_executor.py#L122-L217)

可借鉴：Application 产生子任务及聚合规则，Runner 只调度；不要让并发数决定业务 fan-out。

#### Celery `group/chord`：调用方显式注册任务图，队列负责分发

Celery `group` 接收调用方构造的 task signatures 列表；调用后将这些 task 发布出去并返回
`GroupResult`。`GroupResult.join()` 按调用顺序聚合结果。
[官方文档源码](https://github.com/celery/celery/blob/ed354d9100dbdaf7b867f103593b4dea4148670a/docs/userguide/canvas.rst#L685-L846)
[官方实现](https://github.com/celery/celery/blob/ed354d9100dbdaf7b867f103593b4dea4148670a/celery/canvas.py#L1598-L1756)

需要 fan-in 时，`chord(header, body)` 明确把并行任务列表注册为 header；所有 header
完成后，body 才收到结果列表并执行。拆分和 reducer 都由调用方声明，不由 worker pool 猜测。
[官方文档源码](https://github.com/celery/celery/blob/ed354d9100dbdaf7b867f103593b4dea4148670a/docs/userguide/canvas.rst#L892-L969)
[官方实现](https://github.com/celery/celery/blob/ed354d9100dbdaf7b867f103593b4dea4148670a/celery/canvas.py#L2088-L2113)

Celery 也明确区分几种提交形态：

- `group`：每个 signature 是独立、可并行的队列任务；
- `map/starmap`：只发一条 task message，在这个 task 内顺序处理整个序列；
- `chunks`：调用方给出固定 chunk size，机械分块后再形成任务。

[官方文档源码](https://github.com/celery/celery/blob/ed354d9100dbdaf7b867f103593b4dea4148670a/docs/userguide/canvas.rst#L280-L336)
[Map/Chunks 文档源码](https://github.com/celery/celery/blob/ed354d9100dbdaf7b867f103593b4dea4148670a/docs/userguide/canvas.rst#L1109-L1215)

durable queue 的背压和失败恢复属于 broker/worker 协议：prefetch 限制一个 worker
能 reserve 的消息数；late acknowledgment 让运行中的任务在 worker 崩溃后可重新投递，
代价是任务必须幂等。这不是 `group` 的业务拆分职责。
[官方优化文档源码](https://github.com/celery/celery/blob/ed354d9100dbdaf7b867f103593b4dea4148670a/docs/userguide/optimizing.rst#L111-L198)

#### Temporal Workflow/Activity：Workflow 编排，Worker 注册和执行

Temporal 的官方 Python 并行 Activity 示例由 Workflow 显式创建五个
`workflow.execute_activity(...)`，再用 `asyncio.gather` 聚合。Worker 只注册 Workflow、
Activity 和 `task_queue`，并提供 Activity executor。
[官方示例源码](https://github.com/temporalio/samples-python/blob/b0e4d843ed49a647b5fcd1e18453ab1d048ba857/hello/hello_parallel_activity.py#L12-L65)

SDK Worker 进一步把 task queue、注册的 Workflow/Activity、执行器和
`max_concurrent_activities` 分开；文档明确后者只是“这个 Worker 同时会被交付多少个
Activity task”。它不会据此修改 Workflow 的 fan-out。
[官方 Worker 源码](https://github.com/temporalio/sdk-python/blob/fc5299b9ddab38d6a9064a23295bf36412f4b487/temporalio/worker/_worker.py#L101-L181)
[并发槽位源码](https://github.com/temporalio/sdk-python/blob/fc5299b9ddab38d6a9064a23295bf36412f4b487/temporalio/worker/_worker.py#L213-L280)

可借鉴：持久编排的“总任务”属于 Workflow；Activity 是可重试执行单元；task queue 和
Worker 容量负责分发，不负责决定业务 Activity 的粒度。

### 3. 有限 batch 和 durable queue 不能伪装成同一种行为

| 属性 | 有限 batch `map` | durable queue `serve` |
|---|---|---|
| 输入来源 | 当前进程的 list/generator | 可跨进程、跨重启的 Store/broker |
| 提交 | 调用 `map` 即提交 | 先原子 publish/register，再由 Worker claim |
| 背压 | Runner 暂停消费 iterable | source/broker 的 reserve/prefetch + Runner 槽位 |
| 身份 | 本次调用内的 item ID | 持久 task/submission ID |
| 崩溃恢复 | 默认没有 | claim owner、lease/ack、renew、redelivery/recover |
| 聚合 | `BatchOutcome` 按输入顺序 | Application 根据 submission/stage 完成条件聚合 |
| 适用 | 短期、已知有限输入 | NewsAgent 这类长任务、CLI 重启和部分恢复 |

二者可以共享 Worker invocation、Outcome 和事件模型，但不能假装内存 Future 自带
持久语义。

### 4. AgentLoom 最小 Interface

AgentLoom core 不新增一个“万能自动拆分器”。它只接受已经规划好的 `WorkItem`：

```python
@dataclass(frozen=True)
class WorkItem:
    id: str
    input: Mapping[str, JSONValue]


# 有限输入：不做持久注册
outcome = runner.map(
    worker,
    items,
    max_in_flight=20,
)


# 持久输入：Application 先原子注册，再由 Runner 消费
submission = source.publish(items)
summary = runner.serve(
    worker,
    source,
    max_in_flight=20,
    cancellation=token,
)
```

`source.publish()` 是否暴露在 AgentLoom 的公共 Interface，可在接入第二个 durable Adapter
后再决定。NewsAgent 首版不应为“统一形式”重写数据库，而应让 `NewsTaskSource`
Adapter 把现有 `enqueue_stage`、`claim`、`renew`、`fail/retry` 和完成提交映射到
Runner 的 source lifecycle。

`runner.map` 和 `runner.serve` 共享：

- 每个 item 创建 fresh Agent/runtime；
- `max_in_flight` 槽位；
- execution context 传播；
- 结构化 Outcome 和生命周期事件；
- 停止领取并 drain 已启动执行。

只有 durable source 拥有：

- 原子 publish 和稳定 task identity；
- claim owner 与 lease/ack；
- renew、崩溃 recover 和 redelivery；
- 持久进度与跨进程状态；
- task-level retry/requeue policy。

Application 仍拥有：

- 总任务如何切为 `WorkItem`；
- task 输入的 token/业务约束；
- 阶段依赖；
- 所有 item 完成后的 reducer/finalizer。

### 5. NewsAgent 应怎样使用

当前 NewsAgent 其实已经有正确流程的雏形：

1. `prepare_day` / `submit_initial` / `submit_refined` / `submit_validation` 规划并注册 task；
2. Store 的 `enqueue_stage` 原子发布整阶段；
3. `worker_pool` 从 Store `claim`，再把 task 交给线程池；
4. `_execute` 续租并调用一个 Worker；
5. `finish_stage` 检查 task/ledger 完整性后聚合并发布结果。

真正的问题是任务规划读了执行并发数：

- 初筛 `target_batches=settings.initial_workers`；
- 精筛 `target_batches=self.config.refined_workers`；
- 校验 `target_candidates=ceil(pending/concurrency)`。

这会导致只修改“同时跑几个 Worker”，就改变 Agent 输入、任务数和判断上下文。

建议改成两组独立配置：

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

# applications/news_agent/config/settings.yaml
batching:
  token_encoding: o200k_base
  initial_max_tokens: 350000
  refined_max_tokens: 350000
```

对 NewsAgent 用户而言，完整调用应类似：

```python
# NewsAgent 负责规划和持久注册
submission = pipeline.submit_initial(
    day,
    task_sizing=TaskSizing(max_input_tokens=350_000),
)

# AgentLoom 只负责最多同时执行 20 个已注册 task
runner.serve(
    initial_worker,
    NewsTaskSource(store, submission),
    max_in_flight=20,
)

# NewsAgent 负责阶段聚合
pipeline.finish_stage(submission)
```

如果 token 上限使一天产生 37 个 task，`max_in_flight=20` 就先跑最多 20 个，完成一个再
claim 一个；如果只产生 7 个，也只运行 7 个，不为了凑满 20 而破坏业务分片。

### 6. 最终设计判断

- “注册总任务”的动作属于 Application：它必须先把业务输入变成稳定 `WorkItem`。
- AgentLoom 应提供的是深的执行 Module，而不是假装理解所有业务的 planner。
- 机械的 `items_per_task`/固定 chunk helper 可以作为可选工具，但 token、亲和关系、阶段依赖
  必须由 NewsAgent policy 决定。
- 有限批处理使用 `runner.map`；需要恢复、重启、多个 Worker 竞争消费时使用
  `source.publish + runner.serve`。
- `max_in_flight` 只控制执行槽位；任务数量和任务内容必须在提交前固定。

## 九、反证审查：哪些设计仍然想当然

本节不新增大方案，而是尝试否定根目录方案。结论是：**有限 `map()` 可以进入实现，
共享 Agent-as-Tool 槽位和通用 durable `serve()` 目前不能直接进入实现。**

### P0：不解决就不能发布

#### P0-1：共享槽位加同步嵌套调用会自锁

根目录方案要求 Python `map()` 和 Agent-as-Tool 共用 Worker capacity controller，并只拒绝
“同一 Worker 递归”。这个条件不够。

假设容量为 2：

1. Worker A 和 Worker B 各占一个槽位；
2. A 同步调用 B-as-Tool，B 同步调用 A-as-Tool；
3. 两个父调用都持有槽位等待子调用，两个子调用都等待槽位。

即使 A、B 使用不同 Worker key，也会形成环。若 capacity controller 与
`ThreadPoolExecutor` 又是同一个对象，执行线程和容量 permit 还会被混为一谈。

CPython 官方文档明确警告：线程池中的 callable 等待同池另一个 `Future` 会死锁。
[CPython `ThreadPoolExecutor` 文档](https://github.com/python/cpython/blob/5564790598b8bcc58c00df31a62ca56745961911/Doc/library/concurrent.futures.rst#L142-L178)
PydanticAI 也在复用同一个 limiter 的嵌套模型请求处显式检测递归并报错，而不是继续等待。
[PydanticAI 递归 limiter 检测](https://github.com/pydantic/pydantic-ai/blob/69ea1e57201ed59e7d30fbe4a7590d193acecce0/pydantic_ai_slim/pydantic_ai/models/concurrency.py#L41-L158)

设计裁决：

- **接受**：容量控制与执行后端是两个内部 Module；permit 不等于线程。
- **拒绝**：一个共享 `ThreadPoolExecutor` 同时承担顶层 batch 和嵌套 Agent-as-Tool。
- **拒绝**：只检测“同一 Worker key”递归。
- **推迟**：Agent-as-Tool 与 `map()` 共用容量域。首版 `map()` 只控制它自己启动的顶层
  invocation；等 lineage-aware admission policy 定义并验证后再堵 Agent-as-Tool 旁路。
- 若必须首版共用容量域，则任何持有该域 permit 的 Worker 再同步提交到该域时，必须在
  **提交前**抛出 typed admission error，不能排队等待。跨容量域的环仍需单独处理。

#### P0-2：lease 不是 exactly-once；旧 Worker 的迟到副作用必须被 fencing

根目录方案说“每个 lease 恰好 settle 一次”，这只是进程内调用次数，不是持久执行语义。
lease 过期后，Source 可以把同一任务交给新 Worker，而旧 Worker 的线程仍不能被强制停止。
此时两个 attempt 会并行。只在最终 `settle()` 比对 owner，无法撤销旧 attempt 已经发出的
Tool、副作用或外部请求。

Celery 官方文档明确说明 late acknowledgment 会在 Worker 中途退出时重新投递，因此任务
必须幂等；它没有提供 exactly-once 承诺。
[Celery prefetch 与 late acknowledgment](https://github.com/celery/celery/blob/ed354d9100dbdaf7b867f103593b4dea4148670a/docs/userguide/optimizing.rst#L111-L198)
Temporal 把 Activity task、Worker 容量和执行生命周期分开，Activity 的并发槽位也不等于
业务提交只发生一次。
[Temporal Worker Interface](https://github.com/temporalio/sdk-python/blob/fc5299b9ddab38d6a9064a23295bf36412f4b487/temporalio/worker/_worker.py#L101-L280)

NewsAgent 当前 `StageLedger._owned()` 对每次数据库提交检查 owner 和 lease，这是有价值的
业务 fencing；但它不能自动覆盖未知外部 Tool 的副作用。

设计裁决：

- **接受**：durable Source 的真实承诺是 at-least-once invocation，加 fenced/idempotent
  commit；绝不能写成 exactly-once execution。
- **接受**：每次 claim 返回单调递增的 attempt/fencing token；`renew`、业务提交和
  `settle` 都必须带 token，并在一个原子写入点拒绝旧 token。
- **拒绝**：Runner 在未知 Tool 有外部副作用时自动重试整个 Worker。
- **推迟**：通用 `WorkerRunner.serve()`。首版保留 NewsAgent 当前 Store 和 StageLedger，
  只在其专用 Adapter 内实验；具备第二个 durable Adapter 和统一 fencing contract 后，
  才把 `serve()` 升为公共 Interface。

### P1：实现前必须收紧 Interface

#### P1-1：线程取消只能是合作式，不能把 running Future 标成已取消

CPython `Future.cancel()` 和 `shutdown(cancel_futures=True)` 只能取消尚未开始的工作；正在运行
的线程会继续到 callable 返回。
[CPython shutdown/cancel](https://github.com/python/cpython/blob/5564790598b8bcc58c00df31a62ca56745961911/Doc/library/concurrent.futures.rst#L96-L123)
AnyIO 的 `abandon_on_cancel=True` 也只是放弃等待，线程仍继续运行；真正停止需要同步代码
主动检查取消。
[AnyIO worker thread cancellation](https://github.com/agronholm/anyio/blob/017197d02bab88766afb55361fc8939e27041550/docs/threads.rst#L20-L31)
[AnyIO cooperation check](https://github.com/agronholm/anyio/blob/017197d02bab88766afb55361fc8939e27041550/docs/threads.rst#L297-L318)

设计裁决：

- **接受**：首版只承诺 stop-admission-and-drain；停止消费、取消未启动 future、等待已启动
  invocation 清理。
- **拒绝**：`AgentRuntimeRequest` 尚无 cancellation contract 时宣称 running Worker
  已取消。
- **推迟**：interrupt cancellation；需 Pi、smolagents 两个 Adapter 都能合作式响应，
  并验证 Tool/MCP/transport/workspace 均已清理。

#### P1-2：惰性输入、全量结果和取消三者不能同时无条件承诺

根目录方案同时承诺：

- 惰性消费 generator；
- 所有输入都有按输入顺序的 Outcome；
- fail-fast/cancel 后不再消费输入。

对于尚未迭代的 generator，Runner 根本不知道剩余 item 的 ID 和数量，因此不可能为它们
生成 cancelled Outcome。必须选择一个明确 Interface：

- `map()` 接受有限 `Sequence`，可以保证一项一 Outcome；或
- `map_iter()` 接受 iterable，但取消后只对**已经 admission** 的 item 返回 Outcome，
  未消费输入不属于此次 submission。

CPython `Executor.map(buffersize=N)` 的 backpressure 是“已提交但尚未 yield 的结果数”，
buffer 满时停止消费输入；它没有把未知的剩余 iterator 元素伪装成 cancelled Future。
[CPython map/buffersize](https://github.com/python/cpython/blob/5564790598b8bcc58c00df31a62ca56745961911/Lib/concurrent/futures/_base.py#L603-L689)

另外，“内存只随 concurrency 增长”也只能描述 scheduler working set。若
`BatchOutcome` 最终保存所有结果，总内存仍至少随任务总数增长。

设计裁决：

- **接受**：执行中的 outstanding submission 有界；完成顺序事件与输入顺序结果分开。
- **拒绝**：对未消费 generator 承诺 cancelled Outcome。
- **推迟**：同时暴露 `map` 和 `map_iter`。首版优先有限 `Sequence`，先把语义做实。

#### P1-3：`ContextVar` 传播不能直接复制整个可变运行世界

AnyIO 明确说明，新任务/worker thread 得到的是当前 context 的副本，子线程的 ContextVar
赋值不会回传父任务。
[AnyIO task context propagation](https://github.com/agronholm/anyio/blob/017197d02bab88766afb55361fc8939e27041550/docs/tasks.rst#L285-L303)
[AnyIO thread context propagation](https://github.com/agronholm/anyio/blob/017197d02bab88766afb55361fc8939e27041550/docs/threads.rst#L270-L279)

但“复制 context”不等于复制其中的对象。AgentLoom 的 ContextVar 包含 logger binding、
checkpoint coordinator、goal provider、provider call budget 和资源 owner 等可变对象；
`copy_context()` 会让 siblings 共享这些对象引用。哪些应该共享、哪些必须形成子作用域，
不能由线程池偶然决定。

设计裁决：

- **接受**：每个 item 单独 capture/rebind，且生成新的 task/subtask/agent identity。
- **拒绝**：把裸 `copy_context()` 当成完整隔离证明。
- **接受**：优先用现有 `ExplicitExecutionContext` 作为显式快照；逐项审计其他 ContextVar：
  标注 `share`、`derive` 或 `forbid`，并用并发测试验证。

#### P1-4：同步/异步桥接必须成为明确的调用约束

当前 `AgentRuntime.run()` 是同步 Interface。若同步 `runner.map()` 在 event-loop 线程中阻塞，
会冻结该 loop；若内部私自 `asyncio.run()`，在已有 loop 中又会失败。AnyIO 为这类桥接提供
worker thread 和 blocking portal，但 portal 自身有明确生命周期，退出 context 就关闭 loop。
[AnyIO BlockingPortal](https://github.com/agronholm/anyio/blob/017197d02bab88766afb55361fc8939e27041550/docs/threads.rst#L118-L203)

设计裁决：

- **接受**：首版 `WorkerRunner.map()` 是纯同步 Interface，并明确不得在 event-loop 线程直接
  阻塞调用；Application 的 async Adapter 必须显式 offload。
- **拒绝**：Runner 内部隐式检测 event loop 后切换实现。
- **推迟**：原生 `amap()`；只有出现第二个真实 async caller 后再建立 async Seam。

#### P1-5：错误模型必须区分 item failure 与 scheduler/source failure

`failure="collect"` 不能吞掉以下错误并伪装成普通 item failure：

- executor/controller broken；
- Source claim/renew/settle 失败；
- observer 或 checkpoint 基础设施破坏一致性；
- Runner 自己违反 invariant。

AnyIO task group 可能在 sibling 清理时产生多个异常，因此用 `ExceptionGroup` 保留多个根因，
而不是只留最后一个字符串。
[AnyIO multiple errors](https://github.com/agronholm/anyio/blob/017197d02bab88766afb55361fc8939e27041550/docs/tasks.rst#L215-L263)
LangGraph 在 step failure 时取消其他 sibling 再传播异常，也没有把执行器错误伪装成节点结果。
[LangGraph failure/cancellation](https://github.com/langchain-ai/langgraph/blob/b4991f1ba36a2449ab59c38ab71828ee865bff84/libs/langgraph/langgraph/pregel/_runner.py#L652-L699)

设计裁决：

- **接受**：业务/Runtime 的一次 invocation failure 进入 `WorkerOutcome`。
- **接受**：scheduler/source invariant failure 终止整个 batch，抛 typed
  `WorkerRunnerError`，携带已经完成的 Outcomes 和全部 cleanup errors。
- **拒绝**：根据任意 exception 上临时存在的 `retryable=True` 就做框架级重试。

### P2：先记录，不阻塞有限 `map()`

- **容量域身份**：`同一 Application Run 内同一类 Worker` 仍不精确。Worker alias、相同 YAML
  的两次加载、不同模型 profile 是否共享额度，需要稳定 `worker_definition_id` 和显式 scope。
- **公平性**：semaphore/线程池不保证严格公平；首版只保证上限，不承诺 FIFO。
- **进度回调**：observer 应被串行投递或明确要求线程安全；其失败默认记录为 observer error，
  不能改变 Worker Outcome。
- **超时**：线程级 timeout 不能停止正在运行的 invocation。首版不增加“硬超时”字段；
  等 Runtime 合作式取消落地后再定义 deadline。

### implementation-ready 验证矩阵

| 场景 | 必须观察到的结果 | 否决条件 |
| --- | --- | --- |
| 有限 100 项，`concurrency=4` | 实测活动 invocation 峰值为 4；outstanding submission 有界 | 峰值超过 4，或提前创建 100 个 Runtime/Future |
| 首项慢、后项快 | completion event 按真实完成顺序；最终结果按输入顺序 | 为保序阻塞后续调度，或结果与 item ID 错配 |
| 一个 item 失败，`collect` | 其他 item 继续；失败保留 typed cause | sibling 被意外取消，或错误降成 traceback 字符串 |
| 一个 item 失败，`fail_fast` | 停止 admission；未启动 future 取消；running item drain | 返回时仍有线程、Runtime、MCP 或后台任务存活 |
| 调用取消 | 只把未启动工作标 cancelled；running 工作仍标真实终态 | running 线程继续执行却已对外报告 cancelled |
| generator 在第 K 项后取消 | 不再调用 `next()`；只返回已 admission 的 item | 为补全 Outcome 继续耗尽 generator |
| sibling ContextVar 隔离 | task/agent identity 独立；明确 shared 对象具备锁/并发合同 | sibling 串日志、串 checkpoint/goal/session 或资源 owner |
| Worker A→B→A 嵌套 | 提交前明确拒绝，或无阻塞完成 | 测试超过 deadline 仍未返回 |
| Agent-as-Tool 与 batch 同压 | controller scope 可观测，Application 结束后完全释放 | 使用 process-global controller，跨 run 串额度/泄漏 |
| lease 在运行中失效并被重领 | 旧 attempt 的 renew/submit/settle 全被 fencing token 拒绝 | 旧 attempt 覆盖新结果，或产生未幂等重复副作用 |
| Source `settle` 失败 | 整体报 infrastructure failure，并保留已完成 Outcomes | 被记录成普通 item failure 后继续 claim |
| Pi 真实 Application | 并发、失败、停止后资源均符合上述语义 | 只有线程池单测通过 |
| smolagents 真实 Application | 与 Pi 通过同一 contract tests | 两个 Runtime 的外部语义不同 |
| NewsAgent CLI 重启 | lease recover、迟到写 fencing、任务分片不随并发变化 | 出现重复业务提交、阶段提前 seal 或分片漂移 |

### 合并前硬性否决条件

出现任意一项，都不应合并：

1. 依赖 sleep 才能“证明”无死锁，测试没有 deadline 和线程 dump；
2. running thread 被对外标为 cancelled，但代码仍可继续写文件、调用 Tool 或提交结果；
3. `serve()` 声称 exactly-once，却没有 attempt/fencing token 贯穿 claim、renew、提交和 settle；
4. 同一个 executor 可被其 Worker 同步递归提交，且没有 admission-time 拒绝；
5. `map()` 为了返回全量 cancelled Outcome 耗尽原本应停止消费的 generator；
6. 只使用裸 `copy_context()`，没有审计可变 ContextVar 的 share/derive/forbid 语义；
7. `collect` 把 source/controller/invariant failure 当成普通 item failure；
8. 只通过线程池单测，没有 Pi、smolagents 的真实 Application 以及 NewsAgent CLI 恢复验证。

因此实现顺序应收缩为：

1. 先做有限 `Sequence` 版 `WorkerRunner.map()`；
2. 验证有界 submission、顺序、错误、ContextVar 和 stop-and-drain；
3. 再定义 lineage-aware Agent-as-Tool admission；
4. 最后在 fencing 与 at-least-once contract 成立后开放 durable `serve()`。

## 本轮复核：作用域、配置与实测

2026-10-11 读取引用聊天并核对当前 `bc5cff6e` 源码后，用户确认首版上限限定为单次
`map()`。下面是对早期方案的修正，最终 Interface 见根目录方案。

| 早期提案中的问题 | 当前决策及原因 |
| --- | --- |
| 前半段允许代码覆盖，后半段又要求只能配置一次 | 固定 system < Supervisor binding < call override。内部 Runner 只接收解析后的 snapshot，解析器只有一个 |
| 同时承诺每次调用可覆盖、同 Run 同 Worker 共享固定上限 | 用户选择单次 batch 容量；两个 map 各 20 可以合计 40，不再承诺 Run 总量 |
| 只检查单次值 <=64，却称 process_hard_limit | 首版删除该配置。全进程 admission 与资源单位尚未定义，不能凭字段名给出保证 |
| 默认配置必须按每个 Worker 全量列出 | 允许共同默认值和逐 Worker 覆盖；其他合法路径未参与当前 batch 不报错，未知/无效路径仍拒绝 |
| 假定 RootSettings 会拒绝新增未知配置 | 当前 extra=allow 会接受。必须增加 worker_execution 类型和显式校验，接受 YAML 不代表执行实现存在 |
| 在整个 Run 启动前冻结所有最终配置 | system/definition 绑定后固定；本次代码覆盖在 batch 启动前冻结，按 batch_id 保存 |
| 并发写 root manifest 保存各 Worker 值 | batch 独立记录，根 manifest 由生命周期统一汇总。原子文件替换不能防止读改写丢更新 |
| frozen dataclass 或 copy_context 即可隔离任务 | 嵌套 input 深复制/冻结；Context 中共享可变对象必须审计同步和资源归属 |
| 完成事件具有严格物理完成顺序 | controller 记录接收顺序；保证快项不被慢的前项阻塞，不虚构同时完成线程的总序 |
| 本库迟到写也必须改成单调 token | 现有同事务 owner/status/lease 比较可拒绝本库旧 attempt。外部系统另需其支持的幂等键或 fencing |
| 关闭失败只是普通 item 失败，可以继续补位 | 停止 admission，保存结构化 cleanup failure，不能先回收未确认已释放的资源容量 |

Supervisor `execution.concurrency` 只定义属于该 binding 的批量默认值，不是多个模型
Agent-as-Tool call 的共享上限。首版先交付 system 和代码覆盖，Supervisor Adapter 后续
显式支持；支持前预检拒绝该新字段。NewsAgent 不新增 Supervisor。

### 本地受控探针

以下探针使用项目 `.venv/bin/python`（3.14.8），不请求模型，不写项目业务数据；
线程同步使用 Event/Barrier 和 5 秒 deadline。manifest 探针只使用可自动删除的临时 RuntimeHome。
这些验证现有行为，不能充当尚未实现的新 Runner 验收。

| 探针 | 观察结果 | 对应源码 |
| --- | --- | --- |
| `ParallelAgentExecutor(max_workers=32)`；32 项，用 gate 保持前 20 项在途 | 配置 32，实测峰值 20；32 份调用者输入全部被加入 `_start_time` | `src/execution/concurrency/parallel_executor.py:40,94-112` |
| callable 已通过 Event 确认开始，再 `Future.cancel()` | 返回 False，Future 仍 running；释放 gate 后正常完成 | CPython `Future.cancel()` 合同，详见执行合同复核 |
| frozen dataclass 内保存 dict/list，再 append 到嵌套 list | 修改成功，frozen 未保护 payload | Python dataclass 只限制字段赋值 |
| ContextVar 存 dict，copy_context 后修改 dict | 父 Context 观察到同一修改 | `src/execution/context.py:887-890` 直接使用 copy_context |
| RootSettings 接收 worker_execution 下的 0、True、auto | 三种非法并发均被放进 model_extra | `src/config/config_validation.py:357-376` |
| 两线程调用同一 RuntimeContext.update_manifest；Barrier 保证读取同一旧值 | 两次更新只保留一次，稳定复现 lost update | `src/execution/context.py:751-764` |
| `hasattr(agentloom.app, "Application")` | False；Application.open 示例尚不存在 | `src/app/__init__.py` |

manifest 探针在读取后插入 Barrier，只控制已有 read-modify-write 的时序，不替换写入实现。
最终 JSON 完整但少了一个更新，说明问题是并发一致性，不是文件写坏。

项目最低支持 Python 3.12，而本地 `.venv` 为 3.14；`map(buffersize=...)` 只在新版存在。
执行合同复核另在 3.12.13 和 3.14.8 实测了 queued/running cancel 与 shutdown 等待行为。

### 首版交付范围

根目录方案现已统一为：typed 配置与来源链 → 接入既有 Run 生命周期的 finite map →
Supervisor binding 默认值 → NewsAgent 专用持久迁移。Tool Dispatcher、Provider admission、
跨批次容量、通用 durable serve 和自动重试不混入第一阶段。

验收新增两个并行 map、嵌套可变输入、batch 记录丢更新、Application owner 退出竞态和
cleanup failure。继续要求 Pi/smolagents 真实 Application runner/CLI 以及 NewsAgent
迁移证据；本轮文档修改不意味着这些产品验收已经执行。
