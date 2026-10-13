# Pi 与 MCP 接入 Application 进程范围

2026-10-14。结论：**Pi 使用官方公开 `BashOperations.exec` 替换进程执行，MCP stdio 使用公开 `ClientSession` 接一层 AnyIO JSONL transport。两者可以加入 Application 的 OS 管理范围，不需要修改 SDK 私有实现。** Pi 的 `spawnHook` 和 MCP 的 `StdioServerParameters` 均不能直接控制是否另建 session。[Pi 公开接口][P1]、[MCP stdio 源码][M1]

这个适配只负责 Runtime 资源归属。持久队列、领取和重试仍交给 DBOS；任务完成的条件仍包括外层所有者确认资源退出。源码核对不等于正式 Pi／MCP 或 NewsAgent 已通过故障验收。[进程所有权研究](upstream-process-resource-ownership-20261014.md)

## 已核对的实际版本

研究 worktree 是 `/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof`，HEAD 为 `99f6e5d8ad4f2cd6e856a2b4acd991e9c28f30d1`。研究期间父任务正在编写未提交的 process scope 与 managed stdio 候选；本轮只读，没有改产品或运行模型。

| 组件 | 实际来源 | 一手绑定 |
| --- | --- | --- |
| Pi coding-agent 1.0.0 | 主工作区 `.venv/share/pi/bridge/node_modules/@earendil-works/pi-coding-agent` 已安装；新 worktree 的 `.venv/share/pi` 尚不存在 | npm 1.0.0 的 `gitHead=a13d35a742c6ef8462812a28fbe1d8c8b7431c32`；与 worktree package-lock 的 integrity 相同 |
| MCP 1.26.0 | 新 worktree Python 3.14 site-packages | 固定 tag commit `3d9d34552a9ab8988acf8d06e4a705bd799fc32b`；stdio、session、memory 文件与安装文件逐字一致 |
| mcpadapt 0.1.20 | 同一 site-packages | 固定 tag commit `538cd85628b555ef4ad9392b7270b52274f444d4`；`core.py` 与安装文件逐字一致 |
| AnyIO 4.11.0 | 同一 site-packages 与 uv.lock | 读取公开 process、memory/text stream、blocking portal 与 context propagation 合同 |

Pi 已安装 `bash.js` 的 SHA-256 是 `f32e87a42f66276b2eafaaf0848babf52523d37ab0fd448439586376747f03c4`。其 source map 携带的完整 `bash.ts` 与上述 gitHead 的官方源码逐字一致，源码 SHA-256 是 `de2b54d4f4bfbb844ce6d8077f63d715f7d8e80079dd9689b561e45b6679c81b`。[npm 发布 metadata][P0]、[固定 Pi 源码][P1]

## Pi 的公开扩展点足够，默认 local operations 会脱离组

官方 package root exports 包含 `createBashTool`、`createBashToolDefinition`、`BashOperations`、`createLocalBashOperations` 和 `getShellConfig`。`BashOperations.exec(command, cwd, options)` 接收 `onData`、`AbortSignal`、可选 timeout 和 env，返回 `exitCode`。这些是明确公开的扩展点，可以保留 SDK 的工具定义、schema、输出截断和展示逻辑，只替换底层进程执行。[root exports][P2]、[operations 接口][P1]

但 `createLocalBashOperations()` 没有 detached 参数，其内部 `createLocalShellOperations()` 的关键配置固定为：

```typescript
spawn(shell, args, {
  cwd,
  detached: process.platform !== "win32",
  env: env ?? getShellEnv(),
  // 其余 stdio 配置见固定源码
});
```

POSIX 上 Node 的 `detached=true` 会让子进程成为新进程组和 session 的组长。因此，仅让 Python PiBridge 加入 Application 的组，SDK 自带 bash 仍会脱离。[Pi spawn 源码][P1]、[Node detached 合同][N1]

`BashSpawnHook` 仅接收并返回 command、cwd、env，无法改 `detached`。官方示例也只演示这三个值的变换。让 hook 注入一个环境变量，不会改变 local operations 的硬编码行为。[hook 类型][P1]、[官方 hook 示例][P3]

当前 worktree 的 [tools.ts](../../src/runtimes/pi/bridge/tools.ts) 用官方 definition 检查 schema；真正执行时转入 [capture.ts](../../src/runtimes/pi/bridge/capture.ts)。后者已经使用 `operations.exec` 包装，但内部继续调用 `createLocalBashOperations().exec()`。最小接线是在这一处选用受管理的 exec；外层授权、dispatch、raw capture、settle 和 receipt 可以继续使用原入口，避免增加另一套工具协议。

受管理 exec 可用公开 `getShellConfig()` 保留 shell 选择，使用 Node 官方 `child_process.spawn`，显式 `detached=false`，并保留 SDK 传入的 cwd、env、onData、signal 和 timeout。它不应自行调用 SDK 私有的 `waitForChildProcess()`、`killProcessTree()` 或内部模块绝对路径。这些 helper 不在 root exports 中，package exports 也没有公布任意内部 subpath。[root exports][P2]、[SDK shell helper][P4]、[Node spawn][N1]

## 工具 timeout 必须和整个任务停止分开

同组模式下，PiBridge、bash、MCP 与 Python Application 共用受管理的 PGID。工具取消若执行“取得真实 PGID，再 killpg”，会同时终止 Application 与其他工具。Pi SDK 自带 helper 则默认把 child PID 当 PGID，先向负 PID 发 KILL，再退回单 PID；它也不适用于继承父组的自定义 exec。[SDK helper][P4]、[MCP POSIX helper][M2]

最小合同应当明确：

- 单个工具的 Abort／timeout 只向该工具的具体 PID 或已确认属于该工具的后代发送停止信号，不向 Application PGID 发送信号。
- Node `child.kill()` 返回成功，只表示信号发送成功。Node `exit` 事件也不表示 stdio 全部关闭；`close` 与管道关闭仍是另一个事件。[Node 子进程事件][N1]
- 后代仍运行、仍持有输出管道或资源归属无法确定时，保留 uncertain／原错误，交给外层 Application 所有者完成整次任务收尾。不得在未知状态继续 Agent 并写“工具已完整结束”。
- 整次 invocation 的 OS 所有者可以终止完整任务组。它确认退出后，上游队列才允许结束本次占用或开始替代 Runtime。

这是一项待验证的适配合同，不是 Node 单 PID kill 自动提供的保证。官方 Pi 的 `waitForChildProcess` 在 root exit 后使用 100ms 输出 idle grace，随后可关闭孙进程仍持有的流。当前 capture 因此把 bash 的输出覆盖记为 unknown。换底层 operations 后必须保留这种不确定性，或者取得确实的 EOF 证据；不能悄悄把既有 `complete=null` 改为 true。[Pi wait helper][P5]、[当前 capture](../../src/runtimes/pi/bridge/capture.ts)

若需要让单个工具的任意 detached 子树都独立完成强制退出，同时保持 Application 继续运行，单个 macOS job process group 不提供这一层隔离。不能再用一套未经验证的 tree scan 暗中扩大合同；本轮先让受管理工具留在任务组，并将无法确认的工具收尾交给整次 invocation。

## MCP stdio 需要 transport adapter，不能只改 spawn 参数

MCP 1.26.0 的 `StdioServerParameters` 只有 command、args、env、cwd、encoding、encoding_error_handler。公开 `stdio_client(server, errlog)` 没有 process factory 或 `start_new_session` 参数。内部 POSIX 分支固定 `anyio.open_process(..., start_new_session=True)`。[stdio 公开参数及实现][M1]

更关键的是，SDK 的 POSIX cleanup 会 `os.getpgid(pid)`，然后 TERM／KILL 整个组。若仅 monkeypatch `_create_platform_compatible_process()`，让它启动同组子进程，却保留原 cleanup，就会把 Application 父组当清理目标。因此，这里必须完整替换 stdio 的进程与字节 transport 接线，不修改私有函数，也不调用原 `_terminate_process_tree()`。[POSIX cleanup][M2]

官方 `ClientSession` 本身可以继续用。它公开接受 `MemoryObjectReceiveStream[SessionMessage | Exception]` 与 `MemoryObjectSendStream[SessionMessage]`，负责初始化、请求关联、超时、通知和 `call_tool`。`process.stdout` 的 bytes stream 不能直接传给它。[ClientSession 构造][M3]、[官方 client 示例][M4]

最小 adapter 由公开接口拼接：

```text
AnyIO open_process（不另建 session）
   stdout → TextReceiveStream → JSONRPCMessage → SessionMessage → memory stream
   stdin  ← JSONRPCMessage.model_dump_json + 换行 ← memory stream
ClientSession(read, write) → initialize() → list_tools() / call_tool()
```

SDK 的 stdio 源码正是这个消息转换模式；MCP stdio 规范要求每条 JSON-RPC 消息用换行分隔，stdout 不得混入普通日志，stderr 可接日志。协议层继续由官方 JSONRPC 类型和 ClientSession 处理，这层只把公开 stream 接到受管理进程。[SDK transport 模式][M1]、[stdio 协议][M5]

需要保留 command／args 的直接执行、默认安全环境与显式 env 合并、cwd、encoding、错误处理、stdout 的跨 chunk 分帧、stderr 分离以及 `read_timeout_seconds` 的 timedelta／None 合同。清理时先关闭 stdin，给服务器退出机会，再对具体 PID TERM／KILL 并等待。取消中的清理需要 AnyIO shield；所有字节与 memory stream 也要在启动失败、协议错误和取消时关闭。[SDK 环境和关闭顺序][M1]、[AnyIO subprocess][A1]

stdio 规范顺序和单个 process.wait 可以证明直属 MCP server 收尾，不能单独证明它启动的孙进程退出。后者仍由外层任务范围处理。当前 SDK 的 graceful stdin exit 和 same-group tree 测试可以参考，但它们按 SDK 自建组的合同测试，没有覆盖继承 Application 父组的 adapter。[官方 stdio 测试][M6]

## mcpadapt 和 HTTP 分支可以保留

mcpadapt 0.1.20 的 `mcptools` 接受 StdioServerParameters 或 HTTP transport dict，没有公开注册自定义 stdio process factory 的接口。其主要动作是打开 transport、构造 ClientSession、initialize 和 list_tools。受管理 stdio 分支可以返回同样的 `(session, tools)`；外层 [AgentLoomMCPAdapter](../../src/integrations/mcp/adapter.py) 和同步 tool binding 不必更换。[mcpadapt 固定源码][MA1]

当前 [MCP client](../../src/integrations/mcp/client.py) 已使用 AnyIO blocking portal。只有“受管理 scope ＋ stdio 参数”需要选择新的 transport；普通 stdio、HTTP／SSE 可以继续用现有 `mcptools`。AnySearch 目前走的 HTTP 分支不涉及本机 server 进程，scope 适配不得改变 key 顺序、HTTP 头或 failover 规则。

新的 adapter 不应接管 JSON-RPC request ID、tool schema 验证、HTTP reconnect 或 MCP tool retry。这些已有 SDK／业务层职责与本次进程归属无关。

## ContextVar 应由任务入口绑定，跨语言显式传递

process scope 应由完整 Application 的专用 OS job 入口绑定，不在普通 CLI、SDK import 或任意用户 env 出现时绑定。它至少记录已核实的任务组身份；发现当前进程不属于预期范围，应拒绝受管理运行，不能回退为默认 detached 启动。

Python `ContextVar` 不会跨进程或跨到 Node。PiBridge 启动时应读取可信 scope，按 scope 选择 `start_new_session=False`，并把运行模式显式传给 Node bridge。环境标志只用于桥接模式选择，不能作为资源授权凭据；必须先清除继承来的同名值，再由可信绑定生成。[当前 Pi environment](../../src/runtimes/pi/environment.py)、[当前 Pi transport](../../src/runtimes/pi/transport.py)

AnyIO 文档规定 worker-thread／async 调用复制 context；本项目 Pi 平台回调也使用 `copy_context().run`。这可以让 scope 传到 MCP portal 内，但不会把 portal 任务改动反传给同步 caller，普通 `ThreadPoolExecutor.submit()` 也不应被假设自动继承。应实际验证 `_MCPConnection._serve()` 在绑定 scope 下选择了受管理 stdio；scope 丢失时默认启动 detached 子进程会破坏合同。[AnyIO context propagation][A2]、[Pi callback 接线](../../src/runtimes/pi/transport.py)

scope 是本次资源归属，不是队列容量、任务领取或租约。外层启动单例检查和旧资源核验必须先于 `DBOS.launch()`；这层 Runtime adapter 不负责恢复调度。[当前持久执行规格](../specs/durable-worker-open-source-engine-integration-v2.md)

## 仍需正式运行证明

已有局部 Application 探针没有证明以下完整接线。后续验证应经过正式 Application／Runtime 入口，保留 PID、创建时间、PGID 与退出事件：

| 入口与故障 | 必须观察的行为 |
| --- | --- |
| Pi 正式 bridge ＋公开 BashOperations | Node bridge、shell 及受管理后代属于同一任务范围；官方 schema、授权、capture 和 receipt 继续成立 |
| bash timeout／Abort，另一工具仍在执行 | 工具清理没有误杀 Python Application 父组；未知子树收尾不能被当作成功 |
| shell 正常 exit 后孙进程继续写 stdout | 输出覆盖按实际 EOF／unknown 处理，避免 idle grace 被当完整输出 |
| MCP stdio initialize／list_tools／call_tool | ClientSession 真实成功，不只验证启动 PID；env、encoding 和 timeout 合同保持 |
| MCP 无效帧、跨 chunk 换行、启动失败、取消 | 原错误可见，stream／portal／直属 server 收尾完成，父组仍在预期状态 |
| Application／PiBridge 突然退出 | 外层成熟 OS 所有者清理同组资源，新 Runtime 不先于实际退出补位 |
| 故意 setsid 的子进程 | 保留原 negative 或明确不支持，不能伪装成同组已覆盖 |

只有这些接线通过，才继续实际 Pi／smolagents runner、业务原子提交权限和完整 News 日期。研究报告及候选 adapter 不替代这些验收。

## 一手来源

[P0]: https://registry.npmjs.org/@earendil-works%2Fpi-coding-agent/1.0.0
[P1]: https://github.com/earendil-works/pi/blob/a13d35a742c6ef8462812a28fbe1d8c8b7431c32/packages/coding-agent/src/core/tools/bash.ts
[P2]: https://github.com/earendil-works/pi/blob/a13d35a742c6ef8462812a28fbe1d8c8b7431c32/packages/coding-agent/src/index.ts
[P3]: https://github.com/earendil-works/pi/blob/a13d35a742c6ef8462812a28fbe1d8c8b7431c32/packages/coding-agent/examples/extensions/bash-spawn-hook.ts
[P4]: https://github.com/earendil-works/pi/blob/a13d35a742c6ef8462812a28fbe1d8c8b7431c32/packages/coding-agent/src/utils/shell.ts
[P5]: https://github.com/earendil-works/pi/blob/a13d35a742c6ef8462812a28fbe1d8c8b7431c32/packages/coding-agent/src/utils/child-process.ts
[N1]: https://nodejs.org/docs/latest-v22.x/api/child_process.html
[M1]: https://github.com/modelcontextprotocol/python-sdk/blob/3d9d34552a9ab8988acf8d06e4a705bd799fc32b/src/mcp/client/stdio/__init__.py
[M2]: https://github.com/modelcontextprotocol/python-sdk/blob/3d9d34552a9ab8988acf8d06e4a705bd799fc32b/src/mcp/os/posix/utilities.py
[M3]: https://github.com/modelcontextprotocol/python-sdk/blob/3d9d34552a9ab8988acf8d06e4a705bd799fc32b/src/mcp/client/session.py
[M4]: https://github.com/modelcontextprotocol/python-sdk/blob/3d9d34552a9ab8988acf8d06e4a705bd799fc32b/README.md#writing-mcp-clients
[M5]: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#stdio
[M6]: https://github.com/modelcontextprotocol/python-sdk/blob/3d9d34552a9ab8988acf8d06e4a705bd799fc32b/tests/client/test_stdio.py
[MA1]: https://github.com/grll/mcpadapt/blob/538cd85628b555ef4ad9392b7270b52274f444d4/src/mcpadapt/core.py
[A1]: https://github.com/agronholm/anyio/blob/4.11.0/docs/subprocesses.rst
[A2]: https://github.com/agronholm/anyio/blob/4.11.0/docs/threads.rst#context-propagation
