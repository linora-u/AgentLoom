# 开源持久执行接入方案

2026-10-14。状态：worktree 已提供 DBOS 产品入口和 News 接线，有本机局部测试通过证据；2025-09-01 首轮真实全日退出 1／RED。可信 factory 绑定修复的一项真实 Pi 测试转为 GREEN，第二轮全日运行中，验收未完成。当前采用 DBOS 3.2.0 持久队列与 macOS launchd 管理任务进程，尚未合入 main。[产品接入状态与一手来源](../research/dbos-product-integration-status-20261014.md)

本方案按用户最新要求重新选择实现路线：采用成熟开源引擎，减少执行与业务存储的耦合。旧自研内核候选停止扩展，原始失败和全日结果继续保留。旧规格的业务目标、输出预算合同及真实 CLI 验收仍有效；后端选择与共享事务实现方式由本方案重新评估。

九个执行引擎及 Agent 集成方式的取舍集中见 [开源选型结论](../research/durable-open-source-selection-20261014.md)，每项链接到官方文档、固定源码和版本证据。

## 当前 DBOS 官方接入

产品采用 DBOS 3.2.0 的单本地 executor、多 CLI producer 模式。生产者通过公开 `DBOSClient.register_queue()` 和 `enqueue_in_transaction()` 声明队列、原子提交有限批次；消费者通过 `DBOS.listen_queues()`、`DBOS.launch()` 执行。按具体 Worker 绑定三个队列，分别配置初筛 20、精筛 30、校验 20。多个日期提交到相同阶段队列，不各自启动一组 executor。有限 `map()` 仍按单次调用管理并发。[固定上游 API 与产品对应](../research/dbos-product-integration-status-20261014.md#公开-api-对应当前产品)

三个阶段分别有队列额度，DBOS 底层共用一个执行线程池；不能宣称它提供三个独立线程池。首轮无模型实测中，两个独立 CLI 共提交 152 个任务，三个阶段同时达到并保持 20／30／20。该结果仅覆盖所测同步 workflow，不覆盖真实模型、Pi／MCP 或所有故障路径。[实测记录](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-official-single-executor-go-no-go-20261014-v1/REPORT.md)

单 executor 是本地部署约束，SDK 的进程内 singleton 不能证明跨 CLI 单例。重复 CLI 只创建 Client；当前消费者先取得 namespace 文件锁、核对 PID 与创建时间、校验已有队列配置、恢复旧 OS job，再允许 `DBOS.launch()`。真实 SIGKILL／恢复和未退出旧 scope 拒绝 resume 已进入产品测试；独立 controller 空载实测确认两个 producer 取得同一正式 executor 身份。启动中的未知所有权仍保守阻断，不把正向启动外推为所有崩溃窗口已通过。[当前验证范围](../research/dbos-product-integration-status-20261014.md#局部验收与独立复核)

DBOS 原生负责持久入队和 workflow 恢复，AgentLoom 只适配任务描述、Application invocation 和执行回执。接入不调用 DBOS 私有 system tables。默认 system database 与 News 业务 SQLite 分文件，移除执行存储必须参与新闻原生事务的要求。

这条路线利用现有公开 SDK 和已经验证过的真实 Runtime 接线，默认无需新增手动部署的数据库服务。DBOS Client 直接连接 system database；队列容量由 SDK 执行计数管理，尚不能据此承诺物理线程或模型请求在全部取消路径下严格不超限。[DBOS 复评](../research/dbos-comparison-contract-reassessment-20261014.md)

队列配置通过 `on_conflict="never_update"` 保留已有声明。对已有队列，先通过公开 Client 读取实际配置，统一检查身份和容量，再 launch；不能在恢复已开始后才发现容量声明冲突。新队列使用官方注册入口，并核对保存的配置。不同容量不得静默覆盖。SDK 提供持久队列配置；冲突拒绝由框架公开入口落实，不宣传为 DBOS 默认不可变声明。

远程替换优先使用引擎公开的连接配置或执行接口。DBOS 可从本地 SQLite 切到 PostgreSQL；改用其他引擎则接其公开 Client／Worker。不能把上游私有数据库表包装成原 `DurableStore`，再保留框架自己的领取循环。接口可替换不等于本轮已经验证多机器执行。

## Prefect 用于验证进程隔离

Prefect 3.8.8 是第二候选。使用一个本地 API server 和官方 Process worker，业务进程通过 Client 使用执行状态，避免直接持有执行库连接。若它能以更少适配满足相同故障合同，可替换首选路线。

完整依赖解析已确认当前 AgentLoom 与 Prefect 3.8.8 不能直接安装在同一环境：框架固定的 OpenTelemetry SDK 1.26 要求 API 1.26，而 Prefect 要求 API 至少 1.27。已在独立环境启动其官方 SQLite server，health 返回 200，并执行官方 Process worker 的真实 Application 验证。不能通过默改框架追踪依赖来宣称问题已解决。

独立 flow run 进程不等于每次 retry 都换进程；SDK 的逻辑取消和 lease 回收也不证明 Pi 或 MCP 已退出。必须以所选运行模式的实测结果判断，不能只因存在 ProcessPool 就宣布解决生命周期问题。[本地任务引擎复评](../research/local-task-engines-prefect-huey-reassessment-20261014.md)

官方 Process worker 的真实 Application 接线现也出现严格退出 RED：old Flow 已失败，新 Runtime 开始时，旧 Application 已登记的子进程仍活着。首轮导入问题的 0 次 invocation 与后续实际执行分别保留。这个结果说明还需明确完整 Application 资源树的所有权，不能继续把换队列或换进程 Worker 当成清理保证。[Prefect 实测](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/prefect-application-go-no-go-20261014-v1/REPORT.md)

Hatchet、Temporal 保留在比较记录中。Restate 的部署方式有参考价值，但 Server 的 BSL 许可证及实验阶段并发接口不作为本次默认开源路线。Huey 的在途任务恢复缺口、LangGraph checkpoint 与队列容量的差别，已在研究中记录。[其他引擎](../research/durable-open-source-engine-reassessment-20261014.md)、[Agent 接入](../research/durable-agent-ecosystem-integration-reassessment-20261014.md)

## 系统管理一次 Application 的进程

队列选择与进程资源管理分别确定。新增研究核对了 AnyIO、Trio、loky、Supervisor、Huey、Prefect 以及 Apple launchd、Linux cgroup／systemd 的官方源码和测试。AnyIO／Trio 的等待主要覆盖直属进程；Python 父子关系在根进程崩溃后会失去后代归属。继续补框架私有 Thread 启动、join、lease 状态机不能解决这个问题。[进程资源研究](../research/upstream-process-resource-ownership-20261014.md)

loky 3.7.0 的公开单任务 executor 接真实 Application，关闭阻塞后取消、关闭回调失败两项通过；Runtime 中 `os._exit(17)` 后，旧子进程仍活着却启动新任务，三项为 2／3。保留它作为比较，不再把直接 shutdown 作为完整恢复方案。[实际比较](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-loky-application-go-no-go-20261014-v1/REPORT.md)

macOS 产品 Adapter 使用随系统提供的 launchd 管理一次完整 Application，以公开 `launchctl bootstrap` 启动一次性 job，显式关闭 OS 自动重启，避免与 DBOS 重试重复。job 主进程死亡后，由系统清理相同进程组。Linux 后续采用 systemd transient service／cgroup 等价接入；目前没有 Linux 实测，不宣传为已交付的跨平台实现。

完整 Application 在 OS job 内同步调用 `BoundWorker.invoke()`；外层公开 `DBOS.run_step_async(preemptible=True)` 负责取消和等待 Adapter 收尾。Adapter 只有确认所管理的根进程和任务资源范围均已退出后，才返回结果或原失败。发送信号、引擎终态、launcher 命令返回以及 launchctl 的诊断文本均不充当退出证明。普通进程组仍不能覆盖任意第三方脱离行为。[公开取消与 OS 合同](../research/dbos-product-lifecycle-source-review-20261014.md)

本机最小 launchd 实验保留了同组通过与独立 session 失败的对照。随后官方 DBOS 与真实 Application 接线中，受控 native 子进程加入任务组，关闭阻塞后取消、关闭回调失败和根进程崩溃三项均通过。仅改变受控资源的组归属，原退出断言未改。正式 Runtime 的候选接线另行验证，见下文；这些局部结果仍不替代完整持久执行验收。[组范围对照](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/launchd-process-group-go-no-go-20261014-v1/REPORT.md)、[Application 接线](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-launchd-application-go-no-go-20261014-v1/REPORT.md)

进程管理 Adapter 的首版职责限制如下：

- 在实际执行进程内重建 Application、Runtime 和可信业务依赖；不跨进程传活句柄。
- 默认只接入能归入任务作用域的受管理资源。Pi、smolagents 与 MCP 的 managed 接线已有下述正式 CLI 局部验证；普通路径的独立 session 行为仍保留，不能据此宣称任意 Runtime／第三方后代都受管理。MCP managed stdio 使用公开 AnyIO streams 与 `ClientSession`，不 patch 上游私有函数。未接入受管理范围的配置仍须在执行前明确拒绝。
- 启动前先核对本地 executor 与旧任务 OS job，再允许 `DBOS.launch()` 恢复。未知旧资源时拒绝启动新 Runtime；并发启动两个 CLI、宿主被 SIGKILL 和启动中断均须实测。
- OS job 的描述只记录执行所需进程身份与输入引用，不成为另一套持久待办队列。领取、阶段容量、重试和恢复继续由 DBOS 负责；OS job 不配置业务重试。
- 容量按任务的实际运行和收尾占用，远端 HTTP 请求另留在途记录；本地客户端退出不能证明 Provider 已停止推理。

上述本机故障边界和业务写权限是接入 News 的先行条件。worktree 的产品与业务局部验证有通过证据，News 已迁到相同公开执行入口；首轮真实全日暴露可信 factory 绑定缺口，修复后的单项真实 Pi 测试通过，第二轮全日仍待结果。未知启动窗口继续保留边界。若后续需要自建另一套持久调度器，继续缩减实现路线，不能因为已写代码而坚持旧方案。

## Runtime 适配已有局部验证

Pi 继续使用官方公开 `BashOperations`。候选曾采用 Execa，但独立评审复现了 stdin EOF、后台输出等待和零超时参数的差异；源码核对还发现环境合并不同。已删除 Execa，改为固定 Pi 源码衍生的 Node 适配，保留完整等待函数与 MIT 许可证。Pi 最新 1.2.0 仍没有所需的进程组配置项，升级不能省去适配。[执行行为研究](../research/pi-managed-bash-execution-parity-20261014.md)

普通 shell 退出沿用官方输出 idle 等待；timeout／Abort 则停止具体 shell PID 并关闭本工具输出流，进入 uncertain 收尾。输出后代可能仍存活，必须由外层 OS 所有者确认整组退出。评审的真实对照已保留这一负面观测，不能把流结束当作后代退出。[复评证据](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/managed-scope-standards-review-20261014-v3/REPORT.md)

MCP 只为受管理 stdio 使用公开 AnyIO streams 与 `ClientSession`。普通 stdio、HTTP 和 AnySearch 接线保持原路径。生命周期用自身 CancelScope 请求停止，保留 Future 的清理错误；无效 JSONL 帧保留原解析诊断。此前吞错的真实 RED 与修复后的对照分别保存。

候选的 Pi／MCP／Application 回归有 166 项通过，最后的持续输出修复另有 13 项正式 Pi CLI 与 launchd 资源测试通过。前者不包含最后修复，后者是该修复的新增证据；均未请求远端模型，也没有运行新 News 日期。[回归日志](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/managed-scope-final-20261014-v2.log)、[最新局部验收](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/managed-parity-20261014-v3.log)

smolagents 候选已把两处 spawn、具体进程关闭、前台转后台和输出 watchdog 接到共享资源所有者。正式 CLI 验证了前台、显式后台、超时结束、超时转后台及关闭失败；最新相关回归为 304 项通过、1 项跳过。原后台脱离组的 RED 保留；这些结果仍未覆盖 DBOS 产品入口或真实 DeepSeek 请求。[smolagents 研究](../research/smolagents-managed-process-scope-20261014.md)、[原始 RED](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/managed-smol-20261014-red-v1.log)、[最新回归](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/managed-smol-final-20261014-v3.log)

独立评审还复现了 watchdog 关闭失败只留在线程异常、随后重试成功掩盖失败的问题。watchdog 现把原异常交给自身的 Run／Agent 所有者；确认线程退出后报告失败，最后收尾仍可释放实际已关闭的句柄。原 100 MB 正式 CLI 脚本逐字重跑后为 `run.failed`，本机进程确实退出；原异常对象与二次关闭也分别复验通过。[Standards 复评](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/managed-smol-standards-review-20261014-v3/REPORT.md)、[Spec 复评](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/managed-smol-spec-review-20261014-v3/REPORT.md)

## 业务与执行分别确认完成

NewsAgent 保留 Planner、StageLedger 和三阶段衔接。筛选、计分以及既有失败重算规则继续由业务代码决定。引擎状态不替代新闻封存条件。

新闻结果和稳定业务完成回执在业务库的同一事务提交。回执使用业务任务身份与 revision，恢复先核对回执；已提交业务而引擎确认丢失时，返回原完成结果，不重新跑模型。执行库与业务库允许短暂不同步，通过回执对账收敛，不承诺跨库原子事务。

回执不能防止尚未完成任务的重叠执行或旧 Worker 晚提交。当前 News managed 任务在业务事务内核对 grant、revision 和状态，再提交新闻结果；grant 撤销与 finalize 竞争已有本地测试。调用远程引擎查询后再写业务库仍不能作为原子权限校验。局部通过不等于完整迁移验收完成。[业务／Host 证据](../../.agentloom/validation/news-business-host-20261014-v1/REPORT.md)

业务写权限由 News Repository 维护：宿主在执行入口安装本次提交授权，停止或替换时撤销旧授权，工具提交在业务事务内校验当前授权并作条件写入。必须验证授权的安装、撤销与实际提交竞争。它只管新闻提交权限，不承担执行领取、租约、容量或自动重试。旧规格已允许分库；本次撤回的是当前原生同连接实现选择，不是撤回业务原子权限目标。

业务 task、revision 与引擎 workflow 身份要有稳定映射。同身份不同输入必须拒绝；已完成任务再次提交须返回原业务完成结果。不能把 SDK 的 return-existing 行为直接当作输入冲突校验，或在未告知调用方时丢弃新参数。

Worker 入口在实际执行环境内重建可信 binding。传入完整任务描述；Agent、Runtime 和业务句柄均在该次执行内创建。父进程的活句柄、closure、threading.Event 不作为可透明跨进程传输的接口。

main 的 `BoundWorker.map()` 只接受有限计划。worktree 已提供同步 `BoundWorker.invoke()`，DBOS／News Host 都复用该入口的结果、usage 与清理收集；可信 factory 在执行进程内绑定，该入口不领取任务、不做持久重试、不另起任务池。相关 90 项前期测试及两个真实信号窗口通过，最新产品测试另列于下文；仍未证明所有异步中断窗口安全，尚未合入 main。完整进程的终止交由 OS Adapter，不能借用旧 `DurableWorkerService.run_until_idle()`。[worktree 实现](../../src/app/application.py)

## 验证决定是否继续

先以选定引擎的真实公开接口验证以下行为，不增加第二套 claim、lease、持久 slot 或自动重试调度器：

先行验证覆盖旧资源退出与业务原子写权限，使用真实本机进程和受控业务事务，不请求模型。当前 18 项框架产品测试、21 项提交／业务／Host 测试已通过；独立 OS、公开入口和 controller 复核分别保留原 RED 与修复证据。任一后续合同需要重建持久调度内核时即停止该候选。[局部验收与独立复核](../research/dbos-product-integration-status-20261014.md#局部验收与独立复核)

首轮已实测同步任务取消后仍占用单 executor 的本地额度，下一任务在旧 body 与受控子进程退出后开始。同时实测确认默认 `DBOS.destroy()` 会在 body 与子进程仍存活时返回，随后旧 body 无法 checkpoint。该轮直接包装未通过完整退出验收；后续 OS Adapter 结果另列。`destroy` 返回始终不得作为 drain 完成或安全重启凭据，详细时序和原始 SDK 异常继续保留。

独立复核确认了容量和同步取消观测，也指出 old／new 复用同一池线程；body 末尾事件不是 native Thread 退出证明。本轮没有制造容量声明冲突，也未验证业务权限、输入身份冲突或提交后 ACK 丢失。不能将列出的验收条目写为已经通过。[复核记录](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-official-single-executor-independent-review-20261014-v1/REPORT.md)

后续真实 Application 验证已否决直接包装方式：通过框架公开资源登记创建的子进程，在关闭回调失败后仍存活，DBOS 却已启动下一任务；保持关闭回调阻塞的取消控制项通过。原严格断言保留，结果 1／2。这些失败独立保留，新的 OS 同组实验通过不消除原来的 detached 负例，也不代表正式 Runtime 迁移已经交付。[Application 实测](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-application-go-no-go-20261014-v1/REPORT.md)

- 两个 CLI 提交完整任务，阶段在途容量共享，冲突声明被拒绝；分别记录引擎执行数、Runtime 和客户端 HTTP 在途数。
- 取消或失联时旧同步 Worker、关闭回调和 Pi／MCP 尚未退出，不能启动超过阶段容量的新实际 invocation。
- 业务提交后、引擎确认前 kill，恢复后业务写入次数仍为一次，已完成任务不重新调用模型。
- executor 重启及信号中断后，能够确认或明确阻塞旧本机资源；拒绝把租约到期、Cancelled 或普通 Future 状态当作退出证明。
- 实际 Pi、smolagents Application 与正式 CLI 均复用既有 invocation，并在新尝试读取当前配置。

若严格退出合同要求再造通用持久调度器，候选接入即判过重，转下一成熟引擎，不扩大自研内核。无法由现成能力或小规模 Runtime 适配满足的合同应如实列出，不能隐去故障或放宽已有验收断言。

全日验收目标仍是 2025-09-01：完整新闻与行情输入、DeepSeek v4.1、7 个已认证 AnySearch Key、20／30／20，推理前准备 trace，保留全部输出。首轮真实运行退出 1／RED：prepared snapshot 的快返回路径丢失 News trusted factory 新 fixed_args 和 Stop hooks，Worker 被错误标记为完成。修复重新绑定 captured layers、在所有绑定之后保存快照，保留已捕获的 LLM 配置；一项真实 Pi public factory 测试转为 GREEN。20 个初筛任务已通过官方 retry 进入 revision 2，累计 attempts 40，第二轮真实全日运行中，尚未完成验收。[首轮 manifest](../../.agentloom/validation/durable-news-dbos-full-2025-09-01-v1/run-manifest-v1.json) 和原 RED 保留，[第二轮 manifest](../../.agentloom/validation/durable-news-dbos-full-2025-09-01-v1/run-manifest-v2.json) 在推理前写入，详见 [全日状态](../research/dbos-product-integration-status-20261014.md#全日首轮-red保留第二轮运行中)。2025-08-31 的通过证据仅绑定原 AFE 候选，不外推到新引擎。

## 当前交付状态

worktree 的 `durable` extra 已固定 `dbos==3.2.0`，提供 `Application.durable()`、有限批次提交和独立消费者 CLI。News 三阶段复用同一执行入口，业务 grant／receipt 留在自身 SQLite，controller 共用一个本机 executor；没有自有 claim／lease／持久 slot 内核，产品不调用 DBOS 私有 system tables。18 项框架测试、21 项提交／业务／Host 测试和独立复核支持这些局部结论。[产品接入状态](../research/dbos-product-integration-status-20261014.md)

2025-09-01 的真实模型／AnySearch 首轮运行退出 1／RED，绑定修复的单项真实 Pi 测试通过；第二轮全日运行中，未完成验收，未合入或推送。Linux、实际 PostgreSQL 与多机执行未验证。旧自研候选的 SQLite 锁失败及取消启动失败继续保留；本轮绑定失败或后续接入通过都不会把旧 `SQLITE_BUSY`、历史 `522/SHORT_READ` 或索引异常定案为已修复。

既有输出预算修复继续独立交付：未配置时不注入通用 cap，显式数字限制，null 可解除继承；输入压缩预留单独管理。该合同无需为更换任务引擎重新设计。
