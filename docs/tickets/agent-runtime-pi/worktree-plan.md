# Pi 并行开发与 main Changes 交付计划

更新：2026-09-20。配套：[核心规格](../../specs/agent-runtime-pi-integration.md)、[执行索引](README.md)、[工具归属](tool-ownership.md)。本文件使用 01–14 票号；旧 C0/S1/G1/P1 宽阶段不再作为另一套排期依据。

## 1. 当前基线

01/02 已完成。准备提交 45ddbfdc、最终代码验收 42662ed6、历史收尾提交 8d63be5f 保留在 codex/pi-integration。主工作区现为 main，成果展开为未提交 Changes；历史开发/验证 worktree 已删除。原 main 提交为 c697b24f602e71d56c9aa2c532e6079c0db5aa9d，未来启动时必须重查。

本次只更新票据，没有新建 worktree。当前 Changes 已有 03 实现与合同草案，但尚无完整验收及冻结交接记录；下一步先核对并收口这些已有成果。本轮修订不在旧 integration 提交中，不能只从 main HEAD 或旧 integration 开分支而丢失上下文。

不使用 git add . 纳入既有 codex/、temp/、参考 pi/、私有配置或无关研究文件。凭证、用户 memory DB、运行数据和既有 stash 均不属于迁移清理范围。

## 2. 串行门禁与并行窗口

| 阶段 | 可执行票据 | 必须已集成的前置 |
| --- | --- | --- |
| 串行准备 | 03 | 01、02，以及最新工具归属修订 |
| 四路并行 | 04 / 05 / 07 / 08 | 03 同一冻结提交 |
| 治理续接 | 06 | 04、05 |
| Pi 工具续接 | 09 | 05、07、08 |
| 并行应用/工具/发行 | 10 / 11 / 13 | 分别为 06+09 / 09 / 04+09 |
| Pi 恢复 | 12 | 10，可与未结束的 11/13 并行 |
| 串行收口 | 14 | 11、12、13 |

这些是按前置解锁的窗口，不要求每个横向阶段一起结束。例如 06 与 09 可并行，11 也不必等 10。全部依赖图见 README。

新增 04→06 的原因：04 搬迁自研 smol 的文件/Shell 实现；06 会抽取其中的平台保护策略并修改调用点。必须先形成稳定布局，再提取策略，避免两个 worktree 修改或删除同一批文件。05 只改公共 native 读取治理，不依赖这次搬迁，仍可与 04 并行。

## 3. 03 必须先做出的内容

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

worktree 放在仓库外同级 AgentLoom-worktrees/tNN，分支使用 codex/pi-tNN-主题。03 产出的冻结提交作为 04/05/07/08 的共同起点；其他票从已包含全部前置的 integration 提交开始。

每个 worktree 独立配置 Python/Node 依赖与运行目录。验证 agentloom 实际导入路径；测试用默认配置来自示例，不复制用户凭证到仓库。所有本地锁定包使用可复现安装；不依赖主工作区已装的 smol 或 node_modules。

一个 session 可连续处理同一链，例如 07→09→10→12，但每票开始前必须合入其新前置。三个实现者时先安排 04、05、07，04 完成后转 08；不要为人员不足伪造技术依赖。

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

路径仅用于分工定位；当前逐文件/函数草案见 [03 文件交接](03-file-ownership.md)，由 03 验收时确认并冻结。未列明的共享文件先由协调者决定归属，不得自行跨目录批量替换。

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

03 放行四路前，逐项核对以下交接材料：

- [ ] 冻结 SHA 包含当前 01/02 成果、最新票据与通过验证的公共基线。
- [ ] 04/08 的登记入口和混合文件归属已分开，明确哪些文件可各自编辑、哪些交协调者。
- [ ] 05/07 能消费同一版本的 runtime、模型投影、manifest 与 prepare/settle 合同，无须自行重设计。
- [ ] 资源关闭与 Hook 环境依赖已接到公共合同，smol 原有行为实测通过。
- [ ] 每路的验收入口、受保护的兼容行为和不支持能力明确；未完成项不能标成“后面一起处理”。

### 新 session 直接使用的领取说明

03 尚未验收时，只把下面这段交给协调 session：

> 收口 `docs/tickets/agent-runtime-pi/03-freeze-integration-contracts.md`。先核对 main Changes 中已有实现，保留用户改动；按本票条件补齐并验证，发布可复现冻结 SHA、合同/文件交接清单和验收记录。只做 03，不启动 04–14。

03 放行后，每个新 session 使用下面的共用模板，并从表中选择一行：

> 实现本目录 NN 号票据，只做这一票。读取 README、tool-ownership、03-contracts、03-file-ownership、worktree-plan 和本票。先验证协调者提供的基线 SHA 已包含全部 Blocked by 的已验收提交，再从该 SHA 创建独立分支/worktree。仅修改本票拥有的文件；共享入口由协调者串行接线，本票必须在真实入口验证后才算完成。返回基线 SHA、成果 SHA、变更文件、验收结果及移交项。不要自行交付 main 或开发下一票。

| NN | 建议分支 | 建议 worktree（仓库同级） | 实现重点 |
| --- | --- | --- | --- |
| 04 | `codex/pi-t04-smol` | `AgentLoom-worktrees/t04` | 完整自研 smol Agent 与基础工具；移交保护调用点给 06 |
| 05 | `codex/pi-t05-governance` | `AgentLoom-worktrees/t05` | 公共 native 只读执行与持久结算 |
| 07 | `codex/pi-t07-runtime` | `AgentLoom-worktrees/t07` | 正式 Pi adapter/bridge；真实无工具 Application |
| 08 | `codex/pi-t08-platform` | `AgentLoom-worktrees/t08` | 平台工具、按需专业工具、MCP 与 Skill 入口 |

后续 06、09、10、11、12、13 同样可以开新 session，但必须等各自 Blocked by 已集成。05→06 和 07→09→10→12 建议复用负责该方向的 session；这是保持上下文的安排，不要求始终使用同一会话。分支或目录已存在时先核对所属任务，不覆盖重用。

main 的未提交 Changes 不会自动进入新 worktree。冻结应在隔离索引/准备区中精确纳入本项目成果，并保留为可取出的提交；这不要求在 main 自动提交。03 交付记录必须写出实际 SHA，不能只写随时会移动的分支名。

## 6. 逐票集成与能力开放

完成一票就合入 integration，并验证旧 smol 和该票的实际入口。合并串行，分支实现可并行。

- 07 当票注册真实无工具 Pi Application，只开放本票验证过的能力。
- 09 当票接通只读、平台工具回调、Worker 所需能力与 Goal，不提前开放文件修改/Shell。
- 10 才开放验证过的官方写入/Shell 映射；12 才声明已验证的恢复能力。
- 平台功能要求某项 Hook/授权能力而基座无法落实时，preflight 明确拒绝；不能通过提示词假装已经落实，也不能偷偷回退 smol。
- 只改配置中的后端，不承诺底层消息或会话跨基座转换。长期记忆、协作定义和应用身份保持平台所有。

公共合同变更由协调者形成独立提交并更新共享 fixture，再让受影响分支同步。各分支不能各加临时私有字段，也不能重复 cherry-pick 同一个功能补丁。

## 7. 验证与证据

现有检查示例，按受影响范围选用；本轮文档更新没有重新执行产品测试：

```sh
uv run pytest tests/application_test/test_runtime_adapter_config.py tests/lib_test/runtime/test_agent_runtime_contract.py tests/lib_test/runtime/test_runtime_definition_seam.py -q
uv run pytest tests/lib_test/runtime/test_tool_gateway_pipeline.py tests/hooks_test/test_tool_runtime_boundary.py tests/self_learning_test/test_evidence_gate_v6.py tests/self_learning_test/test_session_event_importer.py -q
uv run pytest tests/test_runner.py tests/test_cli_run_observability.py tests/test_cli_run_transport_exit.py -q
```

每票提供测试命令、退出码、revision、实际依赖版本和独立文件/产物 oracle。Pi 测试使用真实发布 SDK；模型响应可确定性替换，Agent 循环和实际工具不能伪造。共享 fixture 不能代替 09/10 的真实 SDK 接线。

必须覆盖的边界：旧 smol YAML 与基础工具行为；Pi 不加载 smol 基础工具；专业工具按需启用；Hook 与保护在副作用前生效；长期记忆保持原 scope/审核并跨基座复用；ContextRef 对应第一次执行的受限原文；取消不串用 Worker；恢复不重跑已提交副作用。

13 可以早做发行机制初验；14 必须用包含 10/12 的最终候选重建发行物，重跑 Pi-only/smol 安装及 CLI 失败路径。不能用开发机 import 成功代替干净安装；all-groups 不等于自动安装新增 extras。

真实 provider smoke 按实际配置单独记录 PASS/FAIL/NOT-RUN。未运行不能算通过；已失败要修复或撤回对应支持后复验，不能重新标为 NOT-RUN。所有尝试用新的私有证据目录，不复用用户运行目录。

## 8. main Changes 交付与 worktree 清理

1. 各实现者提交自己范围的成果，协调者逐票合入 integration，保留提交与验收证据。
2. 开始 14 前确认全部依赖已集成，清理没有消费者的内部过渡形式，在同一候选上完成 A01–A14、既有必需 CI 和最终安装验证。
3. 交付前重新检查 main 及其未提交用户改动。需要时先将新的 main 变化并入候选并复验，不能覆盖现有 Changes 或用重置消除冲突。
4. 按维护者最新要求，把已验证成果展开到主工作区 main 的未提交、未暂存 Changes；不自动新增 main 提交、不推送。记录候选 SHA、main 基线、交付差异和内容核对结果。
5. 核对每个本次创建的 worktree：提交已集成，未提交源码/配置已处理，所需证据已保存；随后删除这些 worktree。共享虚拟环境引用、用户数据和其他任务的 worktree 不在清理范围。
6. 保留实现分支和仓库外验收日志作为可恢复来源。最终报告说明 Changes 位置、验证范围和清理结果。

未来只有维护者明确改变交付方式时，才改为提交/PR/推送 main。当前只修订计划；01/02 已有记录不改写为后续任务完成。
