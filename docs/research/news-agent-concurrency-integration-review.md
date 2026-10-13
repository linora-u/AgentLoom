# NewsAgent 与框架并发接入审查

状态：事实研究与全日实跑已完成，具体持久后端仍待验证。基线为私有 main `94491c2d`；尚未修改产品代码。首轮真实初筛因输出预算耗尽失败；用户授权将本地预算调至 32,768 后，同 root 官方恢复、全日 run-day、封存及 evaluate 已成功。持久执行默认本地存储、接口可替换为远程后端已确认。

## 已查实的执行边界

| 对象 | 当前责任 | 接入时必须保留的区别 |
| --- | --- | --- |
| 框架 Application / BoundWorker | 有限 WorkPlan 的执行、配置来源、独立 invocation、结果与资源清理 | 一次 map 的容量不是整个阶段工作池的共享容量 |
| 框架 schedules serve | 持续检查到期任务并启动 Application | 调度一个应用不等于消费新闻阶段中的业务任务 |
| NewsAgent 阶段工作池 | 有空位才领取任务，持续补位，多个日期共享阶段容量 | 不能按日期各开一个容量 20 的 map 并称为总容量 20 |
| Store / StageLedger | 业务领取、有效尝试身份、工具提交完整性、重试、阶段完成与封存 | 模型或 Worker 返回成功不等于业务已完成 |

代码依据：`src/app/application.py:241,511,616`、`src/execution/concurrency/batch_runner.py:141,167`、`src/schedules/service.py:97`、`applications/news_agent/runtime/pipeline.py:211,268`、`applications/news_agent/runtime/store.py:329,562,791`。

## 为什么不能机械替换线程池

1. **领取时点**：先领取整个队列再 map，会使未启动项也占有有效期有限的租约。现有消费者仅在执行槽位空闲时领取。
2. **持续补位**：分波次领取 N 项再同步 map，必须等最慢一项结束后才能提交下一波；当前消费者在任一项完成后立即补位。
3. **每项可信绑定**：新闻任务需要独立 session、允许提交的序号、有效尝试身份、工具固定参数、初筛分类合同和 Stop 检查。这些由宿主确定，不能让模型选择。当前公开 BoundWorker 没有对应的每项准备接口。
4. **配置生效时点**：NewsAgent 的新尝试读取当前提示词、Skill、模型和定义；BoundWorker 在绑定时捕获定义，并由 Application 缓存。长期持有同一个绑定会改变现有行为。
5. **完成与清理**：工具 finalize 可以先写业务 completed，随后模型响应或运行退出失败。接入必须分别保存业务完成、执行失败及资源清理证据。
6. **必需持久化与通知**：框架 event_sink 是可失败的观察者；其普通异常只记诊断并停止后续通知。它不能承担必须成功的任务确认或租约操作。

对应依据：`runtime/worker.py:64,117,141,187`、`runtime/store.py:310,452,791`、`src/app/application.py:595,616`、`src/execution/concurrency/batch_runner.py:155`。

## 当前完善程度与待验证问题

新框架已经验证有限批次的容量、补位、输入快照、错误、stop/drain、作用域、上下文和资源清理。最终验收包含 Python 3.12/3.14 各 582 项通过及有界真实 Provider CLI；详见 `../reviews/worker-batch-execution-v1-acceptance.md:338`。这些是框架执行合同证据，不是完整 NewsAgent + AnySearch 业务证据。

长期消费者接入所缺的公开准备、领取及必需完成接口属于适配需求；缺少这些接口不构成已经复现的有限 map 缺陷。应研究把通用持久任务、尝试身份、领取、恢复及共享容量纳入框架，应用继续定义新闻阶段与业务完成规则。迁移必须明确兼容哪些现有恢复行为。

NewsAgent 代码还暴露了以下需要故障注入验证的边界：

- 心跳续约失败后，心跳线程退出，但正在进行的模型或搜索没有收到中止信号。工具拒绝过期写入与停止消耗资源是两件事。
- **已在隔离故障注入中复现**：stop 在外层循环检查，内部填充空位时没有逐项检查。在容量 3 的 worker_pool 中，首次 claim 后设置 stop，仍继续 claim 两项；停止领取应逐项参与 admission 判定。
- stop-workers 返回的是停止请求已提交，没有等待进程物理退出。真实验收应继续核对工作池终态与资源退出。
- **已在隔离故障注入中复现**：旧尝试失去租约后晚到的 retryable 错误，其 fail 因 owner 不匹配被拒绝，但随后的 `retry(strict=False)` 不携带旧 owner。它能把新尝试在 max_task_attempts=3 时的 failed 状态改回 pending，随后 claim 出第 4 次尝试。任务失败、失败重试与尝试身份必须作为一个有条件的状态转换处理。

依据：`runtime/pipeline.py:214,237,277,287`、`runtime/supervisor.py:204`。这里的 runtime 路径均相对 `applications/news_agent/`。

竞争复现调用当前 `NewsPipeline._execute` 和真实 SQLite Store，只有模型调用替换为可控晚到异常，无模型请求，不访问整日运行 DB 或共享输出。脚本及观测结果：`.agentloom/validation/news-concurrency-20261012/fault-injection/stale-attempt-retry.py`、`stale-attempt-retry-result.json`。这是恢复竞争的单独验证，不代替真实 NewsAgent 全日验收。

停止补位复现使用当前 `worker_pool` 与真实 Store，首次领取后设置 stop，执行函数仅在独立临时 DB 完成任务，没有模型请求。脚本及结果同目录 `stop-admission.py`、`stop-admission-result.json`。此探针验证停止时继续领取的行为，不验证真实 Runtime 的中止能力。

## 设计讨论树

已确认：保持新闻筛选、校验、计分及失败恢复业务规则，评估统一执行层；先完整运行一个真实日期，不裁剪新闻样本，保持默认 20/30/20。

第三轮原建议把领取、租约与队列恢复保留在 NewsAgent。用户提出把持久化队列与恢复纳入框架，并要求继续研究开源项目。这是本轮研究的输入，原建议未被接受。

待研究的前置事实：Temporal、DBOS、Prefect、LangGraph、Celery、arq 如何划分通用持久执行和业务编排、恢复粒度、并发范围及外部副作用。详见完成后的 `durable-agent-execution-open-source.md`。

事实收敛后，再确认框架持久执行边界、部署依赖、跨日期容量、新尝试配置、业务完成后执行失败及失去租约时的处理。架构选择尚未形成，不预先写成 accepted ADR。

第四轮已确认：默认本地持久化，例如 SQLite；接口应允许替换为远程后端。因此默认部署不引入外部服务。自建 SQLite 核与嵌入 DBOS 的路线仍需比较，不把“本地 SQLite”自动等同于必须自建。

第六轮已确认：首版覆盖单机多进程、共享阶段容量，失租时停止旧 Worker 并确认本地资源退出后再恢复补位；预留远程存储，首版不承诺多机器分布式执行。部署边界已记录为 `../adr/0001-local-durable-execution-with-pluggable-store.md`。

第七轮已确认输出预算修复合同：未配置不额外注入 cap，显式数值才限制，null 可解除继承，输入预留独立；必需 cap 的协议能力未知时要求配置或提前报错。详见 `model-output-budget-default-review.md` 与 `../adr/0002-explicit-output-cap-with-independent-input-reserve.md`。这是已确认的修复合同，尚未实施产品修改。

## 完整真实验收的现场条件

- 三阶段配置均为 `deepseek_v4_1` / `openai_responses` / Pi；关闭原生 web_search。校验使用官方 AnySearch MCP，真实联网及非空返回已在本次运行验证。
- 使用开发月 `2025-08-24`：原始 1,802 条，按当前准备算法去重后 1,426 条，默认 20 个初筛分组。初筛正文共 681,840 tokens；每组 33,023–35,892 tokens。这不是完整模型用量或费用估计。
- 当日 111 个历史已上市 ETF 均有 `2025-08-25` 入场价及 `2025-08-28` 出场价；现场 115 份行情副本校验通过。旧的当日 6 条 probes 不能作为整日验收。
- CLI 可用，现场没有遗留 NewsAgent 工作池。独立 --root 可隔离输入、数据库及工作池标记；正式结果输出和模型运行目录仍由应用统一管理。检查时两个共享目录均不存在，没有已有结果需要覆盖。
- 运行应按正式 CLI 完成 prepare、copy-prices、run-day、封存检查及 evaluate，保持默认 20/30/20，不裁剪新闻、不伪造结果、不因故障静默降低并发。
- 必须实际产生校验候选，并有成功的 `mcp__AnySearch__*` 调用及查证材料。没有候选的空校验不能证明联网路径。
- 分别记录全量阶段工具覆盖、业务封存、计分状态、实际在途峰值及清理终态。配置容量 20 不等于观测到 20 个同时运行；必须如实区分。

正式验收已启动，证据目录：`.agentloom/validation/news-concurrency-20261012/full-2025-08-24-20261012T083423Z`。prepare 得到全部 1,426 条新闻与 20 个初筛分组，copy-prices 完成 115 份行情副本，run-day 实测初筛 20 项同时运行。首轮 run-day 因 `Pi model request failed` 退出 1，最终初筛 16 批完成、4 批失败，工具覆盖 1,143/1,426。未进入精筛和校验，没有真实 AnySearch 成功调用、封存或计分；不能把此次失败当作完整验收通过。三个工作池均 stopped，采样归属于本次运行的 23 个后代进程核查均已物理退出。

### 已复现的配置问题与恢复边界

四个失败批次 `initial-0002/0005/0012/0019` 的实际请求均带 `max_output_tokens=16384`，实际输出达到相同上限，底层错误为 `Response incomplete: length`。其中三批 16,384 tokens 全部用于 reasoning，另一批 reasoning 为 16,339 tokens。长度错误不属于当前瞬时重试类别：SDK request attempt 为 0，Store attempts 为 1，不能表述为已经耗尽三次任务重试。

NewsAgent profile 省略 `max_output_tokens`，但 `src/config/llm_config.py:191` 对非 Codex adapter 注入默认上限；`src/runtimes/pi/bridge/model.ts:133` 将它传给 Provider。因此 README“应用不设置 max_output_tokens，模型原生上限仍有效”遗漏了实际生效的框架默认值。此问题影响完整任务能否执行，当前证据不能把它归因于并发上限或网络。

还有一项已查实的 Provider 兼容缺口：四个响应的 rawStopReason 为 `incomplete.length`。本机 Pi SDK Responses adapter 仅将 `incomplete_details.reason=max_output_tokens` 归为 stopReason=length；Provider 实际返回 reason=length，结果被归为 error。框架现有预算耗尽的 output_validation 分支因此未命中，最终成为不可瞬时重试的 generic provider error。修复应统一响应原因的语义，而不是对所有 provider error 盲目增加重试；本次未修改 SDK 或产品代码。

正式 CLI 的 `--config` 只覆盖新闻 settings；Worker 从固定 Application 路径加载模型，不能通过该参数覆盖输出预算。四批通过官方 `retry` 在同一 root 恢复，16 批已完成项不会重复；重复相同上限不能保证成功。后续实际请求已验证服务端接受 32,768，但原生最大输出仍未知，不从 Pi 自注册的默认值反推供应商能力。

第五轮用户已明确允许自行调整输出预算。恢复仅修改已忽略本地模型配置中的 `max_output_tokens`，从 32,768 开始，使用真实请求核验服务端接受；如果仍仅是预算耗尽，最多再提高一次到 65,536。保持模型、reasoning、Prompt、完整输入、默认 20/30/20 和已完成 16 批，不修改产品代码。首轮失败记录保持原样，后续结果另行记录。

### 同 root 恢复后的结果

四个失败批次在 32,768 下全部完成，原 16 个已完成批的 spec、entries 与 attempts 保持不变。未再出现模型 length 失败，无需提高为 65,536。

- 初筛 20/20 批、1,426/1,426 条完整覆盖。
- 精筛 30/30 批、447/447 入选新闻完整覆盖。
- 主体校验 186 个候选完成后，按原规则追加 3 项行业净方向校验，最终 23 个校验任务与 189 个候选全部完成，真实 AnySearch 路径得到非空返回。
- 官方 `run-day`、`evaluate` 均退出 0，日期已封存，没有绕过新鲜度校验。
- Store running 实测峰值 20/30/20；它表示持租任务数量，不等于同时 Provider 请求数。所归属进程最终剩余 0；实际 Runtime/请求峰值单独核验。
- 仅本地忽略配置增加显式 32,768 输出预算，tracked 产品源码差异为 0。首轮失败与恢复过程均保留，不能把此次成功表述为隐式默认值或新持久框架已修复。

最终 [完整验收报告](../../.agentloom/validation/news-concurrency-20261012/full-2025-08-24-20261012T083423Z/campaign/full-day-acceptance.md) 与同目录机器 JSON 已落盘。真实 AnySearch 非空成功 batch_search 110、search 23、extract 109；31 次 extract_failed 与 1 次参数 blocked 保留为已反馈的工具错误，最终任务没有失败。77 条方向 review、131 个业务来源实例均通过历史可得时间检查。

独立 runtime scope 区间与 709 组完整 HTTP→SDK response 客户端在途边界的阶段峰值也为 20/30/20；实际 Pi node 每 2 秒采样峰值为 30，供应商内部调度峰值未知。累计采样 83 个归属进程，最终存活 0。

官方计分 32 个聚合信号：命中 4、弱命中 7、未命中 21，综合得分 23.4375%，缺价排除 0。这验证执行与计分完成，不代表业务预测表现已经充分。

配置可追溯性的边界：当前 News baseline model_signature 读取公开 model.yaml，不纳入 model.local.yaml 补丁。本次 cap 变化由运行回执及真实 HTTP 请求记录，官方新鲜度检查没有被绕过；后续配置签名应使用脱敏后的有效配置，把非秘密的本地覆盖纳入 revision，不能因为忽略凭据就一起忽略执行参数。
