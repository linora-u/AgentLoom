# Worker batch v1 验收记录

最终冻结源码 `ed506f44` 已通过 CI 等价宽套（2,410 passed、1 项 Windows-only skip）、
Python 3.12/3.14 验收（各 582 passed、0 skipped）、repo_map 独立收集（各 60 passed）
及两个有界真实 Provider 完整 CLI。终态持久化的原始 control 保留问题也已修复，新增
26 个公开组合在两个版本和宽套均通过。产品验收、main 前置基线准备和 7 个旧实现
工作树清理均已完成。最新 main 合入 `6adf6183` 后的 NewsAgent 回归另有 360 passed。
正式交付入口是 main draft [PR #12](https://github.com/linora-u/AgentLoom-private/pull/12)；
公开 CI、双仓 main 合并和 #109/#110 关闭仍是发布门槛，详见末节交付更新。

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
Ruff 与 `git diff --check` 通过。历史组合套件不能证明 repo_map 独立 collection 可用；
F 的迁移及两个版本的独立 collection 结果另见最终集成版本证据。

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
`bb3c63a9` 的两个 live campaign 已采用这个有界策略，逐项校验实际 invocation、Tool
证据和完整 CLI 终态。其结果见末尾；它们不证明后续产品修复版本已经通过。

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
E：`test_worker_batch_runtime_application.py` 及本节 live/artifact evidence；F：原审查修复和复核。
下表中的测试已在 `ed506f44` 的最终 JUnit 中逐名核对，合同 ID 计数不是替代行为验收。
设计禁止项检查使用
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
| 34,35,36,37,38 | 22,29,30,33 | C collect/fail-fast/startup race；F true-once startup stop和broken-token停止观测/实际drain；E真实HTTP错误、阻塞同步Tool stop/drain；无强制终止或产品退出deadline |
| 39,40 | 21,31,32 | B/C cleanup/required persistence/trace/observer；E公开executor rejection；F原始control与普通二次persistence/cleanup、root lease错误及terminal control/control组合已通过；三个公开新增测试覆盖26种组合 |
| 41 | 34 | C不重放；E真实副作用后HTTP错误、一次实际Tool执行；Provider请求retry与Worker replay分开 |
| 42,43,44 | 28 | C同步registration-close race、根资源寿命、stale binding/self-close与nested map独立窗口 |
| 45 | 11,35,38 | E容量1/20时真实响应含两个Tool calls；Pi Tools回归；同步map无hidden asyncio.run，async offload为文档中显式Application adapter，native async延后 |
| 46 | 36,37 | Dlegacy payload/receipt/status/order、auto/retired配置诊断、repo_map/Goal迁移；F移除mixed fixture退役配置并提取稳定shared test support；repo_map两版本独立collection各60passed |
| 47,48,49 | — | C/E公开seam；ed506f44真实Pi/smol完整runner/CLI；Python最低3.12与开发3.14各582passed，宽套2410passed；未用历史green替代新版本验收 |

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
| 15 | 显式stop/真实同步Tool阻塞 | E `test_real_synchronous_tool_stop_drains_actual_started_work_and_resources`；C stop cleanup；F `test_broken_stop_observation_drains_actual_started_work_and_cleanup` |
| 16 | submitted/startup stop race | C `test_stop_at_submitted_startup_seam_has_no_fictitious_invocation_identity`；F `test_true_once_stop_at_invocation_start_is_latched_for_entire_batch` |
| 17 | observer普通/control异常 | C observer-failure；F `test_observer_control_survives_secondary_required_receipt_failure`；terminal-control公开矩阵保留原始signal与secondary cause |
| 18 | runtime creation/close/resource close | C construction/cleanup/root retry；B retained handles；F `test_worker_control_survives_secondary_cleanup_and_owner_retry`及root teardown故障；Pi真实physical lifecycle fault tests；新增terminal-control矩阵已通过 |
| 19 | side effect后失败无整项重放 | E `test_real_provider_failure_keeps_dispositions_and_never_replays_side_effect` |
| 20 | owner exit-registration race/stale | C `test_owner_close_registration_race_drains_accepted_and_rejects_zero_work`、nested/stale |
| 21 | capacity-one parent-child | C `test_nested_child_map_uses_independent_window_and_self_close_fails_immediately` |
| 22 | concurrent/repeated identity/Hook/checkpoint/usage/resources | C invocation/context tests；E fixed-plan两个批次及实际物理oracle |
| 23 | 并发记录/root refs/credentials | B storage；C双batch；E actual secret scan |
| 24 | Worker容量与多Tool calls解耦 | E fixed-plan两runtime、容量1/20、同响应read+MCP；Pi Tools真实dispatch回归 |
| 25 | 固定plan在1/20等价 | E `test_fixed_business_plan_survives_capacity_changes_with_real_runtime_ownership` |
| 26 | 真实Pi/smol Application/CLI | E deterministic真runtime两CLI；ed506f44有界live均完整CLI成功，终态/receipt/物理清理一致 |

审计没有把已知缺口计作通过。原 F 配置导入、mixed-runtime caller、repo_map collection、
broken-token、startup stop及普通二次cleanup/persistence修复已经通过集成版本测试。
新增的 terminal persistence control/control 缺口已由 `30605297` 保留原始对象和
secondary cause，并在集成源码 `ed506f44` 完成全部新版本验收。早期 prompt-root 网络 preflight 只用于定位连接问题，
不替代新 map 的任何验收结果。

## 历史集成版本 bb3c63a9 的证据与后续门槛

2026-10-12 回读的冻结版本为 `bb3c63a97b70ad7db8f39bc3711a60d4d1f5d129`。
这次记录只修改文档，没有再运行 Provider，也没有改变产品或测试。完整非 docs tree 的
SHA-256 为 `616d69e2e305c47d737cda52e08cf7ff52804a46e760676b6297a87033e0b5f0`；
Runtime harness 选择的 `src/applications/tests/test_support/config/pyproject.toml/uv.lock`
tree 指纹为 `df552d1740be0180780ee7a3af0fcf596bade7b7d5a956b67eb519ce552df0e3`。
支持版本 preflight 和两个 live attempt 都明确导入 E 工作树自己的
`/Users/bytedance/code/.worktrees/agentloom-worker-batch-acceptance/src/__init__.py`；
宽套导入根 integration 的 source。二者的完整非文档 tree 一致。

下列 `final-bb3c63a9/` 路径相对既有 acceptance 证据目录；宽套证据另在
`/Users/bytedance/code/AgentLoom-private/.agentloom/validation/worker-batch-final-20261011/`。
已完成进程的状态由 `result.json` / `campaign.json` 的真实退出记录、CLI terminal event
和 JUnit 交叉确认，不根据旧 session handle 是否存在推断成功。

| 冻结版本检查 | 回读结果 | 权威物证 |
| --- | --- | --- |
| Python 3.12.13 验收 | 556 passed，0 skipped，JUnit 269.290 s | `final-bb3c63a9/py312-final-attempt2.xml` / `.log` |
| Python 3.14.8 验收 | 556 passed，0 skipped，JUnit 267.875 s | `final-bb3c63a9/py314-final-attempt2.xml` / `.log` |
| repo_map 独立收集，3.12 / 3.14 | 各 60 passed，0 skipped，JUnit 22.968 / 23.132 s | `final-bb3c63a9/repo-map-py312.xml`、`repo-map-py314.xml`及各自日志 |
| CI 等价宽套，Python 3.14.8 | 2,384 passed、1 Windows-only skip；日志 1,015.72 s，JUnit 1,015.657 s；进程 wall 1,018.415 s、exit 0、source unchanged | final 证据目录的 `final-bb3c63a9/pytest.log`、`.xml`、`attempt.json`、`result.json` |
| Pi 有界真实 Provider | 完整 CLI `run.completed`，2/2 success，容量 1，exit 0，90.655 s | `final-bb3c63a9/live-pi-attempt1/campaign.json`、`pi/report.json`、`pi/cli.stdout` |
| smolagents 有界真实 Provider | 完整 CLI `run.completed`，2/2 success，容量 2，exit 0，16.565 s | `final-bb3c63a9/live-smol-attempt1/campaign.json`、`smolagents/report.json`、`smolagents/cli.stdout` |

唯一 skip 是 `test_shell_tool_windows_encoding_patch`，原因是
`Windows specific test reliant on wexpect`。Pi、smolagents、MCP 或 batch 相关测试没有跳过。
原宽套 `9499f247` 的 63 项失败全部保留在 `pre-review-9499f247/`：62 项源于 mixed
fixture 的退役 Supervisor `concurrency`，另 1 项为未修改的 Hook double-fork cleanup。
该 Hook 例曾独立重跑通过，且包含在本次完整绿色宽套中；没有删除它的原始失败。

版本来自 `preflight-py312.json` / `preflight-py314.json`，没有假定两个环境依赖完全相同：

| 项目 | Python 3.12 环境 | Python 3.14 / live 环境 |
| --- | --- | --- |
| Python / pytest | 3.12.13 / 9.1.1 | 3.14.8 / 9.0.1 |
| MCP | 1.30.0 | 1.26.0 |
| smolagents / litellm / httpx / openai | 1.26.0 / 1.104.2 / 0.28.1 / 2.54.0 | 相同 |
| Pi SDK / Node / Node ABI | 1.0.0 / 24.15.0 / 137 | 相同 |

### 命令来源和实际收集范围

历史支持版本 556 项的完整 shell argv 未持久化，不能从 JUnit 反推为“实际命令”。
这里保留实际 JUnit/log 观测范围：A/B/C/D/E batch/value/policy/ownership suites；
Runtime contract/builder；全部 `tests/application_test`；Pi Application/Tool/process lifecycle；
Agent-as-Tool thread safety；legacy parallel executor；repo_map SkillBuilder；17 项 architecture
checks。`final-contract-evidence-audit.json` 保存每个实际 module 的收集数量，以及 60 个
公开 batch/配置边界测试函数在 3.12、3.14 和宽套中的通过变体数。
repo_map 另以其整个 tests 目录独立收集，各 60 项。首个 Python wrapper 在收集前因 shell
引号丢失而抛 `NameError: name 'src' is not defined`；`py312-final.log`、`py314-final.log`
保留该失败，只有 `*-attempt2.xml` 用于验收。后续产品修复版本将先持久化精确 argv 再启动。

宽套的精确 argv 已保存于 `attempt.json` / `result.json`。在根 integration 中，展开后的
Python source assert wrapper 运行以下 pytest 参数；环境明确提供 Node24 和
`PYTHONPATH="$PWD:$PWD/applications"`：

```sh
.venv/bin/python -c 'import sys;from pathlib import Path;import agentloom,pytest;assert Path(agentloom.__file__).resolve()==Path("/Users/bytedance/code/AgentLoom-private/src/__init__.py"),agentloom.__file__;raise SystemExit(pytest.main(sys.argv[1:]))' \
  tests/ \
  applications/memory_feature_validation/scripts/test_offline_memory_campaign_contract.py \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_capsule.py \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_contract.py \
  -q --tb=short --color=no \
  --junitxml=/Users/bytedance/code/AgentLoom-private/.agentloom/validation/worker-batch-final-20261011/final-bb3c63a9/pytest.xml
```

两个 live campaign 的精确子进程 argv 在各自 `campaign.json`，以下只将证据路径缩写为
`ARTIFACTS`，并去掉绝对工作树前缀以便阅读；执行使用 `.venv314`、Node24 和本 E source：

```sh
.venv314/bin/python tests/acceptance/worker_batch_validation.py --child \
  --runtimes pi --profile codex_luna_news_initial --items 2 --concurrency 1 \
  --output "$ARTIFACTS/final-bb3c63a9/live-pi-attempt1/pi" \
  --model-source /Users/bytedance/code/AgentLoom-private

.venv314/bin/python tests/acceptance/worker_batch_validation.py --child \
  --runtimes smolagents --profile deepseek_v4_1 --items 2 --concurrency 2 \
  --output "$ARTIFACTS/final-bb3c63a9/live-smol-attempt1/smolagents" \
  --model-source /Users/bytedance/code/AgentLoom-private --ark-interface-route
```

两次真实请求策略均为 `num_retries=1`、`timeout=45`、`max_output_tokens=2048`，没有完整
Worker replay。每次 2 项各有一个独立 invocation，Tool trace 和实物读取结果、Hook/local
身份、usage、checkpoint、batch/root receipt 一致。

Pi Run/batch：`run_20261011T103420482595Z_a4dbb8ff8b2e` /
`batch_20261011T103437407990Z_7a6dc43380f7`；smol Run/batch：
`run_20261011T103420482565Z_2190739ae41d` / `batch_20261011T103424403618Z_b11bbf36e7b3`。
两个 live child 保存退出前 callback/portal thread 快照，PID、listener、owned thread 均无残留；
Pi 3 个 private directory 已删除。campaign 父进程再次检查 Pi 的 5 个、smol 的 2 个已记录 PID，
没有存活项。该容量 1 的 Pi 有限 live 样本不用于证明 sibling 并发；sibling resource isolation
由两个版本的真实 Runtime 确定性并发测试独立证明。

### 完整产物与合同审计

两个 final live child 的实际 credential scan 分别为 227 / 243 个文件、0 matches；
campaign 层连同 stdout/stderr/receipt 分别为 231 / 247、0 matches。
补充扫描整个 acceptance、final 和 F review-fixes 三个任务证据目录，以当前 effective
profiles 的实际 API key、Pi access/refresh 和 `fixture-key` / `fixture-secret` 两个哨兵
在内存中匹配，未输出或持久化凭据值。完整结果为 34,652 个文件、63,388,922 bytes，
actual credential / fixture match / read error 均为 0，见
`final-bb3c63a9/final-artifact-secret-scan.json`。

205 个 `model.yaml` 是输入配置，单独分类并排除于产物匹配计数。其中含当前实际凭据的
3 个输入均为 0600；历史合成 fixture 输入保留原模式（总共 85 个 0600、120 个 0644），
不能宣称整个历史目录每个 model 输入都为 0600。两个 final live config 目录为 0700、
model 输入为 0600。不删除历史失败，也不为报告改写历史产物权限。

`final-contract-evidence-audit.json` 核对 Spec 原文、上述归属表和最终 JUnit：50 stories、
39 decisions、26 matrix rows 均一一归属，无重复或遗漏；60 个公开 batch/配置边界测试函数
在两个 Python 版本和宽套均有实际通过记录。`final-terminal-evidence-audit.json` 从真实
exit records、CLI terminal、完整 batch 和 physical cleanup 核对进程完成状态。这些索引仍
明确记录复核新增的 control/control 缺口，不能把“ID 齐全”或旧绿色测试当作它已通过。

### 当时尚未关闭的交付要求（修复后证据见下节）

独立 Standards 复核在 `bb3c63a9` 没有剩余 hard/judgement finding；独立 Spec 复核确认
原修复成立，但新增 P1：`persist_uncaught_failure` 只捕获 `Exception`，终态写入第二个
`KeyboardInterrupt` 会遮蔽原始 Worker control。standalone 和 runner 的公开 probe 均失败，
实际 invocation 已 drain；runner 还跳过原 `ApplicationRunInterrupted` adapter。
完整分轴复核保留在 [review-fixes](worker-batch-execution-v1-review-fixes.md)，没有合并或改写排序。

该产品修复、新 revision 的必要回归/支持版本/有界真实 Runtime gate、最终复核尚待完成。
另一个任务拥有 main 前置提交发布；本报告不改变 PR #11 的 draft/baseline-target 状态。
PR retarget、公开同步 CI/合并、ticket closure 和本任务 worktree cleanup 仍是 integration
交付要求，不能因为当前版本的验收证据齐全而标记整个 spec 完成。

## 最终冻结源码 ed506f44：产品验收关闭

`306052971e6eaba16bbaef3ccbc32023c7033782` 修复 terminal persistence 的第二个 control
遮蔽原始信号问题，集成冻结点为 `ed506f4474552ab13ed622894c7e64ce6da89516`。
二者完整非 docs tree 相同，SHA-256 为
`2314313f9b7a3263073b77ed4f7036d47d5ab5390ca57192c69f2a08097c993e`。
Runtime harness 选择的 source tree 指纹为
`51cd41e1c0ecbf4be41ba8123ce5b6d834e73798709e04676cb13367323aa479`。
下面每个测试和 campaign 都在 clean 冻结源码上运行，返回后确认 source unchanged。
本报告提交只改 docs，不以旧版本的绿色结果替代修复版本验收。

新 acceptance 证据根目录为
`/Users/bytedance/code/AgentLoom-private/.agentloom/validation/worker-batch-acceptance-20261012/final-ed506f44`。
完整宽套证据另在同级 `worker-batch-final-20261012/final-ed506f44`。

| 检查 | 真实结果 | 证据文件 |
| --- | --- | --- |
| Python 3.12.13 合同/真实 Runtime/迁移/架构验收 | 582 passed，0 skipped，JUnit 222.638 s，exit 0 | `py312-acceptance.xml/log`、`py312-acceptance-result.json` |
| Python 3.14.8 同范围验收 | 582 passed，0 skipped，JUnit 215.624 s，exit 0 | `py314-acceptance.xml/log`、`py314-acceptance-result.json` |
| repo_map 独立收集，3.12 / 3.14 | 各 60 passed、0 skipped，JUnit 21.254 / 21.169 s，exit 0 | `repo-map-py312.xml/log`、`repo-map-py314.xml/log`及各自 result |
| CI 等价宽套，Python 3.14.8 | 2,410 passed、1 Windows-only skip；日志 761.45 s、JUnit 761.405 s、进程 wall 763.089 s，exit 0 | final 目录的 `pytest.xml/log`、`attempt.json`、`result.json` |
| Pi 完整真实 Provider CLI，容量 1 / 2 项 | `run.completed`，batch completed，2/2 success，32.809 s，exit 0，无 timeout | `live-pi-attempt1/campaign.json`、`pi/report.json`、`pi/cli.stdout` |
| smolagents 完整真实 Provider CLI，容量 2 / 2 项 | `run.completed`，batch completed，2/2 success，13.999 s，exit 0，无 timeout | `live-smolagents-attempt1/campaign.json`、`smolagents/report.json`、`smolagents/cli.stdout` |

支持版本 preflight 均导入 E 工作树自己的 source：Python 3.12 的 MCP 为 1.30.0，
3.14/live 的 MCP 为 1.26.0，其他已列 Runtime 依赖与前节相同；均为 Node24.15.0/ABI137、
Pi SDK1.0.0。唯一 skip 仍是 Windows/wexpect 专用编码测试；Runtime 和 batch 无跳过。

### 精确启动记录和公开合同覆盖

每次启动前已保存精确 argv、cwd、环境 overlay、source assert、版本和两个 tree 指纹：

- `commands.json`：两个支持版本及两个 repo_map 独立 pytest argv；`preflight-py312.json` / `preflight-py314.json` 记录实际导入路径和依赖。
- `results.json` 及四个 `*-result.json`：真实退出码、耗时、JUnit 数量、最终 source 状态。
- `live-launch-attempt1.json`：两个有界 Provider 父进程 argv；各 `campaign.json`：真实 child argv、退出码、耗时、父进程 PID 复查。
- 宽套 `attempt.json` / `result.json`：根 integration 的完整 CI 等价命令，使用上一节相同 pytest scope 和新的绝对 JUnit 路径。

支持版本使用全 `tests/application_test`（包含新 terminal-control module）及前节实际观测的
其他合同/Runtime/架构 modules，显式关闭 plugin autoload、加载 AnyIO/pytest-mock、采用
importlib collection。pytest source assert 通过结构化 subprocess argv 执行，没有历史 shell
引号错误。所有实际命令均保存于 JSON，没有反推或伪造缺失的历史 argv。
Provider 命令仍为前节相同 profile/items/capacity/deadline 参数，新目的地是上述 `final-ed506f44`
目录；smol 使用同一进程内 Ark route。每个 Runtime 只有一次新 attempt，未重放 Worker。

`final-contract-evidence-audit.json` 核对原 Spec、当前报告归属表及实际 XML：50 stories、
39 decisions、26 matrix rows 无重复或遗漏；63 个公开 batch/配置边界测试函数在两个版本
和宽套均有通过记录。新增 terminal-control module 的 26 个公开变体也在三个 XML 中逐项核对：

- 18 个 original/secondary 控制类型组合：KeyboardInterrupt、SystemExit、custom BaseException × 两个公开 entrypoints。
- 2 个普通 Application 错误后首个 terminal control：新 control 传播，原普通 cause 保留。
- 6 个成功工作后首个 terminal control：信号可观测，成功 batch 保持真实状态。

这些断言检查 exact exception identity、typed secondary causes、实际 started/canceled
dispositions、invocation IDs、真实 Runtime drain 及 runner terminal events，不检查私有线程池状态。
独立两轴复核在 `30605297` 无新 finding，其源码与冻结版本相同；原复核文本保留在
[review-fixes](worker-batch-execution-v1-review-fixes.md)。复核时尚未完成的最低版本 focused
运行随后实际通过：`final-py312-attempt2.xml` 为 162 passed、0 skipped、8.911 s；首次 Phoenix
autoload 缺少 `pkg_resources` 的失败日志没有删除。宽套和两版本 582 进一步关闭最终版本门槛。

### 终态、资源和历史证据保全

Pi Run/batch：`run_20261012T041646873607Z_59f24e57b4b1` /
`batch_20261012T041650513931Z_77ec7c2c961b`；smol Run/batch：
`run_20261012T041646873635Z_e287889c2460` / `batch_20261012T041650223313Z_f8062570e399`。
请求 overlay 均为 retries1/timeout45/max-output2048；每项独立 invocation、真实 Tool trace、
Hook/local 身份、usage、checkpoint 和 persisted batch/root receipt 一致。
子进程退出前所有 owned PID/listener/callback thread 无残留，Pi 3 个 private directory 删除；
父进程复查 Pi 5 个及 smol 2 个已登记 PID，全部退出。child actual-key scan 为 221 / 243 个
文件，campaign 层 225 / 247，均零匹配。`final-terminal-evidence-audit.json` 交叉核对真实
exit records、CLI `run.completed`、完整 batch、物理清理和宽套 source unchanged。

E 工作树内遗留的 `e-runtime-attempt3` 至 `8` 已按原内容和 modes 归档至：
`/Users/bytedance/code/AgentLoom-private/.agentloom/validation/worker-batch-delivery-20261012/retired-worktree-evidence/acceptance-validation`。
`acceptance-validation-archive.json` 记录 11,205 个 regular files、1,448 个目录、15 个 symlink、
22,164,565 bytes，内容/mode mismatch 为 0。只复制验证证据，保留 symlink 而不复制目标；
没有复制 venv、SDK、node_modules 或 caches。工作树清理不会丢失历史 scaffolding 失败物证。

完整实际凭据和 fixture-key 扫描覆盖全部 9 个任务证据目录，包括这份归档、初期 preflight、
F/terminal 修复 red→green、历史失败以及新宽套/live。当前扫描为 46,483 个文件、
87,514,847 bytes，actual credential / synthetic fixture match / read error 均为 0，见
`final-artifact-secret-scan.json`。239 个 model 输入单独分类、排除于产物计数；其中含当前
实际凭据的 6 个输入全部 0600。历史纯 fixture modes 如实保留，新 live config 为 0700、
model 为 0600；没有输出或保存 API key / Pi access-refresh 值。

## main 交付更新（2026-10-12）

产品修复、完整合同矩阵、支持版本、两个真实 Runtime 和宽套的本地验收均已关闭。
前置基线 `bc5cff6e` 已进入 `origin/main` 的 `68d7aca5`，并合入 integration
`6adf618381dd6b34f48ef4d15f339670fbfe6074`。这次合入只改变 24 个
`applications/news_agent/` 私有文件；排除 docs/私有目录的公开产品树与已验收 `ed506f44`
完全相同，指纹为 `a02be5f07159b2d01361e99b0df310ed7dde2de89319b502d179d87cdfd5bb5c`。
这是公开产品源码等价证明，不声称含 NewsAgent 的完整非 docs tree 与旧冻结点相同。

merger 在 `6adf6183` 使用 Python 3.14.8 / Node 24.15.0 执行整个
`applications/news_agent/tests/`：**360 passed、0 failures/errors/skips，pytest exit 0，
进程 wall 162.8395 s**。JUnit 和结果记录一致，返回后 tracked source/revision unchanged。
首次外层验收 runner 因新增 untracked `applications/news_agent/data/output/` 的 clean-state
检查返回 90，与 pytest exit 0 区分保留。merger 随后将 20 个运行产物（3,715 bytes）移动到
ignored `test-generated-news-output/`，校验内容 hash 后确认根工作树恢复 clean；原结果中的
dirty 状态未改写。此文档任务没有重跑 pytest 或 Provider。

这次 main 集成证据位于
`/Users/bytedance/code/AgentLoom-private/.agentloom/validation/worker-batch-delivery-20261012/main-integration`：

- `merge.json`：真实 merge、24 个私有变更文件和公开产品树等价证明。
- `public-source-evidence-carryforward.json`：逐项核对既有验收 XML、50/39/26 合同映射和公开源码未变。
- `news-agent-regression-attempt.json` / `news-agent-regression-result.json`：精确 argv/cwd/env、真实退出码、耗时和源码检查；`news-agent-py314.xml/log` 为原始测试证据。
- `news-agent-runtime-artifact-preservation.json` / `news-agent-regression-final-state.json`：运行产物保全及随后 root clean、tracked source unchanged 的真实复查。
- `delivery-final-proof.json`：integration `6adf6183` 已 push 并读回 origin feature，根工作树 clean，公开产品 diff 相对 `ed506f44` 为空；之后的文档提交仍由 merger 合入并推送。

| 交付项 | 已核实状态 |
| --- | --- |
| 本地产品验收 | 已完成；使用 `ed506f44` 的原始宽套、两版本、repo_map 和 live 证据，公开源码等价检查后保留效力 |
| main 前置基线 | 已就绪；`68d7aca5` 包含 `bc5cff6e`，integration 已合入最新 main |
| 7 个旧实现工作树 | 全部正常移除；policy/application/legacy/terminal-control/lifecycle/review-fixes/E 分支保留，历史证据已归档 |
| 正式 main PR | [PR #12](https://github.com/linora-u/AgentLoom-private/pull/12) 已建立，当前为 draft；公开 CI、公开 main 后私有 main 合并、#109/#110 关闭仍待完成 |

旧 [PR #11](https://github.com/linora-u/AgentLoom-private/pull/11) 于 06:12 UTC 合入临时
`codex/worker-batch-baseline`，merge 为 `2aba10e6`；这不是正式 main 合并，不能用于宣称
Worker batch 已发布。上文历史审查中 PR #11 的 draft/pending 描述按原审查时点保留。
已出现的同步成功或 skipped checks 不替代正式交付 revision 的必要公开 CI。

旧工作树清理以 delivery 根目录 `worktree-cleanup.json` 的
`cleanup_history` / `latest_final_checks` 为准：最终清理结束于 **04:41:27 UTC**，
`remaining_task_worktrees=[]`；最初 04:21 的四项清理快照也保留。
`retired-worktree-evidence/` 保留 acceptance/runtime/lifecycle 归档；前节凭据扫描仍是当时
46,483 个文件的快照，不扩称覆盖后来新增的交付 metadata。
`all-local-branches-push-20261012.json` 记录 06:16 UTC 已有 10 个本地分支和 2 个 tag 与
origin 一致，不包含之后创建的文档分支或之后发生的新提交。

本次新建的隔离文档工作树在提交交给 merger 后另行清理，不计入已完成的 7 个旧工作树。
用户已明确公共同步身份策略：**Author 和 Committer 都固定为 `linora-u`**。
该新增调整通过基于 `origin/main` 的独立 bootstrap PR 交付；因 trusted coordinator 使用
main 上的同步脚本，先完成该 PR 的公开 CI、公开 main 后私有 main 合并，再将 Worker batch
PR #12 标为 ready。身份策略的实现、受影响验证和真实发布结果另行记录，不属于上述
既有产品验收证明，当前正式 Worker batch PR 仍保持 draft。
公开 CI、双仓正式 main 合并及票据关闭仍需真实结果；本记录不将 draft PR 或本地通过
视作整个 spec 已完成。
