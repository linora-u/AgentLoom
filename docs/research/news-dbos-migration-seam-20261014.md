# NewsAgent 的 DBOS 迁移接点

2026-10-14。依据本地 v2 规格、当前实现及缓存的 DBOS 3.2.0 官方源码。未请求远端模型、搜索服务或输出私有配置。本文件记录迁移边界和本地证据，不声明完整系统验收完成。

最短路径是让 DBOS 执行一次由 OS 管理的完整 Application，News 保留业务计划、提交授权和完成事实。DBOS 管入队、恢复与容量，OS owner 管进程与资源退出；业务 SQLite 不再领取工作，也不拥有执行 lease。[v2 规格](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/docs/specs/durable-worker-open-source-engine-integration-v2.md:77)。

## 删除执行机制，保留业务流程

| 接点 | 当前边界 |
| --- | --- |
| Store.claim／recover／renew | 保留旧 API 兼容；正式 Host 仅使用 grant、receipt 与审计，不使用时钟续约或业务领取。[grant](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/store.py:288) |
| Pipeline._execute／worker_pool | 默认消费中的 heartbeat、ThreadPoolExecutor、claim 轮询与 validation 后台线程已由共享 durable executor 替换；run_stage／run_day 转应用服务。Planner、_query、finish_stage、seal、evaluate 保留。[run_stage](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/pipeline.py:209) |
| Supervisor 三组进程和 leases | 正式入口使用单 executor，20／30／20 绑定三个实际 Worker 队列，多日期共享。必须先拒绝并存旧消费者，再切换执行权威。[Supervisor](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/supervisor.py:49) |
| worker.call_worker 的自建 RuntimeHome／Agent-as-Tool 根入口 | 保留兼容入口；正式 Host 使用 public Application.open／BoundWorker.invoke。process-local factory 绑定 ledger、fixed_args、初筛分类合同、Stage Skill、动态 Stop 与 AnySearch。[factory](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/worker.py:227) |
| Service.run_day／retry／status／stop | 保留 CLI 业务语义；同日编排 flock 只防重复 Planner／封存，不承担执行 lease。[Service](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/service.py:308) |

初筛过滤、精筛分段、整条新闻校验、输入哈希、来源时间检查、industry_net 补充校验、投影与计分保留。validation 重试沿用原 task id/spec/batching，不能为换执行器重算既有分组。[分组测试](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/tests/test_validation_submission.py:257)。封存仍检查业务完整覆盖、候选与补充任务，不能读 DBOS SUCCESS 就直接 sealed。[seal](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/pipeline.py:268)。

## 业务 revision 与执行 attempt 分开

Durable input 为完整初始 query，加 news_task 的 id/day/stage/spec/revision。根路径及本地配置位置由可信 HostSpec 安装，任务不携带 Store、Agent、closure 或 Event。Host 校验完整 descriptor 指纹，重新计算 remaining query，再以 query-only WorkItem 调用 Worker；模型看不到 news_task。[descriptor](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/durable_host.py:30)。

DBOS workflow ID 由 namespace、Worker、item.id、revision 决定；相同身份的不同不可变输入必须拒绝。[workflow identity](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/src/execution/durable/client.py:62)。自动重试保持 revision；人工 retry 显式递增 revision，保留 task id/spec 与历史累计 attempts。每次调用 fresh Application，读取当前定义／Prompt／Skill／模型并冻结该 attempt。执行 config revision 与业务分析新鲜度指纹分开。

阶段 manifest 先在业务事务保存完整任务列表，跨库 enqueue 中断后只补既定任务。这个对账不能形成第二个 claim 循环。

## grant、完成回执和历史

Store 的 managed_tasks schema 有版本标记（当前 v3），新增当前 grant/revision、task_attempts、completion_receipts 与 task_retry_operations，不新增执行队列。[schema](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/store.py:169)。既有只读命令不隐式修改 schema；写入口在确认旧资源退出后显式迁移。

1. grant 在 BEGIN IMMEDIATE 内核对完整业务描述、revision、输入指纹、累计预算与旧 grant。并发安装只能有一个成功；新 attempt 保存旧状态和部分结果，再按原规则整项清理。
2. StageLedger.submit/finalize 在同一业务事务检查当前 revision/grant。session 固定原 revision，不能读取新任务 revision 来替旧 Worker 续权。[权限检查](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/store.py:876)。
3. finalize 同事务验证完整覆盖、写 task completed、冻结业务结果与 Runtime/config 身份、写唯一 receipt 并清 grant。部分 submit 允许持久化，最终完成事实和回执一起提交。[finalize](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/store.py:1110)。
4. revoke 只撤销 exact task/revision/invocation grant，并保存原部分结果。晚到提交拒绝；旧 revoke/failure 不改变新 attempt 或复制新结果到旧审计。撤销记录不宣称物理退出。[revoke](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/store.py:342)。

业务 commit 后、DBOS ACK 前崩溃，reconcile 读取同 revision receipt，零模型返回。已完成业务遇到 Runtime／trace／cleanup 尾部错误不回退；原错误和实际 WorkerOutcome 保留在 attempt，OS owner 仍必须证明旧资源退出。正常返回与回放使用同一结果形状，附 business_receipt；实际 usage 与原 Run 身份来自该 receipt 对应 invocation 的 attempt。[reconcile](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/durable_host.py:43)。

Root/config 身份在首个可能模型请求前写入 attempt，并随 finalize 冻结。Worker 返回或抛错时立即保存实际 outcome，避免 Application 清理异常盖住失败用量；共用 exception_outcomes 从异常链提取回执。完整 Application 关闭后 Host 才向 OS adapter 返回。durable timing 只附 performance，保留 Application 自己的最终 manifest status。[invoke](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/durable_host.py:82)、[timing](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/worker.py:240)。

若业务已完成，但 outcome/usage 写回前进程死亡，reconcile 返回 usage=null、usage_complete=false，保留已冻结的 Run/config 身份，不能称作零 token。旧 completed 由显式 migrate_completed_receipt 验证完整覆盖后幂等冻结既有结果，保留 owner/started/ended/attempts/error provenance；未知 Runtime/usage 保持 null，不虚构历史 attempt，不调用模型。[legacy 完成迁移](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/store.py:240)。day pipeline_receipts 仍是封存投影，不能代替逐 task 原子完成回执。

## 重试与原历史保全

整项重算清 entries/invalid；refined 删除本 producer candidates；validation 清当前批次旧 verdict，保留原分组。同一 attempt 内 submit 的纠错/upsert 继续保留。[重算边界](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/store.py:614)。

News 自动重试规则仍为 retryable 或 output_validation，并受持久累计 max_task_attempts 限制。未耗尽预算且当前 grant 匹配时，失败事务写 retrying，SDK 负责下一次调用；业务服务不能在 SDK 重试前把它当终态失败或重新 enqueue。耗尽预算写 failed；completed 的尾部错误永不自动重算。Host 向 OS 传明确 retryable 决策，OS 分类回退不能覆盖明确 false。[失败事务](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/store.py:361)。

人工 retry 只接受最新 failed task，整组原子校验；保存操作前状态／唯一旧错误／部分结果，生成新 revision 与操作许可，下一真实 attempt 消费许可并关联操作 ID。累计次数不清零。旧库没有逐次 attempt 的部分不能补造，保留可证明的累计计数和旧状态。[retry](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/store.py:759)。

DBOS 3.2.0 public StepOptions.should_retry 可承载分类决定，不需要应用自建 retry 循环；SDK 一次调用的 max_attempts 不能替代 News 累计计数。failed step 会重放原错误，ERROR 需要 public rewind，不能将 resume 当新执行；产品人工 retry 使用显式新 revision。[should_retry 官方源码](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-contract-reassessment-20261014-v1/3.2.0/dbos/_core.py:390)、[失败重放](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-contract-reassessment-20261014-v1/3.2.0/dbos/_core.py:2331)、[public rewind](/Users/bytedance/code/AgentLoom-private/.agentloom/validation/dbos-contract-reassessment-20261014-v1/3.2.0/dbos/_client.py:887)。

## SQLite 诊断与本地证据

业务 Store 直接观察原 sqlite3 core，未改变 connect 的 timeout=30/isolation_level=None/uri、busy_timeout=30000 或 WAL 设置。campaign 业务根 sqlite-diagnostics 中的 JSONL 仅含时间、PID、native thread ID、connection identity、事务阶段、绝对 DB 路径、原 sqlite_errorcode/sqlite_errorname；不含 SQL、新闻、错误正文或私有配置。不加载 system SQLite probe，不引入第二个 SQLite core。诊断写失败不替换原业务异常；body/commit/rollback/close 分别记录，保留原异常对象。[诊断](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/runtime/store.py:490)。

本地零远端模型验证：Store／NewsHost 17 tests passed（7.32s），覆盖并发 grant、finalize×revoke、旧回调隔离、累计预算、人工历史、legacy 完成迁移、真实 App 失败用量／root manifest、完成后尾错、未知用量、空输入零 Runtime。[业务测试](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/tests/test_durable_business.py:18)、[Host 测试](/Users/bytedance/code/AgentLoom-private.worktrees/durable-worker-v2/dbos-proof/applications/news_agent/tests/test_durable_host.py:46)。

早一轮 Host/business＋Stage Pi/usage/neutral compatibility：25 passed（32.16s），仅既有 MCP 弃用 warning。删除旧消费链前 Store/业务兼容基线：53 passed（61.74s）。这两组是各自冻结时点证据，不能代替当前正式 CLI／共享容量／OS 故障验收。尚需父任务完成多日期共享队列、CLI 完整业务链、强制中断后先 drain 再恢复，以及目标 Linux OS adapter 验证。
