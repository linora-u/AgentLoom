# 13: 交付干净 Pi-only 与 smol 安装路径

**What to build:** 新环境可安装并运行 Pi-only AgentLoom 而不依赖 smol，同时原推荐 smol 安装方式仍能运行已有 YAML。

**Blocked by:** 04：收拢自研 smol Agent 及其基础工具，保持旧应用行为；09：Pi 调通原生读取、平台工具与 Goal

**Status:** planned — 等待前置任务集成并验证，尚未实施。

**Required:** Yes — 本票属于最终交付必做项。

**Unlocks:** 14

**Session:** 独立发行 session；可与 10、11、12 并行

## Scope

Python packaging、安装入口、CLI 失败路径和 CI。消费 Pi session 所有者维护的 bridge 产物，不共同修改其 Node manifest/lock。

**Edit boundary:** 独占 Python manifest/lock、安装器、CLI 安装/失败路径及发行测试；Pi SDK/Node 依赖修改交 Pi 链所有者。公共 registry/readiness 的变化交协调者，安装范围以本票前置已支持的能力为准。

本票以 09 已开放的无工具、只读和平台调用验证安装，不等待或假设 10/12 已完成。维护者已明确 SDK 按固定版本自动下载，不把 SDK 源码塞进仓库或 Python 发行包。已有 `uv run loom install-runtime pi` 安装入口消费 bridge 源码、schema 与 npm lock，在 Pi 目录下载依赖并构建；本票复用它，验证发行资源完整性和独立环境安装。10/12 若改变构建入口或资源合同，先经协调者同步，14 再使用最终提交重建验收。

按 [工具归属](tool-ownership.md) 验证依赖所有权：04 已完成基础工具迁移，08 已通过 09 集成。专业工具依赖按实际选择处理，不把 smol 基础工具包或 SDK 验证程序当作 Pi 生产资源。

## Acceptance criteria

- [ ] 从锁定依赖和明确 Node 版本生成可复现 bridge/发行物，不依赖源码 checkout、全局 pi 命令或运行中临时 npm install。
- [ ] Pi-only 干净环境不存在直接或 instrumentation 等间接 smol 依赖，能在 checkout 外运行真实 Pi Application。
- [ ] Pi-only 环境实际构造并执行选定的平台/专业工具，不导入 smol Tool、基础工具或私有进程注册表；同时验证 09 所用专业工具的查询资源随发行物可用，未选工具不被默认加载。
- [ ] smol profile 在另一干净环境实际安装并启动旧 YAML，不能仅以本机已有 smol 或 import 成功证明兼容。
- [ ] 安装器和 CI 显式选择相应 extra/profile；给出真实验证过的命令，不能假设 all-groups 自动选择 extras。
- [ ] 无 smol 环境覆盖 CLI help、provider/child failure 和缺依赖错误，普通失败分类不再硬导入 smol。
- [ ] 发行包包含自有 bridge 源码、schema 和锁文件；安装入口下载固定版本 SDK、构建 JavaScript 并校验所需 assets。SDK、node_modules 与本地构建产物不随 Python 包重复分发，记录版本、lock/hash 和构建来源。
- [ ] 明确本票只是发行机制初验；14 必须用包含 10/12 的最终候选重新构建、重新执行干净安装验证。

## Handoff

交付两类安装命令、构建流程和证据；Python manifest/lock 的生成只有本票负责人执行。

实施遵循 Pi 接入规格及本目录最新的 [工具归属](tool-ownership.md)、[执行索引](README.md) 与 [worktree 计划](worktree-plan.md)。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。
