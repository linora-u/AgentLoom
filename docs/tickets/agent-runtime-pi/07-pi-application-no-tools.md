# 07: 从现有应用入口运行一个无工具 Pi Agent

**What to build:** 用户通过现有 YAML 和 execute_app/CLI 启动一个明确无工具、无 Goal、无需恢复的 Pi Agent，使用原生模型接口获得正确的 ApplicationRunResult。

**Blocked by:** 03：冻结基座与平台边界，交付可并行的公共基线

**Status:** ready-for-agent — 03 已验收；从 `refs/agentloom/ticket03-frozen` 解析准确 SHA 后可新建独立 worktree。本票尚未实施。

**Required:** Yes — 本票属于最终交付必做项。

**Unlocks:** 09

**Session:** Pi session；可与 04、05、08 并行

## Scope

最小生产 tracer bullet：受控 Node bridge、原生 provider、无工具会话、基础事件与模型阶段取消。只声明本票已经验证的能力。

**Edit boundary:** 独占 `src/adapters/pi/` 的生产实现、Node manifest/lock 与 Pi 专属测试，消费 [03 合同](03-contracts.md)。公共注册、readiness、模型投影接线由协调者串行完成；不为 Pi 新建另一套多 Agent 编排或长期记忆服务。

遵循 [工具归属](tool-ownership.md)。Pi adapter 管理自己的 SDK/进程/会话，不把自研 smol 的工具实现、会话状态或 Tool 类型作为构造前提；生产 bridge 与 01 的测试程序分别维护。

## Acceptance criteria

- [ ] 使用锁定的真实发布 SDK 和原生 AgentSession；仅模型响应可以确定性替换，不能 mock runtime 或 monkeypatch 主入口绕过注册。
- [ ] 现有模型 profile 转换为 Pi 原生请求，支持项准确生效，不支持参数/协议和缺依赖明确失败。
- [ ] 由协调 session 在本票完成前纳入 registry/readiness 小型接线；配置经真实公共校验，调用经 execute_app 返回正常 Run receipt。
- [ ] 明确无工具的 manifest 传入 Pi，不自动发现基础工具、扩展、用户目录配置或 smol Todo/final_answer。
- [ ] 无工具应用经过真实 YAML 装配后仍不加载历史 smol 默认基础工具；不通过复制自研 read/write/Shell 实现来满足 Pi 入口。
- [ ] 模型调用期间的取消、进程退出和协议损坏能终结请求并清理本票创建的进程，CLI stdout 不被协议或诊断污染。
- [ ] 无 Goal 情形的 Stop/终态映射正确；不得提前宣告工具、Worker、Goal 或恢复可用。
- [ ] 建立独立 bridge manifest、完整 lock 与构建入口，后续发行任务消费这一产物而不接管或重写其锁文件。

## Handoff

交付可独立演示的无工具 Pi Application 与精确版本；09 在此基线上添加工具闭环，13 消费 bridge 产物。

实施遵循 Pi 接入规格及本目录最新的 [工具归属](tool-ownership.md)、[执行索引](README.md) 与 [worktree 计划](worktree-plan.md)。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。
