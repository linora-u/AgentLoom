# DBOS 产品接入的取消、恢复和 OS 生命周期

2026-10-14。**取消可复用 DBOS 3.2.0 公开 preemptible async step，不需要框架另写 workflow 状态轮询器。恢复仍须先确认旧 OS invocation 退出，并显式回到原阶段队列。** SDK 没有同 workflow ID 的物理排他合同；本轮公开 API 加真实 launchd 的四个观测进一步确认了这个边界。[公开 step 参数][D1]、[执行计数合同][D3]

研究以当前 [开源选择](durable-open-source-selection-20261014.md) 和 [V2 规格](../specs/durable-worker-open-source-engine-integration-v2.md) 为范围，没有重复广泛选型、修改产品或运行模型。只在独立 evidence 目录运行官方 DBOS、SQLite、公开 run_step 和本机一次性 launchd job，未接新闻输入、业务库或 Runtime。

## 固定的一手来源

实际 worktree Python 3.14.8 安装 DBOS 3.2.0；官方 tag commit 为 `d324156616281d7d4923bc60851617bdbb70ea5e`。本机 `_core.py`、`_dbos.py`、`_client.py`、`_sys_db.py`、`_queue.py`、`_recovery.py` 均与固定源码逐字相同，SHA 记录在 [MANIFEST](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-preemptible-public-lifecycle-20261014-v1/MANIFEST.json)。官方文档固定为 dbos-docs commit `6ffca1d4fc6c4aeaf6cd3634bb58fb8eaab9fa16`。Pydantic AI 集成参考固定 main commit `b8c1850638ec12f6e23990bda8cc8e2e7c854e2a`，不把其未发布分支当作本项目已安装依赖。[文档][DOC1]、[Pydantic 集成][PA1]

## 同步调用不具备抢占，外层 async OS step 可以复用官方取消

`DBOS.step()`／`DBOS.run_step()` 的公开 StepOptions 包含 preemptible、timeout_seconds、retries_allowed、max_attempts 和 should_retry。`preemptible=True` 与 step timeout 都只支持 async 函数；将它们用于同步 step 会明确报错。普通同步 step 的取消只会在下一 step 边界或下一次系统 checkpoint 检查中被观察，不能打断运行中的 Python 同步函数。[公开 API][D1]、[step 验证][D2]、[取消文档][DOC1]

官方 `_supervise_step` 提供了当前需要的协调：

- preemptible step 每 1 秒查询其 workflow 是否 Cancelled。
- 观察取消或 deadline 后，取消 step coroutine，并等待它完成取消后的 cleanup，再抛 DBOSWorkflowCancelledError 或 DBOSStepTimeoutError。
- 若 step 已经完成，保留它的完成结果。
- 被 workflow 取消的 step 不记录结果，允许 resume 重跑；timeout 的结果则可被持久化并按已配置重试处理。[监督源码][D2]

因此，可以让完整 Application 仍在一次 OS job 中同步执行 BoundWorker.invoke，只有“启动／等待／关闭 OS job”的薄 adapter 是 async。同期同步 workflow 也可以用公开 run_step 调用这个 async adapter；不要求替换 AgentLoom 同步 Runtime。

最短公开接线如下，invoke_os_application 是资源 adapter，不是新的任务队列：

```python
from dbos import DBOS

@DBOS.workflow(name="application_worker")
async def application_worker(description):
    return await DBOS.run_step_async(
        {"name": "os_owned_application", "preemptible": True},
        invoke_os_application,
        description,
    )
```

同步 workflow 的等价公开调用是 `DBOS.run_step(options, invoke_os_application, description)`；run_step 允许 async callable，内部提交到 SDK event loop 并等结果。本轮真实观测采用的就是这个组合。[run_step 实现][D4]、[官方 run_step_async 测试][T1]

**SDK 等待的是 coroutine 的结束。** 如果 adapter 的关闭失败后直接抛错、却让 native job 继续存活，SDK 仍会结束这次调用并释放本地计数。adapter 必须在返回成功、失败或取消前确认 OS 范围退出；清理诊断单独保存，不能把“抛出清理错误”当退出证明。[workflow 释放][D3]、[本项目原直接包装负例](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-application-go-no-go-20261014-v1/REPORT.md)

## resume 会重新入队，不等待同 ID 的旧进程

取消会立即把持久状态改为 Cancelled，清空 owner、queue 等字段；它没有直接清掉本地 active entry。resume 对未完成任务改为 Enqueued，并清空 owner／recovery count／deadline；没有等待旧执行的动作。[状态操作源码][D5]

默认 resume 使用内部队列。官方文档明确将此能力称为 bypass queue；只有传 `queue_name` 才返回指定阶段队列。因此公共管理入口必须显式绑定原 stage，不能把普通 SDK 默认 resume 直接暴露为“保持 20／30／20 的恢复”。[resume 文档][DOC2]、[Client 参数][D6]

即使传原 stage，仍不具备同 ID 排他。ActiveWorkflowById 的固定源码直接注明：

> A refcount, not a lock: a workflow re-dispatched here (a resume of one still running) runs alongside the stale execution, which parks at its next write.

每次执行各占一个 queue bucket，stage 还有可用容量时，旧、新同 ID 都能运行。计数守住阶段 N，并不保证每个任务只有一个物理 invocation。[计数源码][D3]

## 本轮公开 API 与真实 launchd 的有效观测

独立程序使用 SDK 原版、SQLite 默认设置、同步 workflow 加公开 `DBOS.run_step({preemptible:True}, async adapter)`。每次 invocation 启动随机自有 launchd label，KeepAlive=false，native 子进程继承 job 组。首次取消后的 cleanup 人为保持不结束，用于判断 resume 是否先于实际资源退出。没有请求模型，没有改 SDK 或容量判断。

| 场景 | 原始结果 | 能支持的结论 |
| --- | --- | --- |
| 取消已被 SDK 观察，cleanup 未释放，默认 resume | 第二 invocation 启动时旧 native 活着 | 默认恢复没有物理排他，会绕开原 stage 限额 |
| 同样 held cleanup，resume 回原 stage，worker_concurrency=1 | held 期间未启动第二 invocation；释放后成功 | 本地单额度保留到 coroutine／OS cleanup 完成 |
| 同样 held cleanup，resume 回原 stage，worker_concurrency=2 | 第二 invocation 启动时旧 native 活着 | 阶段容量与同 ID 排他不同；没有超过配置 2 |
| cancel 返回后立刻默认 resume | 第二 invocation 开始时旧 native 活着，旧 step 尚未观察 Cancelled | 1 秒轮询前清掉 Cancelled 可能让旧执行错过取消 |

原始 observations 为 [默认恢复](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-preemptible-public-lifecycle-20261014-v1/default-after-cancel-v3/observations.json)、[原队列额度 1](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-preemptible-public-lifecycle-20261014-v1/same-stage-one/observations.json)、[原队列额度 2](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-preemptible-public-lifecycle-20261014-v1/same-stage-two/observations.json)、[立即恢复](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-preemptible-public-lifecycle-20261014-v1/immediate-default/observations.json)。程序退出 0 表示观测结束，三项 `same_id_physical_non_overlap_passed=false` 保留为负面结果，不能算合同通过。

首次脚本把 attempt 计数误作“实际 adapter 已启动”，StopIteration 退出 1；当时重复的一次同脚本运行也保留。只修了等待明确 adapter_started 事件的观测竞态，未改故障、资源范围或期望。最后 20 个已记录 root／native 身份均已退出，12 个自有 job label 均已移除；数据和原日志未覆盖。[进程核对](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-preemptible-public-lifecycle-20261014-v1/final-process-verification.json)、[自有 job 清理](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-preemptible-public-lifecycle-20261014-v1/final-owned-job-verification.json)

管理入口可以做一次具体 OS owner stop／exit 核验，再调用 SDK resume(queue_name=stage)。任务 OS scope 的非阻塞文件锁可拒绝同 ID 在途执行。这些是针对当前调用的资源保护；不应添加另一套持久 cancellation intent、claim、lease 或重试定时循环。

## Failed 重试和 Cancelled 恢复是不同 API 合同

`resume_workflow` 对 Success／Error 是 no-op；它主要恢复 Cancelled 或超过 recovery 次数的状态。已经记录的 step error 也会被重放。因此，正式业务 Failed 不能只叫 resume 然后宣称重新调用模型。[状态操作][D5]、[step 重放][D2]

公开 step retries 可通过 retries_allowed、max_attempts、should_retry 配置，当前 body 的 finally 完成后才进入下一次 retry。若需要对已 Error workflow 重新执行，公开 fork／rewind 可明确从指定 step 重启；queue_name 同样须绑定原阶段，业务 revision 与完成回执另行校验。选择哪个入口由 News 既有重算规则决定，框架不另写 Failed 任务领取器。[step retry][D2]、[fork／rewind 文档][DOC1]

## destroy 和 launch 都不能替代 OS owner

官方文档明确：destroy 最多等 workflow_completion_timeout_sec，取消尚未开始的 executor task；它不会中断还在运行的同步 workflow，关闭后旧执行不能再 checkpoint。源码在 timeout 后调用线程池 `shutdown(wait=False, cancel_futures=True)`，随后关闭数据库。[destroy 文档][DOC3]、[destroy 源码][D7]

async 执行也不能从 destroy 返回推断清理已完成。SDK background event loop 的 stop 会取消 tasks，join 只有有限 timeout，超时可留下警告后继续关闭系统。物理退出应由 OS owner 确认；单本地 executor 身份不能因为 FileLock 已释放或 destroy 返回就被当成已经死亡。[event loop stop][D8]

launch 内部会主动恢复本 executor／application version 的 Pending workflow，并启动 queue listener。`reenqueue_for_recovery` 使用 SDK 自己的原子重新入队，保留已有 queue 或 fallback 内部队列；不能在 launch 后才检查旧 OS job，否则恢复已经有机会运行。[启动恢复][D7]、[恢复源码][D9]

startup 顺序应是 OS 单例锁、旧 executor 具体身份检查、旧 job／组退出核验，再 DBOS.launch。单例锁使用成熟文件锁接口，锁只保护合作宿主互斥；它不证明旧 native 退出。持有者进程／组仍活着或未知时阻止启动，避免用第二套数据库租约来猜测存活。[Python flock][F1]、[V2 规格](../specs/durable-worker-open-source-engine-integration-v2.md)

## macOS launchd 能管理 root crash，但身份握手必须覆盖启动窗口

Apple 原始 launchd.plist 手册规定：job 死亡时默认杀同 PGID 的剩余进程；AbandonProcessGroup=true 才关闭该行为。KeepAlive=false 避免 OS 与 DBOS 重试重复，bootstrap／bootout 管理具体自有 job。launchctl print 的诊断输出不构成稳定 API，也不是物理退出证据。[Apple 固定手册][A1]、[本机 launchctl 手册](/usr/share/man/man1/launchctl.1)

本项目前序同组与 detached 对照，以及 DBOS／Application 三项实验已有证据；本轮四项进一步证明真实 async adapter 可以等待 root 和组退出。它们没有证明正式产品在“描述已写、bootstrap 尚未返回、identity 未写、executor 被 kill”这些 startup 窗口均正确。[前序 OS 对照](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/launchd-process-group-go-no-go-20261014-v1/REPORT.md)、[前序 Application](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-launchd-application-go-no-go-20261014-v1/REPORT.md)

必要适配是已提交的 OS job descriptor、具体 PID／创建时间／PGID、输入与结果文件引用，以及授权前握手。descriptor 不能成为第二套待办任务表。bootstrap 取消后若可能晚到，cleanup 必须等该启动操作确定结束，再 bootout／核验；授权旧 job 失去身份时应拒绝恢复，不能写退出 marker 后放行。[Apple 启动机制][A1]、[当前规格](../specs/durable-worker-open-source-engine-integration-v2.md)

## 可复用的 Agent 集成模式，以及不能替代的部分

Pydantic AI 的 DBOSDurability capability 是值得复用的分层参照：model、tool、MCP 以及事件处理包装成官方 `DBOS.step`，序列化与重放交给 SDK。它也将 DBOSWorkflowCancelledError／ConflictIDError 识别为取消类控制，而不把它们当普通业务失败。[capability][PA1]、[operation backend][PA2]

这套集成没有提供 launchd／本机子进程树所有者，也没有同 workflow ID resume 的 OS drain hook；它的 typed StepConfig 当前只列 retries_allowed、interval_seconds、max_attempts、backoff_rate。把 Pi／smolagents 替换为 Pydantic AI 并不能从这些源码推出完整 native 退出保证。因此，直接复用 DBOS 官方 async preemption 加小型 OS adapter，更符合当前目标。[step config][PA3]、[公开能力范围][PA1]

本轮没有找到可以直接接管 AgentLoom 全部 OS 资源、原子业务授权以及恢复前身份核验的 DBOS 公共集成。这个结论限定上述官方接口与固定源码，不声称所有项目均不存在。必要小适配应只连接 Runtime 与 OS 生命周期；DBOS 已有的取消轮询、checkpoint、step retries、入队、容量及恢复必须继续复用 SDK。

## 产品 Adapter 的只读回归

随后独立审查了 host、job_entry、os_job、executor 和 client。启动顺序已是具体 executor 身份／单例检查、旧 OS job 恢复，再 DBOS.launch；resume 使用原 Worker queue，Error 用公开 rewind，未另写 claim／lease／slot 内核。

清理异常路径实际复现了两次 RED。一次是 finally 无条件释放 active 与 scope.lock；后一次虽然增加 cleanup.result 的退出等待，`await asyncio.shield(cleanup)` 先抛 Task 原异常，仍绕过后续处理。两次 stage 容量为 1，新 invocation 启动时旧 native 都活着。完整原日志、固定源码和哈希保留，未覆盖失败。

修复后的固定 os_job.py SHA-256 为 `21903bc31da4a007c7e9a1f2b1d8d537db3c1b0331914398d8ab43d3c974c96f`。同一 OSError 故障下，active／scope.lock 保留，旧 native 活着期间不补位；外部真实 bootout／退出后才启动新任务，原错误也保存。完成历史 descriptor 的组重用模拟未被误杀；真实 loaded、未启动 job 遇到 bootout 非 0 时不写退出 marker，不授权业务。三个最终探针均退出 0；18 个自有进程身份已退出，17 个自有 label 诊断均未命中，没有遗留 job_entry root。[产品审查报告](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-product-os-review-20261014-v1/REPORT.md)

另实跑了 shutdown 创建窗口：reconcile 期间停止接单，旧版仍创建未 bootstrap 的 descriptor，导致 invocation 无法结束。最终文件 `8e918dce80e09e545d3068f85f9cccba9adf472a97ba5cfe485664b9a0b7c423` 将创建全部移入 admission 锁、第二次 stopping 检查之后；同一故障不创建 descriptor、不执行 OS 控制，直接抛 AdmissionStopped 并释放 scope.lock。此项单独通过，未把前三项旧 hash 的结果冒称为新 hash 的重复实测。[shutdown RED／GREEN](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-product-os-review-20261014-v1/REPORT.md)

launchctl 公共手册只承诺 0 表示操作成功，未给非 0 的“服务已卸载”稳定合同。最终适配严格接受 0，单独保存卸载控制记录；物理退出仍查具体身份／组。Apple SMJobRemove／SMJobCopyDictionary 及 PyObjC 绑定可作参照，但 SDK 头文件明确这些 API 已 deprecated，不能为了补恢复窗口就当作新的长期接口。此轮未验证 bootstrap 所有崩溃窗口，也不替代正式 Runtime 和全日 News 验收。[控制语义与绑定来源](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-product-os-review-20261014-v1/REPORT.md)

## 一手引用

[D1]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_dbos.py#L1168-L1215
[D2]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_core.py#L2121-L2388
[D3]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_core.py#L924-L997
[D4]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_core.py#L2391-L2481
[D5]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_sys_db.py#L1217-L1305
[D6]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_client.py#L831-L862
[D7]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_dbos.py
[D8]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_event_loop.py#L40-L81
[D9]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/dbos/_recovery.py
[DOC1]: https://github.com/dbos-inc/dbos-docs/blob/6ffca1d4fc6c4aeaf6cd3634bb58fb8eaab9fa16/docs/python/tutorials/workflow-management.md
[DOC2]: https://github.com/dbos-inc/dbos-docs/blob/6ffca1d4fc6c4aeaf6cd3634bb58fb8eaab9fa16/docs/python/reference/contexts.md#resume_workflow
[DOC3]: https://github.com/dbos-inc/dbos-docs/blob/6ffca1d4fc6c4aeaf6cd3634bb58fb8eaab9fa16/docs/python/reference/dbos-class.md#destroy
[T1]: https://github.com/dbos-inc/dbos-transact-py/blob/d324156616281d7d4923bc60851617bdbb70ea5e/tests/test_async_workflow_management.py#L316-L428
[PA1]: https://github.com/pydantic/pydantic-ai/blob/b8c1850638ec12f6e23990bda8cc8e2e7c854e2a/pydantic_ai_slim/pydantic_ai/durable_exec/dbos/_durability.py
[PA2]: https://github.com/pydantic/pydantic-ai/blob/b8c1850638ec12f6e23990bda8cc8e2e7c854e2a/pydantic_ai_slim/pydantic_ai/durable_exec/dbos/_operation_backend.py
[PA3]: https://github.com/pydantic/pydantic-ai/blob/b8c1850638ec12f6e23990bda8cc8e2e7c854e2a/pydantic_ai_slim/pydantic_ai/durable_exec/dbos/_utils.py
[A1]: https://github.com/apple-oss-distributions/launchd/blob/d448a1c8f70a61202f8705f94337f686b87c30c4/man/launchd.plist.5
[F1]: https://docs.python.org/3/library/fcntl.html#fcntl.flock
