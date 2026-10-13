# 单机任务的进程资源由谁收尾

2026-10-14。研究对象是完整 Application invocation 的本机进程资源，持久队列继续交给成熟执行引擎。已核对官方文档、固定源码与上游测试，父任务另完成 loky／launchd 的无模型比较。没有修改产品依赖、请求模型或重跑旧 SQLite 失败矩阵。

**建议保留 DBOS 队列候选，继续验证由成熟 OS 进程所有者管理完整 Application 的路线：macOS 使用 launchd 的一次性 job 与同组清理，Linux 使用 systemd/cgroup。** Pi、MCP 与工具进程必须加入任务的受管理范围。loky 3.7.0 的公开单任务 executor 已有 2／3 项真实 Application 故障观测通过，但 Worker 突然退出后遗留子进程。单靠其 `shutdown(wait=True, kill_workers=True)`，不能写成“完整资源树退出已解决”。[loky 终止源码][L1]、[上游后代退出测试][L2]、[本项目观测](../../.agentloom/validation/dbos-loky-application-go-no-go-20261014-v1/worker-crash/observations.json)

当前 macOS 的进程组可以覆盖受管理且留在组内的子进程。若合同要求任意子进程脱离组、祖先进程被 SIGKILL 后仍能自动准确发现并清理，Linux cgroup 提供了更强的内核边界；下列 Python 库和 POSIX 进程组都没有同等合同。默认本机 macOS 仍需明确受管理进程范围，清理无法确认时阻止恢复新 Runtime，而不能把超时当成退出。[POSIX setsid][P2]、[psutil 后代关系说明][PS1]、[cgroup v2][K1]

## 当前失败揭示的需求

已经保存的 DBOS 与 Prefect 实测否决了两条直接包装路径：Application 的资源关闭回调抛错后，已登记的真实子进程仍存活，新 Runtime 却已开始。这证明需要一个不依赖 Agent 关闭回调成功的进程所有者；它没有证明这两个引擎的所有接入方式都失败。[DBOS 实测](../../.agentloom/validation/dbos-application-go-no-go-20261014-v1/REPORT.md)、[Prefect 实测](../../.agentloom/validation/prefect-application-go-no-go-20261014-v1/REPORT.md)

本次研究将“退出”分开核对：

| 已观察的事件 | 能证明什么 | 仍不能证明什么 |
| --- | --- | --- |
| 引擎状态已 Failed／Cancelled，Future 已完成 | 引擎的逻辑状态或调用已结束 | 原模型客户端、Pi、MCP 或本机后代退出 |
| 向 PID／进程组发出 SIGKILL | 信号发送成功 | 全部目标已经退出；另一个组内的后代也收到信号 |
| `Popen.wait()`／Worker `join()` 返回 | 直属进程已经退出并可由父进程回收 | 孙进程及已 reparent 的进程退出 |
| `psutil.wait_procs()` 返回 `alive=[]` | 给定集合中的进程已经退出 | 给定集合未包含的后代退出 |
| 受隔离的 cgroup `populated=0` | 该 cgroup 及子 cgroup 没有活进程 | 隔离前逃出的进程、远端服务端请求结束 |

这些边界由 Python 的 `multiprocessing` 文档、psutil 的公开接口及 cgroup 文档明确给出。Python 文档直接说明 `Process.terminate()` 不会终止后代，后代将成为孤儿；被终止的直属进程也不会执行 `finally`。[Python 文档][P3]、[psutil wait_procs][PS2]、[cgroup populated][K1]

## AnyIO 和 Trio 解决直属进程的结构化生命周期

AgentLoom 的 lockfile 固定 AnyIO 4.11.0。该版本的 asyncio `Process.aclose()` 会屏蔽取消来关闭流，在等待受取消时调用 `kill()`，随后再次等待直属进程；`kill()`／`terminate()` 委托给 asyncio Process，并未递归清理后代。`start_new_session=True` 只创建 session，不自动改变 `kill()` 的目标。[AnyIO 4.11.0 源码][A1]、[Python subprocess 参数][P1]

AnyIO 当前固定源码的测试 `test_process_aexit_cancellation_doesnt_orphan_process` 验证了取消后的直属进程返回码。另一个测试 `test_wait_returns_on_process_exit_with_open_stdout` 特意创建仍持有 stdout 的孙进程，要求 `wait()` 在直属进程退出后返回，再单独解锁并等待孙进程结束。这正好划出了它的等待范围。这项新测试不属于已安装 4.11.0，不能当成本项目已获得的修复。[AnyIO 测试][A2]；原始取消缺陷见 [官方 issue #669][A3]。

Trio `run_process()` 在取消时，POSIX 默认先 TERM，5 秒后 KILL，并始终等待直属进程退出后再抛取消。它公开允许自定义 `deliver_cancel`，但文档和源码同时说明：**直属进程退出时，`deliver_cancel` 自身会被取消**。因此，把 `killpg` 后等待全部后代的逻辑放进该钩子，并不能凭 API 合同证明完整组退出。正常结束也不会走取消钩子。[Trio 参数及取消源码][T1]、[Trio 钩子测试][T2]

AnyIO 适合复用为异步进程句柄和取消收尾基础；Trio 适合已有 Trio Runtime。为了这一个缺口引入另一个事件循环，没有显示出额外收益。两者均不应被包装成通用进程树容器。这是对本项目接入成本的判断，未做性能实测。

## loky 3.7.0 更贴近完整 Application 隔离，但终止与等待仍有区别

loky 是成熟的 `ProcessPoolExecutor` 实现，3.7.0 metadata 明确支持 Python 3.14，官方 CI 配置包括 macOS Python 3.14。运行依赖只有 cloudpickle；psutil 是可选项。AgentLoom 已有 psutil，验证时应明确使用该路径。metadata 和 CI 声明不是本机完整故障验收。[3.7.0 metadata][L3]、[官方 CI][L4]、[PyPI 发布信息][L5]

公开 `ProcessPoolExecutor.shutdown(wait=True, kill_workers=True)` 会等待 executor 管理线程。管理线程调用 `kill_process_tree()`，psutil 路径执行以下源码：

```python
descendants = psutil.Process(process.pid).children(recursive=True)
for descendant in descendants[::-1]:
    descendant.kill()
psutil.Process(process.pid).kill()
process.join()
```

源码会忽略已消失的进程，根进程已经不存在时直接返回；这里没有 `descendant.wait()` 或 `wait_procs()`。上游 `test_kill_process_tree` 在调用结束后另加 `psutil.wait_procs(children, timeout=5)`，检查 `alive=[]`。这说明上游验证也允许子进程在终止函数返回后才完成退出。[终止源码][L1]、[executor shutdown][L6]、[退出测试][L2]

对当前候选的影响如下。表中区分源码推断和下述实际探针，不能把两项通过外推为全部故障保证。

| 情况 | 可复用能力 | 尚缺的保证 |
| --- | --- | --- |
| Application 正常返回或 Python 异常，包括资源关闭回调抛错 | loky 捕获调用的 `BaseException` 并发送结果；Worker 通常仍活着，可在父进程取得结果后终止其后代 | 回调内部先 reparent 的孙进程可能已从递归关系中消失；后代 kill 后仍需确认退出 |
| Application 挂住，父进程仍正常 | `shutdown(kill_workers=True)` 可强制结束 Worker 与当时可发现的后代 | 递归快照和 kill 之间仍可能 fork；没有内核范围的原子隔离 |
| Worker `os._exit()`、崩溃或被 SIGKILL | broken executor 会结束剩余 Worker，报告 `TerminatedWorkerError` 等 | 死亡根的后代已 reparent，`children()` 无法重新证明归属 |
| executor 宿主被 SIGKILL 后重启 | 本次活宿主中的线程和 Python finally 均不能再收尾 | 新宿主仅调用新 executor 不会准确回收旧孤儿；必须由 OS 所有者或事先取得的资源身份处理 |

其中第一行的“Worker 通常仍活着”是源码执行顺序的推断。loky 仍有 idle timeout、Worker 内存回收与进程异常等生命周期；本项目若使用一个 executor 执行一个 invocation，必须实际核对取得结果到外部清理这一段，不能依赖私有 `_processes` 字典或默认 reusable executor 来建立产品合同。[结果处理源码][L6]

**最小验证应使用非复用 executor、单个任务和完整 Application 创建／关闭。** 先保存原成功或异常，再执行上游 shutdown；只有任务所有者已经掌握的资源身份确实退出，外层 invocation 才返回成功或原异常。补 `wait_procs()` 属于资源收尾适配，不另起 claim、租约、容量或重试循环。若为补 root death 而开始重新实现持久 orphan registry、定时领取和恢复调度，就应暂停这条路线，回到 OS 隔离边界评估。[psutil wait API][PS2]

本项目随后已完成官方 DBOS 3.2.0、loky 3.7.0 与真实 Application 的无模型探针，原严格断言保留：

| 原始用例 | 已观测结果 |
| --- | --- |
| 关闭回调阻塞时取消 | 通过。旧任务 Cancelled，保持关闭期间没有补位；释放后旧子进程退出，新任务成功 |
| 关闭回调抛错 | 通过。旧任务 Error，新任务 Success；新 Runtime 开始时旧子进程已退出 |
| Runtime 创建原生资源后 `os._exit(17)` | 失败。旧任务 Error，新任务 Success；新 Runtime 开始时旧子进程仍存活 |

这是 2／3 项结果，不能判 loky 全部不可用，也不能据此放行完整合同。失败后测试 controller 只清理了自己的子进程，最终记录三项 PID 均已消失；该收尾不能计入框架通过。[held 观测](../../.agentloom/validation/dbos-loky-application-go-no-go-20261014-v1/held/observations.json)、[failure 观测](../../.agentloom/validation/dbos-loky-application-go-no-go-20261014-v1/failure/observations.json)、[crash 观测](../../.agentloom/validation/dbos-loky-application-go-no-go-20261014-v1/worker-crash/observations.json)、[最终进程核对](../../.agentloom/validation/dbos-loky-application-go-no-go-20261014-v1/final-native-process-verification.json)

## Prefect 的 process utility 有可借鉴的分层，但不宜导入私有执行器

Prefect 3.8.8 的普通 `processutils` 管理一个 AnyIO Process，在关闭时 shield 收尾。其内部 process manager 另外定义单 PID 和 POSIX process group 两种 termination scope：后者 `is_alive()` 用 `killpg(pid, 0)`，TERM／KILL 也发到组。EngineCommandStarter 的 `isolate_process_group` 默认 false，true 才启动新 session 并安装组 scope。[utility 源码][F1]、[scope 源码][F2]、[starter 源码][F3]

这些类位于 `prefect.runner._*` 私有模块。Process worker 的公开配置与内部默认路径不能混为一谈；不能仅因源码存在 group scope，宣称本项目之前测试的公开 Process worker 已隔离所有后代。`ProcessManager.kill(force=True)` 发送 KILL 后直接返回，普通路径在升级 KILL 后同样没有再等组消失。这些内部行为可以用来理解当前失败，不应直接拿来充当框架公开依赖。[kill 方法][F2]、[公开 worker 源码][F4]

当前 dependency resolver 的 OpenTelemetry 冲突和实际 child 遗留证据均应继续保留。换成 Prefect 或拷贝它的 process utility，没有自动弥补进程树所有权。[既有实测](../../.agentloom/validation/prefect-application-go-no-go-20261014-v1/REPORT.md)

## Supervisor、Huey 和 launchd 管理的范围

Supervisor 文档提供 `stopasgroup=true` 和 `killasgroup=true`。前者将停止信号发到组，并隐含后者；后者在 `stopwaitsecs` 超时后向组发送 KILL。源码以直属进程 PID 的负值发送信号，`finish()` 收到该进程的 SIGCHLD 即置为 STOPPED。文档的等待条件也是该程序的 SIGCHLD，并没有承诺验证完整组为空。[配置合同][S1]、[发送与状态源码][S2]、[官方测试][S3]

因此，Supervisor 可以复用为持续本地 executor 的宿主，能够独立于提交 CLI 管理宿主。但它不能仅靠这两个选项保证正常结束后残留的后代、逃到另一个组的进程或监督器自身被 SIGKILL 后自动收尾。开启 `autorestart` 还可能在旧资源尚未确认退出时重启执行器；持久任务重试应继续由选定执行引擎负责。这是由其信号目标与状态转换推导的限制，未在本轮启动 Supervisor。

Huey process consumer 的 graceful shutdown join 各 Worker；强制退出向各 Worker PID 发停止信号后短暂 join。源码没有为每次任务安装完整 process group/cgroup 边界。它也不会弥补此前已记录的在途恢复合同，故不适合作为本次队列或进程所有权替代。[Huey consumer 源码][H1]、[既有队列研究](local-task-engines-prefect-huey-reassessment-20261014.md)

macOS 随系统提供的 `launchd.plist(5)` 明确说明：job 死亡后，launchd 默认 kill 与 job 同 process group ID 的剩余进程；`AbandonProcessGroup=true` 会关闭这个行为。原文是：

> When a job dies, launchd kills any remaining processes with the same process group ID as the job. Setting this key to true disables that behavior.

Apple 开源 man 源码保留同样合同。它能处理直属 job 死亡这一事件，优于只在活 Python 父进程 finally 中收尾，但范围依旧是相同组，不能覆盖 `setsid()` 后的子进程。**默认保留清理的合同由原文直接给出；实际清理结束是否先于新任务开始，仍需本项目验证。**[Apple man 源码][M1]、[Apple job 编写指南][M2]

一次性 invocation 应显式 `KeepAlive=false`，不要让 launchd 重试业务。系统 `launchctl(1)` 手册特别说明 legacy `submit` 在失败时会 keep alive；应验证 `bootstrap` 的一次性 plist 路径。系统当前手册也明确说明新版命令的输出不构成 API，不适合程序解析，因此不能只看到 `print` 的某个状态字段或 bootout 返回 0 就宣称整个组已退出。[Apple submit 原始源码][M3]、[本机系统 launchctl 手册](/usr/share/man/man1/launchctl.1)

launchd 与 Supervisor 可以作为宿主所有者参照；它们不应再配置一套业务任务 retry／恢复。随后父任务用系统 launchctl 临时注册独立测试 job，显式 `KeepAlive=false`、`AbandonProcessGroup=false`，完毕仅 bootout 自己的随机 label。主进程 `os._exit(17)` 后，同组 native 已退出；独立 session 的负例仍存活并让原断言失败。[组范围实测](../../.agentloom/validation/launchd-process-group-go-no-go-20261014-v1/REPORT.md)

再接官方 DBOS 和真实 Application，让受控 native 加入任务组后，关闭阻塞时取消、关闭失败、根进程崩溃三项均通过，step 等具体根身份和 OS 组消失后才返回。仅改变测试资源的组归属，没有改原严格退出断言。此处没有运行正式 Pi／MCP，没有验证 executor 宿主死亡，也未修改产品；当前框架内部独立 session 的接线仍需处理。[真实 Application 实测](../../.agentloom/validation/dbos-launchd-application-go-no-go-20261014-v1/REPORT.md)

## Linux cgroup 提供跨祖先退出的资源边界

cgroup v2 规定 fork 的子进程出生在父进程所在的 cgroup；祖先 PID 消失和 `setsid()` 不会自行改变 cgroup。`cgroup.kill` 向整个 cgroup 树发送 SIGKILL，并由内核处理 concurrent fork 和迁移竞争；`cgroup.events` 的 `populated=0` 表示本组及子组没有活进程。因此可以在根进程退出后继续判断资源范围，而不依赖重新构造父子关系。[内核文档][K1]

systemd 的 `KillMode=control-group` 默认终止 unit 的全部进程，允许 TERM 后升级 KILL；`ExitType=cgroup` 可以让 service 在其 cgroup 仍有进程时保持运行状态。可复用 transient service，而不是自研 process tree supervisor。这里依旧要检查实际组为空，不能只认发送 KILL 或 CLI 返回。[systemd.kill][D1]、[systemd.service][D2]、[systemd-run][D3]

该方案限定 Linux 和可用的 systemd/cgroup 权限，不能作为 macOS 默认实现。任务也不能获得将进程迁移出所属边界的权限；预先迁移出范围的进程不受后来一次 `cgroup.kill` 覆盖。内核 kill 没有承诺固定耗时，无法退出时仍应保持未完成并阻止补位。以上属于官方合同及权限边界，不是本机 Linux 实测。

## 当前 AgentLoom 接线必须先核对的细节

当前 [process.py](../../src/execution/process.py) 的 `run_captured_process()` 为每次工具调用设置 `start_new_session=True`。`run_process_to_fds()` 也采用独立 session。**只把完整 Application 放入一个新 session，再 kill 它的 PGID，会漏掉这些框架本来就创建的独立工具组。** 这是当前源码事实和 POSIX 组语义的直接推论。[POSIX setsid][P2]

同一文件还有三处不能直接当作新实现的强保证：父进程已退出时，关闭回调跳过 `terminate_process_tree()`；`psutil.wait_procs()` 的 `alive` 返回值未检查；每次工具又生成新的 run token，不能仅靠一个 invocation token 假设所有资源都可找到。无需为本轮研究修补它们，但后续完整 Application 验证必须包括这些路径。[当前源码](../../src/execution/process.py)

已安装 MCP Python SDK 1.26.0 的 stdio transport 也在 POSIX 启动处固定 `start_new_session=True`，当前 AgentLoom 通过 mcpadapt 0.1.20 调用此 transport。不是所有独立组都由本项目的一处 spawn 开关控制。该上游版本的 stdio 接线不能仅凭修改 Pi transport 就纳入 Application 的同组清理；需要核对公开可替换 transport，或限制首版支持的接线，不能 monkeypatch 私有 SDK 来掩盖缺口。[MCP 1.26.0 官方源码](https://github.com/modelcontextprotocol/python-sdk/blob/v1.26.0/src/mcp/client/stdio/__init__.py)、[当前客户端](../../src/integrations/mcp/client.py)

同组改造还要保留单个工具的超时行为。当前 shell 清理按子进程 PGID 发送信号；子进程加入 Application 组后，不能仍无条件 kill 整个共享组，或把未发到正确目标的信号当成成功。正式接入必须在实际 Runtime 上验证工具超时、资源关闭和任务作用域终止各自的效果，不能只批量将 `start_new_session=True` 改成 `False`。[当前工具清理](../../src/runtimes/smolagents/tools/shell/tree_kill.py)

可复用的最小职责划分是：成熟执行引擎负责队列／恢复；单次 Application 进程负责 Agent、Runtime、可信依赖与业务调用；独立 OS 所有者负责所管理的本机进程退出；业务库负责完成回执和原子写授权。进程隔离不会把远端模型请求的取消变成服务端已经停止，HTTP 在途必须继续单独记录。

单本地 executor 可用 OS 文件锁防止同时启动。在 `DBOS.launch()` 之前核对旧受管理 job／组，确认退出后才能恢复队列；身份不清或资源仍活着时拒绝启动。文件锁只证明宿主互斥，不能替代旧资源退出。该接线不重新解释任务状态，也不维护另一套领取、租约和重试。macOS 的持久任务 job 身份与恢复前核验仍是待验证适配；不能因为使用 launchd 就宣称已经交付。

下一轮无模型验证应保持已有严格断言，覆盖正常返回、关闭回调抛错、阻塞后取消、Worker `os._exit()`／SIGKILL、中间父进程退出后的 reparent，以及宿主被 kill 后重启。记录 PID 加创建时间、组或 OS 边界身份和实际消失时间；每一个未知或仍活着的资源都应让补位被阻止。完整 News、Pi／MCP、业务原子权限及全日验收仍是之后的交付要求，不能由上述源码研究或单个小探针替代。[当前规格](../specs/durable-worker-open-source-engine-integration-v2.md)

## 一手来源及取证范围

可变分支经 `git ls-remote` 固定 SHA，并重新下载相关源码确认一致。loky 使用发布 tag 3.7.0 的 peeled commit；Prefect 使用 3.8.8 tag。当前 OS 自带 `launchd.plist(5)` 也已读取，其进程组合同与 Apple 开源版本一致。部分 ReadTheDocs 返回 403，改读官方仓库的同源文档或函数 docstring；GitHub API 返回限流时使用公开 git refs，未从二手文章补事实。本轮未执行任何上游测试，因此文中测试是已读取的上游测试合同。

[A1]: https://github.com/agronholm/anyio/blob/4.11.0/src/anyio/_backends/_asyncio.py#L1066-L1097
[A2]: https://github.com/agronholm/anyio/blob/e414ce85d4adaa2e8248f20a108c2e51332cb86d/tests/test_subprocesses.py#L243-L258
[A3]: https://github.com/agronholm/anyio/issues/669
[T1]: https://github.com/python-trio/trio/blob/4630d02872c0aa5778ce0afd702c5d532a2ccf60/src/trio/_subprocess.py#L606-L635
[T2]: https://github.com/python-trio/trio/blob/4630d02872c0aa5778ce0afd702c5d532a2ccf60/src/trio/_tests/test_subprocess.py#L626-L666
[L1]: https://github.com/joblib/loky/blob/935c2922ab80f0f269cb18de0d1c5c35374ae1e0/loky/backend/utils.py#L16-L51
[L2]: https://github.com/joblib/loky/blob/935c2922ab80f0f269cb18de0d1c5c35374ae1e0/tests/test_loky_backend.py#L718-L738
[L3]: https://github.com/joblib/loky/blob/935c2922ab80f0f269cb18de0d1c5c35374ae1e0/pyproject.toml
[L4]: https://github.com/joblib/loky/blob/935c2922ab80f0f269cb18de0d1c5c35374ae1e0/.github/workflows/test.yml
[L5]: https://pypi.org/project/loky/3.7.0/
[L6]: https://github.com/joblib/loky/blob/935c2922ab80f0f269cb18de0d1c5c35374ae1e0/loky/process_executor.py
[PS1]: https://github.com/giampaolo/psutil/blob/482856954e42496499730ded1b1b264039cf6afc/psutil/__init__.py#L1138-L1164
[PS2]: https://github.com/giampaolo/psutil/blob/482856954e42496499730ded1b1b264039cf6afc/psutil/__init__.py#L1888-L1928
[F1]: https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/utilities/processutils/__init__.py
[F2]: https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/runner/_process_manager.py
[F3]: https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/runner/_starter_engine.py
[F4]: https://github.com/PrefectHQ/prefect/blob/b73b09dc3699489c799dbb40ac5ed2da459e391c/src/prefect/workers/process.py
[S1]: https://supervisord.org/configuration.html#program-x-section-settings
[S2]: https://github.com/Supervisor/supervisor/blob/abc60468ea4b78c446cf3a194f6fccb83d90f670/supervisor/process.py
[S3]: https://github.com/Supervisor/supervisor/blob/abc60468ea4b78c446cf3a194f6fccb83d90f670/supervisor/tests/test_process.py#L945-L978
[H1]: https://github.com/coleifer/huey/blob/7e937f9d741f4fbc6fa8a18e589826f5f61554aa/huey/consumer.py#L484-L531
[M1]: https://github.com/apple-oss-distributions/launchd/blob/d448a1c8f70a61202f8705f94337f686b87c30c4/man/launchd.plist.5
[M2]: https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html
[M3]: https://github.com/apple-oss-distributions/launchd/blob/d448a1c8f70a61202f8705f94337f686b87c30c4/man/launchctl.1
[K1]: https://docs.kernel.org/admin-guide/cgroup-v2.html
[D1]: https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html
[D2]: https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html
[D3]: https://www.freedesktop.org/software/systemd/man/latest/systemd-run.html
[P1]: https://docs.python.org/3/library/subprocess.html#subprocess.Popen
[P2]: https://pubs.opengroup.org/onlinepubs/9799919799/functions/setsid.html
[P3]: https://docs.python.org/3/library/multiprocessing.html#multiprocessing.Process.terminate
