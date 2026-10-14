# 默认受管执行验收记录

2026-10-14。Spec #122 的实现与本地验收已完成，main 交付记录另列。OpenClaw 只作为固定源码参考；运行机制采用已有 Python／psutil，DBOS 3.2.0 继续作为唯一持久调度器，默认 SQLite。用户只配置并发数，无需选择 owner、安装 OpenClaw 或设置系统服务。

## 真实日期完整通过

正式 `prepare`、`run-day --stop-started-workers`、官方 `retry`、`status` 和 `evaluate` 验证 2025-08-18，使用 DeepSeek v4.1、认证 AnySearch 与独立 20／30／20 阶段额度。原始 1,740 条新闻，prepare 后 1,596 条，去重 144 条。初筛通过 442 条、不通过 1,154 条，全部有去向；初筛 20 批、精筛 30 批、校验及补充校验 26 批全部完成，日期封存。正式计分覆盖全部 41 条信号，无缺行情排除项，`scoring_complete=true`。

行情为官方复制的 115 个 ETF 文件，按现有历史资格规则有 111 个在截止前存在行情；其余四个不满足历史资格，未裁剪原候选池。正式入场日为 2025-08-19、离场日为 2025-08-22。计分为 8 条命中、19 条弱命中、14 条未命中，分数 `0.4268292682926829`。

首轮冻结源码为 `62dd0aeb`，初筛与精筛全部完成；校验遇到 AnySearch 内容提取失败，被错误归类为凭据耗尽。已修复为普通工具反馈，模型可以查询其他来源，同一健康 Key 保留。真实 HTTP 认证／额度／传输错误仍走已授权的切换规则；不从任意 MCP `isError` 或文本猜账户额度。

修复后的冻结源码为 `7a4d6787`，通过官方 retry 恢复 20 个失败校验任务并完成补充校验。初筛与精筛未重算，业务重试规则、模型、Prompt、全量输入和生产 MCP 90 秒超时未改。所有模型请求使用已显式配置的 65,536 输出预算；省略预算及 null 解除继承的框架合同另有公开 transport 回归。完整日期经历两轮源码，不能称为最终版本从零重算。

公开核对确认当前 76 个 SDK workflow 全部 SUCCESS，业务完成回执与完整 StageLedger 一致；230 个当前候选均已完成，包含 6 个行业净方向补充任务。两轮共 96 个资源作用域均确认物理退出、退出回执落盘，必需引擎终态确认通过；旧轮 20 个 CANCELLED workflow 及失败证据仍保留，未发现仍活着的受管身份。

实际 trace 中 989 个最终 HTTP 请求均携带显式输出预算 65,536。978 个可配对请求与响应的客户端区间，观测峰值为初筛 20、精筛 30、校验 20；另 11 个被中断的旧请求缺少响应，保留为未知，不纳入该峰值。累计可见响应 usage 为 input 40,439,264、output 1,928,693，其中 reasoning 1,718,505 是 output 子集，不相加计费。

真实 AnySearch 有 380 次完成且非空的响应，353 次含 URL；其中 255 次 search／batch_search 响应非空，254 次含 URL。另保留 14 次 extract 错误。该证据证明真实搜索及内容错误反馈经过正式 MCP，不单凭配置声称供应商账户计费归属。

新鲜业务 SQLite 诊断累计观察 593,020 条 connect／close／begin／commit 记录，覆盖首轮、校验 retry 及只读核验，未记录原始 SQLite 错误；DBOS 必需终态确认没有持久化错误。完整时间范围和分轮计数在独立 audit 中保留，旧轮 114,302 条记录没有冒充两轮合计。

## 公开故障矩阵

| Spec 合同 | 验证入口与证据 |
| --- | --- |
| 默认装配、每次 map 的额度、配置优先级、顺序结果、live factory 与 Context | Application／BoundWorker 的真实 Pi、smol Runtime 与 CLI；`test_worker_batch_runtime_application.py`、`test_python_process_owner.py` |
| DBOS 唯一调度器、队列稳定身份、跨 producer 唯一执行域、额度冲突、queued 无物理尝试 | DurableApplication 公开提交与 Client；`test_durable_submission.py`、`test_durable_executor.py` |
| 先登记、撤权、启动闸门、取消事务与 dispatch 竞态 | 实际 Application host 与 News 工具授权；`test_managed_durable_executor.py`、`test_managed_authorization.py` |
| 宿主 SIGKILL、根先退出、Pi／MCP／工具孙进程、端口及 Linux reaping | 实际 Application／公开 executor 强杀；`test_python_process_owner.py`、`test_python_durable_runtime.py`、`test_python_runtime_resources.py`、`test_python_smol_cli.py` |
| 未确认退出保留容量、退出回执写入失败、DBOS 终态提交失败、快任务补位 | 公开 status／stop／recover 与持久化故障注入；`test_managed_durable_executor.py`、`test_durable_executor.py` |
| 原始清理错误、多个 STOP 调用、控制异常身份、旧描述观察及恢复 | Application 和公开恢复入口；`test_python_process_owner.py`、`test_managed_durable_executor.py` |
| 已提交业务恢复、三个阶段自动衔接、独立额度、单阶段语义、静态 Runtime readiness | NewsService／正式 CLI 与实际 Application；`test_durable_controller.py`、News 业务回归和完整日期 |
| 输出预算、MCP 原始错误、内容错误反馈及真实 Key failover | Pi／smol HTTP transport、真实 MCP ClientSession；输出预算、AnySearch acceptance／failover、MCP manager 回归 |
| 标准安装、禁用旧安装器、无 OpenClaw 发行资产、最小依赖变更 | 标准 wheel 逐模块哈希核对及公开 CLI；安装纠正报告、archive/cache audit |

macOS：冻结 `e91c56c6` 的相关公开故障回归 66 项通过；其后的 Pi／smol readiness 4 项通过、预算 22 项通过、AnySearch 与 MCP 修复回归 41 项通过。News 全业务回归此前为 437 项通过，后续只重跑受影响检查，不把这组数量宣称为最终提交的单次全量结果。

Linux `ssh dev`：`62dd0aeb` 标准安装 wheel 的公开矩阵为 93 项通过。另六项因测试夹具明确要求 source import，在相同模块源码 overlay 下通过；不是 99 项都在 wheel 下通过。最新 `7a4d6787` wheel 的 Pi／MCP 与内容错误 delta 11 项通过。现有隔离 Python 3.12.15／SQLite 3.53.1 保留，仅补装声明的 smol 可选依赖，没有升级既有包。

`7a4d6787` 标准 wheel 为 `agentloom-1.0.1-py3-none-any.whl`，SHA-256 为 `840dfa99b25b64318ad64d37007855961c3a5be0ab7d500284d40902740093ef`，289 个 Python 模块与该冻结源码逐字一致。最终 CLI 合入 `07b93ba9` 后已重新核对全部 289 个模块；`src`、`pyproject.toml`、`uv.lock` 的 Git 对象不变，未重复构建或实跑无关业务。

最终 CLI 修复合入 `07b93ba9`，成功 stdout 保持单个 JSON，诊断实时输出到 stderr，失败时末行保持既有 error JSON，帮助输出保持 stdout。官方 retry 在真实 MCP 配置加载后的成功与输入校验失败、两种 help 共 4 项通过；完整 CLI layout 回归 43 项通过。最终 CLI delta 的 Standards 和 Spec 评审各为零发现。临时缓冲方案已撤回，没有新增 diagnostics 字段或修改业务错误分类。

相对基线 `6e1a8a85`，生产 Python 改动 37 个文件，新增 2,011 行、删除 354 行；测试改动 20 个文件，新增 1,866 行、删除 108 行。旧 Node 桥与 owner 安装清单删除 7,315 行，移除其专用 Node wheel 依赖及安装接法。Python 适配量有所增加，DBOS 仍独自承担持久调度，未再实现 claim／lease／slot 内核。最终框架 wheel 为 875,290 字节，不包含 OpenClaw 运行资产。

## 资料保留与边界

原始 2025-08-17／2025-08-24 的 18 个输出文件哈希不变；已有完成日期 2025-08-24 跳过。新增日期输出保留在 `applications/news_agent/data/output/2025-08-18`。运行资料保存到主工作区的 `.agentloom/runs/news_agent`、`.agentloom/traces/news_agent`、`.agentloom/validation/default-managed-v3-news-2025-08-18-v1` 和 `.agentloom/validation/default-managed-execution-v3-evidence-20261014`。原路径与 relocation 清单保留；复制 campaign 用于审计，不把旧路径绑定的 namespace 当成可直接恢复的移植目录。

历史 SQLite 522 的责任调用者仍未确定。本轮成功不关闭该 RCA；已独立确认的 SQLite 3.51.1 Unix WAL close 死锁则在接纳前拒绝，实际验收使用既有 canonical Python 3.14.8／SQLite 3.53.1。本机退出证明本机受管资源已退出，远端推理停止及供应商账户计费不由该回执证明。首版为单机多进程，远程存储接口保留，多机执行不在本轮范围。

## 证据索引

- [完整日期独立核验](../../.agentloom/validation/default-managed-execution-v3-evidence-20261014/independent-aug18-public-audit-7a4d6787.json)、[正式计分原结果](../../.agentloom/validation/default-managed-execution-v3-evidence-20261014/news-aug18-evaluate-7a4d6787.json)。
- [macOS 公开矩阵](../../.agentloom/validation/default-managed-execution-v3-evidence-20261014/merger-e91c56c6-public.log)、[Linux 安装矩阵](../../.agentloom/validation/default-managed-execution-v3-evidence-20261014/linux-installed-wheel-public.log)、[Linux source CLI](../../.agentloom/validation/default-managed-execution-v3-evidence-20261014/linux-source-cli-public.log)、[最终 Linux MCP delta](../../.agentloom/validation/default-managed-execution-v3-evidence-20261014/linux-mcp-delta-7a4d6787-public.log)。
- [最终 CLI 43 项](../../.agentloom/validation/default-managed-execution-v3-evidence-20261014/news-cli-final-json-regression.log)、[源码与发行包核对](../../.agentloom/validation/default-managed-execution-v3-evidence-20261014/merger-cli-final-result.json)。
- [Spec 主评审](../../.agentloom/validation/default-managed-execution-v3-evidence-20261014/review-spec-7a4d6787.md)、[Standards 主评审](../../.agentloom/validation/default-managed-execution-v3-evidence-20261014/review-standards-7a4d6787.md)、[CLI Spec 评审](../../.agentloom/validation/default-managed-execution-v3-evidence-20261014/review-spec-cli-a40a99dc.md)、[CLI Standards 评审](../../.agentloom/validation/default-managed-execution-v3-evidence-20261014/review-standards-cli-a40a99dc.md)。

文稿核对保留所有计数、源码边界、错误因果和范围；只更新当前验收事实，没有将历史报告改写为新机制证据。中文机械扫描及 source／output 哈希的结果保存在同目录的交付文稿检查记录。
