# Pi 并行开发与 main 合入计划

更新：2026-09-20。配套：[核心规格](../../specs/agent-runtime-pi-integration.md)、[执行索引](README.md)、[工具归属](tool-ownership.md)。本文件使用 01–14 票号；旧 C0/S1/G1/P1 宽阶段不再作为另一套排期依据。

## 1. 当前基线

01/02/03 已完成。01/02 的历史提交 45ddbfdc、42662ed6、8d63be5f 保留；03 受检实现为 `9f08aa09ce3784d9c55eb82b01571256ded36ecf`，冻结引用 `refs/agentloom/ticket03-frozen` 为 `9e19d18285f2d2ea8e7b7d11c26e315760d153a3`。此前 main 基线为 `9d5a88915efbd402d0f2697318125933fc1500e0`。2026-09-20 按维护者最新要求，main 已快进到 `da4a049d`，纳入 04/05/07/08 的全部已验收改动并保留分阶段提交；源码、测试和交接文档已正式进入分支历史。

07 交付时，`codex/pi-integration` 更新到 `0b5c4f4ed32244ef97b107361dcb45dbe2be149e`，包含 03、05、07；07 固定引用为 `refs/agentloom/ticket07-frozen`（`eca311eeca103dc6cd64eef959428e8b01eb22b2`）。04 交付时 integration 包含 04+05+07，历史冻结入口为 `refs/agentloom/ticket04-05-integrated`。08 已完成，见 [实施与验证](08-implementation.md)；共同冻结入口 `refs/agentloom/ticket08-frozen` 包含 04/05/07/08。**新 worktree 基于提交创建，不会自动获得 main 的未提交 Changes。** 06 已在独立 worktree 完成并正式交付 main，见 [06 交接](06-implementation.md)；冻结入口 `refs/agentloom/ticket06-frozen`。09 已在 `codex/pi-t09-tools` 完成，与 main 的 06 及目录清理集成后，全量 4408 passed、1 skipped，真实模型 40/40，见 [09 交接](09-implementation.md)。冻结入口 `refs/agentloom/ticket09-frozen`。10、11、13 可从包含该引用的 main 分别新建 worktree 并行；12 必须等 10，14 必须等 11+12+13。领取前重新解析 integration，不能把此处记录的交付 SHA 当成永远不变的分支头。

03 记录的完整回归为 4225 passed、1 skipped，SDK 为 13 passed；逐项证据见 [实现报告](03-implementation.md) 和 [验证记录](03-validation.json)。历史报告中的 main Changes 状态保留为当时的交付记录，不代表现在的 Git 状态。已有工作区的本次核对快照如下；领取或清理前重新检查，不把表内路径存在当作任务待开发。

| 票据 | 已有分支 | 本次核对到的 worktree |
| --- | --- | --- |
| 04 | `codex/pi-t04-smol` | 已验收并清理 t04；6 个阶段提交、冻结引用和外部证据保留 |
| 05 | `codex/pi-t05-governance` | 已验收并清理原 t05 工作区；提交保留，见 [05 交接](05-implementation.md) |
| 06 | `codex/pi-t06-governance` | 已验收；交付后清理本次 t06，源码阶段提交与失败记录保留 |
| 07 | `codex/pi-t07-runtime` | 已交付 main Changes 并清理 t07；6 个增量提交及冻结引用保留，见 [07 交接](07-implementation.md) |
| 08 | `codex/pi-t08-platform` | 已验收交付后清理本次 t08；阶段提交、冻结引用和外部证据保留，见 [08 交接](08-implementation.md) |
| 09 | `codex/pi-t09-tools` | 已验收；正式交付后清理本次 t09，分支、冻结引用与项目外证据保留，见 [09 交接](09-implementation.md) |

这是工作区登记，不是完成声明。重新领取前运行 `git worktree list` 并核对原 session；不得新建另一份同票工作区抢改，也不重置已有分支。冻结引用是必须包含的最低基线；可使用经检查的后继提交，后续任务还必须包含各自已验收的前置。

不使用 git add . 纳入既有 codex/、temp/、参考 pi/、私有配置或无关研究文件。凭证、用户 memory DB、运行数据和既有 stash 均不属于迁移清理范围。

## 2. 串行门禁与并行窗口

| 阶段 | 可执行票据 | 必须已集成的前置 |
| --- | --- | --- |
| 串行准备，已完成 | 03 | 01、02，以及最新工具归属修订 |
| 03 后的首个并行窗口 | 04 / 05 / 07 / 08；已完成票不重开 | 包含 03 的已核对基线 |
| 治理续接，已完成 | 06 | 04、05 |
| Pi 工具续接，已完成 | 09 | 05、07、08 |
| 当前可并行：应用/工具/发行 | 10 / 11 / 13 | 分别为 06+09 / 09 / 04+09，均已满足 |
| Pi 恢复 | 12 | 10，可与未结束的 11/13 并行 |
| 串行收口 | 14 | 11、12、13 |

这些是按前置解锁的窗口，不要求每个横向阶段一起结束。例如 06 与 09 可并行，11 也不必等 10。全部依赖图见 README。

保留 04→06 依赖的原因：04 搬迁自研 smol 的文件/Shell 实现；06 会抽取其中的平台保护策略并修改调用点。必须先形成稳定布局，再提取策略，避免两个 worktree 修改或删除同一批文件。05 只改公共 native 读取治理，不依赖这次搬迁，仍可与 04 并行。

## 3. 03 已交付的公共基线

- 根据实际调用者登记基座基础工具、平台工具、可选专业工具；拆分 catalog 的登记所有权，保留公共只读聚合。
- 兼容装配通过旧 smol Application、显式无工具 native fixture 和非法选择用例验证；新后端不自动收到 smol 基础工具。
- 将已有公共消费者需要的进程环境辅助入口和资源关闭接线从 smol 工具细节中分离。基础工具算法不在这里整体搬迁。
- 冻结完整调用合同、模型投影、工具 manifest、native prepare/settle、错误/终态及 capability；Pi JSONL 是其中一个 adapter 的桥接协议，不要求其他基座使用同样的进程形式。
- 模型投影包含所选 profile 与有效请求 headers 的优先级；凭证不进入公开元数据、日志或指纹。
- 原始参数、变换后严格校验、一次授权、持久结算与 uncertain 状态有共享 fixture。ToolCallRecord 的既有终态不变，未结算状态由独立 journal 表达。
- 逐文件登记混合模块和测试的唯一所有者，尤其是 search 辅助文件、基础 file_ops 与报告/大纲、Todo 与 Goal、SkillCatalog 与原生激活。
- 整理 01 的验证程序到测试目录并重放，保留原始结果；不把 PoC 当作正式 Pi adapter。

03 的模型映射与 SDK 接入结论复用 01 的发布物验证；不得仅依赖本机上游 checkout。冻结以实际测试通过的提交为准，不能只写合同文档。

## 4. 分支与环境

新建 worktree 可放在仓库外同级 AgentLoom-worktrees/tNN，也可使用 Codex 管理目录；分支使用 codex/pi-tNN-主题。已有路径保持不动。04/05/07/08 的基线必须包含 03；其他票从已包含全部前置的已验证 integration 提交开始。

每个 worktree 独立配置 Python/Node 依赖与运行目录。验证 agentloom 实际导入路径；测试用默认配置来自示例，不复制用户凭证到仓库。所有本地锁定包使用可复现安装；不依赖主工作区已装的 smol 或 node_modules。

一个 session 可连续处理同一链，例如 07→09→10→12，但每票开始前必须取得包含其全部前置的已验证提交。已完成票保留历史；08 已完成，空闲 session 按解锁状态领取尚未完成且未被占用的后续票，不为人员安排伪造技术依赖。

### 一个协调 session，按需创建实现 worktree

| session | 按顺序领取 | 工作边界 |
| --- | --- | --- |
| 协调 | 03 → 逐票集成与公共接线 → 14 | 发布冻结 SHA、维护共享合同、核验依赖完成；不在实现者的 worktree 同时编辑 |
| smol | 04；09 完成后可领取 11 | 04 的基础工具迁移交接完毕后，再转协作/记忆验收 |
| 治理 | 05 → 等 04 → 06 | 05 的读取路径与 06 的写入/Shell 保护按序开发 |
| Pi | 07 → 等 05/08 → 09 → 等 06 → 10 → 12 | 同一条 Pi adapter/bridge 链不可拆成同时修改的分支 |
| 平台工具 | 08；04/09 完成后可领取 13 | 平台工具完成后转发行，不接管 Pi 的 Node lock |

同一个 session 可以在上一票完成后接新票；每票仍记录单独的基线、提交和验收。表中“等”表示实际阻塞，不因代码已写完但尚未集成而提前开始。资源有限时减少并行 session 即可，不改变 Blocked by。

## 5. 文件唯一所有权与移交

路径仅用于分工定位；已冻结的逐文件/函数清单见 [03 文件交接](03-file-ownership.md)。未列明的共享文件先由协调者决定归属，不得自行跨目录批量替换。

| 所有者 | 独占范围 | 接线与限制 |
| --- | --- | --- |
| 03，完成后协调者接收 | 公共 agent_runtime/agent/factory/invocation、Application 定义/校验/readiness/lifecycle、配置、catalog 聚合、公共合同 fixture | 先做最小公共依赖拆分；以后各票接线仍在当票完成前串行集成 |
| 04 | adapters/smolagents、自研基础 read/write/edit/list_directory、grep/glob、Shell/后台任务、Todo、smol prompt 与私有状态；对应整文件测试 | 从原 tools 目录迁出指定实现；不移动 Markdown/大纲/AST/LSP、平台记忆或 Goal；不改 Gateway |
| 05，随后 06 | Tool Gateway、native journal 实现、Hook/授权与证据、ContextRef/产物接线及对应治理测试 | 05 不修改 04 正在搬的文件；06 等 04/05 合入后接收保护提取与调用点 |
| 08 | 平台 Goal、memory/history、ContextRef 工具入口、SkillCatalog/提案/激活入口、AST/LSP/大纲/Markdown、MCP、平台/专业 manifest | 不改 smol 基础工具和 Todo；不改公共 Gateway；共享 helper 按 03 划定边界复用 |
| 07→09→10→12 | adapters/pi、独立 Node manifest/lock/build、Pi 专属测试 | 不改 Python lock、smol adapter 或公共治理；09 消费 05，不与 06 共改治理实现 |
| 11 | 混合应用与独立 oracle、协作/记忆验收 | 公共编排接线由协调者完成；不改 Pi bridge 或治理代码 |
| 13 | Python manifest/lock、installer、CLI 安装/错误路径、CI 与发行检查 | 消费 Pi bridge 产物；不共同改 Node lock，不搬 04/08 的文件 |
| 14 | 已完成分支的集成清理、最终验收与交付记录 | 不补前票遗漏的大功能，不删除仍有消费者的兼容接口 |

重点处理：

- 原 tools/file_ops 中的基础读写归 04，Markdown 和大纲归 08；原 tools/search 中 grep/glob 归 04，AST/LSP 与查询资源归 08。不能把两个目录整体交给任一方。
- 原 tools/todo 与 runtime/todo 属于自研 Agent，归 04；tools/goal 与应用 Goal 归公共平台，08 只处理工具入口。
- 读取去重等基座优化随 04 迁移；写前保护、文件历史和 Shell 公共政策的行为先保留，06 再按清单提取共用部分并接回 smol。公共层不能新增对 smol 工具包的反向依赖。
- Hook 的进程环境辅助依赖、Application 对 Shell 注册表的直接调用由 03 先建立公共接线；04 实现正确的实例/Run 清理，避免两个任务共同修改 lifecycle。
- 所有 catalog 聚合、旧 YAML 默认解释和注册/readiness 补丁交协调者串行合入。04 与 08 只提交自己的登记分区，不同时编辑全局工具表。
- 03 需登记旧测试和内部入口的移交；04/08 不通过删除仍有消费者的装饰器/别名获得“无依赖”。最终清理由 14 做，旧 YAML 支持保留。

共享文件的接线流程：实现者提交本票的接线需求、参数/能力变化与验收用例，协调者在 integration 上串行落地公共补丁；实现分支同步该提交后验证实际入口，才可标为完成。04/08 若需要修改 smol 对平台工具的兼容转换，也走该流程，不能两路同时修改 smol adapter 的公共构造入口。接线工作计入提出需求的票据，不另设一个允许长期欠账的任务。

一次共享文件变更只由一个提交承载。协调者先公布该提交，再让相关 worktree 同步；实现者不得在自己的分支重复实现同一补丁。若发现新依赖会使某张票无法独立验收，先更新该票及 README 的 Blocked by，再调整排期，不能把未完成接线藏进“最后集成”。

03 放行材料已逐项核对：

- [x] 冻结 SHA 包含当前 01/02 成果、最新票据与通过验证的公共基线。
- [x] 04/08 的登记入口和混合文件归属已分开，明确哪些文件可各自编辑、哪些交协调者。
- [x] 05/07 能消费同一版本的 runtime、模型投影、manifest 与 prepare/settle 合同，无须自行重设计。
- [x] 资源关闭与 Hook 环境依赖已接到公共合同，smol 原有行为实测通过。
- [x] 每路的验收入口、受保护的兼容行为和不支持能力明确；未完成项不能标成“后面一起处理”。

### 新 session 直接使用的领取说明

01–05、07 不重开，08 继续已有工作区。后续新 session 使用下面的共用模板。表中的建议目录只用于尚未创建的工作区，创建前核对依赖已验收且已进入起始提交。

> 实现本目录 NN 号票据，只做这一票。读取 README、tool-ownership、03-contracts、03-file-ownership、worktree-plan 和本票的最新修订。核对该票是否已有 session/worktree，并保证只有一个修改者。验证基线 SHA 已包含全部 Blocked by 的已验收提交；没有工作区时才从该 SHA 创建。仅修改本票拥有的文件；共享入口由协调者串行接线，本票必须在真实入口验证后才算完成。返回基线 SHA、成果 SHA、变更文件、验收结果及移交项。不要自行交付 main 或开发下一票。

| NN | 建议分支 | 建议 worktree（仓库同级） | 创建前必须包含 |
| --- | --- | --- | --- |
| 06 | `codex/pi-t06-protection` | `AgentLoom-worktrees/t06` | 04+05 及保护调用点移交 |
| 09 | `codex/pi-t09-tools` | `AgentLoom-worktrees/t09` | 05+07+08 |
| 10 | `codex/pi-t10-effects` | `AgentLoom-worktrees/t10` | 06+09 |
| 11 | `codex/pi-t11-mixed` | `AgentLoom-worktrees/t11` | 09 |
| 12 | `codex/pi-t12-recovery` | `AgentLoom-worktrees/t12` | 10 |
| 13 | `codex/pi-t13-packaging` | `AgentLoom-worktrees/t13` | 04+09 |
| 14 | `codex/pi-t14-integration` | `AgentLoom-worktrees/t14` | 11+12+13 及全部传递依赖；协调者串行收口 |

06 已完成；后续 09、10、11、12、13 可以按实际领取状态开新 session，但必须等各自 Blocked by 已集成。05→06 和 07→09→10→12 建议复用负责该方向的 session；这是保持上下文的安排，不要求始终使用同一会话。分支或目录已存在时先核对所属任务，不覆盖重用。

main 的未提交 Changes 不会自动进入其他 worktree。本次票据修订已随冻结入口形成独立文档提交；继续开发的分支按文件所有权同步并记录 SHA。最新交付规则要求正式合入 main；03 等历史验收记录仍保留当时的状态，不倒改历史。

## 6. 逐票集成与能力开放

完成一票就合入 integration，并验证旧 smol 和该票的实际入口。合并串行，分支实现可并行。

- 07 当票注册真实无工具 Pi Application，只开放本票验证过的能力。
- 09 当票接通只读、平台工具回调、Worker 所需能力与 Goal，不提前开放文件修改/Shell。
- 10 才开放验证过的官方写入/Shell 映射；12 才声明已验证的恢复能力。
- 平台功能要求某项 Hook/授权能力而基座无法落实时，preflight 明确拒绝；不能通过提示词假装已经落实，也不能偷偷回退 smol。
- 只改配置中的后端，不承诺底层消息或会话跨基座转换。长期记忆、协作定义和应用身份保持平台所有。

公共合同变更由协调者形成独立提交并更新共享 fixture，再让受影响分支同步。各分支不能各加临时私有字段，也不能重复 cherry-pick 同一个功能补丁。

依赖放行需要这些具体交接物：

| 下游 | 必须先拿到什么 | 为什么不能提前做 |
| --- | --- | --- |
| 06 | 04 的迁移后保护调用点清单；05 的 native prepare/settle 实现 | 避免边搬基础工具、边提取同一批保护代码 |
| 09 | 05 治理入口、07 真实 Pi Application、08 中立工具与生命周期 | 三者缺一，都无法验收真实 Pi 双向工具调用 |
| 10 | 06 写入/Shell 保护；09 双向桥接 | 先有授权与结算，再开放有副作用的原生工具 |
| 11 | 09 已验证的只读、Worker、记忆/Goal 入口 | 验收混合协作不需要等 Pi 写入或恢复 |
| 12 | 10 的真实副作用及 journal 证据 | 恢复必须验证已执行效果不被重做 |
| 13 | 04 的依赖收口；09 可独立运行的 Pi 入口 | 具备无 smol 安装和平台工具验证条件；不等 10/12 |
| 14 | 11/12/13 的已集成成果及传递依赖 | 在同一最终版本上验证组合行为、发行和交付 |

同一窗口里的合入没有固定票号顺序；谁先验收完成就由协调者先合入，再检查下游是否解锁。共享入口接线也纳入本票验收，不能用“等待最后合并”掩盖未完成实现。

## 7. 验证与证据

后续各票按受影响范围选择检查；03 已执行的完整检查和结果见实现报告。以下为可复用的定向入口：

```sh
uv run pytest tests/application_test/test_runtime_adapter_config.py tests/lib_test/runtime/test_agent_runtime_contract.py tests/lib_test/runtime/test_runtime_definition_seam.py -q
uv run pytest tests/lib_test/runtime/test_tool_gateway_pipeline.py tests/hooks_test/test_tool_runtime_boundary.py tests/self_learning_test/test_evidence_gate_v6.py tests/self_learning_test/test_session_event_importer.py -q
uv run pytest tests/test_runner.py tests/test_cli_run_observability.py tests/test_cli_run_transport_exit.py -q
```

每票提供测试命令、退出码、revision、实际依赖版本和独立文件/产物 oracle。Pi 测试使用真实发布 SDK；模型响应可确定性替换，Agent 循环和实际工具不能伪造。共享 fixture 不能代替 09/10 的真实 SDK 接线。

必须覆盖的边界：旧 smol YAML 与基础工具行为；Pi 不加载 smol 基础工具；专业工具按需启用；Hook 与保护在副作用前生效；长期记忆保持原 scope/审核并跨基座复用；ContextRef 对应第一次执行的受限原文；取消不串用 Worker；恢复不重跑已提交副作用。

13 可以早做发行机制初验；14 必须用包含 10/12 的最终候选重建发行物，重跑 Pi-only/smol 安装及 CLI 失败路径。不能用开发机 import 成功代替干净安装；all-groups 不等于自动安装新增 extras。

真实 provider smoke 按实际配置单独记录 PASS/FAIL/NOT-RUN。未运行不能算通过；已失败要修复或撤回对应支持后复验，不能重新标为 NOT-RUN。所有尝试用新的私有证据目录，不复用用户运行目录。

## 8. 正式合入 main 与 worktree 清理

1. 各实现者提交自己范围的成果，协调者逐票合入 integration，保留提交与验收证据。
2. 开始 14 前确认全部依赖已集成，清理没有消费者的内部过渡形式，在同一候选上完成 A01–A14、既有必需 CI 和最终安装验证。
3. 交付前重新检查 main 及其未提交用户改动。需要时先将新的 main 变化并入候选并复验，不能覆盖现有 Changes 或用重置消除冲突。
4. 按维护者 2026-09-20 最新要求，将已验收提交正式合入 main，保留分阶段提交；可快进时直接快进，有分歧则集成验证后合并。不要只把成果留在 Changes。记录候选 SHA、main 合入前后提交及内容校验；未获额外指令不推送远端。
5. 核对每个本次创建的 worktree：提交已集成，未提交源码/配置已处理，所需证据已保存；随后删除这些 worktree。共享虚拟环境引用、用户数据和其他任务的 worktree 不在清理范围。
6. 保留实现分支和仓库外验收日志作为可恢复来源。最终报告说明 main 提交、验证范围和清理结果。

此次要求已替代先前“只交付 main Changes”的规则；PR 与远端推送仍需另有指令。单票完成、合入 integration、正式合入 main 和清理 worktree 分别记录；不能用其中一步替代其余步骤，也不能用历史状态覆盖已经新增的验收报告。
