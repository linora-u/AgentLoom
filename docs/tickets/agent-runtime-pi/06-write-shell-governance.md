# 06: 补齐文件修改和 Shell 的治理与保护

**What to build:** 文件修改和 Shell 委托调用在执行前落实既有权限与文件保护，并在执行后保留本次查询或命令范围内可追溯的产物。

**Blocked by:** 04：收拢自研 smol Agent 及其基础工具，保持旧应用行为；05：通过统一治理执行一次原生读取并持久记录

**Status:** ready-for-agent — 04+05 已集成并验证；从 `refs/agentloom/ticket04-05-integrated` 解析准确 SHA 后可新建 worktree。先读 [04 保护调用点](04-implementation.md) 和 [05 接口交接](05-implementation.md)。本票尚未实施。

**Required:** Yes — 本票属于最终交付必做项。

**Unlocks:** 10

**Session:** 沿用工具治理 session；可以与 07、08、09 并行

## Scope

只做治理层、操作 metadata 和已有策略提取，不实现第二套 Node 工具包装；真 Pi 写入/Shell 端到端归 10。先等 04 的基础工具迁移及 05 的只读治理集成，才能接收迁移后保护调用点，避免搬迁和策略提取同时改文件。

**Edit boundary:** 延续 05 的治理文件修改权，并接收 04 明确移交的保护文件/调用点。只读缓存、搜索算法、Shell 会话和输出展示仍归 smol；已有公共权限实现继续复用。与 09 并行时双方只通过冻结合同协作，不共改实现文件。

## Acceptance criteria

- [ ] 根据 04 移交清单提取已有的公共文件/Shell 保护，smol 与 native 路径消费同一公共规则；公共层不反向导入 smol 基础工具，Pi 不调用自研 smol 的读写/Shell 算法。
- [ ] 文件操作按逻辑能力和实际参数匹配授权，不因 Pi 名称不同而绕过既有规则；必要备份先于修改。
- [ ] Shell 命令策略、适用执行隔离和搜索范围限制在真实执行入口生效，不能以 top-level path 检查代替。
- [ ] 拒绝写入、拒绝命令及搜索排除测试通过，禁止的副作用不存在。
- [ ] 保留受授权查询限额约束的原始结果或完整产物引用，明确 coverage、query limit 与展示截断的差别。
- [ ] 错误、取消、被拒绝调用不产生成功证据；不能把重查得到的新数据冒充第一次执行的原文。
- [ ] 生产 Gateway 的 executor-fixture 测试与原有文件/Shell 回归通过，不修改 09 正在实现的 Pi 调用代码。

## Handoff

把操作映射、治理接口及测试样例交给 10；与 09 共用的合同需变更时先交协调者统一升级。

实施遵循 Pi 接入规格及本目录最新的 [工具归属](tool-ownership.md)、[执行索引](README.md) 与 [worktree 计划](worktree-plan.md)。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。
