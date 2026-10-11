# Worker batch v1 验收记录

E（#109）交付真实 Runtime 的 Application/CLI 验收和完整合同索引。它不代表
#110 的最终交付通过：已发现的组合控制异常、stop token 和回归问题须由 F 修复，
修复后的最终版本还要完成有界真实 Provider 验证。

合同：[Spec](../specs/worker-batch-execution-v1.md)，
[任务图](../specs/worker-batch-execution-v1-tasks.md)。主要测试入口是
Application Run → `BoundWorker.map()`；测试不读取线程池、Future 或 admission 私有状态。

## 版本、命令和可保留证据

产品基线：C `6c517bb9`，D `9499f247`，E 合并点 `e541ff38`。
验收实现：`fc4db8a3`、`9fb578fb`、`f684d531`；最后的 `db4dd5d3` 只调整 live campaign
的有界 Provider request retry overlay 和参数记录。报告提交只修改文档。
E 已包含 integration `9499f247`。

所有日志、JUnit、失败尝试和运行记录保存在工作树之外：
`/Users/bytedance/code/AgentLoom-private/.agentloom/validation/worker-batch-acceptance-20261011`。
下表中的路径相对此目录。报告不包含 live model 配置或凭据。

| 检查 | 实际结果 | 证据 |
| --- | --- | --- |
| C 正式版、D 合入之前，Python 3.12 / 3.14 | 各 162 passed，133.45 / 131.32 秒；不是最终 D 验证 | `py312-contract.log/xml`、`py314-contract.log/xml` |
| post-D，Python 3.14，`9fb578fb` | 304 passed，222.73 秒 | `py314-post-d-9fb578fb.log/xml` |
| post-D，Python 3.12，首次收集 | 缺少可选 repo_map `grep_ast` 依赖，collection error；保留原失败 | `py312-post-d-9fb578fb.log/xml` |
| post-D，Python 3.12，安装解析依赖后 | 302 passed，7 failed；七项均缺少 `networkx`，未删除迁移断言 | `py312-post-d-deps-ready.log/xml` |
| E 新 winning-source 检查首次执行 | 5 passed，0.84 秒；已有实现直接通过，未制造历史 red | `winning-source-first.log/xml` |
| CLI 成功而 batch 失败的保留检查 | 2 passed，17.11 秒；campaign 必须失败并保留实际终态及清理证据 | `failed-batch-report-first.log/xml` |
| E 代码 `f684d531`，Python 3.12 / 3.14 | 各 311 passed，238.79 / 222.40 秒，0 skipped | `py312-e-delivered-f684d531.log/xml`、`py314-e-delivered-f684d531.log/xml` |
| campaign policy `db4dd5d3`，Python 3.12 / 3.14 | 各 4 passed，41.80 / 36.53 秒，覆盖受影响真实 CLI 正/负例 | `py312-campaign-policy-db4dd5d3.log/xml`、`py314-campaign-policy-db4dd5d3.log/xml` |

最初的验收 scaffolding 错误和日志保留在 `scaffolding-attempts/`。这些是测试夹具的
JSON、内容形状和路径断言错误，不被当作产品 red。早期 `py312-final` / `py314-final`
的包名冲突与 pytest-mock 环境错误也保留。

每个 venv 都明确导入本 E 工作树的 `src/__init__.py`。环境：macOS 26.6.2 arm64；
Python 3.12.13 / 3.14.8；smolagents 1.26.0、litellm 1.104.2、httpx 0.28.1、
openai 2.54.0、MCP 1.26.0；Pi SDK 1.0.0、Node 24.15.0（ABI 137）；
pytest 9.1.1 / 9.0.1、pytest-mock 3.16.0。每次 Runtime 命令均显式使用 Node 24，
未因宿主默认 Node 25 不兼容跳过 Pi。Python 3.12 的 repo_map 可选依赖补齐日志为
`py312-repo-map-dependencies.log` 和 `py312-repo-map-graph-dependencies.log`。

在 E 工作树分别将 `PY` 设为 `.venv312/bin/python`、`.venv314/bin/python` 执行：

```sh
env PATH="/Users/bytedance/.nvm/versions/node/v24.15.0/bin:$PATH" \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "$PY" -m pytest -q \
  -p anyio.pytest_plugin -p pytest_mock --import-mode=importlib \
  tests/application_test/test_worker_batch_application.py \
  tests/application_test/test_worker_batch_storage.py \
  tests/application_test/test_worker_batch_runtime_application.py \
  tests/application_test/test_worker_batch_legacy.py \
  tests/lib_test/execution/test_worker_execution_policy.py \
  tests/lib_test/execution/test_worker_batch_values.py \
  tests/lib_test/execution/test_resource_ownership.py \
  tests/lib_test/execution/test_agent_runtime_contract.py \
  tests/agent_test/runtime_builder_test/test_runtime_builder.py \
  tests/pi_test/test_process_lifecycle.py tests/pi_test/test_application.py \
  tests/pi_test/test_tools_application.py tests/test_agent_as_tool_thread_safety.py \
  tests/lib_test/execution/test_parallel_executor.py \
  applications/repo_map/tests/test_skill_builder.py \
  --junitxml="$ARTIFACTS/$ATTEMPT.xml" > "$ARTIFACTS/$ATTEMPT.log" 2>&1
```

`ARTIFACTS` 为上述绝对证据目录，`ATTEMPT` 与表中日志名相同；实际展开后的命令另存
`commands.json`；`contract-id-audit.json` 检查了 50/39/26 个 ID，无重复或遗漏。
Ruff 与 `git diff --check` 通过。组合套件通过不能证明 repo_map
独立 collection 可用，该问题仍属于 F。

## 真实 Runtime 和 Provider

确定性验收使用真实 Pi SDK、smolagents、native read、Python Tool 和 stdio MCP；只替换
远端模型的 HTTP 服务。固定业务计划含四个独立已知事实
`BATCH-ALPHA-8149`、`BATCH-BETA-3952`、`BATCH-GAMMA-6471`、`BATCH-DELTA-9027`。
容量 1/20 运行同一计划，保持输入 ID、数量、payload、fingerprint 和归约结果。
四项计划不声称达到 20 个在途；C 的同步闸门用足够输入单独验证实际可达容量。
同一模型响应同时发出 read/MCP 两个 Tool calls，因此这也覆盖 Worker 容量与 Tool dispatch 解耦。

每个成功项都校验实际 Tool trace 的读文件路径、文件事实、MCP query/实际 reply、
模型输出与物理 Tool 返回值相等、Run/task/invocation/local/Hook 身份、usage 及 checkpoint。
HTTP 400 在真实物理副作用之后按输入选择触发；collect/fail-fast 保留全量 dispositions，
副作用标记证明没有重放完整 Worker。阻塞同步 Tool 的 stop 测试证明返回前真实工作和清理完成。
公开 Application map 的 executor submission 拒绝也保留原始错误和部分结果。

真实 Provider 使用两项有限计划，每次在独立进程、私有目录中运行；所有失败均有独立目录，
没有在一个 map 内重试完整 Worker。profile 来自根项目的 effective snapshot，包含忽略的
`model.local.yaml`。Pi 为 `codex_luna_news_initial / openai_codex_responses / gpt-6-luna`；
smol 为 `deepseek_v4_1 / openai_responses / ep-20260924170925-dggfr`，保留原 Ark endpoint。
smol 的主机路由问题使用进程内 `bind_ark_interface_route()`（`/tmp/agentloom_ark_route.py`），
保留 TLS/SNI/auth，通过物理 en0；未修改系统路由、代理或 TUN。

| 独立尝试 | 代码 / 容量 | 实际结果 | 证据 |
| --- | --- | --- | --- |
| smol 初次 | `fc4db8a3` / 2 | 完整 CLI 成功，16.17 秒 | `live-smol-attempt1/` |
| Pi 初次 | `fc4db8a3` / 2 | root 初末轮成功；两个 Worker 首轮 transport 失败，无业务 Tool；campaign 失败 | `live-pi-attempt1/`，`failed-resource-exit.json` |
| Pi 进程内显式代理诊断 | `fc4db8a3` / 2 | root 自身失败；没有系统修改 | `live-pi-attempt2-explicit-proxy/` |
| Pi 有限成功样本 | `fc4db8a3` / 1 | 完整 CLI 成功，47.43 秒；Worker total tokens 5185 / 5196 | `live-pi-attempt3-capacity1/` |
| smol post-D | `e541ff38` / 2 | 完整 CLI 成功，18.11 秒；两个 Worker 成功 | `live-smol-final/` |
| Pi post-D | `e541ff38` / 1 | 两个 map Worker 成功，usage 4229 / 5124；root 后续 Provider transport 失败，完整 CLI 失败，73.88 秒 | `live-pi-final/` |
| Pi refined harness | `9fb578fb` / 1 | root completed；item-0 Provider request failed（usage 1163），item-1 成功（usage 5279）；batch/campaign 失败，74.02 秒 | `live-pi-post-d-refined-9fb578fb/` |

关键 Run/batch：

- Pi 完整成功：`run_20261011T070027547012Z_d4f88c8beaeb` / `batch_20261011T070033144548Z_df9191357c50`。
- smol post-D 成功：`run_20261011T070446324073Z_d0b7268e638c` / `batch_20261011T070452159398Z_bf44bcc3b6ac`。
- Pi post-D map 成功、root 失败：`run_20261011T070446324055Z_e61fce401265` / `batch_20261011T070451615388Z_261eb05005ed`。
- Pi refined 失败批次：`run_20261011T083613631262Z_3a781f49bcf9` / `batch_20261011T083619821378Z_c9b23ce6c0c7`。

真实命令为上述 Node24 前缀加 `.venv314/bin/python tests/acceptance/worker_batch_validation.py`
`--runtimes pi --profile codex_luna_news_initial --model-source /Users/bytedance/code/AgentLoom-private`
`--items 2 --concurrency 1 --deadline 180 --output <独立绝对目录>`。
smol 相应为 `--runtimes smolagents --profile deepseek_v4_1 --concurrency 2 --ark-interface-route`，
并设置 `PYTHONPATH="/tmp:$PWD"`。每个目录中的 `campaign.json` 保存实际子进程命令、退出码和耗时；
`attempt.json` 保存准确 revision、dirty 状态、版本和 CLI 参数。180 秒是实验护栏，不是产品 drain deadline。

历史 live 尝试均显式使用 Provider `num_retries=0`。两个实际 profile 的原值都是 2；这属于
Adapter/SDK 的模型请求重试，与 map 的完整 Worker replay 不同。`db4dd5d3` 的 final campaign
改为 `min(profile.num_retries, 1)`，并在 attempt 保存 `provider_request_policy`；实际 profile
解析已确认 retries=1、timeout=45、max_output_tokens=2048。确定性 failure fixture 继续 retries=0。
修复后最终版本的 live gate 应采用这个有界策略，并继续验证每个输入只有一个 invocation、
物理副作用不重放。这里不把尚未执行的 retries=1 live gate 写成成功。

## 物理清理和凭据检查

Node shim exec 真正的兼容 Node，只记录 bridge PID/cwd。每项 Python Tool 持有真实 loopback
listener，并按 invocation 登记 close。验收在 sibling 阻塞时独立连接其 listener、检查其 MCP PID
仍存活；已完成 sibling 的 listener 已关闭。结束后 OS 检查每个 Pi/MCP PID、private temp directory
及 listener，子进程退出前检查 Pi reader/callback、AnyIO portal threads，campaign 父进程再检查 PID。
这些不是“close callback 被调用”或 registry 数量断言。

成功报告的 `proof.resource_exit` 均为无存活 owned PID、无开放 listener、无剩余 owned callback
thread，Pi 私有目录已删除。`live-smol-final` 与 `live-pi-attempt3-capacity1` 的 actual credential scan
分别扫描 254 / 221 个文件，0 matches。真实 API key 和 Pi access/refresh 仅在内存扫描，未输出。
私有 model 配置 mode 0600，目录 0700；model.yaml 是输入，不进入产物扫描或 Git。

两个 post-D Pi 失败的 `post-run-map-and-cleanup-audit.json` 由独立进程校验实际成功项 Tool trace，
并确认各 5 个已记录 Pi/MCP PID、所有 listener/private directory 都已消失；实际凭据扫描为
210 / 186 个文件、0 matches。**这两个旧失败 child 没有保存退出前线程快照**，因此不声称它们证明
Python callback thread drain；确定性测试与成功 live child 有该证据。
最新 harness 在 terminal 或 batch verifier 失败时写 `failure-report.json`，保留 child 内线程快照、
真实批次和 secret scan；确定性负例覆盖“root completed 但 batch failed”，不把它算作 campaign green。

## 50 stories / 39 decisions 审计

下表每个合同 ID 恰好归属一行。A：policy/value tests；B：storage/resource/invocation tests；
C：`test_worker_batch_application.py`；D：`test_worker_batch_legacy.py`、maintained caller tests；
E：`test_worker_batch_runtime_application.py` 及本节 live/artifact evidence。设计禁止项检查使用
`src/app/application.py`、`src/execution/concurrency/batch_runner.py` 和
[Context ownership](../research/worker-batch-context-ownership.md)，不创造额外验收接口。

| Stories | Decisions | 证据及边界 |
| --- | --- | --- |
| 1,2,3,4,50 | 1,2,3,4,5,39 | C current-run/standalone/facade；E固定计划、真实runner/CLI；NewsAgent durable consumer 仍明确排除 |
| 5,6,7,8,9,10,11,12,13,14 | 6,7,8,9,10,12 | A strict scalar/overwritten layer/unknown field；C composition/targets/alias/pinned binding；E五层真实 winning source receipt |
| 15,16,17 | 23,24,25 | C gated容量1/2/20/32/65、129大于输入、双批次各4可达8、capacity-one子调用；双Python版本 |
| 18,19,20,21,22,23,24,25 | 16,17,18,19,20 | A深immutable JSON/None/error；C整计划验证、caller/sibling snapshot、ordering/fast refill/empty；E固定计划 |
| 26,27,28,29 | 13,14,15 | B canonical分记录/root references；C相同scope pinned revisions；E实际credential scan与记录关联 |
| 30,31,32,33 | 26,27 | C/B fresh invocation、root memory/coordinator/context跨owner及背景drain；E真实Hook/usage/checkpoint、PID/listener/thread独立oracle |
| 34,35,36,37,38 | 22,29,30,33 | C collect/fail-fast/startup race；E真实HTTP错误、阻塞同步Tool stop/drain；无强制终止或产品退出deadline |
| 39,40 | 21,31,32 | B/C cleanup/required persistence/trace/observer；E公开executor rejection；控制异常与第二次持久化/cleanup组合仍需F修复 |
| 41 | 34 | C不重放；E真实副作用后HTTP错误、一次实际Tool执行；Provider请求retry与Worker replay分开 |
| 42,43,44 | 28 | C同步registration-close race、根资源寿命、stale binding/self-close与nested map独立窗口 |
| 45 | 11,35,38 | E容量1/20时真实响应含两个Tool calls；Pi Tools回归；同步map无hidden asyncio.run，async offload为文档中显式Application adapter，native async延后 |
| 46 | 36,37 | Dlegacy payload/receipt/status/order、auto/retired配置诊断、repo_map/Goal迁移；repo_map独立collection及旧mixed fixture待F |
| 47,48,49 | — | C/E公开seam；真实Pi/smol runner/CLI；Python最低3.12与开发3.14；最终交付版本live gate仍未关闭 |

## 26 个 acceptance matrix 场景

| # | 场景 | 实际验收入口 |
| --- | --- | --- |
| 1 | 容量1/2/20/32可达且不超限 | C `test_capacity_is_attainable_and_never_clipped_or_exceeded` |
| 2 | 容量大于输入且超过64 | 同上65；C `test_capacity_above_input_count_runs_only_supplied_items_and_empty_none_are_valid`（129） |
| 3 | 同Worker双批次各4、停止隔离 | C `test_two_batches_have_independent_capacity_stop_tokens_and_records` |
| 4 | 四配置层winning source/provenance | E `test_application_receipt_reports_each_winning_configuration_source`；A source-chain |
| 5 | override4/default20/override2 | C `test_call_overrides_sources_and_binding_snapshots_are_pinned_per_scope` |
| 6 | 无效scalar、被覆盖layer、unknown field | A policy parameter矩阵；C invalid composition；resolver严格校验call scalar |
| 7 | 无效target、canonical alias/escape | C invalid-unused-policy/valid-unused-worker-symlink tests |
| 8 | 末项非法或duplicate IDs零启动 | C `test_entire_plan_is_validated_before_any_worker_start`；A values |
| 9 | empty/None成功 | C capacity-above-input；A receipt None |
| 10 | caller nested mutation | C `test_input_snapshots_and_per_invocation_mutable_copies_do_not_cross_contaminate` |
| 11 | sibling mutable copy | 同一公开map测试及A deep immutable snapshot |
| 12 | 首项阻塞、后项完成补位 | C fast-later-completion；E真实sibling resource-isolation观测 |
| 13 | collect普通错误 | C ordinary-failures；E真实Provider failure collect |
| 14 | fail-fast完整dispositions/drain | C ordinary-failures；E真实Provider failure fail-fast |
| 15 | 显式stop/真实同步Tool阻塞 | E `test_real_synchronous_tool_stop_drains_actual_started_work_and_resources`；C stop cleanup |
| 16 | submitted/startup stop race | C `test_stop_at_submitted_startup_seam_has_no_fictitious_invocation_identity` |
| 17 | observer普通/control异常 | C observer-failure；二次persistence/cleanup遮蔽control异常的组合缺口由F关闭 |
| 18 | runtime creation/close/resource close | C construction/cleanup/root retry；B retained handles；Pi真实physical lifecycle fault tests |
| 19 | side effect后失败无整项重放 | E `test_real_provider_failure_keeps_dispositions_and_never_replays_side_effect` |
| 20 | owner exit-registration race/stale | C `test_owner_close_registration_race_drains_accepted_and_rejects_zero_work`、nested/stale |
| 21 | capacity-one parent-child | C `test_nested_child_map_uses_independent_window_and_self_close_fails_immediately` |
| 22 | concurrent/repeated identity/Hook/checkpoint/usage/resources | C invocation/context tests；E fixed-plan两个批次及实际物理oracle |
| 23 | 并发记录/root refs/credentials | B storage；C双batch；E actual secret scan |
| 24 | Worker容量与多Tool calls解耦 | E fixed-plan两runtime、容量1/20、同响应read+MCP；Pi Tools真实dispatch回归 |
| 25 | 固定plan在1/20等价 | E `test_fixed_business_plan_survives_capacity_changes_with_real_runtime_ownership` |
| 26 | 真实Pi/smol Application/CLI | E deterministic真runtime两CLI；上列live结果与限制；最终revision的有界live待F |

审计不是把已知缺口计作通过。F 的交付门槛包含：原始 control exception 在 cleanup/persistence
组合中的传播；broken stop-token `is_set()` 的机制错误处理；配置启动导入路径；旧 mixed-runtime
shared fixture 迁移；repo_map standalone collection；最终宽套与修复后版本的有界真实Provider运行。
这些产品/迁移修复不混入 E 验收代码。早期 prompt-root 网络 preflight 只用于定位连接问题，
不替代新 map 的任何验收结果。
