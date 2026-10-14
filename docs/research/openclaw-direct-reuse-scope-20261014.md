# OpenClaw 能否直接复用完整进程机制

核对日期：2026-10-14。源码固定为 `openclaw@2026.10.3`，官方 commit `59cb44796b0f1f0daa0e37af8b1495abb45d9bc1`。本次只读源码和已安装包，没有修改产品、测试、配置或运行任务。

## 结论

可以更直接地调用 OpenClaw 的底层进程所有权实现；不能据此承诺删除 Python/Node 边界，或把完整 OpenClaw Worker 宿主作为任意 Python Application 的现成执行器。**建议继续区分“复用上游进程 owner”和“引入完整 OpenClaw 执行平台”**，先判断接入后实际能删多少自有代码，再决定是否改用内部入口。

## 已核实事实

| 入口 | 能力 | 对 AgentLoom 的限制 |
|---|---|---|
| 公开 `openclaw/plugin-sdk/process-runtime` | 导出 `runCommandWithTimeout`、`withCommandProcessScope` 等命令运行与收尾函数 | 未导出 `createOwnedStdioProcess`、`createChildAdapter` 或 `createNodeWorkerSupervisor`。公开 helper 不能等同完整 Worker owner。[1] |
| 内部 `createOwnedStdioProcess` | 接收任意 `argv`、环境、工作目录；调用 `createChildAdapter`，固定 `ownProcessTree: true`；关闭函数等待 root 与 `waitForExtinction` | 是任意 Python 命令可接入的底层 owner 候选；不是公开稳定接口，也不提供 DBOS 持久任务调度。[2] |
| 内部 `createChildAdapter` | POSIX 的普通 `ownProcessTree` 路径进入 `createServiceChildRelayAdapter` | 上游已有 relay/anchor，不必复刻其杀进程和确认退出的实现。[3] |
| 内部 `createNodeWorkerSupervisor` | 创建 journal、launch/turn store、capacity、workspace、recovery；管理完整 OpenClaw Worker admission 与生命周期 | `launch()` 需要 OpenClaw descriptor/connection endpoint；实际启动固定 namespace/hash 下的 Worker bundle，不是通用 `argv` API。会再引入一套 claim、容量和持久状态，和 DBOS 的职责重叠。[4][5] |

### 父死亡监护不能跨语言边界自动发生

OpenClaw 的 relay 监听 **其 Node 调用者** 的 IPC `disconnect`，向 anchor 发送 `parent-loss`；anchor 也监听 disconnect 并执行 cleanup。Linux owner 在专用进程中通过 Koffi 调用 `prctl` 获得 subreaper，再 drain/reap 后代。这些是上游实现，可以原样复用。[6][7]

若结构仍是 `Python Executor → Node adapter → OpenClaw owner → Python Application`，强杀最上层 Python 并不会同时关闭 `Node adapter → owner` 的 IPC。Node adapter 需要感知其 Python 父死亡，再向上游发出终止请求。直接改用内部 `createOwnedStdioProcess` **不能自动消除这段关联**。

把 Node 改为外层入口，并让 OpenClaw owner 包住 Python Executor，有机会少一层 Python→Node EOF 适配；但这会改变启动入口与进程所有权，而且 Executor 又会派生多个任务。是否还能保留任务独立终止、容量补位和回执，需要新的集成设计。本次没有验证，也不据此承诺代码更少。

### 任意命令与完整 Worker 的退出语义不同

anchor 源码明确区分：普通命令 root 退出后可保留 background descendants；Linux `ownedWorker` root 退出才主动 retire scope。`ownedWorker` 的启动包含 `openclaw-worker-start-v1`、lineage FD 和 cleanup binding；因此不能把普通 Python 命令入口等同已接入完整 Worker crash 协议。[8]

上游确有“外层 owner 被 SIGKILL → IPC disconnect → 进程树死亡 → 恢复后归还容量”的真实测试，但测试对象是 OpenClaw Worker 协议，不能直接当成 AgentLoom 任意 Application 的验收结果。[9]

## 已核实的打包边界

官方 npm 安装包包含 `dist/owned-stdio-uMyyXCno.mjs`，导出使用编译后短名称，文件名有内容 hash；直接导入属于锁定构建产物的内部依赖。公开 `package.json` exports 没有对应入口。[1]

本机固定版的该 chunk，经字面量相对 import 递归只读统计，涉及 **89 个本包构建文件**；这个数字不包含通过 runtime entrypoint 名称选择的 relay/anchor、外部包内部依赖，也不是“最小可 vendor 闭包”。因此不能只复制 `owned-stdio.ts` 一个文件就声称复用了完整机制。保留完整官方包可以避免我们维护这组依赖，但不会自然减少安装体积。

## 推荐与未验证部分

- **推荐**：持久任务仍只交 DBOS。若改接内部 owner，只考虑通用 `createOwnedStdioProcess`/relay 这一层，锁定上游版本；不引入 `NodeWorkerSupervisor` 的 journal、capacity、claim、workspace。
- **推荐**：Python/Node 薄适配只负责命令、结果、取消与父死亡关联；进程树清理继续由上游执行。是否比当前公开 SDK 接法更短，应拿实际删除的代码和新增依赖评估，不能仅凭“完整机制”这个名称。
- **未验证**：内部 owner 对现有完整 Application 的替换、Node 外层入口、独立任务回收与容量恢复。此次没有新增实现或实跑。

## 补充：完整 Worker 机制如何处理取消与清理异常

本节核对的是完整 `NodeWorkerSupervisor`，不把公开 command SDK 的异常行为当成它的行为。

**启动与取消并发。** `launch()` 先登记可 abort 的 admission；`cancelTurn()` 在首次 journal await 之前，同步 abort 匹配 admission、设置活动 turn 的 `cancelled=true`（[supervisor:592–609](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.ts#L592)）。capacity claim 与 turn claim 把 `assertCurrent` 传入 journal；journal 在 transaction grant 前再次调用它（[capacity:120–132](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-capacity.ts#L120)、[journal:92–128](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-journal-worker.ts#L92)）。已经取消的 durable row 不会被 `markRunning` 覆盖为 running；更新还要匹配原 supervisor PID/start time（[kernel:645–711](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-launch-store.kernel.ts#L645)）。child 先登记物理 identity，再检查 durable state、abort 与 owner identity；开 start gate 前和发业务 descriptor 前各检查一次（[child-lifecycle:253–321](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-child-lifecycle.ts#L253)）。外部 cancel 会 join admission 的收尾，而非只返回一份 ACK（[supervisor:466–487](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.ts#L466)）。上游测试在 markRunning 交界注入 cancel/close，断言业务孙进程未产生（[startup test:289–320](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.startup.test.ts#L289)）。这是对已登记 admission/claim 的保证；源码没有“取消不存在的 launch ID 后永久拒绝未来首次 launch”的普遍 tombstone 合同。

**清理结果未确认。** observer 将 uncertain/失败转换为 `deferred` outcome；保留 active owner，不发布物理 terminal。后续 `statusOwner`/`stopChild` 尝试 `confirmExtinction()`；仍未确认就抛 `cleanup remains unconfirmed`，不会在调用体内自行添加无限轮询（[observation:40–76](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-launch-observation.ts#L40)、[child-lifecycle:493–505、609–620](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-child-lifecycle.ts#L493)）。上游测试明确断言：停止请求报错、durable state 仍为 running、available=0、替补因 capacity exhausted 被拒绝；恢复确认能力后才能收尾（[cleanup-result test:53–78](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.cleanup-result.test.ts#L53)）。

**请求等待与后台所有权分开。** 对 owned-anchor/Linux-subreaper 的恢复，默认调用方最多等 1 秒，随后返回当前 receipt，后台精确 cleanup observation 保留；指定 `awaitCleanup` 则 join 完整观察（[recovery:32–99](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor-recovery.ts#L32)）。旧 Linux owner 尚活着时持续等其完成；owner 死亡但缺少 `workerDescendantsReaped` 证据，或 macOS anchor 缺少 `workerLineageSettled`，日志告警并返回原 receipt，容量继续保留，不把重启当清理证明（[recovery:166–182、231–241](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor-recovery.ts#L166)）。

**有界错误与仍需等待的范围。** turn 的 cancel 请求/回复等待为 1+4 秒，失败则转进程 stop；relay cleanup 有 5 秒 hard deadline，超时拒绝等待结果并保留失败事实；满容量 admission 默认 10 秒后给出 capacity exhausted。以上是不同层的时限，不能承诺所有 `close()` 都固定几秒内成功。`close()` 封闭 admission 后仍 join 已接受的工作和物理观察，聚合 cleanup/reconciliation 错误；terminal 回执成功持久化后，才删除 active owner、唤醒 turn waiters和发布容量（[supervisor:642–718](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.ts#L642)、[relay deadline:198–235](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-relay-host.ts#L198)、[capacity:19、241–254](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-capacity.ts#L19)、[terminal reconciliation:293–345](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor-recovery.ts#L293)）。

**可直接借鉴的最小原则：** 取消先封住 admission/start gate，再处理已创建资源；异步边界前后重新核对当前授权；控制请求报错或有界返回，不代表物理资源已退出；未确认资源保留其原容量与清理权，后续按上游入口继续收尾；只有物理证明及终态持久化都成功，才允许补位。这里引用的是上游已有机制，没有提出新的自研阻塞状态机。

## 一手来源

1. [公开 process-runtime exports](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/plugin-sdk/process-runtime.ts#L1)；[npm exports](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/package.json#L950)。
2. [owned-stdio 通用 argv 与关闭合同](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/owned-stdio.ts#L11)。
3. [child adapter 进入 relay](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/adapters/child.ts#L172)。
4. [NodeWorkerSupervisor 构造与 launch](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.ts#L76)；[claim 与容量](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.ts#L315)。
5. [Worker 启动 transport](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-launch-transport.ts#L82)；[固定 bundle entry 解析](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-entry.ts#L6)；[OpenClaw state journal](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-journal-worker.ts#L19)。
6. [relay 父死亡与 IPC disconnect](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-relay.ts#L99)；[anchor parent-loss cleanup](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-group-anchor.ts#L665)。
7. [Linux subreaper 获取与 drain](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/linux-child-subreaper.ts#L42)；[选择 Linux native owner](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-relay-preparation.ts#L48)。
8. [普通 command 与 ownedWorker root 退出差异](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-group-anchor.ts#L650)；[worker-start IPC 协议](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/process/supervisor/service-child-group-anchor.ts#L387)。
9. [外层 owner SIGKILL 真实恢复测试](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.recovery.test.ts#L761)。
