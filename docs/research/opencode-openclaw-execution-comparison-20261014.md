# OpenCode、OpenClaw 如何管理并发与进程

后续开发已验证一个更窄的公开 SDK 接法：独立 Node 控制桥把 Python 宿主的管道 EOF 转为 `runCommandWithTimeout` 的取消，并等待 `withCommandProcessScope` 清理完成。macOS 的 Application 和真实 Pi/MCP 强杀验收通过，无需复用内部 relay/anchor。Linux、默认切换及新后端全日验收尚未完成。见 [接入规格](../specs/openclaw-process-owner-integration.md)。以下保留此前源码研究的范围和事实。

2026-10-14。按用户要求，只研究官方源码、文档和上游测试，未安装或运行这两个项目，未执行新的模型或故障实验，未修改 AgentLoom 产品代码。

**两者都把会话并发和进程生命周期分开。OpenCode 主要依靠作用域和取消收尾；OpenClaw 对受管理 Worker 已有独立监护和所有者强杀测试，值得比通用进程池更优先研究。它们都包含项目自行维护的进程适配代码，并非只装一个通用第三方库就得到全部保证。**

## 固定版本

| 项目 | 核对的发布口径 | 对应官方源码 commit |
| --- | --- | --- |
| OpenCode / `opencode-ai` npm 默认频道 | 1.19.0 | `733b2c627e5aec39cb33cc22373bdce33053ef2c` |
| OpenCode GitHub 发布 tag | v2.0.28 | `026b106bc5d41b679ffc413264229c340871e4d3` |
| OpenClaw / `openclaw` | 2026.10.3 | `59cb44796b0f1f0daa0e37af8b1495abb45d9bc1` |

npm 版本来自官方 registry 的 latest metadata，commit 由官方仓库对应 release tag 核对。OpenCode 的 GitHub v2.0.28 与 npm 默认频道不同，核对时 `opencode-ai` 没有发布该 npm 版本，不能混称为同一个最新稳定版。源码仅下载供阅读，v1/OpenClaw 记录在 `.agentloom/research/opencode-openclaw-20261014/upstream/` 的 manifest，v2 快照在 `.agentloom/research/opencode-v2.0.28/`；没有把本地修改的 OpenCode checkout 当成上游证据。[OpenCode npm 频道源码](https://github.com/anomalyco/opencode/releases/tag/v1.19.0)、[OpenCode v2 发布 tag](https://github.com/anomalyco/opencode/releases/tag/v2.0.28)、[OpenClaw release](https://github.com/openclaw/openclaw/releases/tag/v2026.10.3)

## OpenCode：会话协调、作用域关闭与 v2 恢复

`SessionRunCoordinator` 用进程内 Map 与 Effect Fiber 管理执行：同一 Session 的运行串行或加入已有执行，不同 Session 可以并发；interrupt 等待该 Fiber 的清理。这里没有数据库领取租约，也不是 NewsAgent 所需的跨重启持久任务队列。[会话协调源码](https://github.com/anomalyco/opencode/blob/733b2c627e5aec39cb33cc22373bdce33053ef2c/packages/core/src/session/run-coordinator.ts)

工具进程由 `AppProcess` 封装，使用 Effect 的作用域、超时和取消。自有 `CrossSpawnSpawner` 接 Node 子进程，在 POSIX 默认创建独立进程组；作用域释放时发送停止信号，并等待直属进程 exit，可按配置升级 KILL。上游测试包含超时与 Fiber 中断后的子进程清理；它们是在宿主还能执行收尾代码的场景。[进程封装](https://github.com/anomalyco/opencode/blob/733b2c627e5aec39cb33cc22373bdce33053ef2c/packages/core/src/process.ts)、[spawn 与释放](https://github.com/anomalyco/opencode/blob/733b2c627e5aec39cb33cc22373bdce33053ef2c/packages/core/src/cross-spawn-spawner.ts#L365)、[测试](https://github.com/anomalyco/opencode/blob/733b2c627e5aec39cb33cc22373bdce33053ef2c/packages/core/test/process/process.test.ts#L156)

它另有自写 `Shell.killTree()`：POSIX 对组发 TERM，约 200ms 后按直属进程的退出状态决定是否发 KILL；失败时回退为直属 PID。Windows 用 `taskkill /T /F`。这个实现没有在 KILL 后等待整组为空，不能把函数返回当成全部孙进程退出。[Shell 清理](https://github.com/anomalyco/opencode/blob/733b2c627e5aec39cb33cc22373bdce33053ef2c/packages/core/src/shell.ts#L31)

MCP 使用官方 MCP SDK，但 OpenCode 额外维护了 scope finalizer：取得 stdio transport PID，用 `pgrep -P` 枚举后代，逐个发 TERM，然后关闭 MCP client；关闭异常被忽略。它证明“复用 SDK 仍需要应用适配”，没有建立顶层宿主 SIGKILL 后由独立监护完成全部清理的合同。[MCP 创建与 finalizer](https://github.com/anomalyco/opencode/blob/733b2c627e5aec39cb33cc22373bdce33053ef2c/packages/opencode/src/mcp/index.ts#L533)

OpenCode 有客户端和执行服务之分。关闭某个客户端与强杀执行服务是不同事件；不能把 TUI 退出后的行为套到执行所有者被 kill -9。[官方服务架构](https://opencode.ai/docs/server/)

**核对结果：**所读发布路径主要解决正常结束、取消、超时的收尾。未找到覆盖完整执行宿主强杀、全部受管理 MCP/工具后代退出的独立 guardian 合同。历史 orphan issue 只能说明项目遇到过同类问题，不能证明当前版本仍复现。[历史问题 #11527](https://github.com/anomalyco/opencode/issues/11527)、[MCP 问题 #26714](https://github.com/anomalyco/opencode/issues/26714)

### GitHub v2.0.28 的增强

v2 在开始执行前，将会话执行意图与 started event 一起持久提交。完成时释放，宿主崩溃或 SIGKILL 留下未完成 claim；下一次受管理服务启动时扫描并恢复，明确采用 at-least-once。它依赖旧服务已退出的启动协议，不承诺外部副作用只执行一次，也没有把恢复成功当成全部旧 OS 后代退出。[开始与完成事实](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/src/session/execution.ts#L81)、[重启恢复](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/src/session/execution/restart.ts)

v2 MCP stdio 也已通过自己的 Environment spawner 创建，纳入与 shell 相同的作用域，复用协议库处理消息。上游测试包含“wrapper 正常退出，忽略 TERM 的 MCP 后代仍须停止”。这比 v1.19.0 的 finalizer 后代扫描更完整，但仍是在存活宿主执行 transport close 的场景。[v2 stdio](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/src/mcp/stdio.ts)、[MCP 后代测试](https://github.com/anomalyco/opencode/blob/026b106bc5d41b679ffc413264229c340871e4d3/packages/core/test/mcp.test.ts#L794)

因此应借鉴“所有工具进程进入同一所有权入口”的接线。AgentLoom 已有 DBOS 提供持久任务和恢复，不再因 OpenCode 实现自己的 claim 而新增另一套 claim/sweep 循环。

## OpenClaw：简单调度之外，另有专门监护层

普通自动回复使用进程内 lane 队列：同会话串行，不同会话可并行，每个 lane 有容量。`exec/process` 的后台会话登记在内存，官方文档明确说明进程重启后丢失；这不能直接替代 NewsAgent 的持久阶段任务。OpenClaw 的会话存储、cron 恢复、Node Worker journal 属于其他职责，不能把这些持久能力与内存 exec registry 混为一谈。[队列说明](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/docs/concepts/queue.md)、[后台执行说明](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/docs/gateway/background-process.md)

常驻 Gateway 可由 OpenClaw 的安装命令配置系统监管：Linux systemd，macOS launchd。它也支持前台运行，不能把系统服务写成所有本机调用的必要条件。[Gateway 运维说明](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/docs/gateway/index.md#L199)

更贴近 AgentLoom 问题的是受管理进程路径：

- 独立 relay 与 group anchor 保留进程所有权；宿主 IPC 断连或控制通道消失会触发 parent-loss 清理。它们能在应用所有者死亡后继续工作，不依赖死亡宿主的 finally。[relay](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-relay.ts#L98)、[anchor](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-group-anchor.ts#L193)
- Linux Node 模式用专用 child subreaper，在启动工作前取得内核后代领养权；通过 `koffi` 调用 `prctl/waitid/waitpid`，停止并回收被领养后代。`koffi` 提供 FFI，完整监护协议仍由 OpenClaw 自己实现。[Linux 所有权实现](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/linux-child-subreaper.ts)
- macOS 等 POSIX 路径使用受管理进程组，并核对组消失；输出管道关闭、root exit 与资源范围退出分开处理。[relay 准备与平台选择](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-relay-preparation.ts)、[组所有权](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-group-ownership.ts)
- Node Worker journal 记录具体启动身份及清理事实，恢复容量先核对旧资源。它是进程所有权的恢复机制，不需要让新闻业务事务替它管理进程。[Worker 恢复](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor-recovery.ts)

关键上游测试 `uses IPC disconnect after external-owner SIGKILL, then reconciles only after exact tree death` 做了实际强杀：先启动执行所有者和一个含孙进程的 Worker，杀所有者，等待 supervisor、Worker、孙进程身份死亡；随后新宿主读取清理事实，恢复容量。测试在非 Windows 上运行，macOS 使用 owned-anchor，Linux Node 另测 linux-subreaper。[固定测试](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.recovery.test.ts#L761)

**这是比普通取消测试更强的一手证据。** 本轮只阅读测试代码，没有执行，也未核对相应 CI 运行结果；该测试的对象是 OpenClaw 受管理 Node Worker fixture，不能直接外推为 AgentLoom Application、全部 Pi/MCP 接线已经通过。

## 能否直接复用

OpenClaw 确有公开 `openclaw/plugin-sdk/process-runtime`，导出命令执行、`withCommandProcessScope`、`killProcessTree`、PTY 等帮助接口。它没有在这个公开入口导出完整 `createNodeWorkerSupervisor` 或 relay/anchor 构造接口；所以“有公开 process SDK”不等于“完整强杀所有权引擎已经可独立接入 Python”。[公开导出](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/plugin-sdk/process-runtime.ts)、[发布 exports](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/package.json)

OpenCode 的 Effect Scope 是通用第三方运行时能力，CrossSpawnSpawner、MCP 后代清理和 Session coordinator 都含项目自己的接线。OpenClaw 的进程所有权实现更完整，但其内部协议与 Node Worker journal 有联系，直接复制会把生命周期维护责任带回 AgentLoom。

用户此前已明确不新增自研 supervisor，因此本轮只把 OpenClaw 列为优先研究的参考与组件来源，不直接复制内部模块，也不宣布选型完成。它不是现成 Python 库；若评估 npm SDK 接入，还须核对 Python/Node 之间的宿主死亡通知、完整 Application 范围及安装要求。

## 对 AgentLoom 方案的修正

应保留 DBOS 的持久队列、20/30/20 阶段容量和恢复，保留 NewsAgent 业务规则；把剩余问题集中在完整 Application 的进程所有权适配。OpenClaw 的所有者强杀用例可以作为验收参考，先核对其公开组件能否独立承担同类保证。通用进程池和 Ray 不再作为这一步的默认首选。

“简单并发”和“宿主死亡后仍能收尾”应分别验收。无需因为生命周期缺口重做新闻业务、数据库领取循环或所有框架接口。当前要求仍包括自动清理，不能暗中降级为重启后手动处理。

子报告见 [OpenCode 源码研究](opencode-process-lifecycle-source-review-20261014.md)、[OpenClaw 源码研究](openclaw-process-lifecycle-source-review-20261014.md)。前轮 Python 库选型范围和历史结果见 [库式执行复核](library-owned-local-execution-reassessment-20261014.md)。
