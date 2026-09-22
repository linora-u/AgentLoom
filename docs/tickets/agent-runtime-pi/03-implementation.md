# 03：公共合同、兼容基线与并行交接

状态：完成。完整回归 **4225 passed、1 skipped**；真实 Pi SDK **13 passed**。

## 实现与交付范围

03 收拢了后续 worktree 共用的合同和装配规则，保持旧 smol Application 可运行。自研基础工具、平台工具、可选专业工具分别登记；公共层按实际选择构造工具与 manifest，native 应用不继承 smol 基础工具。完整 smol 搬迁、生产 native host 和生产 Pi runtime 分别由 04、05/06、07 后续实现。

当前只实施 03。main HEAD 保持 `c697b24f602e71d56c9aa2c532e6079c0db5aa9d`，成果留在主工作区未提交、未暂存 Changes；03 未创建新 worktree。冻结通过单独的内容快照完成，不移动 main，也不纳入无关研究、临时目录或私有配置。

## 可复现版本

| 内容 | Revision / 引用 |
| --- | --- |
| 03 起始基线，包含已交付 01/02 | `7f5417be8dd807eba4c7cba32db1ce1664cabde2` |
| 最终受检实现 | `9f08aa09ce3784d9c55eb82b01571256ded36ecf` |
| 受检 src tree | `eb86e649e32ab68e1f6105c6c46928e00c5a0d77` |
| 受检 tests tree | `9b37f58e44e5a083340e33f19b14300cd83b43cd` |
| 最终交接引用 | `refs/agentloom/ticket03-frozen`；完成本票时固定，后续不移动 |
| 后续集成分支 | `codex/pi-integration`；完成本票时指向上述冻结版本 |

交接版本只在受检实现上补充完成状态和验收文档，src/tests tree 必须相同。创建 04/05/07/08 worktree 前先用 `git rev-parse refs/agentloom/ticket03-frozen` 取得准确 SHA，记录为该票的起点。具体冻结 SHA 与 main 内容核对记录保存在仓库同级 `AgentLoom-worktrees/evidence/t03/freeze.json`。

## 12 条验收对应证据

以下编号对应 [03 票据](03-freeze-integration-contracts.md) 中的验收顺序。命令、最终结果和限制见 [机器可读记录](03-validation.json)。

| 条件 | 实现与验证 |
| --- | --- |
| 1. 合并 01/02 并保持真实旧应用 | 起始快照保留 01/02；`test_smol_compatibility_application.py` 经 `execute_app` 跑真实 smol 循环、文件副作用、Todo、Goal 和 Shell 关闭。完整测试包含 CI 的记忆脚本 |
| 2. 归属 manifest 与独立登记 | `adapters/smolagents/tool_catalog.py`、`tools/platform_catalog.py`、`optional_catalog.py` 分区；catalog 边界测试在新解释器确认不加载执行实现 |
| 3. 旧 YAML / 选择兼容 | 实际 Application 验证全局旧 core 默认不注入 native、显式无工具为空、显式旧工具无映射拒绝、重复工具拒绝；固定参数测试验证真实写入目标与可见 schema |
| 4. 公共环境与资源关闭 | `runtime/subprocess_env.py` 共用环境规则；Application/Invocation 调用 `runtime/resources.py`。真实两个 Shell session 与两个后台进程验证按实例关闭且不串用，原 Shell 测试纳入完整回归 |
| 5. 混合文件分工 | [03 文件交接](03-file-ownership.md) 列明读取缓存/写前保护、Shell 政策/算法、Skill/parser、prompt、测试和包入口；04→06 明确移交修改权 |
| 6. 完整公共调用 | `RuntimeDefinition.tool_manifest` 提供冻结快照，名称/schema/数量不符拒绝；旧 Gateway 保持可用。RuntimeModelSelection 包含实际生效的私有 headers；真实 Application 验证四层优先级、大小写交错和自定义 profile |
| 7. 通用 API 与 Pi IPC 分离 | AgentRuntime 接口保持独立；`adapters/pi/protocol.py` 和 `bridge-v1.schema.json` 定义 Pi v1。协议回归验证 handshake、空 checkpoint、无效版本/重复键、Run 身份及 schema 一致性；transport/待处理请求实现留在 07 |
| 8. native 准备与结算 | `runtime/native_tools.py` 定义请求、最终授权、提交确认、journal 与恢复动作；授权绑定身份、提供方、cwd、manifest 和最终输入，ToolCallRecord 终态未改变 |
| 9. 共享可执行合同示例 | `test_native_tool_contract.py` 验证参数修正、拒绝、先备份、fsync 后确认、两个崩溃窗口、取消、重放及授权篡改；示例明确不代替 05/06 生产 host |
| 10. 注册/readiness 交接 | [合同第 6 节](03-contracts.md#6-注册与版本变更) 明确 07/09/10/12 逐步开放能力；当前真实 builtin registry 不注册 Pi，不支持能力仍在构造前拒绝 |
| 11. 所有者/兼容与版本 | 文件交接清单、[worktree 计划](worktree-plan.md) 和各票 Edit boundary 共同划定所有权；公共变更由协调者串行落地，旧 YAML 兼容不在最终清理时删除 |
| 12. SDK 验证迁移与重放 | 验证程序在 `tests/pi_sdk_compatibility/`，原 RESULT.json 与起始基线逐字节相同；真实发布 SDK 的 TypeScript 检查与 13 项测试通过，源码 hash 与受检候选相同 |

## 审查与修复

### Standards

初审 0 项硬性违例，1 项设计判断：公共 Gateway 按 smol 工具名推导操作。已将 operation、command_parameter 放入各登记分区，Gateway 只投影 metadata；Todo 和后台控制标为 control。独立复审 0 项硬性违例、0 项未解决判断项。

### Spec

初审 3 项均通过失败用例复现并修复：headers 大小写交错导致层级覆盖反转；manifest 缺少完整公共入口；fixture 以收到的批准自我校验。修复后 92 项定向用例通过，Spec 审查者另跑 9 项相关回归通过，复审无未解决规格偏差。

两条审查轴分别检查，不用某一轴通过抵消另一轴的问题；原始审查计数和复审记录见仓库同级证据目录的 `reviews.json`。

## 验证范围与限制

- 完整回归命令：`.venv/bin/python -m pytest tests applications/memory_feature_validation/scripts -q --tb=short`。包括 `.github/workflows/tests.yml` 指定的测试范围，并覆盖该脚本目录的其他测试。
- SDK 命令：在 `tests/pi_sdk_compatibility` 执行 `npm run verify`。使用锁定的 Pi SDK `0.79.4`、Node `v25.9.0`，没有全局 pi 命令依赖；仅模型响应为确定性替身。
- 类型检查：固定 mypy `2.3.1`，对候选 28 个新增/修改源码模块与基线中对应的 17 个已有模块比较。基线 59 项、候选 15 项错误，按文件与诊断内容核对无新增；剩余是旧 Gateway schema 推断、factory 签名/列表、配置缓存属性、Shell 可空 session 标注。这里不声明全库类型检查通过。
- 原 SDK RESULT.json SHA-256：`b90e39a811eaeaac9caba7ba5a5f154cb73795eab3420f4c29d69edf23a02abc`；重放源码 SHA-256：`b1fb67a087a6cccdda4c40304a6077ef34f95a6b35a97063e36bb58d9273a04e`。
- 真实远程 provider 检查为 NOT-RUN；03 不宣称已接通生产 Pi、生产授权/journal 或完整恢复。现有第三方 Pydantic 警告单独保留在完整日志中。

原始日志位于仓库同级 `AgentLoom-worktrees/evidence/t03/`。`full-suite-1.log` 保留首次失败，`review-regressions-red.log` 保留审查问题的失败用例；最终通过记录不覆盖这些历史证据。

## 后续开工

03 完成并固定交接引用后，04、05、07、08 可从同一准确 SHA 创建四个 worktree。06 等 04+05；09 等 05+07+08；其余按每票 Blocked by 解锁。本票不启动任何下游实现。主工作区仍在 main，最终大功能集成、main Changes 交付与 worktree 清理由 14 收口。
