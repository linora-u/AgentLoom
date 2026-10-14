# OpenClaw 进程生命周期源码核对

2026-10-14。研究对象是 OpenClaw 的并发、持久状态与本机进程清理实践，目的是判断它能否简化 AgentLoom 的 Application invocation 资源合同。只读官方 npm 元数据、官方 GitHub tag 源码、源码内测试和官方文档；未改产品代码、未运行模型、未做进程实验。

**结论：OpenClaw 的进程清理做得比普通 Node CLI 强很多，值得借鉴的是“按执行路径显式声明资源合同、清理不确定就失败”的做法；但它不能直接替代 AgentLoom 需要的 pip-local、无用户配置 OS service、顶层 Python SIGKILL 后自动回收完整 Pi/Node/MCP/tool descendants 的合同。** OpenClaw 默认是长驻 Gateway 产品：消息入站用 SQLite 队列恢复，背景 exec 只在内存中保留，强物理清理主要发生在 host exec、MCP stdio、service child / worker 等被 ProcessSupervisor 明确拥有的路径。Gateway 顶层被 SIGKILL 时，只有外部 service manager、Linux subreaper 所在的仍存活 owner、Windows Job 或后续基于持久 custody 记录的补救路径可能继续完成清理；这不是 npm 本地默认的一次性 invocation 容器。

## 版本与源码范围

npm `openclaw@2026.10.3` 的 metadata 指向 `git+https://github.com/openclaw/openclaw.git`，Node engine 为 `>=24.16.0 <25 || >=26.1.0`，tarball 为 `https://registry.npmjs.org/openclaw/-/openclaw-2026.10.3.tgz`。官方 Git tag `v2026.10.3` 是 annotated tag，peeled commit 为 `59cb44796b0f1f0daa0e37af8b1495abb45d9bc1`；本轮源码快照保存在 `.agentloom/research/openclaw-source-20261014/openclaw-v2026.10.3-121737`。[npm metadata](../../.agentloom/research/opencode-openclaw-20261014/openclaw-npm.json)

OpenClaw 是 “Multi-channel AI gateway with extensible messaging integrations”，包内依赖包括 `execa`、`kysely`、`@modelcontextprotocol/sdk`、`@lydell/node-pty`、`koffi`、`croner` 等。它复用 `execa` 和 MCP SDK，但关键的进程生命周期并非交给这些库原样处理；OpenClaw 自己实现了 `src/process` 下的 command scope、ProcessSupervisor、service child relay、owned stdio、spawn broker 和平台特定清理。

## 并发和持久状态不是一个通用任务引擎

OpenClaw 的持久恢复重点在 Gateway 消息入站。以 LINE 文档为例，webhook 在返回 `200` 前写入 durable queue；queue-to-agent 边界是 at-least-once，Gateway shutdown 或 crash 可以让事件重放；每次 drain 开始会回收 owner Gateway process 已不再运行的 claim，30 分钟 claim lease 是 PID 身份无法验证时的兜底。[LINE durable delivery](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/docs/channels/line.md#L54-L113)

Telegram 文档同样把边界写清楚：长轮询收到 update 后，先事务性写入 `state/openclaw.sqlite` 的 `channel_ingress_events` queue，再推进 restart offset；崩溃可导致已经入队的 update 被 Telegram 重投，queue 只提供 bounded replay dedupe，不是 exactly-once。[Telegram ingress boundary](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/docs/channels/telegram/transports.md#L49-L66)

源码层面，claim owner 是 `pid:startToken:uuid`。start time 绑定 PID 实例，避免 Linux TID 或 recycled PID 冒充死 owner；claim lease 默认 30 分钟。`isIngressClaimOwnedByOtherLiveProcess()` 只有在 owner PID 仍存在、claim 仍 fresh、且 start token 匹配或不可读时才保护 claim。[ingress claim owner](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/channels/message/ingress-claim-owner.ts#L1-L180)

`createChannelIngressDrain()` 的注释直接定义了它拥有的范围：claim recovery、per-lane serialization、adoption-time complete、retry/dead-letter、pre-adoption stall watchdog。它会刷新 claim lease，dispatch 前注册 active state，`onAdopted()` 时完成 claim，disposed 或 abort 的情况保留 claim 给后续恢复。[ingress drain](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/channels/message/ingress-drain.ts#L1-L111)、[dispatch lifecycle](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/channels/message/ingress-drain.ts#L367-L547)

这套队列可借鉴的是“用持久 claim 表示逻辑所有权，用 PID+start time 防止错误回收”。但它管理的是 channel ingress 到 agent-turn adoption 的消息，不是“完整本地 Application invocation 的所有本机进程”。它也明确承认 at-least-once 和 replay，而不是立即、分布式 exactly-once。

## background exec：进程可跨 turn，不能跨 Gateway 重启

OpenClaw 的用户可见背景 exec 不是磁盘持久任务。官方文档写明：backgrounded sessions “in memory only, not on disk”，Gateway process restart 后 session 会丢失；session logs 只有通过 `process poll` / `process log` 进入聊天历史后才保存。[background process docs](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/docs/gateway/background-process.md#L208-L230)

文档还区分了本机 host exec 与 worker/node 环境：在 node-backed worker 中，background process 属于 session environment，turn 结束后可继续运行；但 moving/retiring environment、replacing ownership 或 stopping node 会停止这些进程，process handles 不会 survive worker 或 node restart。[worker environment process docs](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/docs/gateway/background-process.md#L90-L111)

因此，OpenClaw 没有把 background shell 做成 DBOS/Prefect 那类 durable invocation。它用 ProcessSupervisor 阻止安全重启提前发生，但不承诺重启后恢复一个正在跑的 background bash handle。这一点和 AgentLoom “旧 Application 未确认退出前不能补位，新 Runtime 不能开始”有关，但不能替代 AgentLoom 的 durable queue / Application lifecycle。

## command runner：OpenClaw 把信号发送和进程组灭绝分开

普通 command runner 用 `execa` 启动，但当 `killProcessTree` 为真时，POSIX 下会用 `detached: true` 让子命令成为自己的 process group leader；`requireProcessTreeExtinction` 只有和 `killProcessTree` 一起使用才允许。[exec-runner options](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/exec-runner.ts#L80-L97)、[spawn detached command](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/exec-runner.ts#L236-L294)

取消时，`createCommandTerminationController()` 对 POSIX process group 先发 TERM，再按 grace 升级 KILL，并循环观察 `isChildProcessTreeAlive({ pid })`。源码注释很明确：信号送达不是退出证据；只观察这个 group，不重新 signal 一个 retired PID。cleanup 的结果是 `normal | cooperative | forced | uncertain`，不确定会进入调用结果和上层 scope。[termination controller](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/exec-termination.ts#L88-L201)

底层 `killProcessTree()` 也是保守设计：Unix group kill 只在 PID 被验证为 process group leader，或调用者显式传 `detached: true` 时使用；`detached: false` 时不 group-kill，Linux attached child 则通过 `/proc` 枚举 descendants。注释直接说明这是为了避免误杀 Gateway 自己的 process group。[kill tree contract](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/packages/agent-core/src/harness/env/kill-tree.ts#L26-L43)、[kill selection](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/packages/agent-core/src/harness/env/kill-tree.ts#L62-L114)

`withCommandProcessScope()` 还维护了一个 AsyncLocalStorage scope。scope 关闭会 stop 并 settle 子命令；child 的 native close、broker close 和 retained cleanup 会单独 join。测试覆盖了“bounded runner result 和 scope cleanup 分开”：调用已返回 `cleanup: "uncertain"` 时，scope 仍未结束，直到 cleanup owner 完成后才释放。[command scope](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/exec-spawn.ts#L41-L160)、[scope settlement test](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/exec-scope-settlement.test.ts#L213-L239)

这部分对 AgentLoom 的直接启发是：工具超时、root exit、pipe close、cleanup completion 必须是独立状态；`Future` 完成不能当成 descendants 退出。OpenClaw 的实现已经体现这个原则。

## Linux subreaper 是强路径，但不是所有路径默认都有

OpenClaw 有一个专门的 Linux child subreaper owner。`acquireLinuxChildSubreaper()` 要求 Linux、Node runtime、built process owner、启动前没有已有 children；它调用 `prctl(PR_SET_CHILD_SUBREAPER, 1)`，用 `waitid(... WNOWAIT | __WALL)` 判断 wait ownership，用 `waitpid(... WNOHANG | __WALL)` reap adopted children。`drain(signal)` 最终以 `ECHILD` 作为 extinction observation。[linux child subreaper](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/linux-child-subreaper.ts#L41-L160)

service child relay 会在 Linux、Node、非 Bun、非 worker deploy build 时选择 native process owner；否则退到 process-group control。若 unsupported ownership contract 不能成立，源码会在 launch 前失败，而不是降级为 transport-only。[relay preparation](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-relay-preparation.ts#L47-L131)、[process group control assertion](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-group-ownership.ts#L17-L30)

relay host 的关闭条件也不是“root exited”。它要求 closing receipt、lineage EOF、stdout/stderr drain；group 模式还要 anchor exit 后 `isOwnedProcessGroupGone(anchorPid)`，subreaper 模式则要求 closing receipt 明确是 `linux-subreaper`。缺 receipt 或 cleanup deadline 过期都会进入 identity-lost / cleanup failure。[relay close authority](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-relay-host.ts#L338-L395)、[closing receipt handling](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-relay-host.ts#L397-L479)、[adapter contract](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-relay-host.ts#L688-L727)

OpenClaw 文档把这个合同写得更直白：managed exec 和 negotiated worker workspace commands 在 Linux Node 下使用 dedicated child subreaper，它在 launch 前获取 kernel child ownership，能 adopt orphaned descendants，并在 descendants 创建新 process group 或 session 时也停止并 reap；完成需要 kernel 报告无剩余 children、matching owner receipt 和 output drain。它不依赖 group-directed signals、process-table census 或 inherited-pipe closure alone。[background process child bridging](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/docs/gateway/background-process.md#L153-L179)

这个路径有独立 native owner，能在应用所有者死亡后继续处理后代。AgentLoom 当前持久实现已使用 macOS LaunchdJobs，不能概括成只靠 Python finally/psutil；OpenClaw 值得研究的是跨平台的库内所有权组件。它仍不是所有执行路径的相同合同：one-shot tool cleanup 不会停止 shared sandbox，也不声称每个 remote descendant 已退出。[one-shot cleanup boundary](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/docs/gateway/background-process.md#L180-L189)

## ProcessSupervisor 的合同是按 scope 选择的

`ProcessSupervisor` 暴露 `acquireScopeCleanup(scopeKey, { processTree })`，其中 policy 是 `required-all | owned-only | transport-only`。源码注释定义：`required-all` 包括 external execution；`owned-only` 不要求 external lifetimes；`transport-only` 不做 execution-tree claim。[supervisor types](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/types.ts#L168-L180)

`createProcessSupervisor()` 内部用 `requiresProcessTree()` 决定是否要求树清理：`required-all` 总是要求，`owned-only` 只在非 external cleanup 时要求。spawn 前如果 PTY 路径不能满足 required tree cleanup，会在 launch 前拒绝。真正启动后，`owner.waitForExtinction` 会在 run wait 之外另行 join；cleanup failure 会记录给所有 cleanup owners。[supervisor policy](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/supervisor.ts#L39-L49)、[acquire cleanup](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/supervisor.ts#L152-L178)、[spawn tree requirement](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/supervisor.ts#L180-L263)

这一点反而支持“不做 grand scheduler”：OpenClaw 没有给所有模块套一个统一最大合同，而是让每条路径声明需要哪种清理强度。AgentLoom 若要简化，应该把 Application 的 OS process-owner boundary 做小，而不是把队列、容量、retry、lease 重新造一遍。

## MCP stdio 走 OpenClaw 的 owned stdio，不是 MCP SDK 原生 stdio

OpenClaw stdio transport 包装了 MCP SDK 的 message framing，但 process 启动和关闭走 `createOwnedStdioProcess()` / `closeOwnedStdioProcess()`。启动时传 argv/cwd/env/stderr，关闭时先 end stdin，超时后 TERM，再超时后 KILL，最后同时等待 `process.wait()` 和 `process.waitForExtinction()`；如果无法确认 descendant extinction 会抛 `OwnedStdioCleanupError` 或原 cleanup error。[MCP stdio transport](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/agents/mcp-stdio-transport.ts#L1-L103)、[transport close](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/agents/mcp-stdio-transport.ts#L188-L234)

`createOwnedStdioProcess()` 直接 `createChildAdapter({ ownProcessTree: true, stdinMode: "pipe-open" })`，因此 MCP server subprocess 可以使用 service child / process group / Windows Job 路径。`closeOwnedStdioProcess()` 明确说 protocol EOF requests shutdown；只有 spawn owner 能 certify descendant extinction。[owned stdio](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/owned-stdio.ts#L11-L60)

测试也覆盖了 SDK 初始化失败、abort、timeout 时不能把丢弃的 close promise 当成 closure certification；cleanup rejection 会进入 agent cleanup scope 的 uncertain outcome。[MCP cleanup tests](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/agents/mcp-stdio-transport.test.ts#L311-L360)

这对 AgentLoom 的 MCP 接入很有启发：不要依赖 MCP SDK 默认 stdio 的 process options，而是在本项目控制的 transport 层拥有 child。OpenClaw 的做法是小范围替换 transport，而不是改 MCP SDK 私有实现。

## Windows 和 macOS/Linux 的边界不同

Windows 下 OpenClaw 尝试用 Job Object：`createChildAdapter()` 在 Windows spawn 时调用 `spawnWindowsJobChild()`，`windowsCleanup = owned.job.certify()`，并把 `waitForExtinction` 暴露给 caller。测试覆盖 `required-all`、`owned-only`、`transport-only` 与 external cleanup 的组合：Job unavailable / admission failed / observation failed 不会让 command execution 本身失败，但若 cleanup policy 要求 tree，则 cleanup call 会 reject。[Windows owned stdio policy test](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/owned-stdio.windows.test.ts#L332-L395)

普通 `exec-termination.ts` 里也有相反注释：Windows `taskkill /T` 需要 live root PID；对 dead reusable PID 重试可能打到无关树；更强 ownership 需要 spawn-time Job Object。[Windows taskkill limitation](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/exec-termination.ts#L93-L99)

macOS 和 retained Bun process-group owners 仍要求 kernel group disappearance；permission-denied probe 不能证明 absence。OpenClaw 文档没有声称 macOS 可覆盖脱离 group/session 的任意 descendant。这里和 AgentLoom 当前 macOS launchd 研究一致：process group 是强于 Python finally 的边界，但不是 cgroup/subreaper。

## Gateway 服务监管与任务进程监护分开

OpenClaw 支持普通 `openclaw gateway run`，也支持 managed service。Team server 文档要求 Linux host、persistent storage、dedicated service account，并建议 `openclaw onboard --install-daemon`；同一文档还要求“one lifecycle owner per installation”，正常 managed installation 使用 native Gateway service commands，外部部署系统拥有 service 时不要和 OpenClaw updater 竞态。[team server install](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/docs/gateway/team-server.md#L48-L79)、[one lifecycle owner](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/docs/gateway/team-server.md#L466-L473)

Gateway run loop 会识别 launchd/systemd/external supervisor 身份，获取 Gateway lock，安装 SIGTERM/SIGINT/SIGUSR2 handler，drain active work 后关闭 server 或 restart。它会读取 systemd stop timeout 或 launchd ExitTimeOut，计算 shutdown drain budget；源码注释特别说明 launchd ExitTimeOut 只约束 launchd 正在执行的 stop，外部 SIGTERM 不会启动那个 clock。[run loop startup](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/cli/gateway-cli/run-loop.ts#L65-L110)、[shutdown budget](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/cli/gateway-cli/run-loop-shutdown-budget.ts#L15-L55)

Windows service path更明显：`runWindowsGatewayTaskSupervisor()` 把真正 Gateway 子进程跑在 ProcessSupervisor 管理的 anchored shell 里；子进程请求 restart 时，supervisor 会先 `await managed.waitForExtinction?.()`，再启动下一代。注释称外层 hidden task launcher owning outer Job，command anchor owning inner Job。[Windows task supervisor](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/cli/gateway-cli/task-supervisor.ts#L33-L38)、[restart after extinction](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/cli/gateway-cli/task-supervisor.ts#L69-L110)

这些服务监管负责 Gateway 的常驻与重启。顶层 Gateway 被 SIGKILL 后，它的进程内 cleanup 不会运行，但独立 service-child anchor/subreaper 可以继续处理已经纳入其所有权的任务后代。不能把 Gateway 的服务重启要求写成这些独立监护组件必须依赖 systemd/launchd，也不能由某条组件路径推导所有资源均受管。

主 agent 另核对了真正的所有者强杀测试：`node-worker-supervisor.recovery.test.ts` 的用例杀执行所有者，随后等待 supervisor、Worker、孙进程身份死亡，恢复时核对清理事实才释放容量。它在 macOS/Linux 测 owned-anchor，Linux Node 另测 subreaper。这是上游已有更强能力的证据；本轮没有运行该测试，也没有证实全部 AgentLoom Pi/MCP 接线适用。[固定上游测试](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.recovery.test.ts#L761)

## 顶层 Gateway SIGKILL 时，必须分清四种事实

OpenClaw 里至少有四种“结束”：

| 观察 | 能证明 | 不能证明 |
| --- | --- | --- |
| channel queue claim 被释放或重试 | 消息逻辑所有权可恢复 | 旧 agent/tool subprocess 已退出 |
| `run.wait()` / root result 完成 | 根进程或 transport 结果完成 | descendants 已退出 |
| `waitForExtinction()` resolves | 该 adapter 声称自己的 process owner 清理完成 | 所有未纳入该 adapter 的 Gateway 资源退出 |
| service manager 重启 Gateway | Gateway 宿主生命周期恢复 | 无 service/cgroup/Job/subreaper owner 的旧 descendants 自动消失 |

OpenClaw 自己的代码也反复避免混淆这些事实：`ProcessRunActivity` 注释写“settled result does not establish descendant extinction”；`ManagedRun.waitForExtinction` 是独立可选方法；background process 文档写 timeout result 可先返回，scope retirement 和 Gateway shutdown 另等 cleanup owner。[supervisor types](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/types.ts#L13-L45)、[timeout cleanup docs](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/docs/gateway/background-process.md#L147-L151)

OpenClaw 的少量 crash-recovery custody 代码也不是默认全局扫孤儿。`settleCommandProcessGroups()` 只针对已记录的 `{pid, startedAt}` identities；它要求原 PID 仍能用 start identity 确认，才能 `killProcessTree(pid, { detached: true, force: true })` 并等待 group 消失。若 identity 不可确认或 group 仍活，会返回 unresolved。[command custody](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/command-process-custody.ts#L1-L47)

相关测试“retains and stops a detached writer after its busy scope owner is killed”证明的是：已记录 identity 的 detached writer 可以在 scope owner 被杀后由后续 `settleCommandProcessGroups()` 清理；测试甚至先验证 writer 仍可写文件，再调用 custody cleanup。它不是证明 top-level Gateway SIGKILL 时所有 descendants 自动消失。[command custody process test](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/command-process-custody.process.test.ts#L13-L126)

## 对 AgentLoom 的直接判断

OpenClaw 没有证明 AgentLoom 需要保留一个自研 orphan registry / lease scheduler。相反，它证明了更小的切分：队列/业务恢复和本机进程所有权应分开；本机进程 owner 只负责自己启动或明确接管的进程树；不确定就阻止清理成功，不要把逻辑完成当成物理退出。

可借鉴的具体做法：

- 在本项目控制的 spawn/transport 层统一拥有进程：OpenClaw 对 MCP stdio 没有用 SDK 默认 stdio，而是用 `OwnedStdioProcess` 包起来。
- 每个执行 scope 显式声明 cleanup policy：`required-all`、`owned-only`、`transport-only` 这种分层比“所有东西都套最大合同”更清晰。
- cleanup result 要进入产品合同：`normal/cooperative/forced/uncertain` 或 cleanup error，而不是日志里吞掉。
- Linux 可以用一个小的 native owner/subreaper 覆盖脱离 process group 的 descendants；macOS process group 和 Windows Job 的边界应分别处理。
- background/poll 型用户体验不等于 durable invocation；OpenClaw 也只把 background exec 保存在内存。

不能外推的部分：

- OpenClaw 的 Gateway 是长驻服务；AgentLoom 的问题是完整 Application invocation 的 DBOS durable queue + finite capacity + Runtime/Pi/MCP/tool 资源收尾。
- OpenClaw 有受管理 Worker 的所有者 SIGKILL 清理测试；还没有建立从独立 Python 宿主经公开 API 调用这些组件、覆盖全部 AgentLoom Application 资源的合同。不能一概否定它的本机监护能力。
- OpenClaw 的 channel ingress queue 是 at-least-once 消息恢复，不是任意 agent task 的 process tree recovery。
- OpenClaw 不禁止所有 escape；它按路径选择 owner。shared sandbox、external cleanup 和 transport-only 路径不声称所有 remote descendants 已退出。

AgentLoom 应保留 DBOS 队列，将剩余工作集中于完整 Application 的进程所有权适配；Pi/MCP/tool 必须进入被选组件的受管理范围。用户已排除新增自研 supervisor 和以配置系统服务作为默认方案，因此不直接复制 OpenClaw 私有 relay/anchor，也不回到新增 launchd/systemd 适配。优先核对其公开 SDK 是否能独立复用所需能力；当前 `plugin-sdk/process-runtime` 导出命令与 scope 帮助接口，没有直接导出完整 `createNodeWorkerSupervisor`。这项接口缺口需明确验证。[公开 SDK](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/plugin-sdk/process-runtime.ts)

## 一手来源

- npm metadata: `.agentloom/research/opencode-openclaw-20261014/openclaw-npm.json`
- Git tag: `openclaw/openclaw v2026.10.3`, peeled commit `59cb44796b0f1f0daa0e37af8b1495abb45d9bc1`
- Source snapshot: `.agentloom/research/openclaw-source-20261014/openclaw-v2026.10.3-121737`
