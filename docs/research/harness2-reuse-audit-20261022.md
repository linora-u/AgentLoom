# Harness² 复用与偏差核查

日期：2026-10-22。状态：源码核查与收敛建议，未实施方法层替换，未启动模型实验。

本地核对版本为 `fd617d4ce22fd2a6fe159ff91b528efa9654d66a`，已包含于当前 main 的合并提交 `dc090494`。上游核对版本为 `google-research/harness2@1cfe254e8d25b15f37048c3730134686abe60335`，本轮远端 HEAD 与该版本一致。已有上游检出位于 `/tmp/harness2-mechanism-20261020`，尚未纳入项目。

## 结论

当前实现没有调用 Harness² 的核心模块，而是重新实现了递归、角色合同、证据、选择、反思与 Library。重复主要发生在方法层，并已损失提案理由传递、证据来源区分和任务流经验可见性。应由上游核心直接拥有这些流程，AgentLoom 负责应用执行适配；不能复制一份上游源码后继续调用原控制器，也不能在两套方法状态机之间增加一层包装。

Pi、完整应用执行、资源快照、隔离、业务验收、发布幂等、停止和真实用量记录具有宿主职责，需要保留并接到上游接口。

## 必须替换的重复与偏差

| 本地位置 | 当前问题 | 复用上游 |
| --- | --- | --- |
| `src/app/harnessing/controller.py:443-592` | 独立实现 parallel/sequential、提案试跑、组合和选择 | `core/recursion.py`、`core/selection.py` |
| `resources/roles/*.md`、`roles.py:59-74` | 五个简化角色合同，要求返回完整文件 edits JSON，遗漏上游逐项决策与证据约束 | `core/prompts.py`、`core/proposer.py` 的原位编辑及 harvest |
| `controller.py:333-373`、`roles.py:14-56` | 巨大 JSON 加字符分片，缺少显式 diff 和独立决策文件 | `core/context.py` 的任务、源材料、候选、产物、diff、decisions、Library 分文件组织 |
| `controller.py:479` 与 `:347-354` | rationale 虽保存到阶段档案，却没有传入后续 compose/reflection 的 pool | 上游将 stage notes/error 写入 `decisions/<label>.md` 并交给组合与反思 |
| `roles.py:132-144` | 将快照项目内 YAML/MD/JSON 全部作为当前候选文件，历史复盘由此混入当前证据 | 上游可编辑树由 Surface 筛选，源材料由 `adapter.task_files()` 明确声明，Library 单独提供 |
| `library.py:18-50`、`controller.py:844` | `active_confirmed` 审核状态阻断实验内经验；现实生成时间与历史新闻截止比较，使下一历史日期仍看不到新经验 | `core/library.py` 按实验任务流提供前序有效经验，不经 Curated Memory 审批 |
| `controller.py:822-849` | 自制反思格式及补修协议 | 上游逐项记录 helped/hurt/no effect/confounded，至多一条可迁移经验，也允许 NO_ENTRY |

原文依据：[递归](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/recursion.py)、[证据构造](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/context.py)、[提示词](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/prompts.py)、[Library](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/library.py)。

## 复制后仍需的最小适配

1. **Pi 调用通道**：上游仅注册 claude_cli/opencode，不能把 Pi 冒充其中一种。接现有 Pi 会话与进程生命周期，保留上游 mission、缓存、Reply、失败和 harvest 契约；选择与反思也必须使用 Pi。
2. **Application 可编辑资源规则**：上游固定路径规则不直接接受 AgentLoom Worker YAML、中文提示文件、工具配置和 Skill references/assets。需要小范围扩展路径识别、应用提示片段及字段校验。仅在 adapter.validate_tree 中补校验不够，因为文件可能已在 read_tree 时被过滤掉。
3. **Application 执行适配**：把上游的一次 execute 映射为一次完整应用任务。保留冻结输入、独立数据库/Runtime/output/session、全量覆盖和独立校验、进程退出确认、选中产物发布与恢复幂等。真实 token/费用未知时保留 unknown，不能将上游默认的 0 当作已知零费用。

依据：[通道和 Adapter 合同](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/spec.py)、[可编辑树](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/tree.py)、[Proposer](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/proposer.py)。

`development_score` 是用户此前明确要求的扩展，2026-10-22 已确认本轮与上游核心一并迁移。它与上游无答案/评分的改进上下文、事后 judge 及泄漏检查不同，不能复制原提示词后继续悄悄注入分数，也不能宣称是上游原生模式。

## 必须保持准确的行为

- 每模式每任务是 `4k` 次完整业务执行、`3k` 个改进阶段，另有选择、反思和重试；k 是有限正整数。复用上游 logical ledger，同时保留实际调用计量。
- sequential 最后从所有组合 checkpoint 中选择，不只交付最后一轮。
- 最终选择池只有组合 final。k=1 直接交付唯一合格 final；采用不证明胜过基线，无有效 final 不回退基线。Compose 可以合法 KEEP_BASE。
- 无效修改可在重试后继承 fallback 树；合法不修改与异常 fallback 必须分开记录。用户停止不能被吞成候选失败后继续执行。
- k>1 选择器失败时虽然可记录首个 final 作为 fallback，但 `Selection.ok` 仍为 false，不能包装为正常成功。
- 普通反思调用失败或没有有效经验时，上游不写库，也不因此作废已选产物；证据构造、泄漏检查等异常仍可能使流程 partial。沿用允许无经验的语义，删除额外反思修复回路。
- Library 按独立实验、模式和任务流组织。上游 retain 会删除不属于此前任务的条目，不能让多个实验共享一个可变根目录。新闻日期按明确日期顺序推进，业务事实截止另由新闻适配保障。
- 新一天仍从声明的 base 开始，并参考之前的 Library；采用候选不自动修改源项目、Git 分支或下一天的默认 base。

依据：[选择与反思](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/selection.py)、[调度与恢复](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/schedule.py)、[阶段预算](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/ledger.py)。

## 收敛后的执行路径

```text
应用 YAML + 一天任务
  → AgentLoom 冻结输入，准备独立试验空间
  → Harness² 原生调度
      baseline
      → 通用/领域提案（Pi 读取证据并编辑候选文件）
      → Application adapter 完整试跑
      → 上游组合、完整 final 试跑与选择
      → 上游反思，有有效经验才写实验 Library
  → AgentLoom 发布被选中的已执行产物
  → 下一日期，读取同一实验此前的 Library
```

调度、证据、选择和 Library 各只有一处实现。现有新闻日序列只保留日期准备、失败留痕与发布幂等，不再管理一套独立 Harness 阶段。上游任务/角色缓存与宿主执行身份衔接，恢复不能重复运行已完成业务或重复发布。

## 已确认的范围取舍

2026-10-22，用户已明确选择：

1. **开发评分同时迁移**：本轮包括上游原生闭环与 `development_score` 扩展，两条路径都必须可用。共享同一套上游递归、组合、选择、反思和 Library，不保留独立的评分版控制器。
2. **保留四类新闻职责**：保留 initial/refined/validation/conflict 业务入口，允许优化各入口绑定的 Agent、子 Agent、Skill、工具及委派关系。本轮不建设改变业务阶段图结构的通用工作流引擎；完整覆盖、独立校验、证据截止继续作为固定业务约束。

### 开发评分扩展的最小合同

- **沿用现有配置**：继续使用 `feedback.mode`、`feedback.evaluator`、`feedback.partition`，不增加第二套启用参数、优化范围或控制器选择项。现有 `roles` 引用用于选择 Pi 执行定义；方法提示主体由上游生成，不再维护五份缩写方法合同。
- **复用现有评分器**：新闻继续调用 `news_agent.runtime.harnessing:NewsDevelopmentEvaluator`，复用封存结果与现有 `pipeline.evaluate()`，沿用开发月份检查和原始行情哈希校验，不另造收益与方向正确率公式。
- **评分时点明确**：在完整 trial 结束后，由可信宿主计算该 trial 的开发评分，供后续改进阶段参考；没有一边执行同一 trial 一边在线修改其配置。最终组合 trial 也计算评分并保留记录。
- **原生路径不接触开发分**：`contrastive` 沿用原版无评分提示与上下文。`development_score` 在明确标识的开发反馈文件中提供当前开发分区的允许结果，并替换与该事实矛盾的无评分提示段。两种模式使用同一方法核心。
- **评分留在宿主反馈记录**：不将 score 塞进上游 `RunView`，通过有限的证据/提示扩展接入。只允许指定开发反馈进入对应改进上下文，仍拒绝隐藏验证/测试结果，不能全局关闭上游泄漏检查。候选可编辑树与开发反馈文件分别管理。
- **保持选择语义**：k=1 仍直接采用唯一合格 final；k>1 的评分反馈使用与候选产物相同的匿名编号。开发评分不新增 baseline 选择分支、确定性取最高分规则或“评分不涨就回退”门禁。
- **分区与缓存隔离**：反馈模式、开发分区、评价器身份及冻结输入属于实验身份。不同反馈模式/分区不共享可变 Library 或角色缓存；同一任务流此前生成的经验按任务顺序可见，不再拿现实生成时间与历史新闻日期比较。
- **完整性与效果分别报告**：未通过业务验收的产物不可因评分高而采用；无可评分信号明确为不可计分，不伪造零分。使用过评分进行调优的日期属于开发证据，不能继续当作独立未见的效果验证；业务 Agent 原始新闻输入继续受截止时间限制，开发调优可能带来的过拟合另由未参与反馈的数据检验。

这是对既有反馈接口的迁移约束，不增设新的评分产品层。实现时需用原生模式与开发评分模式的对照验证，防止扩展改变原生路径行为。

## 建议实施与验证顺序

1. 固定上游源码到 `third_party/harness2/`，保留 LICENSE、版权信息、提交来源和有限补丁说明；实际调用核心，而不是只存一份参考代码。
2. 接 Pi、Application 资源规则和执行 Adapter，同时迁移上述开发评分扩展；删除被替代的方法分支、简化角色合同和大 JSON 证据路径，避免两套控制器并存。
3. 复用上游核心测试，验证适配后的阶段计划、候选集合、失败语义与原核心一致；补充必要的宿主停止、隔离和恢复契约验证，并验证开发评分只进入声明的模式/分区，选择匿名性不被反馈打破。
4. 跑完整新闻任务核对交付，再跨两个日期检查前序经验确实可见、contrastive 不读取事后评分、development_score 能读取指定开发反馈、恢复不重跑。真实模型实验和收益改善证明分别报告。

本轮仅更新术语与本核查文档，未修改执行代码、复制上游目录或启动模型。
