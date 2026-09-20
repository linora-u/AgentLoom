# 13: 交付干净 Pi-only 与 smol 安装路径

**What to build:** 新环境可安装并运行 Pi-only AgentLoom 而不依赖 smol，同时原推荐 smol 安装方式仍能运行已有 YAML。

**Blocked by:** 04：迁移 smol 专属实现，保持旧应用行为；09：Pi 调通原生读取、平台工具与 Goal

**Status:** draft — 拆分待确认，尚未发布 GitHub；不代表已开工或已完成。

**Required:** Yes — 本票属于最终交付必做项。

**Session:** 独立发行 session；可与 10、11、12 并行

## Scope

Python packaging、安装入口、CLI 失败路径和 CI。消费 Pi session 所有者维护的 bridge 产物，不共同修改其 Node manifest/lock。

## Acceptance criteria

- [ ] 从锁定依赖和明确 Node 版本生成可复现 bridge/发行物，不依赖源码 checkout、全局 pi 命令或运行中临时 npm install。
- [ ] Pi-only 干净环境不存在直接或 instrumentation 等间接 smol 依赖，能在 checkout 外运行真实 Pi Application。
- [ ] smol profile 在另一干净环境实际安装并启动旧 YAML，不能仅以本机已有 smol 或 import 成功证明兼容。
- [ ] 安装器和 CI 显式选择相应 extra/profile；给出真实验证过的命令，不能假设 all-groups 自动选择 extras。
- [ ] 无 smol 环境覆盖 CLI help、provider/child failure 和缺依赖错误，普通失败分类不再硬导入 smol。
- [ ] 发行包包含 bridge JavaScript、依赖及所需 SDK assets，记录版本、lock/hash 和构建来源。
- [ ] 明确本票只是发行机制初验；14 必须用包含 10/12 的最终候选重新构建、重新执行干净安装验证。

## Handoff

交付两类安装命令、构建流程和证据；Python manifest/lock 的生成只有本票负责人执行。

实施遵循已生成的 Pi 接入规格、并行开发计划及本轮票据执行索引。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。

