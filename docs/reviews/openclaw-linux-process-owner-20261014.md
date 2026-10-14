# OpenClaw 进程监护 Linux 验收

2026-10-14。在用户指定的 `ssh dev` 上，固定版 OpenClaw 的公开 process SDK 接法通过了 7 项 durable Application 测试、2 项真实 Pi/MCP 测试和 3 种进程故障探针。无需改用内部 relay/anchor，也没有配置 launchd/systemd 服务。DBOS 继续负责持久队列和恢复。

测试的是 `codex/openclaw-process-owner` 当前未提交的实现，基线为 `a5364b8e4b3285a5711099f362a1dacc0e8c90e2`。传输的 wheel 中，Adapter、Node 桥、installer、job_entry、共享调用合同、executor 和 client 均与本地源文件逐字核对一致；不是只打包旧 HEAD。没有在 `lin_home` 拉取或修改仓库。

## 环境与安装

Linux x86_64，Debian GLIBC 2.28，内核 `5.4.143.bsk.8-amd64`，本机 PID 1 为 systemd。隔离环境使用 Python 3.12.15、SQLite 3.53.1、DBOS 3.2.0、Node 24.16.0、OpenClaw 2026.10.3 和 Pi SDK 1.0.0。没有修改系统 Python、全局 Node 或系统服务。

`dev` 访问 PyPI 较慢，依赖改由本机按相同锁文件下载 Linux wheel 后传输；检查了哈希、原生库 ELF 格式和远端依赖兼容性。npm 缓存按 Linux x64 填充，只传输内容缓存；远端仍通过正式 `loom runtime install execution-owner` 和 `loom runtime install pi` 安装、验证和构建，npm 自行核对 lockfile 的完整性。Pi 的真实握手通过。

Pi 首次安装报 `Cannot find module '../lib/cli.js'`：验证 PATH 直接指向 Node wheel 的二进制目录，该目录的 npm 文件不能按普通 Node tarball 的命令布局调用。最终用原始 wheel 正式安装，并在隔离 PATH 中将 Node 与 npm 命令分别链接到原生 Node 和 npm 自身的 CLI；没有修改 npm、Pi 或进程监护源码。安装路径证据保留在 `pi-install-entrypoints.json`，首次错误保留在 `pi-npm-diagnostic.log`。执行所有者 installer 始终使用 Node wheel 的公开 Python API，首次正式安装即通过。

## 结果

| 入口 | 结果 | 观察事实 |
| --- | --- | --- |
| 公开 SDK 故障探针 | 3/3 | root 正常退出、root `os._exit(17)`、顶层 Python SIGKILL；忽略 TERM 的 Node 子进程和 Python 孙进程均退出，记录的 PID、整个 PGID 和监护进程全部消失 |
| durable Application | 7/7，66.41 秒 | 正常执行、关闭失败/阻塞、取消、root 崩溃、业务回执恢复、宿主强杀后恢复；旧资源退出后才补位 |
| 真实 Pi/MCP | 2/2，18.52 秒 | 正常完成和顶层 Executor SIGKILL；实际 Pi bridge、stdio MCP、工具监听端口和监护均自动退出 |
| 最后资源检查 | 通过 | 16 个调用的监护身份均消失，16 份退出回执均确认物理退出 |

强杀探针在故障后约 0.348 秒观察到全部记录身份、进程组及监护退出。这个时间只描述本次运行，不是性能 SLA。故障测试在这些观察完成前，没有启动 replacement executor、调用 recover 或执行测试侧清理。测试的失败保护清理不能计入通过结果。

## 证据与边界

远端隔离目录：`/data00/home/linyuchi.06/agentloom-validation/openclaw-owner-20261014`。trace、PID/创建时间、EOF、SDK 清理结果和回执已取回；证据包 SHA-256 为 `6f498bfffc70630c94893660deac7f9efdb5d5409693edbe44a11a94af21f3a9`。

- [环境](../../.agentloom/validation/openclaw-owner-integration-20261014/linux-dev/artifacts/environment.json)
- [三种故障探针](../../.agentloom/validation/openclaw-owner-integration-20261014/linux-dev/artifacts/public-sdk-results.json)
- [Application 日志](../../.agentloom/validation/openclaw-owner-integration-20261014/linux-dev/artifacts/application-tests.log)
- [真实 Pi/MCP 日志](../../.agentloom/validation/openclaw-owner-integration-20261014/linux-dev/artifacts/pi-mcp-tests.log)
- [最终资源检查](../../.agentloom/validation/openclaw-owner-integration-20261014/linux-dev/artifacts/final-verification.json)
- [复现脚本](../../.agentloom/validation/openclaw-owner-integration-20261014/linux-bootstrap.sh)

本轮没有远端模型或 AnySearch 请求。Pi/MCP 使用真实 Runtime 和本地 HTTP 模型 fixture；不能称为 DeepSeek v4.1 或 NewsAgent 全日验收。产品默认仍是旧 launchd Adapter；新默认和正式 NewsAgent CLI 接线、使用新 owner 的全日业务验收尚未完成。本轮没有提交、推送或合入 main。
