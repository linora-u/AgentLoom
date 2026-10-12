# Worker 执行合同：容量、退出和重试

研究日期：2026-10-11。本文依据当前本地源码和官方上游资料；验收项描述待实现合同，不代表 AgentLoom 已支持。

用户已明确：**并发上限只约束单次 `map()` 调用**。这让首版可以只做有限批处理，保留现有 Agent-as-Tool 路径，无需先实现 Application Run 共享容量、lineage 调度或全进程上限。

## 已核实的事实

| 事实 | 一手证据 | 对首版的影响 |
| --- | --- | --- |
| 当前每次 `execute_batch()` 都创建独立线程池；上限并非同一 Worker 在整个 Run 内的总量。 | `src/execution/concurrency/parallel_executor.py:82-110`；`src/app/factory.py:219-246` | 单次批处理作用域符合用户选择，但仍需修正输入修改、结果乱序和隐藏容量。 |
| Python 线程池限制执行线程数，提交队列自身没有这个容量限制。 | 本机 Python 3.12 `concurrent/futures/thread.py:153-155` 使用 `queue.SimpleQueue()`；[CPython 官方源码](https://github.com/python/cpython/blob/5564790598b8bcc58c00df31a62ca56745961911/Lib/concurrent/futures/thread.py) | `max_workers=C` 不能替代有界提交；Runner 要自己维护 `submit/wait` 窗口。 |
| `Executor.map(buffersize=...)` 从 Python 3.14 才提供；项目最低支持 3.12。 | `pyproject.toml:10`；[官方文档源码](https://github.com/python/cpython/blob/5564790598b8bcc58c00df31a62ca56745961911/Doc/library/concurrent.futures.rst#L49-L90) | 不能以这个参数作为首版依赖；借鉴其背压思路即可。 |
| running Future 不能被 `cancel()` 停止；`shutdown(wait=True)` 会等正在执行的 callable 返回。 | [Python 3.12 官方文档](https://docs.python.org/3.12/library/concurrent.futures.html#concurrent.futures.Executor.shutdown)；[Future.cancel](https://docs.python.org/3.12/library/concurrent.futures.html#concurrent.futures.Future.cancel)；下方双版本实测 | 停止调度后仍需 drain；运行中的任务保留真实终态。 |
| AnyIO 放弃等待线程也不会停止线程。 | [固定版本线程文档](https://github.com/agronholm/anyio/blob/017197d02bab88766afb55361fc8939e27041550/docs/threads.rst#L27-L30)；[合作式检查](https://github.com/agronholm/anyio/blob/017197d02bab88766afb55361fc8939e27041550/docs/threads.rst#L297-L317) | 换 async 框架不会自动兑现强制取消。 |
| Pi 有协议取消和 Node 进程关闭，但还会执行 Python Tool callback。`request()` 最后等待 callback 线程池退出。 | `src/runtimes/pi/transport.py:155,219-284` | 杀掉 Node 进程不能保证任意同步 Python callback 已停止。 |
| Runtime 的公共请求合同没有 cancellation；退出已有实例清理入口。 | `src/execution/agent_runtime.py:741-756`；`src/app/invocation.py:312-350`；`src/runtimes/smolagents/runtime_adapter.py:559-581`；`src/execution/resources.py:68-101` | 首版可以复用调用清理，不应宣称两个 Adapter 已支持通用中断。 |
| NewsAgent 的数据库提交在同一事务内检查 owner、status 和 lease；失败重试会清理旧结果并使用当前模型设置重算。 | `applications/news_agent/runtime/store.py:310-340,562-575` | 本库旧 attempt 可被拒绝；配置快照恢复和任意外部副作用去重尚未因此成立。 |

以上上游固定 commit 已通过本地 checkout 或 `raw.githubusercontent.com` 原始文件读取核查。CPython 引用指向研究快照，不能把其中新版本 API 当成 Python 3.12 的现成能力。

## 单次批处理的容量合同

建议每次调用生成独立 `batch_id`，在开始创建 Runtime 前冻结并发值和来源。`C` 只计本次 Runner 直接执行的 WorkItem，从获得执行资格到 invocation 和其清理结束。每次最多保留 `C` 个尚未收集终态的 Future，获得执行资格前不创建 Runtime。

同一 Worker 同时调用两个 `map(..., concurrency=4)`，两个批处理均可执行 4 个 item，总量可达 8。Agent-as-Tool 子调用、其他批处理、Pi callback 线程和 Provider 请求没有被这个数统一约束。运行事件应带 `batch_id`，让这一作用域可验证。

不建议首版引入 `process_hard_limit: 64`：

- 把每次解析出的 `C` 与 64 比较，只限制单次调用；多个批处理相加仍可超过 64。
- Worker 可能创建子 Runtime、callback 线程或 Shell 进程，Worker 数也不等于进程数。
- 用户尚未要求全进程容量，当前没有资源测量支持 64 这个数。

未来确有全进程容量需求时，再定义它限制的资源、作用域和各入口的 admission 规则。首版只验证显式正整数，拒绝 `0`、负数、`True`、`"auto"` 和字符串数字，不增加隐藏裁剪。

每个批处理独立保存配置快照和执行记录。`RuntimeContext.update_manifest()` 当前是无互斥的读取、修改、写回（`src/execution/context.py:751-764`）；原子替换文件不等于原子更新字段。多个 map 不应并发覆盖同一个 `worker_execution` 字段。

## 嵌套调用为什么会死锁

父 callable 占着唯一线程，同步把子 callable 提交到同一池，再等待子 Future，子 callable 就无法开始。CPython [官方示例](https://github.com/python/cpython/blob/5564790598b8bcc58c00df31a62ca56745961911/Doc/library/concurrent.futures.rst#L142-L172) 直接演示了这一点。容量为 2 时，两个父调用占满线程并等待子调用，也会发生同类问题；增加线程数只能推迟触发。

独立线程池之外再加一个共享 semaphore，仍可能由父调用持有 permit、子调用等待 permit 形成死锁。PydanticAI 对嵌套模型包装器重复使用同一 limiter 会[明确报错](https://github.com/pydantic/pydantic-ai/blob/69ea1e57201ed59e7d30fbe4a7590d193acecce0/pydantic_ai_slim/pydantic_ai/models/concurrency.py#L93-L117)。这是特定嵌套边界的检测，不是通用跨 Worker 环检测器。

首版不合并 map 与 Agent-as-Tool 的容量。若允许同步嵌套的新 map，子批处理必须具有独立批处理状态和执行池，不能重用被父调用占满的池。业务自身的无限递归或互相等待属于编排约束；有限 map 不需要为此引入完整依赖图调度器。

## 停止调度、drain 和资源退出

建议首版退出合同：

1. Runner 处理停止请求后不再 admission 新 item；已知且尚未开始的输入生成 cancelled Outcome。
2. 对 queued Future，只有 `cancel()` 成功才认定它未执行；同时发生的启动须按真实状态处理。
3. 已启动 item 等待调用返回、Runtime 关闭及实例资源清理，再写终态并释放执行资格。
4. 清理失败不能抹掉原始错误或伪装成完整成功；返回已完成 Outcomes，并保留清理错误。

**graceful drain 没有固定退出时限。** 若某个同步 Tool 没有超时且永不返回，首版 map 也可能持续等待。要提供有界中断，需 Runtime、Tool 和 I/O 的合作式取消，或把整个执行隔离到可终止的进程；仅改 Future 状态不能实现。

Pi 的 `_terminate()/close()` 已有 Node 进程组和后代清理（`src/runtimes/pi/transport.py:245-284`；`src/execution/process.py:60-159`），这些是可复用的本地能力。仍应独立验证 callback 线程、MCP 连接和实例资源，不能据此宣称任意 Tool 的远端动作被撤销。Python 官方也区分 [`Popen.communicate(timeout)` 超时后调用方负责 kill 和再次 communicate](https://docs.python.org/3.12/library/subprocess.html#subprocess.Popen.communicate)，与同步线程取消的语义。

## 重试和可复现性

`retryable=True` 表示错误类别可能允许再次尝试，不证明整个 Worker 可以安全重放。若 Tool 已向外部系统写入成功，但响应丢失，Worker 随后失败，重放整个 Worker 会再次触发这个动作。Celery [官方资料](https://github.com/celery/celery/blob/ed354d9100dbdaf7b867f103593b4dea4148670a/docs/userguide/optimizing.rst#L180-L183) 同样要求重投递的任务幂等。

首版 map 不默认重试整个 Worker。Provider 单次请求重试仍归 Provider/Runtime；业务 task 重试由 Application 判断，必要时使用稳定幂等键、下游 fencing 或事务提交。NewsAgent 的唯一 owner 比较已经能够拒绝本库旧提交；只有下游确需比较新旧序号时，才要求单调递增 fencing token。两者都不能自动覆盖未接入这些机制的外部 Tool。

结果保序只保证输入和 Outcome 稳定对应。配置快照、输入摘要及 Runtime 版本用于追踪条件；冻结这些条件仍不能保证模型输出相同。OpenAI 的[官方 seed 示例](https://github.com/openai/openai-cookbook/blob/main/examples/Reproducible_outputs_with_the_seed_parameter.ipynb) 也明确将采样一致性限定为 best effort。该例属于较早的 Chat Completions 功能，不能据此推断 AgentLoom 所有 Provider 都支持 seed。

若以后承诺恢复已有批处理，必须读取其保存的 execution snapshot；重新执行默认读取新配置则应有新的 batch ID，并明确是一次新执行。当前 NewsAgent whole-task retry 使用当前模型设置，不能写成已支持冻结配置恢复。

## 实测与待实现验收

2026-10-11 对宿主 `python` 和项目 `.venv/bin/python` 运行独立 stdlib 探针。用 Event 确认第一个 callable 已启动，将第二个 callable 留在队列，调用 `cancel()` 与 `shutdown(wait=True, cancel_futures=True)`，最后释放第一个 callable：

| Python | `map` 有 buffersize | running cancel | queued cancel | shutdown 等待 running | running 最终结果 |
| --- | --- | --- | --- | --- | --- |
| 3.12.13 | 否 | False | True | 是 | completed |
| 3.14.8 | 是 | False | True | 是 | completed |

两次探针均通过断言。这验证 Python 行为；尚未验证新 WorkerRunner。

| 待实现验收场景 | 必须观察到的结果 |
| --- | --- |
| 同一 Worker、同一 Runner 对象，同时开始两个 `C=2` map，并用 gate 挂起四个 item | 两个批处理各自最多 2 个 running item；两者同时各启动 2 个；停止一个不停止另一个；记录按 batch ID 分开。 |
| 一个 map 的 `C=2`，100 个已知输入 | 同时未完成的直接 admission item/Future/Runtime 不超过 2；最终 Outcome 与输入 ID 一一对应。 |
| 显式配置超过 64 的正整数，输入只有 2 个 item | 不因不存在的硬上限被裁剪或拒绝；只创建有实际输入的执行。 |
| 首个 item 同步调用子 Worker；父批处理 `C=1` | 子调用不排进父调用占用的同一池或 semaphore；能够完成，或在独立编排约束下立即明确拒绝。 |
| `fail_fast` 下一个 item 失败，或请求停止；另一个已启动 item 被 gate 阻塞 | 不再启动新 item；map 尚未返回；释放 gate 后已启动 item 按真实成功或失败终态结束，才完成 drain。 |
| item 已返回，但 Runtime.close 被 gate 阻塞 | 不能提前释放该 item 的执行资格或声称 batch 已结束。 |
| Pi 子进程退出，但一个 Python callback 仍被 gate 阻塞 | 不把进程退出当成全部工作已退出；callback 收敛后才结束清理。 |
| Tool 写入外部计数器成功，随后模拟响应丢失并让 Worker 失败 | map 只产生一次失败 Outcome，不自动执行第二次完整 Worker。 |
| 两个 map 并行读取不同配置来源或并发值 | 各自快照和执行记录保留完整，互不覆盖根 manifest 字段。 |

容量与 stdlib 行为可用受控 callable 验证；Runtime 所有权和清理还需分别跑 Pi、smolagents 的真实 Application runner/CLI。文档更新本身不意味着这些实现验收已经完成。
