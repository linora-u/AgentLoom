# 10 实施与验收记录

10 在 `codex/pi-t10-write-shell` 独立 worktree 开发，初始基线 `2ec1412c`，消费已完成的 06 与 09。官方 Pi SDK 固定为 **0.79.4**；本票没有复制上游源码、替换 Pi 的 Agent loop 或复用 smol 基础执行器。

## 已交付的职责边界

- Pi 执行官方 `read`、`edit`、`write`、`bash`，由对应工具名或 `pi_read/pi_edit/pi_write/pi_bash` toolset 显式选择。未选择的工具、未验证的 grep/find/ls、专业 Markdown 写工具不自动开放。
- AgentLoom 沿用公共 Hook、最终参数校验、权限、文件历史、Shell 规则、持久 journal 和 ContextRef。`read/write/edit/bash` 的逻辑政策名称分别为 `read_file/write_file/edit_file/shell_tool`，只用于平台治理，不调用这些 smol 执行器。
- adapter 负责 SDK schema 的精确核对、协议转换、原始结果捕获和实例清理。官方文件操作包装只验证实际路径与已授权路径一致，并复制本次已读数据；编辑和 diff 算法来自 SDK。图片类型判断使用锁定发布包的内部 MIME helper，升级 SDK 时须重新验证该路径。

原生调用沿 prepare → dispatch → 官方 execute → capture → settle。prepare 执行 Hook 并保存批准；dispatch 紧邻实际 SDK 执行，重新检查文件版本和政策再消费批准。文件读完后才允许编辑/覆盖，不能把尚未执行的同批 read 当作已完成读取。写前备份仍由 06 的公共保护产生。

混合平台/原生批次仍沿 09 的消息预处理机制：平台回调可能在原生 execute 前完成，因此依赖前一步结果的工具应分轮调用并等待回执；本票不声称模型同批次中任意跨类型依赖都按文字顺序执行。

## 产物、覆盖范围与失败语义

bridge 在首次执行时保存完整官方 result，以及原始 read 查询或 SDK 返回的 stdout/stderr；结果不再重复读取文件或重跑 Shell。read 保留原始 offset/limit 边界，查询范围外内容不进入该产物。捕获文件在实例私有目录，使用固定 authorization ID 文件名、0600 权限、摘要及非符号链接检查；wire 只传捕获引用及摘要。缺少文件、字段/null 或校验失败都会使 Application 失败，已派发调用保留 uncertain。

公共 Host 原子提交 journal、原文、身份、最终输入和 provenance 后才确认。大型结果使用已有 ContextEngine 的压缩与存储机制；显式产物保存不受“跳过写工具自动压缩”的默认规则阻止。返回的 SDK details 也受尺寸约束，不能把巨大 diff/patch 再带回 JSONL。ContextRef 存储不可用时，显示退化为持久 journal 引用，原文仍保存；不重做已发生的副作用。

**Bash 的 EOF 完整性为 unknown。** SDK 0.79.4 不公开 pipe EOF，父 Shell 退出后可能关闭后台后代持有的静默管道。平台保留所有已经收集的原始流，coverage=`captured_stream`，并记录 limitation；不会因 exit 0 伪称完整查询，也不会为未知完整性的捕获生成可信记忆成功证据。普通非零退出保留 partial 原文；超时、取消、退出码缺失都是 uncertain，Application 失败，模型不能继续将其当成功使用。read/write/edit 的成功捕获与显示截断分别记录，ContextRef 的预览不冒充原文。

工具取消使用实例继承 token 及公共进程清理入口，覆盖普通 detached/reparented Shell 后代。清理只针对本实例管理范围，不宣称能约束主动擦除标记的任意外部守护进程。当前没有接入 Pi OS sandbox；要求尚不支持的 sandbox、Shell 受保护查询映射会在副作用前拒绝。

## 安装与运行

```sh
uv run --locked loom install-runtime pi
```

uv 提供 Python 安装入口，npm ci 按锁文件下载 Node SDK 到 `src/adapters/pi/bridge/node_modules/` 并构建 AgentLoom bridge。SDK 和传递依赖版本/完整性由 package-lock 固定；`node_modules`、`dist`、安装标记不进入 Git，仓库根参考 `pi/` 不参与生产运行。首次需要 Node 22.19+、npm 和网络；同版本重复安装复用已有结果。安装指纹包含所有 bridge TypeScript 文件、schema 与锁，新增 helper 不会继续使用旧构建。已同步 13 的发行配置、readiness 与 CLI 失败分类修复。Pi JSONL 因强制捕获引用升级为 **v2**，Python/Node/schema/包资源同时更新；公共 native tool contract 保持 **1**。schema 通过版本通配打包，旧 v1 消息明确拒绝；14 仍须对包含 12 的最终树执行发行验收。

```yaml
name: pi_writer
agent_runtime: pi
checkpoint: {enabled: false}
toolsets: []
tools:
  - name: read
  - name: edit
  - name: write
  - name: bash
  - name: loom_retrieve_context
shell_settings:
  allowed_commands: [printf]
  sandbox: {enabled: false}
description: 按授权修改指定文件。
workflow: 先读取指定文件并等待结果，再修改；核对工具回执后交付。
```

## 验证

测试以公开 `execute_app`/CLI 调真实已安装 SDK，只有远端模型 HTTP 服务使用确定性 fixture。独立文件、备份、子进程和 journal 是验收依据，模型最后一句成功不算证明。新增大 edit（原始 diff/patch 超过 8 MiB）、9 MiB Shell 输出、受限 read、Hook 最终参数、并发写入竞态、后台 Shell 输出以及各个副作用故障窗口。

真实 provider 使用 `powerful` Chat 与 `responses_powerful`，每轮 15 个场景，共 30 个 Application，原始证据保存在项目外 `AgentLoom-validation/t10/`。私有模型配置不提交。首轮 29/30：模型在 Hook 改写成功后又重复 write，第二次被 read-before-write 保护拦截；首次实际效果正确，失败记录保留。后续提示明确接受 Hook 改写后的成功回执，不允许重复副作用。

取消测试曾发现 CLI 错误分类冷导入 LiteLLM 导致联网读取价格表，Pi 与 Shell 本身已被回收；独立线程栈见外部验证记录。该共享 CLI 修复由并行 13 单独提交并在 10 复验，未靠放宽超时隐藏问题。

最终集成候选 `9efc5303` 已纳入 main 的 11/13，并通过真实模型 **30/30**；累计 **122 次尝试、120 次通过、2 次历史失败**。另一历史失败是模型将绝对路径 data_clear 拼成 data_clean，未命中预期的未读写入保护；误写测试文件已清理。验收应用现使用精确相对路径、显式目录边界和输入路径 oracle，该场景两模型复验 2/2。专项 53 项、ContextEngine/公共合同 28 项通过；Python 类型检查 12 文件、0 error/0 warning，TypeScript 构建通过。仅修正旧测试参数假设的后继提交 `ef15789b` 完整回归记录为 **4464 passed、1 skipped、1 failed**（686.75 秒，23 warnings）；唯一失败是已有记忆锁测试子进程晚于 10 秒就绪期限，原样复验所在模块 7/7 通过；命令、日志与历史记录见 [10-validation.json](10-validation.json)。

首次全量为 4464 passed、1 skipped、1 failed：旧文件历史测试给全部工具写死 `file_path`，无法覆盖 Pi 官方的 `path`。生产路径合同和实际 Pi 备份均正确；测试改为逐个使用 catalog 声明参数，并加强为准确路径/步骤的一次备份断言。专项复验 14/14，独立 Standards 复核无剩余问题；未修改生产代码。

回归限制：第二轮唯一失败位于未修改的 `test_optional_memory_review_runtime.py` 跨进程锁用例，持锁子进程未在 10 秒内写出就绪文件。失败目录最后保留了锁文件和 `held`，表明进程随后成功取得锁；同一测试首轮全量通过，第二轮后所在模块在相同代码/环境下 7/7 通过（5.74 秒）。冷导入 reviewer 会加载 LiteLLM 并访问远端价格元数据，但未捕获失败当次的线程栈，不能确定这是该次延迟的唯一原因。保留全量失败与复验日志，不声称第二轮全量全绿；14 最终验收仍须检查子进程启动稳定性。

验收对应关系：

| AC | 公开入口与独立依据 |
| --- | --- |
| 1、2、7：官方执行、归属、显式能力 | `test_write_shell_application.py` 的 create/edit/bash；`test_tools_application.py` 的选择/manifest；不支持能力在应用预检拒绝 |
| 3：保护、最终输入、备份 | Hook final write、mutation policy、write race；核对文件内容、备份、journal 和未发生的副作用 |
| 4：原文与检索 | selected-range read、大 Shell、大 edit；检索 ContextRef 对比首次执行产物，超大结果不重执行 |
| 5：证据与失败 | identity、partial/unknown coverage、missing capture；检查 ToolCallRecord/journal 与不产生的可信成功证据 |
| 6：取消与清理 | managed detached descendants、无退出码、timeout，以及 `test_process_lifecycle.py` 的真实 CLI/进程检查 |

真实 provider 场景的逐次结果保存在验证 JSON；两次历史失败及修正原因保留，不以最终通过覆盖历史。

## Standards

独立审查发现省略 capture 仍可成功提交。Pi 协议现要求必填非空，runtime 拒绝 wire 内联原文，新增省略字段/null 与删除文件的真实故障覆盖；复核 0 项剩余发现。

## Spec

独立审查发现大 edit 结果越过 wire 上限、Bash 成功被误判为完整输出。完整官方结果已走捕获文件，Host 返回有界 ContextRef/产物引用；Bash 明确 unknown 并阻止虚假完整证据。大 edit 默认跳过自动压缩的边界也通过公开用例修正，复核 0 项剩余发现。

## 交接与集成

12 可消费真实副作用及 journal：`tests/pi_test/test_write_shell_faults.py` 在 dispatch 前、settle 前、持久 commit 后确认前及捕获损坏处注入 OS 故障。executing/uncertain 不自动重跑；commit 已持久保存却丢确认时，Application 失败但记录仍可用于恢复对账。Pi snapshot/resume 仍由 12 实现，本票不开放。

11、13 已完成并纳入本票。main 已从 `7ec14ae2` 快进至 `ab48014e`，保留所有阶段提交；冻结入口 `refs/agentloom/ticket10-frozen` 指向该交付提交。main 上执行 `uv run --locked --all-extras --all-groups loom install-runtime pi` 成功，确认导入来自 main 的 `src`、SDK 0.79.4、Pi JSONL v2 和 native contract 1。本次 `pi-t10-write-shell/AgentLoom` worktree 已删除，实现分支、冻结引用和项目外证据保留；SDK 安装产物不进入 Git。源码和测试与受检 `ef15789b` 一致，后续仅补交付文档。原有 `codex/`、`temp/`、参考 `pi/` 及两个 stash 保留，未推送远端。

12 现在可从当前 main 创建新的 session/worktree；14 在 12 完成并集成后串行执行，仍需检查已记录的记忆子进程启动稳定性。
