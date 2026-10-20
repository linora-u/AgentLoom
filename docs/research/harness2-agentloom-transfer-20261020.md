# Harness² 机制与 AgentLoom 移植讨论

日期：2026-10-20。状态：上游研究与本地接口核对；未实现、未运行 AgentLoom 改进实验。

上游固定版本：`google-research/harness2@1cfe254e8d25b15f37048c3730134686abe60335`。AgentLoom 核对基点：`7782709ae52f52d83daa748480e03f587ffd3fb6`，附带会话开始时已有的工作区修改。本轮只记录研究、[领域术语](../../CONTEXT.md) 与 [已确认架构决定](../adr/0004-application-scoped-harness-improvement.md)。

执行入口、候选快照与隔离、比较与采用、预算与恢复的接口和验证要求，现已细化到 [执行契约草案](../specs/application-harnessing-v1.md)。该草案区分已确认边界、工程建议与待确认默认值；本研究记录保留机制来源和接入依据。

## 用户已明确的方向

- 做成 AgentLoom 通用能力，先以 `news_agent` 的完整业务任务验证。
- 用户已有目标，希望围绕目标推进。设计应复用现有 Goal，不把逐轮人工挑选配置作为前提。
- 用户选择 `news_agent` 为首个验证应用；具体试验日期及验收标准尚未确定。
- 用户要求将能力集成到 YAML，由配置选择哪些 Application 启用并运行。
- 用户进一步确认显式启用后默认 `on_task`，并要求明确会实际运行执行 Agent；基线、候选和组合配置中的完整应用都会实际执行。
- 用户明确整个 Application 都参与，包含其中多个 Agent 与 subagent；不再以单个精筛 Worker 为首版改进单元。
- 完整 Application 是固定优化单位，不为只有一种取值的优化粒度增加配置项。
- 工具配置可以优化，不限于提示词与 Skill；用户已明确允许增删 Agent 和修改委派关系。
- 用户确认候选通过可校验配置和 Skill 调整真实编排，应用 Python 中需要变化的调度接到配置。候选直接修改业务 Python 或工具源码属于单独声明的能力，本轮不隐式开放；框架为支持配置所需的代码改造不受此候选编辑边界限制。
- 用户明确反馈方式与预算都应可配置：同时支持 Harness² 式无标签对照改进和开发集事后评分反馈，不要求在设计阶段固定选用其中一种。
- 递归方式、预算、改进范围以及改进器自身的角色配置应纳入 YAML；示例值和框架默认值不等于产品只支持这一组参数。
- 用户要求采用与预算处理更多参照 Harness²，并询问无预算上限；随后明确要求分析必要性、删除不必要扩展。首版保留有限 mode/k，不额外设置实验总金额/token上限，也不为“不限制”增加一组 null 字段。
- 自动晋升、基线回退选择模式、预算耗尽交付分支和通用配额结算系统经必要性审查从首版删除，不保留扩展开关或空接口。完整应用适配、隔离、验收、恢复及用户明确要求的反馈/角色配置仍保留。
- 本轮研究机制、记录设计问题，不实施产品改动；具体接口和采用契约仍待细化。

## 上游实际机制

Harness² 固定模型权重，优化模型外围的 system prompt、guardrails、memory、tool guidance、subagents、skills、scripts、plugins。核心是两层递归：[README](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/README.md)、[论文](https://arxiv.org/abs/2610.14224)。

第一层针对当前任务，执行 Propose–Probe–Compose。`k=1` 的流程是：

```text
原配置执行同一任务，形成 baseline
  → 分别提出通用工作方法改进、领域方法改进
  → 两个候选各自重新执行同一任务
  → 对照轨迹、产物和改动说明，组合最终配置
  → 最终配置再次执行任务
  → 选择产物，再反思改进经验
```

通用改进关注工具操作、工作流程、检查与恢复；领域改进关注专业约束、推理方法及交付结构。组合阶段可以取舍，也可以选择保留 base 配置；它不是简单拼接提示词。[改进与组合提示词](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/prompts.py)

Parallel 模式扩宽探索：`k` 次 baseline、`k` 个通用候选、`k` 个领域候选以及 `k` 个组合版本。Sequential 模式先取得 `k` 次 baseline，再进行 `k` 轮连续改进，每轮使用上轮配置及累积证据；最终选择池包含所有组合 checkpoint。[递归实现](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/recursion.py)

每模式每任务计划 `4k` 次完整任务执行、`3k` 次改进 Agent 会话；selector、reflection、重试和正式评分额外计入。因此 `k=1` 也包含四次完整任务执行。两模式同时运行时可共享 baseline，不能把两个单模式预算直接相加。调用轮数也不等于模型请求数或美元费用。[预算合同](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/ledger.py)

第二层针对后续改进过程：反思当前任务的采用结果，将完整 adopted harness 与有证据支持的改进经验保存到同领域 Library。未来任务的改进 Agent 可以参照这些条目。任务原始轨迹、全部候选和产物仍属于实验存档，不会全部自动变成长期经验。[Library](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/library.py)

同领域按任务顺序读取以前条目，不同领域可以并发；库按 benchmark、executor、mode、k、seed 隔离。新任务依旧从 `adapter.base_harness()` 开始，历史库提供改进参考，不是上次 final 无条件覆盖下次 base。此版本也不让 Agent 自动重写递归算法。[调度与初始配置](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/schedule.py)、[实验说明](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/docs/EXPERIMENTS.md)

### 无标准答案，不等于没有判断机制

在线改进使用行为轨迹、可见错误、交付物以及任务要求作比较；不读取 benchmark 的标准答案或正式评分。BenchmarkAdapter 的执行视图与 `score()` 分离，采用后由独立 judge 评分。这适用于开放任务，但模型比较仍然可能判断错误。[接口](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/spec.py)、[独立评分](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/judge.py)

需要保留的限制：

- `selection_pool()` 排除原始 baseline，只选择组合后的 final。`k=1` 直接采用唯一有效 final，因此采用不是相对基线改善的证明。Composer 可保留 base 配置，与最终选择池不包含原始 baseline 是不同层面的事情。
- 正常产物任务会过滤执行失败、无交付物或正文为空的结果；有产物但质量退步仍可能被采用。少数状态型任务允许以轨迹作为后备证据。
- `k=1` 无有效 final 时选择失败，没有 baseline 回退；`k>1` 的 selector 异常可记录首个 final 为 fallback，但 `sel.ok` 仍为 false，不因此成为成功采用/反思入库/评分结果。不能用通用 fallback 函数的描述推断发布路径会回退 baseline。
- 经验反思要求区分 helped、hurt、无观察效果和混杂，并允许 NO_ENTRY；格式与引用门槛不等于机械证明因果关系。
- 作者报告的 benchmark 提升不构成 AgentLoom 适配后效果证明。本轮没有复现论文的模型实验。

依据：[选择池与过滤](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/selection.py)、[反思提示词](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/prompts.py)。

本轮再次核对：上游按 final 实跑 → 选择 → 反思 → 有合适经验时入库的顺序运行；独立 judge 在选择后另行评价，没有“独立分数通过后才切换下次默认配置”的机制。每个后续任务仍读取基础 harness。此前提出的自动默认版本晋升不纳入首版。

上游 ledger 的 budget 主要是 `R=4k / P=3k` 阶段数量契约和消耗记录，并没有整个实验累计金额/token超限停止器，也没有预算耗尽交付的通用分支。`k` 必须为有限正整数；会话/执行器另有局部超时、步数、输出和重试限制。AgentLoom 首版同样不额外建设总限额控制器，保留有限 k、已知消耗记录和底层限制；不等于无限改进直到目标达成。[阶段账本](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/ledger.py)、[任务调度](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/harness2/core/schedule.py)

上游还明确承认默认网络未隔离，验证中出现过 Agent 下载公开 rubric。移植时应将在线可见目标要求与隐藏评估数据区分；不能只靠提示词声称不存在泄漏。[上游实验边界](https://github.com/google-research/harness2/blob/1cfe254e8d25b15f37048c3730134686abe60335/docs/EXPERIMENTS.md)

## AgentLoom 当前已经有什么

以下为当前源码核对，不表示 Harness² 已经接入。

| 所需能力 | 当前接口 | 复用范围与缺口 |
| --- | --- | --- |
| 目标与持续推进 | [Goal 状态](../../src/execution/goal/model.py)、[完成工具](../../src/tools/goal/goal_tools.py) | YAML task 是 objective；根 Supervisor 以非空 evidence 完成。目标状态本身不独立核验业务正确性。 |
| 任务执行与证据 | [execute_app](../../src/app/runner.py)、[RunInfo / ApplicationRunResult](../../src/app/run.py) | 可定位运行身份、manifest、产物和错误；还需生成跨候选比较的实验报告。 |
| 改进角色编排 | [Application / BoundWorker](../../src/app/application.py) | 可承载 proposer、composer、verifier 等有类型输入输出的 Worker。候选求解器若是完整 Supervisor，使用 execute_app；不能把所有应用一律当普通 Worker。 |
| 配置冻结与指纹 | [definition](../../src/app/definition.py)、[revision](../../src/app/revision.py) | 已有定义快照和 revision；还需限定可修改面并形成候选版本。 |
| 并发与恢复 | [有限批次契约](../../src/execution/concurrency/batch_models.py)、[DurableApplication](../../src/execution/durable/client.py) | 可复用调度和持久任务身份；初始小试验无需重造队列。 |
| 经验治理 | [memory 工具](../../src/tools/self_learning/memory_tool.py)、[evidence gate](../../src/self_learning/persistence/evidence_gate.py) | 经验可经现有审查链处理，但精选记忆不应存放完整 harness 文件树。配置候选与长期经验需要各自明确的采用条件。 |

源码中未发现实现上述完整 Propose–Probe–Compose 闭环的现成模块。

### 现有 Goal 是入口，验收判断是仍需补齐的部分

Goal 当前解决“持续围绕哪个任务工作、谁有权结束、以什么文字证据结束”。其 `with_completion()` 检查 evidence 非空；它不自动读取报告检查内容，不判断新配置是否优于旧配置。移植需要让 Goal 的原始要求保持固定，把交付证据交给相应 verifier；优化器只能调整完成目标的方法。

具体场景：目标要求“完整读取十份文档并输出带出处报告”。候选把要求改成只读三份，会更快，但不满足同一个 Goal。候选仅改进并行阅读和引用检查，才属于保持目标的执行改进。若两份报告都通过完整性检查，进一步的质量比较还需事先明确的标准。

### 新闻应用已有评价契约

当前 [settings.yaml](../../applications/news_agent/config/settings.yaml) 声明持有窗口三天、开盘退出、利好阈值 +2%、利空阈值 -1.5%；[outcome.py](../../applications/news_agent/evaluation/outcome.py) 的 `three_value_outcome_v1` 将正式方向信号分为命中、弱命中、未命中，总体 `outcome_score = (命中数 + 0.5 × 弱命中数) / scored_signals`。这些是现有配置和计算定义，本轮没有运行收益评估，也不重新替用户设定目标。

应用候选先满足输入覆盖、独立校验和截止前证据等业务约束，再比较既有质量评价、信号与放弃数量、失败和成本。`outcome_score` 只覆盖有有效行情观察的正式方向信号；中性与放弃不入该分母，完整输入留痕本身也不能证明没有错过机会。无有效信号时分数为 `None`，即使 `scoring_complete=True` 也不能报告质量改善。

需要区分工具提交完整、业务封存、计分完整和相对基线改善。现有 [pipeline](../../applications/news_agent/runtime/pipeline.py) 与 [StageLedger](../../applications/news_agent/runtime/store.py) 提供前两项的业务证据；最终采用还需要针对整应用候选的比较，不能只复用 `sealed` 或 `scoring_complete` 当作效果门禁。

`evaluation/method.py` 当前还包含四角色枚举、模型 profile 与阶段联网检查。这些是现有接入的实现边界；支持用户已允许的角色增删时，需要重做依赖收集和配置签名，不能把四个文件名的枚举等同于永久业务约束。独立查证和时间边界等业务语义继续保留。

用户对 Q7/Q8 的决定是支持配置选择，不固定选用某种反馈或某个轮数：

- **无标签对照模式 `contrastive`**：改进器使用任务要求、执行轨迹和可见产物作对照，不读取当前任务事后收益、标准答案或隐藏评分；事后评分用于独立评估。
- **开发评分模式 `development_score`**：改进器可读取指定历史开发集的事后评分；业务执行 Agent 的输入仍受截止时间约束，后续验证集结果与开发优化隔离。该模式需要绑定评价器及开发数据分区，不能只改开关而沿用无标签模式的上下文装配。
- **递归预算**：使用 `mode` 和正整数 `k` 配置，并行模式下表示每类候选/组合的探索规模，连续模式下表示改进步数。单模式遵循上游流程时约有 `4k` 次完整应用执行、`3k` 次改进会话，反思、选择、重试和验收另外计入；没有固定为一轮或三轮。

以上是能力范围选择，当前没有启动模型运行。实际模式、k 和数据范围由一次具体运行的有效配置确定并冻结。

现有配置声明开发月份和后续验收月份，可作为进一步核对分区的入口；配置存在不表示已完成验收，也不证明某段数据此前从未参与开发反馈。

## AgentLoom 首版移植范围

四部分的接口、配置和验证要求以 [执行契约草案](../specs/application-harnessing-v1.md) 为准，本研究记录不再保留多套重复或已删除的配置草案。

1. **执行入口**：一个控制器包围有限的完整业务请求。Supervisor 与新闻流水线各接一次执行驱动，由 enabled 控制开启后按任务运行；不再增加独立触发模式，子 Agent 不重复开启循环。新闻前后台调度共同响应候选编排配置。
2. **候选快照与隔离**：归档真实使用的配置、prompt、Skill及依赖，贯通子进程资源根；独立业务账本、队列和输出。对照共享事实输入与冻结经验，业务 Agent 不读取隐藏评分。
3. **比较与采用**：固定走上游 final 选择主线；业务验收继续保留，采用不等于效果改善。当前任务交付已执行结果，反思将采用配置和合适经验供后续改进参考，下次仍从基础配置开始。
4. **探索预算与恢复**：mode/k 配置有限探索，不设置实验总金额/token上限。复用用量/失败回执、既有超时重试和取消恢复；不新增通用钱包、预留结算或预算耗尽交付策略。

### 必要性审查后删除的设计

| 删除内容 | 删除原因 |
| --- | --- |
| trigger/on_task/explicit 独立触发字段 | 当前只需要按应用启用后随完整任务运行，enabled 已能表达，无需为额外运行方式增加配置。 |
| 自动切换后续默认版本及其人工/自动晋升选项 | 上游用经验库积累已可完成跨任务改进；没有当前需求要求另一套默认版本生命周期。 |
| baseline_guarded 及 selection.policy | 首版只有上游 final 选择这一条主线，不为单一策略设置参数，也不新增基线回退分支。基线执行证据仍保留。 |
| 实验总金额/token多维硬配额、null开关与预留结算接口 | 用户需要可配置探索规模及不设总资源上限，不必据此建设跨提供方严格预算系统。 |
| return_validated/fail_task 预算耗尽交付选项 | 不建设总配额系统便没有相应触发器；普通中断保留证据并如实返回，不新增交付分支。 |
| 自动延迟评分/晋升调度及通用网络回放平台 | 现有评价入口与检索证据记录足以支撑当前验证；这些不是完成首轮闭环的必要条件。 |

用户明确要求的 development_score、改进 Agent 的普通 YAML 配置、所有参与 Agent/subagent 的工具配置及拓扑变化继续保留。必要性精简不削弱完整覆盖、独立查证、证据截止或防重复提交。

### 本地接入仍需处理的具体事实

- Application.open 是生命周期作用域，不能重跑调用方 Python 逻辑；完整业务请求需显式接到执行驱动。
- 新闻的前台和后台均推进阶段，仅改一个入口或 worker_agents 不足以改变真实调度。
- NewsHost 每次 attempt 从导入模块的 APP_ROOT 打开应用；只换业务目录不等于加载了候选。候选资源根必须贯通所有配置/Skill/MCP读取。
- Agent 定义 revision 不包含全部 Skill引用资源，catalog 缓存正文也不等于完整资源冻结；实际依赖必须固定。
- Durable 身份由 namespace、Worker、item ID、business revision 组成；按稳定 trial 分配独立 namespace并在回执中关联候选，恢复使用原 HostSpec 和版本。
- Pi/smol用量可能是累计快照，provider零占位值也不是免费证据；复用已知消耗和缺报语义，不重复累加。
- Goal完成声明不自动证明业务交付正确；新闻原有验收仍由应用执行适配保留。事后分数独立记录，不反向伪造采用效果。

### 当前设计树

```text
完整 Application 改进【已确认】
  ├─ 显式启用后默认 on_task，实际运行执行 Agent【已确认】
  ├─ 配置、Skill、工具配置、Agent组成/委派可优化【已确认】
  ├─ 可变调度接到配置，业务源码不隐式开放【已确认】
  ├─ contrastive/development_score、mode/k、改进角色可配置【已确认】
  ├─ final选择 → 反思/经验库 → 后续任务从base结合经验改进【首版主线】
  └─ 执行驱动、真实快照隔离、业务验收与恢复【待实施的必要接点】
```

后续不再逐项要求用户选固定日期、预算数值或内部接口名。真实新闻实验开始前需要有效输入范围与运行配置；源码装配和多层调用仍需工程验证，当前不能声称能力已实现。

## 核对范围

读取上游论文、文档、递归/调度/选择/经验库实现，并对照本地 Goal、Application、运行证据、定义快照、新闻流水线、Durable恢复与记忆能力。上游临时 checkout 的 5 个离线 unittest 通过，未调用模型；这只验证上游流程合同。AgentLoom 未改产品代码、未进行付费模型实验，也未验证迁移后的业务提升。
