# Responses runtime 修复与真实 Application 验证

> 历史验证记录：本轮验证后，维护者要求删除整个 Goal 模式。下文 Goal 结果仅记录删除前的行为，不表示当前功能仍受支持。

2026-09-20，分支 `codex/remove-code-act-responses-runtime`。
三个确定性运行时问题已修复，最终 Python 全量测试通过。真实 Responses 请求、
工具执行和恢复路径已验证；复杂模型任务仍有失败记录，不能将本轮写成全部验收通过。

## 修复

| 提交 | 根因与修复 | 回归证据 |
|---|---|---|
| `7f3c1828` | 内部工具结果的 `error` / `blocked` 被直接放入 Responses 的生命周期 `status`，不符合协议。投影时省略该可选字段，错误内容与 canonical 状态保留。 | 三种工具结果的协议校验；真实 Application 经历失败、重试成功、Hook 阻断后完成。 |
| `7f3c1828` | 传入 checkpoint 后，默认 `continue_session=False` 使恢复的历史随即被 reset。存在 checkpoint 即按继续会话执行。 | 两种 `record_task` 设置的真实 runtime loop 回归；Chat 与 Responses 的真实 LLM 均在新 runtime 中仅凭 checkpoint 找回旧随机标记。 |
| `36199f32` | Responses 复用了 Anthropic 的系统提示缓存编码，向 `input_text` 注入 `cache_control`；真实服务返回 HTTP 400。移除此跨协议字段，完整保留系统提示和请求级选项。 | 先复现再修复的缓存参数组合测试；修复后真实 Goal Worker 不再因该字段被拒绝，并行预算耗尽及恢复链路通过。 |
| `add19fdc` | Self-learning README 要求最终状态字段为布尔值，但 workflow 只列字段名，模型返回了 memory 对象或字符串。补齐 workflow 的布尔类型与取值来源，完整 memory 结果仍保存于引用文件。 | 三个新 Run 的最终格式与产物检查通过；中途参数错误另记，不能据此宣称整个 README 验收通过。 |

运行时实现为 `36199f326ff1acd77d9630a130ff74fb86e2bb92`，后续 `add19fdc` 只补充
Self-learning 的三行 workflow 指令。前两项修复后的基础验收
记录在 `7f3c1828b65f684f2a4c68fb8e64b5deaf3c08e2`；`36199f32` 的变更只影响 Responses
缓存投影和对应测试。每份 receipt 保留实际执行版本，没有将先前运行改记到最终版本。

## 确定性检查

运行时实现 `36199f32` 执行完整 Python CI 超集：**4142 passed，1 skipped，3 warnings，272.68 秒**。
警告来自 LiteLLM 的 Pydantic 弃用和既有响应对象序列化测试。新增回归均先在缺陷
实现上失败，再在修复后通过。`git diff --check` 通过。

```sh
PYTHONPATH=. .venv/bin/python -m pytest tests/ \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_capsule.py \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_contract.py \
  -q --tb=short --color=no
```

Standards 审阅与 Spec 审阅分别复核了运行时修复，均无剩余代码 finding。
Spec 范围为 `RESPONSES_RUNTIME_SPEC.md` 的 Stage 1；本次没有验证 Stage 2 LangGraph。

## 真实 LLM 验收

使用本机忽略的 `config/llm.yaml` 中现有模型和凭据，新增四个
`responses_powerful*` profile，显式设置 `adapter: openai_responses`。
原有 profile 和默认值保持不变。扩展 Application 验收通过公开配置绑定接口为
Supervisor、Worker 和 summary 选择 Responses，并断言持久化 checkpoint 的
`model_adapter_id` 确实为 `openai_responses`。未以模型桩替代这些请求。

本轮持久化证据记录到 **268 轮已完成模型响应、73 次 Worker 启动、574 条工具
调用结果**。这些计数包含失败尝试及预期中断，不是通过率；部分 Worker 请求没有
对应的 runtime model event，因此也不将其当作完整网络请求次数。

| 场景 | Chat Completions | Responses |
|---|---|---|
| 原生工具调用协议探针 | 通过 | 四个模型 profile 全部通过 |
| Unit Test Studio | 通过；宿主实际执行生成测试 | 通过；宿主实际执行生成测试 |
| Repo Map | 通过；核对符号、引用和 Skill 产物 | 通过 |
| ContextEngine text / JSON / multi-worker | 三类全部通过 | 三类全部通过 |
| Core / Markdown 工具 Application | 两类全部通过 | 两类全部通过 |
| 工具错误恢复与 Hook 阻断 | 通过；真实失败后重试，禁止写入未发生 | 两个模型通过；最终修复版本再次通过 |
| Self-learning | 补齐指令后最终格式与产物通过；中途四次参数错误后恢复，零错误条件未通过 | 两个模型的最终格式与产物均通过；各一次参数错误后恢复，零错误条件未通过；旧失败保留 |
| Goal bounded | 通过 | 缓存修复后第一模型耗尽预算；第二模型 242.93 秒完成全部验收 |
| Goal parallel budget / resume | 通过；已完成 Worker 批次不重跑 | 缓存修复后通过；已完成批次不重跑 |
| 中断恢复：主流程 / Worker / 已完成 Worker | 三类全部通过；副作用各一次，历史恢复 | 本轮未运行这组三类 Application；另有 checkpoint-only 真实 LLM 探针通过 |
| 四 Worker 架构修复任务 | 连续两轮中首轮严格交接验收失败、次轮通过；嵌套入口通过 | 本轮未运行 |
| 静态拒绝与运行时策略 | 均通过；静态拒绝本身不请求 LLM | 工具恢复场景覆盖真实 Hook 阻断 |

Chat 的九项现有 Application 验收全部通过；这不包含另列的复杂架构重复运行。
Anthropic Messages 未做真实 provider 验证；不能从 Responses 的结果推导该协议通过。
这次只覆盖上述 Python runtime 与 Application 场景，没有重跑完整 TUI、wheel 安装矩阵。

## 保留的失败及诊断

1. **已修复的协议失败**：Responses Goal 的初次 bounded 和 parallel 执行都因
   `input.content json: unknown field "cache_control"` 失败，促成第三项修复。
2. **Self-learning 步骤耗尽**：第一次 Responses Run 反复调用 session search，
   并漏传 memory / Skill 参数，达到 16 步上限。后续同模型新 Run 和第二模型
   完成工具操作，但还有下述格式失败；不将首次失败删除或计为通过。
3. **Goal bounded 预算耗尽**：缓存修复后的第一模型 Run 已完成四个 Worker，
   主模型却以九个不同 call ID 重复读取同一 ContextRef，每次返回相同的 12536
   字符内容；最终使用 610010 / 600000 token，状态为 `budget_limited`，尚未
   验证报告并显式完成。没有增加预算或改写验收器把该 Run 转成通过。
   第二模型在同一 600000 token 预算和原验收标准下完成四个 Worker、落盘报告、
   验证标记并显式 `update_goal(complete)`，最终验收通过。
4. **架构任务首轮严格交接失败**：代码产物、宿主测试和原始缺陷负对照通过，
   但 Supervisor 改写了上游结果，repair Worker 的输入 JSON 缺少结束括号，
   最终报告未严格对应独立 verifier。模型原始 tool arguments 与 Worker
   checkpoint 输入相同，实际 final-answer 工具结果也与持久化 result 相同；
   证据指向模型生成的交接内容，不是框架保存时修改。第二轮与嵌套运行均完整通过，
   原 `repeat-native` 汇总仍保持失败。
5. **Self-learning 格式失败与验收标准纠正**：三个完成的 Run 返回了对象或 JSON
   字符串形式的 `memory_proposal`。临时验收器一度接受这些值，并独立确认真实
   memory 工具和文件产物成功；复核 README 后确认它明确要求布尔值，因此
   三次旧 Run 均保留为严格格式验收失败。机器摘要为旧记录额外标注
   `strict_acceptance_status: failed`，不改写原始结果或证据哈希。补齐 workflow
   的类型指令后，使用恢复严格布尔断言的验收器重新请求模型，单独记录新 Run。
   Chat、Responses 原模型和第二模型三个新 Run 的四个状态字段均严格为
   `true`，且实际 memory、Skill 包和临时文件产物再次独立核验成功。但三个 Run
   分别出现 4 / 1 / 1 次 `skill_manage` 漏传 `content` 的执行错误后才恢复成功，
   仍不满足 README 的零非预期错误条件。机器摘要将最终格式、产物检查与完整
   严格验收分列；不再通过重复运行挑选零错误样本。

没有通过放宽正式格式、交接、产物或预算断言消除这些失败，也没有把模型任务不稳定描述为
运行时协议已完全可靠。适合提交带验证记录的 PR 审阅；若合并要求每项真实复杂
Application 一次通过，本轮证据尚不满足该门槛。

## 证据与复跑

脱敏机器摘要见 [runtime-recovery-validation.json](runtime-recovery-validation.json)。
原始日志、checkpoint、Run receipt、模型生成文件、失败诊断和扩展验证脚本保存在
本机 `/tmp/agentloom-runtime-fix-validation-20260920`，未提交私有配置或原始请求。
临时目录可能被系统清理；机器摘要保留结果与证据哈希，但不能代替全部原始证据。

仓库中的可复跑入口：

```sh
PYTHONPATH=. .venv/bin/python tests/acceptance/model_protocol_matrix.py \
  --workspace /tmp/agentloom-protocol-rerun
PYTHONPATH=. .venv/bin/python tests/acceptance/existing_application_validation.py \
  --case all --workspace /tmp/agentloom-app-rerun
PYTHONPATH=. .venv/bin/python -m applications.architecture_contract_validation.run_acceptance \
  --case all --repeats 2 --output /tmp/agentloom-architecture-rerun
```

复跑需要本机有效模型配置；Application 默认使用其 `model_type` 对应的 adapter。
Responses 扩展验收所用脚本为证据目录下的 `extended_validation.py`，支持
`--adapter openai_responses --profile responses_powerful2 --cases goal_bounded --workspace <新目录>`。
所有修复为本地提交；本次未推送 GitHub。
