# 库式本机持久执行：需求与选型复核

2026-10-14。只更新研究和文档，未修改产品源码、CLI、配置或依赖，未执行新故障探针、NewsAgent、模型或搜索服务请求，未提交或推送。三个研究 subagent 使用用户指定的 TraeX · GPT-5.5[1m]；主 agent 另核对关键固定源码和引用。

**已核对的候选接入方式中，尚未找到完整满足新故障合同的组合。建议保留 DBOS 的持久队列职责，独立验证第三方库的进程监护能力；通过前不迁移 NewsAgent，也不新增 AgentLoom supervisor。** 这不是“所有 Python 库都做不到”的结论，更不能用某一次 Worker 崩溃失败代替顶层宿主强杀验证。

后续按用户指定补查 OpenCode、OpenClaw，已发现 OpenClaw 受管理 Node Worker 的执行所有者 SIGKILL、孙进程清理和容量恢复测试；其独立 relay/anchor 比通用进程池更贴近需求。下一步优先核对这些能力能否通过公开组件独立复用，Ray 不再是默认首选。原下文保留本轮 Python 库研究的事实和未执行的验收计划，最新比较见 [OpenCode/OpenClaw 研究](opencode-openclaw-execution-comparison-20261014.md)。

## 已确认的目标

- macOS、Linux 默认 pip 安装后本机运行，默认本地持久化，预留远程存储。无需用户配置 launchd/systemd 或另行部署数据库、broker、服务。
- 允许第三方库自行启动本地 Worker、执行服务和独立监护进程。SDK 自动启动本地服务并不等于要求用户另行部署服务。
- 强杀对象是顶层执行 Python。库自行启动的独立监护进程不同时被强杀，可以继续存活，必须自动清理全部任务后代，完成后自行退出。
- 受管理范围包含 Pi/Node、MCP、工具进程及孙进程。任务不得主动逃离范围；库须实际覆盖正常业务创建的这些进程，不能只把“禁止逃离”写在文档里。
- 保留新闻筛选、校验、计分和失败恢复规则。初筛、精筛、校验分别共享 20/30/20 阶段额度，有限 `map()` 的上限仍只属于本次调用。
- 不自研队列、claim/lease/retry、orphan registry 或 supervisor，不依赖上游私有状态建立产品保证。

部署和故障边界已记录在 [ADR-0003](../adr/0003-library-owned-cross-platform-process-lifetime.md)。具体库尚未选定。

## 问题要落在哪一层

持久队列解决“还有什么工作、从哪里恢复”。进程监护解决“宿主突然死亡后，谁还能执行清理”。业务账本解决“结果是否已经提交、重复执行能否继续写”。三者需要配合，换队列库不能自动得到后代清理保证。

当前代码已经把 DBOS 与 Application 进程调用分开，但后者使用 `LaunchdJobs`，构造时拒绝非 macOS。新要求需要替换这条进程所有权接线；此前全日 NewsAgent 成功只证明已测 macOS 方案，不证明新部署目标。[当前 executor](../../src/execution/durable/executor.py)、[平台限制](../../src/execution/durable/os_job.py)、[既有验收](../reviews/durable-worker-dbos-news-acceptance-20261014.md)

NewsAgent 应继续保留 Planner、三阶段衔接、业务权限和完成回执。执行适配只把完整 Application 调用交给选定的库，队列与恢复由 DBOS 负责。三个阶段的额度互不借用；DBOS 当前底层执行线程池共用，不能把独立额度宣传成三个物理线程池。[当前接入规格](../specs/durable-worker-open-source-engine-integration-v2.md)

## 候选结论

| 候选 | 已有能力 | 对本轮目标的缺口 | 当前判断 |
| --- | --- | --- | --- |
| DBOS 3.2.0 | 本地 SQLite、公开持久队列、阶段额度、重试与恢复 | 没有提供跨平台、宿主强杀后仍存活的进程树监护接口 | 保留为持久层候选 |
| AnyIO / Trio | 直属子进程生命周期和正常取消收尾 | `finally` 与运行时内收尾不能在宿主 SIGKILL 后继续执行，没有独立 guardian | 可用于传输层，不承担强杀清理 |
| loky 3.7.0 / Pebble 5.2.3 | 成熟进程池、Worker 隔离与停止 | 管理线程随宿主死亡；未建立独立监护进程对全部任务后代的清理合同 | 不能直接满足本轮目标 |
| Dask Distributed 2026.8.0 | Nanny 监视和重启 Worker | 普通 LocalCluster 的 Nanny 在宿主中；Worker 父死亡检测不等于递归清理 Pi/MCP/工具后代 | 不是已通过的方案 |
| Ray 2.59.0 | SDK 可启动本地运行环境；POSIX 默认隔离并清理 Worker 进程组 | Worker 死亡与顶层宿主死亡走不同路径，后者尚无完整退出保证；容量释放也不能直接作为物理退出回执 | 最值得定向验证，尚不能选用 |
| Prefect 3.8.8 | 本地 SQLite 控制面、公开流程与取消入口 | 已测公开 ProcessWorker 路径遗留后代；内部进程组代码不等于公开 guardian 合同 | 不因有取消 API 就迁移 |
| Celery 5.6.3 / Billiard 4.2.4 | 成熟任务执行与 Worker 管理，Linux 父死亡信号 | 默认需要 broker；Linux 特性未建立 macOS 及全部孙进程清理保证 | 不符合当前组合要求 |
| processkit-py 1.5.0 文档及固定源码对照 | Python 进程容器接口，正常退出时管理进程树 | 官方平台说明直接指出：macOS 宿主强杀后无自动清理，Linux 父死亡选项只覆盖直属子进程 | 无法补齐本轮关键缺口 |

进程库可以与 DBOS 组合，不要求每个进程库自身都实现 SQLite 队列。Ray 的体积和内部服务属于接入成本，用户没有给体积上限，不能仅据此排除它。[进程库详细研究](python-library-process-ownership-dask-ray-pebble-20261014.md)、[DBOS 与 Runtime 接入研究](python-library-dbos-runtime-integration-20261014.md)、[完整引擎研究](python-library-crash-contract-alternatives-20261014.md)

## Ray 为什么还不能直接采用

Ray 2.59.0 的官方文档明确提供 POSIX 每 Worker 进程组清理，正常继承组的孙进程在信号范围内；主动 `setsid()` 会逃离，该行为已被本轮合同排除。这个能力覆盖 macOS/Linux，是比泛泛讨论线程池更接近需求的依据。[官方生命周期文档](https://docs.ray.io/en/latest/ray-core/user-spawn-processes.html)、[固定发布配置](https://github.com/ray-project/ray/blob/ray-2.59.0/src/ray/common/ray_config_def.h)

但固定源码还有两个不同的宿主死亡路径：

- Linux 普通本地启动选择 kernel fate sharing，raylet 等进程安装父死亡 SIGKILL。强杀顶层宿主可能同时消灭负责清理 Worker 进程组的 raylet，不能拿 raylet 存活时的 Worker 崩溃保证外推。[Node](https://github.com/ray-project/ray/blob/ray-2.59.0/python/ray/_private/node.py)、[启动](https://github.com/ray-project/ray/blob/ray-2.59.0/python/ray/_private/services.py)、[父死亡信号](https://github.com/ray-project/ray/blob/ray-2.59.0/python/ray/_private/utils.py)
- macOS 使用库自带 reaper，控制管道 EOF 后向自身组发 TERM，约一秒后向该组发 KILL。隔离后的 Worker 属于其他组，清理仍依赖 raylet 在退出前完成工作；源码未建立强杀场景下必然完成全部收尾的顺序。[reaper](https://github.com/ray-project/ray/blob/ray-2.59.0/python/ray/_private/ray_process_reaper.py)

`NodeManager::Stop()` 尝试清理已登记 Worker 组，但在发 KILL 后没有等待组为空。Worker 异常断开路径安排约 200ms 后升级 KILL，随后先释放执行资源并调度其他工作。**这是固定源码观察，说明逻辑容量与物理退出不能画等号；本轮未执行实验，不能写成已复现的 AgentLoom 故障。** [清理与资源释放源码](https://github.com/ray-project/ray/blob/ray-2.59.0/src/ray/raylet/node_manager.cc)

## 开源 Agent 是否也遇到这个问题

Hermes Agent 有独立的 MCP death supervisor：宿主持有控制管道写端，宿主死亡后监护进程收到 EOF，清理已登记 MCP 进程组。这证明“宿主死、独立监护继续收尾”的结构在开源 Agent 中有实际应用；它没有证明存在可直接用于完整 Application 的稳定第三方库接口。[固定源码](https://github.com/NousResearch/hermes-agent/blob/d97007c32d6340f7ba79ac1dfb5773cc00846448/tools/mcp_death_supervisor.py)

该实现只登记 MCP 组，明确记录 PGID 重用风险，最后发 KILL 后没有继续等组为空。复制这段代码还需要 AgentLoom 自己扩展登记、退出确认和 Runtime 覆盖范围，会再次承担 supervisor 职责，因此本轮不推荐复制或接私有 helper。

processkit 的官方平台表也明确区分正常作用域退出与宿主硬杀，不能只看 README 中“no-orphan”的概括。核对文件固定于 commit `e56827e7c1d0bcfb445cda9595a4cc2f42a7f5a5`，PyPI 当前版本为 1.5.0；没有安装或运行该版本，不把源码对照当实测。[平台合同](https://github.com/ZelAnton/processkit-py/blob/e56827e7c1d0bcfb445cda9595a4cc2f42a7f5a5/docs/platforms.md)、[发布版本](https://pypi.org/project/processkit-py/1.5.0/)

Pydantic AI v2.56.0 的 DBOS 接入则提供另一个可参考的边界：正常 Agent 在 workflow 中将模型请求和 MCP 通信交给持久 step，业务工具仍须明确处理幂等与重放。它不补充宿主强杀后的本机进程监护；仅作为接入参考，未修改或安装 AgentLoom 依赖。[固定发布文档](https://github.com/pydantic/pydantic-ai/blob/v2.56.0/docs/durable_execution/dbos.md)

## 下一步的最小验证范围

建议下一轮先做库的隔离故障验证，通过前不改产品。此处只写验收计划，尚未实施。

| 场景 | 必须观察到的结果 |
| --- | --- |
| 正常结束或资源关闭回调抛错 | 正确保存原结果或异常；完整任务范围退出后才返回完成 |
| 资源关闭阻塞后取消 | 不能因 Future/DBOS 已终态就补位；库负责终止并完成后代收尾 |
| Application Worker `os._exit()` | 库存活的监护组件自动清理受管理后代；恢复没有旧、新资源重叠 |
| 顶层执行 Python SIGKILL | 不靠宿主 finally，不等重启；库监护自动清理 Pi/Node、MCP、工具和孙进程，然后自行退出 |
| 清理后重启持久执行 | 业务授权与完成回执正确处理重放；原阶段共享额度不因重启叠加 |

入口保持真实 Application / BoundWorker 和真实 Pi/MCP 本机接线，探针使用可控输入而不请求远端模型或 AnySearch。只能使用库公开 API；不靠新增 orphan registry、私有进程表或手写 watcher 使测试通过。macOS 与 Linux 分别验证；单平台通过不能算跨平台完成。

原 loky 探针杀的是 Worker，且原生子进程另起 session；原 Prefect 探针测的是一条公开 ProcessWorker 接线。这些负面结果保留，但均不是本轮新强杀场景的完整验证。[loky 原始结果](../../.agentloom/validation/dbos-loky-application-go-no-go-20261014-v1/REPORT.md)、[Prefect 原始结果](../../.agentloom/validation/prefect-application-go-no-go-20261014-v1/REPORT.md)

如果公开 API 验证仍失败，下一步应定位成熟库缺的具体能力并评估上游补齐或继续选型，不能把失败转成 AgentLoom 新增一套监护内核。历史 SQLite 522 的现场 actor 仍未定案，新后端和一次完整日期成功也不能据此宣称历史根因已经修复。[522 证据边界](sqlite-522-trace-primary-sources.md)

## 资料核对记录

关键官方源码下载到 `.agentloom/research/python-library-reassessment-20261014/`，按发布 tag 或 commit 保留 URL 与 metadata。主 agent 已纠正子报告的 Pydantic AI v1.0.0 失效引用、Ray 2.9.3 过旧选型依据，以及忽略 Celery Linux 父死亡特性的概括；未把未经建立的能力写成保证。

原任务正文传递失败时生成的旧需求推断报告已隔离，未作为这轮选型证据。有效 subagent 已先回报明文需求，研究输出再经主 agent 核对。方案仍处于选型与验收计划阶段。
