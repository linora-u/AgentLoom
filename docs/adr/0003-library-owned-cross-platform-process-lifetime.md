---
status: accepted
---

# 默认本机执行由第三方库管理进程生命周期

2026-10-14，用户确认默认 macOS、Linux 均须支持 pip 安装后本机运行，使用本地持久状态并预留远程后端；不要求用户配置 launchd/systemd 或另行部署数据库、broker、服务，允许成熟第三方库自行启动和管理子进程。顶层执行 Python 被 SIGKILL 后，库自行启动的独立监护进程可以继续存活，但必须自动清理全部受管理任务后代并自行退出；本验收不同时强杀该监护进程。管理范围包含 Pi/Node、MCP、工具进程和孙进程，任务不得主动逃离，候选库必须有实际覆盖这一范围的依据，不能用重启后阻塞或手动清理代替自动回收。

这项部署与故障边界替代 ADR-0001 所记载的当前 macOS 专用执行方式作为后续方案的目标。持久队列、恢复和进程监护均优先采用上游公开接口，AgentLoom 不继续新增自研调度器或 supervisor。

用户后续确认允许锁定上游版本、复用必要的内部进程模块。OpenClaw 公开 SDK 接法已分别取得 macOS 和 Linux 实测证据；这些结果说明原型可运行，不意味着其安装和接入复杂度已满足最终要求。用户随后要求停止代码修改、重新讨论简化方案，并明确继续借鉴 OpenClaw 的机制，清理异常也按上游已有机制核对；不继续开放选库，也不把此答复解释为已确认完整 SDK 或完整 Worker 平台的接入形态。本 ADR 已确认的部署与故障合同继续有效。

未提交的原型采用固定的 OpenClaw 公开 process SDK，加独立 Node 控制桥将 Python 宿主的管道 EOF 转为库的取消请求。进程组终止与退出确认由 SDK 承担，没有复制内部 relay/anchor，也没有新增队列或租约调度。macOS 和 Linux 的 Application、真实 Pi/MCP 及进程故障探针均通过；Linux 证据来自用户指定的 `ssh dev`。未提交分支随后已写入默认入口切换和 NewsAgent 接线，macOS 默认入口与 controller/CLI 30 项、真实 Pi/MCP 2 项通过；新后端完整新闻日期尚未实跑，用户已要求停止继续开发。实现合同见 [OpenClaw 接入规格](../specs/openclaw-process-owner-integration.md)，复评问题见 [设计讨论](../research/process-owner-simplification-design-tree-20261014.md)。
