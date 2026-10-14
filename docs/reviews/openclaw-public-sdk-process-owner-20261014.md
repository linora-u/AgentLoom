# OpenClaw 公开 SDK 接入验收

2026-10-14。开发分支 `codex/openclaw-process-owner`，基线 `a5364b8e4b3285a5711099f362a1dacc0e8c90e2`。

**已完成 macOS 和 Linux 的首版接入及故障验收。** DBOS 继续管理队列、阶段容量和恢复，NewsAgent 业务代码未改。新的 `OpenClawJobs` 显式注入现有 executor；默认值仍是 launchd，新后端的完整新闻日期尚未验收。本轮未提交、推送或合入 main。[Linux 独立验收记录](openclaw-linux-process-owner-20261014.md)

## 实现

完整 Application 继续通过原来的授权握手启动。独立 Node 桥持有 OpenClaw 的公开 command scope，Python Executor 持有控制管道唯一写端。Python 被 SIGKILL 后，管道 EOF 请求 SDK 取消；进程组终止和退出确认由 SDK 承担，完成后 Node 桥退出。

采用 `openclaw@2026.10.3` 的 `openclaw/plugin-sdk/process-runtime`，使用 `runCommandWithTimeout`、`withCommandProcessScope` 和公开结果字段。没有复制内部 relay/anchor，没有新增队列、租约、重试或后代扫描器。SDK scope 收尾成功且 `cleanup` 为 `normal/cooperative/forced`，才能发布资源退出回执；业务结果和资源退出分开保存。

Pi/MCP/tool 的现有组继承接线保持不变，Application 仍是独立组首进程。任意自定义工具另起 session 不属于此保证；任务必须遵守受管理进程组合同。这条公开接法没有启用内部 Linux subreaper；Linux 已单独通过相同的 Application、真实 Pi/MCP 和进程故障探针，结果见独立记录。

实现见 [SDK Adapter](../../src/execution/durable/openclaw_job.py)、[Node 桥](../../src/execution/durable/openclaw_bridge.mjs) 和 [接入规格](../specs/openclaw-process-owner-integration.md)。

## 验收结果

| 入口 | 结果 | 证明范围 |
| --- | --- | --- |
| 公开 SDK 的隔离进程探针 | 3 个场景通过 | 正常 root 退出、root 崩溃、顶层 Python SIGKILL；忽略 TERM 的 Node 子进程和 Python 孙进程自动退出，组消失，监护退出 |
| 新 Adapter 的 durable Application | 7 项通过 | 真实 Application 执行、关闭失败、关闭阻塞后取消、root 崩溃、回执恢复、宿主强杀后恢复及补位不重叠 |
| 新 Adapter 的真实 Pi/MCP | 2 项通过 | 正常完成和顶层 Executor SIGKILL；真实 Pi bridge、stdio MCP、工具监听端口和 Node 监护均自动退出；强杀后没有启动 replacement/recover 来辅助清理 |
| 原 launchd 路径及持久提交 | 11 项通过 | 共用调用合同重构未破坏原 Adapter 和 DBOS 提交入口 |
| 正式 npm 安装及 wheel | 通过 | 提交的 lockfile 安装、公开导出预检、重复安装复用、wheel 包含桥和锁文件，以及固定 Node 依赖 |

9 项新 Adapter 测试已使用正式安装资产通过，最终代码的日志见 [final-owned-scope-tests.log](../../.agentloom/validation/openclaw-owner-integration-20261014/final-owned-scope-tests.log)。此次验证也包含严格退出回执字段检查，以及在 host factory 导入和构造之前绑定任务组。旧路径回归见 [legacy-tests.log](../../.agentloom/validation/openclaw-owner-integration-20261014/legacy-tests.log)，任务组绑定调整后的正常执行和业务回执恢复复验见 [final-legacy-scope-tests.log](../../.agentloom/validation/openclaw-owner-integration-20261014/final-legacy-scope-tests.log)。隔离探针结果见 [public-sdk-results.json](../../.agentloom/validation/openclaw-owner-integration-20261014/public-sdk-results.json)。

探针和 Application 测试均未调用远端模型或 AnySearch。真实 Pi/MCP 使用本地 HTTP 模型 fixture。不能把这些结果称为 DeepSeek v4.1、AnySearch 或 NewsAgent 全日验收。

## 安装与启动观察

`AgentLoom[durable]` 固定依赖 `nodejs-wheel-binaries==24.16.0`，通过公开 Python Interface 获取 Node；不修改全局 Node。`loom runtime install execution-owner` 安装固定 npm lockfile，禁用 install scripts，不启动 Gateway，也不配置 launchd/systemd。资产按平台和锁文件内容发布到虚拟环境内不可变目录。

正式发布包的压缩大小为 102,928,946 字节。当前保留官方完整 npm 发布包，避免自行裁剪内部依赖；只调用公开 process SDK。安装和 wheel 校验见 [owner-install.log](../../.agentloom/validation/openclaw-owner-integration-20261014/owner-install.log) 和 [wheel-verification.json](../../.agentloom/validation/openclaw-owner-integration-20261014/wheel-verification.json)。

曾出现一个本机启动异常：把正式 SDK 文件路径作为 Node CLI 的参数，即使该参数未被程序使用，Node 仍在几毫秒内收到 SIGKILL；隔离资产路径、普通路径和只传资产目录的对照正常。SDK 主体尚未执行，因此不能归因于 SDK 清理实现，也尚未确定信号发送方。

当前接法从资产目录解析公开包入口，不把解析后的 SDK 文件地址作为启动参数；预检进程另有独立进程组。正式安装的 9 项测试随后通过。对照证据保留在 [unused-path-argv-matrix.json](../../.agentloom/validation/openclaw-owner-integration-20261014/unused-path-argv-matrix.json) 和 [sdk-import-matrix.json](../../.agentloom/validation/openclaw-owner-integration-20261014/sdk-import-matrix.json)。这证明当前接法通过，不能宣称该本机信号来源已经查明。

## 剩余工作

Linux 同入口验收已在 `ssh dev` 完成：7 项 Application、2 项真实 Pi/MCP、3 种故障探针通过。后续完成新默认和正式 NewsAgent CLI 接线，再用 DeepSeek v4.1、AnySearch 和默认 20/30/20 跑一个真实完整日期。首版仍是单机多进程，不把远程存储 Interface 外推为多机进程回收保证。

历史 SQLite 522 的 actor 仍未锁定；此次接入及绿色测试不能替代原事故归因，见 [522 一手证据](../research/sqlite-522-trace-primary-sources.md)。
