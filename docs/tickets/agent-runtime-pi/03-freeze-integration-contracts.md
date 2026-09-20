# 03: 汇合验证结果，冻结三路开发的公共基线

**What to build:** 将已经验证的 Pi 接入能力与兼容运行契约合成同一个绿色基线，使后续独立 session 能按同一协议交付可互通的实现。

**Blocked by:** 01：验证发布版 Pi 的 Hook 与会话恢复接入；02：扩展运行契约，同时保持旧 smol 应用可运行

**Status:** draft — 拆分待确认，尚未发布 GitHub；不代表已开工或已完成。

**Required:** Yes — 本票属于最终交付必做项。

**Session:** 协调 session 执行；不可与未完成的 01/02 抢先推进

## Scope

原 C0 的收口门禁。提交公共合同、共享 fixture 和过渡接线；生产 native 治理仍由 05/06 实现。

## Acceptance criteria

- [ ] 集成 01/02 的实际提交，原 smol Application 和公共合约测试通过，记录准确的冻结 commit。
- [ ] 固定 runtime options、模型投影、工具 manifest、逻辑工具与 native 名称映射、结果状态和能力声明；后续分支不得各自添加不兼容私有字段。
- [ ] 冻结双向 JSONL 请求、响应、事件及 handshake、run、snapshot、cancel、close 的身份与错误语义。
- [ ] 冻结 native prepare/settle：最终参数、执行提供方、授权关联、持久提交确认和不确定结果；ToolCallRecord 保持既有终态，执行 journal 独立表达未结算状态。
- [ ] 通过共享 fixture 表达参数修正、拒绝、写前保护顺序、双日志窗口及取消；不将 fixture 通过算作真实 native 生产管线完成。
- [ ] 为后续无工具 Pi 入口确定注册/readiness 接线方式；未实现能力仍拒绝，smol 保持可用。
- [ ] 登记各任务的模块所有者、smol 旧实现和相关测试移交清单、协议版本及合约变更流程。

## Handoff

04、05、07、08 都必须从这个已集成、已验证的 commit 开始；共享入口变更由协调 session 串行处理。

实施遵循已生成的 Pi 接入规格、并行开发计划及本轮票据执行索引。只认已集成、已验证的依赖提交；不要自行跳过阻塞任务，也不要从其他 worktree 复制未提交改动。

