---
status: superseded
---

# OpenClaw 进程监护接入

2026-10-14。本文保留初版公开 SDK 原型的接入记录。后续实施合同以[默认受管并发执行规格](default-managed-worker-execution-v3.md)为准：普通用户只配置并发数，框架自动装配执行模块，不提供 owner 选择配置。

以下章节中的 launchd 默认值及 `jobs=` 显式接入描述属于初版快照。当前未提交分支已切换 durable 默认入口并接入 NewsAgent，随后按用户要求暂停开发；有限 map 的默认模块接线、授权竞态、清理未确认处理和新机制完整日期仍待验收。已有 macOS、Linux 测试不能外推为这些后续工作已经完成。

## 接入范围

```text
DBOS Executor（Python）
    │ 控制管道，死亡时自动 EOF
    ▼
Node 桥（独立进程，公开 SDK scope）
    │ runCommandWithTimeout
    ▼
完整 Application → Pi / MCP / 工具及其后代
```

Node 桥只转换启动参数、停止请求、日志和退出回执。进程组控制、TERM/KILL 升级和退出确认交给 OpenClaw 公开的 `runCommandWithTimeout()` 与 `withCommandProcessScope()`。调用设置 `killProcessTree: true`、`requireProcessTreeExtinction: true`，检查 `cleanup` 并等待 scope 完整收尾。桥不领取任务，不维护队列、租约或业务重试。默认使用本地 SQLite，现有远程存储 Interface 保留。

采用固定上游 `openclaw@2026.10.3`，源码 commit 为 `59cb44796b0f1f0daa0e37af8b1495abb45d9bc1`。直接使用 `openclaw/plugin-sdk/process-runtime` 的公开入口，不复制或导入内部 relay/anchor。用户已允许锁定版本、复用必要的内部模块；当前公开接法在 macOS 和 Linux 均有实测证据，暂不需要切换到内部 Node Worker/subreaper 路径。升级须重新通过故障验收。

## 启动与退出合同

每次调用先建立独立监护，再启动 Application。Application 发布实际 PID、创建时间和 PGID，并等待 Executor 授权后进入业务代码。所有权握手失败时，已经启动的资源仍须交给上游清理。

Executor 持有控制管道唯一写端。Node 桥、Application、Pi、MCP 和工具进程不得继承这个写端。Executor 被 SIGKILL 后，Node 桥收到 EOF，要求上游停止任务，并等清理完成后退出；不依赖 Executor 的 `finally` 或下一次启动。

Application 的逻辑结果和资源退出分别记录。`result.json` 继续表示业务结果；只有上游确认完整管理范围退出后，才写 `exited.json`。DBOS step 及取消收尾必须等待这个事实。清理不确定时保留错误和资源事实，不能写成功退出或释放容量。

公开命令执行路径创建独立任务进程组，Application 仍满足 `PID == PGID`，现有可信组绑定无需放宽。Pi/MCP/tool 继续继承被确认的任务范围。普通 Run 不能通过任意环境变量接管当前 shell 的共享组。

## 恢复与迁移

DBOS 继续负责三阶段的共享 20/30/20 容量和持久恢复。有限 `map()` 继续使用单次调用上限。进程桥不增加 claim、lease、retry 或 sweep 调度器。

重启时，先核对旧调用的上游退出回执，再启动 DBOS。旧业务授权按现有 host 的 `revoke/reconcile` 合同处理；已提交的结果通过业务回执恢复，避免重复模型请求。新旧调用的资源范围不得重叠。

旧 launchd 调用必须由旧 Adapter 排空。资源描述记录实际 owner 类型；不能把旧描述直接当成 OpenClaw 的描述，更不能删除描述来绕过恢复检查。已排空的历史结果可继续读取。

## 安装

无需用户配置 launchd/systemd 或另起服务。固定上游要求 Node `>=24.16.0 <25 || >=26.1.0`。`AgentLoom[durable]` 增加固定的 `nodejs-wheel-binaries==24.16.0`，通过其公开 Python Interface 获得兼容 Node。现有全局 Node 不需要升级。

`loom runtime install execution-owner` 通过提交的 npm lockfile 安装 SDK，禁用 install scripts，验证公开导出后发布到虚拟环境内的不可变版本目录。执行器启动时预检资产；业务任务中不下载依赖。SDK npm 包较大，官方压缩包为 102,928,946 字节；当前保留官方完整发布包以避免自行裁剪内部依赖，但只调用公开 process SDK，不启动 Gateway。

当前通过 `execution.executor(..., jobs=OpenClawJobs(...))` 显式接入。`jobs` 是可信宿主依赖，不能来自模型输入。跨平台故障验收已通过；后续切换默认值并完成 NewsAgent CLI 接线，再以真实全日业务验证新路径。

## 验收入口

使用公开 `Application`、`BoundWorker` 和 durable executor 入口。资源是否退出通过实际 OS 进程身份观察，不把 DBOS 状态、Future 完成或私有 PID 表当作退出证据。探针不调用付费模型或搜索。

| 场景 | 必须观察到的行为 |
| --- | --- |
| 正常完成，包括 root 先退出、后代仍持有管道 | 保存结果，清理全部任务后代后才释放容量 |
| Application 关闭回调抛错或阻塞，随后取消 | 保留原始错误，上游完成资源清理后才补位 |
| Application `os._exit()` | 独立所有者自动清理 Pi/MCP/tool 后代，不等待业务进程收尾 |
| 顶层 Executor SIGKILL | 不启动新宿主，旧后代和监护进程仍自动退出 |
| 清理后恢复 | 先处理旧授权及业务回执，再启动新调用；无资源重叠 |
| 正式 Pi/MCP 接线 | 实际 Node bridge、stdio transport 和工具进程均落在同一受管理范围 |

macOS 和 Linux 分别执行故障验收，两边均通过 7 项 durable Application、2 项真实 Pi/MCP 测试和 3 种进程探针；macOS 另有 11 项旧路径及持久提交回归通过。Linux 使用用户指定的 `ssh dev`，确认 16 个调用的监护身份消失及资源退出回执，见 [Linux 验收](../reviews/openclaw-linux-process-owner-20261014.md)。Pi 使用本地 HTTP 模型 fixture，没有调用远端模型或 AnySearch。后续以 DeepSeek v4.1、AnySearch 和默认 20/30/20 跑一个真实完整日期，业务输入、Prompt、筛选、校验和计分规则保持一致。

本方案不能证明历史 SQLite 522 的具体 actor 或根因已经闭合；该结论仍受原现场证据限制。

证据见 [OpenCode/OpenClaw 比较](../research/opencode-openclaw-execution-comparison-20261014.md) 和 [上游所有者强杀测试](https://github.com/openclaw/openclaw/blob/59cb44796b0f1f0daa0e37af8b1495abb45d9bc1/src/node-host/node-worker-supervisor.recovery.test.ts#L761)。
