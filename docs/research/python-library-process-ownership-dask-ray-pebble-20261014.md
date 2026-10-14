# Python 库进程所有权：Dask、Ray、Pebble

2026-10-14。本轮只做资料研究，没有修改产品源码、配置、依赖，也没有启动新的实验。任务约束见 [ADR-0003](../adr/0003-library-owned-cross-platform-process-lifetime.md)：macOS 与 Linux 都要可 pip 安装本地运行；默认 SQLite/本地持久状态，未来可接远端后端；不要求用户配置 launchd/systemd 或另起数据库/broker/service；不得新写 queue/claim/lease/retry scheduler、私有 SDK 表适配、orphan registry 或自定义 process supervisor；验收里的 SIGKILL 只杀顶层 EXECUTOR Python，库自己启动的独立 guardian/nanny 可以存活并自动清理全部受管理任务子孙进程后退出。任务主动脱组不属于该验收场景。

**所核对的 Dask.distributed、Ray 和 Pebble 接入方式中，尚未建立完整满足这组组合条件的保证。** Ray 的本地 runtime 与 worker process-group cleanup 最接近目标；关键未验证项是顶层宿主死亡后，负责 Worker 组清理的组件是否仍能完成全部收尾，而非它缺少持久队列。Dask 的 Nanny/AsyncProcess 能在父进程意外死亡时让直属 worker 自退，但未建立独立 guardian 递归清理 worker 后代的合同。Pebble 只管理自己的 worker PID。

进程库可以与 DBOS 组合：DBOS 已承担持久队列和阶段容量，NewsAgent 已承担业务回执和写授权，不要求进程库重做这些职责。Ray 值得作为完整 Application 的执行隔离候选继续验证，通过前不能宣布替代方案已成立。

## 判定矩阵

| 要求 | Dask.distributed 2026.8.0 | Ray 2.59.0 | Pebble 5.2.3 |
| --- | --- | --- | --- |
| pip 本地运行，macOS+Linux | 满足安装形态；官方文档支持 pip 安装 `dask distributed`，`LocalCluster` 默认本机 scheduler+worker，processes=True 时默认 Nanny。 | 满足安装形态；官方文档支持 PyPI Linux/macOS，`ray.init()` 可启动本地 runtime。 | 满足安装形态；README 指向 PyPI，setup classifier 为 OS Independent。 |
| 默认不要求用户部署数据库/broker/service | 本地进程可内嵌启动；但 Dask 本身不是持久队列/SQLite 后端。 | 本地 runtime 可由 `ray.init()` 启动；但 Ray 是一组本机 Ray 进程，不是 SQLite 持久队列。 | 是纯库进程池；但没有持久队列/SQLite 后端。 |
| 顶层 EXECUTOR SIGKILL 后，独立库 guardian/nanny 存活并清理 | AsyncProcess 的 child 自己监控父 pipe 并 `os._exit(-1)`；普通 LocalCluster 的 Nanny 在宿主内，未建立独立监护递归清理。 | macOS reaper 清理自身组；默认 Linux fate-sharing 可能随宿主强杀 raylet。隔离 Worker 在其他组，完整清理顺序未建立。 | 不满足。pool manager 是 executor 内线程；executor SIGKILL 后没有独立 guardian。 |
| worker/actor 死亡后清理合法子孙 | Dask 只保证/测试直属 AsyncProcess worker 退出；未建立递归后代清理。 | Linux/POSIX worker 进程组清理有源码和上游测试；同组 child/grandchild 覆盖，`setsid()` 逃逸负例仍存活。macOS 有 POSIX 进程组测试覆盖 nested cleanup，但官方注释把“orphan process killing”测试限定 Linux。 | 不满足。`stop_process()` 对 worker PID terminate/join/kill，不递归子孙。 |
| 公共稳定 API 可作为产品合同 | Nanny/LocalCluster 是公开 API；AsyncProcess 是内部实现，未建立完整资源清理合同。 | `ray.init()`/`ray.shutdown()` 是公开 API，默认 POSIX 组清理有官方文档；可读私有源码理解机制，但产品不应调用私有 Node/reaper 接口。 | `ProcessPool` 是公开 API；但没有所需 guardian/递归清理 API。 |
| 保留 20/30/20 stage capacities 与 Application/BoundWorker 调用合同 | 需要另接业务队列与容量；Dask 任务调度不是当前持久 stage 队列。 | 需要另接业务队列与容量；Ray 的对象/actor 生命周期不同于当前 Application invocation。 | 需要另接业务队列与容量。 |
| 总判定 | 不通过。可借鉴直属 worker 父死自退机制，但不能当完整资源边界。 | 不通过，但最有局部研究价值；可验证“每个 Application 放入 Ray worker/actor 且禁止脱组”的受控沙箱路线。 | 不通过；只适合普通进程池超时/worker 重启，不适合本需求。 |

## Dask.distributed：Nanny 管 worker，不管完整资源树

固定来源为 `distributed` tag `2026.8.0`，peeled commit `0a341df4782bc358a074fb12fe7ce277fe2313ce`。官方安装文档写明可用 pip 安装 `dask distributed`。[Dask install][DASK_INSTALL] `LocalCluster` 文档说明它在本机创建 scheduler 和 workers；`processes=True` 时默认 worker class 是 `Nanny`。[LocalCluster][DASK_LOCAL] Nanny 文档说明 “spins up Worker processes, watches them, and kills or restarts them as necessary”。[Nanny class][DASK_NANNY] 用户文档也说 Dask workers 默认由 small Nanny process 启动、监控和管理。[Worker docs][DASK_WORKER_DOC]

这些是“Dask 管自己的 Worker 进程”的公开事实，不是“Dask 管 Application 内所有 Pi/MCP/tool 子孙”的事实。Nanny 的 `kill()` 先让 worker close，超时后调用 `process.kill()` 并 `join()` worker；docstring 只承诺返回时 worker process 已 join。[Nanny.kill][DASK_NANNY_KILL] `AsyncProcess.kill()` 只是向 underlying `multiprocessing.Process.kill` 发送请求，`join()` 只等该 child process exit。[AsyncProcess kill/join][DASK_ASYNCPROC_KILL]

Dask 有一个重要但仍不足够的机制：`AsyncProcess` 创建 pipe，child 里启动 monitor thread；父端关闭时 child 直接 `os._exit(-1)`。[AsyncProcess parent pipe][DASK_ASYNCPROC_PARENT] 上游测试 `test_asyncprocess_child_teardown_on_parent_exit` 模拟 parent `os._exit(255)`，验证直属 worker 进程会退出。[AsyncProcess parent-death test][DASK_ASYNCPROC_TEST] 这满足“父进程死后直属 worker 自退”的一部分，但不满足 brief 里的“一个单独 guardian/nanny 存活并自动清理全部任务 descendant processes”。原因是退出动作发生在 worker 自己的 monitor thread；如果 worker 已死、worker 的子孙已 reparent，Dask 没有公开 API 或上游测试证明 Nanny/AsyncProcess 能重新定位并递归清理这些后代。

Dask `SpecCluster` 的 `ProcessInterface` 允许外部 resource manager 实现 start/close，并在文档里列举 Kubernetes/Yarn/SLURM/PBS/SGE 等资源管理器。[SpecCluster][DASK_SPEC] 这说明 Dask 的深层模型是“把资源所有权交给外部 resource manager 或 Nanny/Worker 边界”，不是内置跨平台本地进程树所有者。若为了 AgentLoom 在 `ProcessInterface.close()` 里补进程组/cgroup/orphan registry，就已经进入 brief 明确禁止的新 custom supervisor/orphan registry 范围。

对本需求的 Dask 判定：不通过。可以复用的事实只有“本地 Nanny worker”和“直属 AsyncProcess 父死自退”。缺失的是独立 guardian 在 EXECUTOR SIGKILL 后仍存活、递归清理全部 legitimate task descendants、并在 macOS 和 Linux 上都有公开稳定合同。

## Ray：最接近，但边界是 Ray worker/process group，不是完整 Application invocation

固定来源为 Ray tag `ray-2.59.0`，commit `dd83c3a703a5422d0b47f142cb380c6330817608`。官方安装文档写明 Ray 官方 wheel 支持 Linux 和 macOS，并给出 `pip install -U "ray[default]"` 与最小 `pip install -U "ray"`。[Ray install][RAY_INSTALL] 官方启动文档说 `ray.init()` 后可使用 Ray API，并且 “When the process calling ray.init() terminates, the Ray runtime will also terminate”。同页还说明如果 `ray.init()` 启动了新的 local cluster，`ray.shutdown()` 会 terminate all local Ray processes；连接既有 cluster 时只断开 client，不关闭 remote cluster。[Ray starting][RAY_START]

Ray 内部确实有一个独立 reaper。`ray_process_reaper.py` 文件头说明：这是 lightweight reaper process，用来在 main ray process 意外死亡，例如 segfault 或 SIGKILL 后清理 Ray processes；但如果 reaper 自己被 SIGTERM/SIGKILL，可能清理不完整。它通过继承 stdin 检测 parent 死亡，然后先 SIGTERM、再 SIGKILL 自己的 process group。[Ray reaper][RAY_REAPER] `Node.start_reaper_process()` 注释也说 reaper 必须是第一个进程，用于在当前进程死亡时清理 Ray processes。[Ray Node reaper][RAY_NODE_REAPER] `services.start_reaper()` 先让调用进程成为 process group leader，再启动 reaper，从而 reaper 能清理 Ray process group。[Ray start_reaper][RAY_START_REAPER]

Ray 还有 Linux kernel fate-sharing：`detect_fate_sharing_support_linux()` 查 `prctl`，`set_kill_on_parent_death_linux()` 使用 `PR_SET_PDEATHSIG` 给 child 设置 parent-death SIGKILL。[Ray fate sharing][RAY_FATE] `start_ray_process()` 的 `fate_share` 参数说明 “child will be killed if its parent dies”，Linux 下在 `preexec_fn` 调用该设置。[Ray start_ray_process][RAY_START_PROCESS] 这些是 Ray 自身进程的 parent-death 生命周期材料。

Ray 对 worker 内子进程的材料更接近 brief。配置 `process_group_cleanup_enabled` 默认 true，注释写明 POSIX 上 worker 会被放到自己的 process group，并可在 worker death 时通过 `killpg` 清理；Windows no-op。[Ray config][RAY_CONFIG] worker 启动时传入 `new_process_group`，源码注释为 “Workers should be placed into their own process groups (if enabled) to enable per-worker cleanup via killpg on worker death”。[Ray worker pool][RAY_WORKER_POOL] Raylet 的 NodeManager 在 worker disconnect 时，如果非 graceful，会对保存的 PGID 发送 SIGTERM，并在 200ms 后 probe group，仍存在则 SIGKILL；如果 graceful，则等 worker 自己退出后再扫 group。[Ray node manager cleanup][RAY_NODE_CLEANUP] `Stop()` 时也 best-effort 清理剩余 workers 的 process group。[Ray node stop][RAY_NODE_STOP]

上游测试给了正负边界。Linux-only 测试 `test_sigkilled_worker_child_process_cleaned_up` 杀 actor PID 后等待 child PID 消失。[Ray child cleanup test][RAY_CHILD_TEST] `test_nested_subprocess_cleanup_with_pg_cleanup` 覆盖 actor 里 child 再 spawn grandchild，actor 删除后 child 和 grandchild 都消失；该测试标注 POSIX Linux/macOS。[Ray nested test][RAY_NESTED_TEST] 但 `test_detached_setsido_escape_with_pg_cleanup` 明确让子进程 `os.setsid()` 脱离 worker process group，杀 actor 后断言 detached child 仍存活。[Ray escape test][RAY_ESCAPE_TEST]

这些证据说明 Ray 有一个可复用的受控范围：**Ray worker/actor 所在进程组内的子孙可以在 worker death 后由 raylet 清理；Ray local cluster 由 reaper/fate-sharing处理主进程意外死亡。** 但它仍不等于当前需求的完整答案：

1. Ray 管的是 Ray runtime、raylet、Ray worker/actor 与其 process group；brief 要的是 AgentLoom 的完整 Application invocation，包括 Pi/Node、MCP、tool subprocesses 和 grandchildren。只有当这些资源都在 Ray worker 同一 process group 内且不调用 `setsid()`/新 session 时，Ray 的测试事实才覆盖它们。Ray 自己的 escape test 已证明脱组会存活。
2. Ray 的本地 runtime 不是 SQLite 持久队列。保留 DBOS 和 NewsAgent 账本即可承担阶段额度、业务完成回执和写授权，不需要新调度器；这项职责差异本身不是排除 Ray 的理由。
3. Ray `kill_all_processes(wait=True)` 即便显式 wait，也有 bounded wait 后 abandon unreaped process 的源码分支；它不是“永远阻塞直到所有本地资源退出”的合同。[Ray kill wait][RAY_KILL_WAIT]
4. Ray 进程清理与远端 Provider inference 生命周期无关，不能把本地 HTTP client/worker 退出写成远端推理已停止。

主 agent 补充的宿主死亡核对：Linux 的普通本地启动会选 kernel fate sharing，对 raylet 等子进程安装父死亡 SIGKILL；它可能同时消灭 Worker 组的清理者。macOS reaper 对自身组发 TERM 后约一秒升级 KILL，Worker 已在其他组，依赖 raylet 在这段生命周期中完成清理。`NodeManager::Stop()` 发 KILL 后没有继续等待组为空；异常断开路径安排 200ms 升级信号后，先释放执行资源。这些是固定源码观察，不是本轮已复现的故障；它们说明必须单独验证顶层宿主强杀，不能从 actor 清理测试推断通过。[Node][RAY_NODE_REAPER]、[fate sharing][RAY_FATE]、[reaper][RAY_REAPER]、[断开与释放][RAY_NODE_CLEANUP]、[Stop][RAY_NODE_STOP]

对本需求的 Ray 判定：尚未通过完整验收，比 Dask/Pebble 更值得定向验证。完整 Application 的 Pi/MCP/tool 资源须留在 Worker 管理范围，Linux 与 macOS 分别验证，持久队列仍由 DBOS 承担。任务主动脱组的上游负例划出范围，不作为本轮要求库拦截任意逃逸的依据。

## Pebble：公开 ProcessPool 只管理 worker PID

固定来源为 Pebble tag `5.2.3`，peeled commit `b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c`。README 说 Pebble 提供 within an application 管理 threads/processes 的 API，Download 指向 PyPI。[Pebble README][PEBBLE_README] setup.py 声明 Python >=3.10、Development Status Production/Stable、Operating System OS Independent。[Pebble setup][PEBBLE_SETUP] README 展示 `@concurrent.process(timeout=10)` 和 `ProcessPool(max_workers=5, max_tasks=10)` 的公开用法，强调 timeout 和 worker restart。[Pebble examples][PEBBLE_EXAMPLES]

源码边界很窄。`ProcessPool` 启动 pool manager threads，然后 `WorkerManager` 通过 multiprocessing context 创建 worker process。[Pebble ProcessPool][PEBBLE_POOL] `stop_process()` 对一个 `BaseProcess` 调 `terminate()`，join 3 秒；仍活且非 Windows 时对该 PID 发 SIGKILL，然后 join。这里没有 process group、children(recursive)、cgroup 或 guardian。[Pebble stop_process][PEBBLE_STOP] `WorkerManager.force_stop_workers()` 遍历 `self.workers` 并对每个 worker 调 `stop_process()`；`maybe_stop_worker()` 也只 stop 目标 worker。[Pebble worker manager][PEBBLE_WORKERS] 解释器 shutdown hook 只枚举 `multiprocessing.active_children()` 中名为 pebble worker 的直接 child 并 stop。[Pebble shutdown hook][PEBBLE_SHUTDOWN]

上游测试覆盖的是 worker 层。`test_process_pool_join_workers` 只断言 join 后 worker manager 的 workers 字典为空。[Pebble join test][PEBBLE_JOIN_TEST] `test_process_pool_expired_worker` 让 worker 自杀，Future 抛 `ProcessExpired` 并带 worker PID。[Pebble expired test][PEBBLE_EXPIRED_TEST] 还有测试证明 worker 内可以启动 `multiprocessing.Pool` 或另一个 `pebble.ProcessPool` 并成功返回，但这不是异常/崩溃后的递归 cleanup 测试。[Pebble child pool tests][PEBBLE_CHILD_TESTS]

对本需求的 Pebble 判定：不通过。它没有独立 guardian；pool manager 是 executor 内线程，顶层 EXECUTOR 被 SIGKILL 后线程也死。它没有公开 process-group/cgroup/recursive descendant 清理 API，也没有上游测试证明 worker SIGKILL 后 child/grandchild 被自动回收。把 Pebble 改成满足要求需要外部 OS owner 或自写 supervisor/orphan registry，正是本轮禁止的方向。

## 与现有 DBOS/launchd 背景的关系

已有研究已经把“持久任务队列”和“本机进程所有权”拆开：DBOS 3.2.0 负责本地 SQLite 队列、恢复和 stage capacity；OS owner 负责一次完整 Application 的本机进程边界；业务库负责完成回执与写授权。[durable-open-source-selection][PRIOR_SELECTION] 旧 loky 验证也给出相同教训：普通关闭失败可清理，但 root worker `os._exit(17)` 后旧 native 仍存活，说明 Python 进程池 join/kill root worker 不能自动重建已 reparent 后代身份。[loky validation][LOKY_REPORT]

Dask 和 Pebble落在 loky 同一类问题里：它们主要管理直属 worker。Ray 增加了 reaper 和 worker process group cleanup，因此比普通进程池强；但它仍是 Ray worker 边界，而不是持久业务 engine 和完整 Application 资源边界的合体。若把 Ray 当作唯一答案，需要额外回答三个无法从当前一手来源推出的问题：

- DBOS 的阶段队列与 Ray 的 Application 调用如何通过公开 API 接线，使重试先等待资源收尾？阶段额度、SQLite 状态和业务回执已有职责归属，不重做一遍。
- Pi、MCP、tool subprocesses 是否全部能在 Ray worker PGID/cgroup 内运行，且没有合法接线调用 `setsid()`、`start_new_session=True` 或 remote service lifetime escape？
- 顶层 EXECUTOR SIGKILL 后，Ray reaper/raylet 清理完成如何成为“新 Runtime admission 前的可验证事实”，且不用新写 orphan registry 或私有表适配？

在这些问题没有用公开 API 和真实 Application 验证回答前，本轮不能选定 Ray 为替代实现。保持用户确认的库式部署与自动清理要求，保留 DBOS 队列职责，继续验证进程库；不退回 launchd/systemd 方案，也不新增自研 watcher。本轮没有找到已证实满足全部条件的接入方式，不能把它扩大成所有第三方库均不可能实现的结论。

## 证据分级

- **官方文档保证**：Dask pip 安装、本地 `LocalCluster`/Nanny 概念；Ray pip 安装、本地 `ray.init()` runtime 退出/`ray.shutdown()` 行为；Pebble README/setup 的 install/package/API 定位。
- **固定源码推断**：Dask Nanny kill/join 仅 worker；Dask AsyncProcess parent pipe 只让 child 自退；Ray reaper/process group/fate-sharing 作用于 Ray 进程；Ray kill wait 有 bounded abandon；Pebble stop_process 只杀 worker PID。
- **上游测试覆盖**：Dask AsyncProcess 父死直属 worker 退出；Ray worker SIGKILL 后同组 child 消失、nested child/grandchild 消失、setsid escape 存活；Pebble worker join/expired worker/worker 启动 child pool 成功。
- **未建立**：任一候选在 macOS+Linux 上、默认无需 launchd/systemd/外部服务、顶层 AgentLoom EXECUTOR SIGKILL 后由库自带独立 guardian 自动清理完整 Application 的 Pi/MCP/tool 全部子孙，并且把“清理完成”作为下一 Runtime admission 前的可验证条件。

[DASK_INSTALL]: https://github.com/dask/distributed/blob/0a341df4782bc358a074fb12fe7ce277fe2313ce/docs/source/install.rst#L1-L21
[DASK_LOCAL]: https://github.com/dask/distributed/blob/0a341df4782bc358a074fb12fe7ce277fe2313ce/distributed/deploy/local.py#L23-L90
[DASK_WORKER_DOC]: https://github.com/dask/distributed/blob/0a341df4782bc358a074fb12fe7ce277fe2313ce/docs/source/worker.rst#L107-L113
[DASK_NANNY]: https://github.com/dask/distributed/blob/0a341df4782bc358a074fb12fe7ce277fe2313ce/distributed/nanny.py#L65-L106
[DASK_NANNY_KILL]: https://github.com/dask/distributed/blob/0a341df4782bc358a074fb12fe7ce277fe2313ce/distributed/nanny.py#L824-L889
[DASK_ASYNCPROC_PARENT]: https://github.com/dask/distributed/blob/0a341df4782bc358a074fb12fe7ce277fe2313ce/distributed/process.py#L86-L180
[DASK_ASYNCPROC_KILL]: https://github.com/dask/distributed/blob/0a341df4782bc358a074fb12fe7ce277fe2313ce/distributed/process.py#L289-L330
[DASK_ASYNCPROC_TEST]: https://github.com/dask/distributed/blob/0a341df4782bc358a074fb12fe7ce277fe2313ce/distributed/tests/test_asyncprocess.py#L367-L457
[DASK_SPEC]: https://github.com/dask/distributed/blob/0a341df4782bc358a074fb12fe7ce277fe2313ce/distributed/deploy/spec.py#L44-L100

[RAY_INSTALL]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/doc/source/ray-overview/installation.md#L18-L64
[RAY_START]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/doc/source/ray-core/starting-ray.rst#L55-L89
[RAY_REAPER]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/python/ray/_private/ray_process_reaper.py#L7-L56
[RAY_NODE_REAPER]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/python/ray/_private/node.py#L409
[RAY_START_REAPER]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/python/ray/_private/services.py#L1112-L1154
[RAY_FATE]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/python/ray/_private/utils.py#L834-L872
[RAY_START_PROCESS]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/python/ray/_private/services.py#L883-L910
[RAY_CONFIG]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/src/ray/common/ray_config_def.h#L1106-L1115
[RAY_WORKER_POOL]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/src/ray/raylet/worker_pool.cc#L713-L723
[RAY_NODE_CLEANUP]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/src/ray/raylet/node_manager.cc#L1611-L1673
[RAY_NODE_STOP]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/src/ray/raylet/node_manager.cc#L3044-L3064
[RAY_CHILD_TEST]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/python/ray/tests/test_kill_subprocesses.py#L181-L196
[RAY_NESTED_TEST]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/python/ray/tests/test_kill_subprocesses.py#L250-L298
[RAY_ESCAPE_TEST]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/python/ray/tests/test_kill_subprocesses.py#L223-L247
[RAY_KILL_WAIT]: https://github.com/ray-project/ray/blob/dd83c3a703a5422d0b47f142cb380c6330817608/python/ray/_private/node.py#L1828-L1951

[PEBBLE_README]: https://github.com/noxdafox/pebble/blob/b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c/README.rst#L1-L8
[PEBBLE_SETUP]: https://github.com/noxdafox/pebble/blob/b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c/setup.py#L24-L38
[PEBBLE_EXAMPLES]: https://github.com/noxdafox/pebble/blob/b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c/README.rst#L57-L102
[PEBBLE_POOL]: https://github.com/noxdafox/pebble/blob/b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c/pebble/pool/process.py#L48-L103
[PEBBLE_STOP]: https://github.com/noxdafox/pebble/blob/b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c/pebble/common/process.py#L55-L68
[PEBBLE_WORKERS]: https://github.com/noxdafox/pebble/blob/b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c/pebble/pool/process.py#L381-L480
[PEBBLE_SHUTDOWN]: https://github.com/noxdafox/pebble/blob/b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c/pebble/pool/process.py#L560-L567
[PEBBLE_JOIN_TEST]: https://github.com/noxdafox/pebble/blob/b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c/test/test_process_pool_spawn.py#L401-L407
[PEBBLE_EXPIRED_TEST]: https://github.com/noxdafox/pebble/blob/b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c/test/test_process_pool_spawn.py#L456-L464
[PEBBLE_CHILD_TESTS]: https://github.com/noxdafox/pebble/blob/b368bf5a265ca1bf9a758bc83ea8a60fbdd9862c/test/test_process_pool_spawn.py#L597-L612

[PRIOR_SELECTION]: durable-open-source-selection-20261014.md
[LOKY_REPORT]: ../../.agentloom/validation/dbos-loky-application-go-no-go-20261014-v1/REPORT.md
