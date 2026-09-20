# 11 实施与验收记录

本票从 main `2ec1412c` 创建 `codex/pi-t11-mixed` 独立 worktree，消费已完成的 09。只实现混合应用、必要公共接线和独立验收，不修改 Pi bridge/Node lock、原生读写执行器或记忆产品政策。

## 已验证的行为

- smol Supervisor → Pi Worker 与 Pi Supervisor → smol Worker 均从 YAML 经 `execute_app` 运行；分别调用官方 `read` 与自研 `read_file`，检查实际结果、模型工具选择和 Pi 持久 journal。
- 每个方向并行运行两个 Worker，随后再次调用同名 Worker；模型调用 ID 可以重复，实例/Hook/local Run 不复用，root Run/task 保持一致，先前会话不流入下一实例。
- 现有只读专业工具 `get_file_outline` 产生共享 ContextRef；另一个基座的 Supervisor 检索第一次执行产生的大纲。验证在中途修改源文件，通过原文产物校验不是重新执行。
- Worker 的 update_goal 被拒绝；Supervisor 收到真实读取证据后仍观察到 active，再由 root 完成目标。
- smol 执行真实文件读取和有明确可信证据声明的业务工具，经过现有 ReviewOrchestrator、SQLiteEvidenceGate 和人工认可 API；新的 Pi Run 在原文件删除后使用认可记忆。
- 待审核候选不可见；应用记忆不泄漏到其他 Application，显式人工推广后 Project 记忆才共享。另在 Pi root 已开始后认可上一 Run 的候选，确认随后启动的 smol Worker 仍使用该 root 已冻结的旧 snapshot；下一 root 才看到新记忆。

真实模型验证定位并修复三处公共接线：

- 审核提示明确 provenance 必须是完整对象数组，可信领域事实照原文引用；不改变候选解析、证据门禁或认可政策。
- Responses 可在最终 assistant JSON 旁返回 ReasoningItem；审核层忽略这一已有协议元数据，仍拒绝 Tool/ToolResult/user 项和没有有效最终 JSON 的返回。
- 注入的记忆 snapshot 之前进入 SessionStart/TaskCreated 等事件的 task_text，被既有防伪 fence 规则拦截，导致后续工具账本不可用。公共 invocation 现在记录注入前的业务任务，模型继续收到冻结的 snapshot；不豁免或删除用户输入中的伪造标签。Goal objective/fingerprint、checkpoint、运行绑定维持原行为。

平台记忆、历史、ContextRef、审核与 Goal 的语义沿用现有实现；不新增必填 YAML，也不把 Pi 局部会话当作长期记忆。Application 业务工具只声明经过字段校验的固定策略事实，不把任意读取内容升级成可信记忆。

## 验证入口

- `tests/application_test/test_mixed_runtime_application.py`：公开 Application 入口，真实 smol/Pi runtime + HTTP 模型 fixture。
- `tests/application_test/test_mixed_runtime_memory.py`：真实运行、持久证据、审核状态机、scope 与 root snapshot。
- `tests/acceptance/mixed_runtime_validation.py`：隔离子进程、真实 provider，多种 profile；每次尝试保存 revision、dirty、Run、工具 ledger、原始审核模型输出和验收结果。
- `applications/mixed_runtime_validation/`：可运行双向 YAML 与领域策略 fixture。

逐项证据见 [11-validation.json](11-validation.json)。公共修复按回归先红后绿执行：两个记忆账本场景和一个 Responses 元数据场景先失败；修复后相关 19 项通过，扩展 Application/审核/生命周期/Goal 141 项通过，7 个修改文件类型检查无错误。最终完整回归 **4423 passed、1 skipped**，443.51 秒。真实矩阵最终轮 17/18，其中一例 provider HTTP 超时；随后记忆交接两个 profile 复验 2/2，通过全部 18 个 case/profile 组合。59 次历史尝试中 52 通过、7 失败，全部保留。

复现命令：

```sh
uv sync --locked --all-groups
uv run --locked loom install-runtime pi
uv run --locked python -m pytest tests/ applications/memory_feature_validation/scripts/test_memory_review_campaign_capsule.py applications/memory_feature_validation/scripts/test_memory_review_campaign_contract.py -q --tb=short
uv run --locked python tests/acceptance/mixed_runtime_validation.py --output /absolute/path/to/new-private-directory --profiles powerful responses_powerful
```

类型检查使用 pyright 1.1.403，范围是新增 Application/验收 Python 和公共 invocation/review_orchestration，共 7 文件；不是全仓库类型检查。确定性测试只替换外部 HTTP 模型，真实 provider 两个配置均实际访问模型服务。Pi SDK 0.79.4，Node v25.9.0，Python 3.12.13。

保留所有真实失败：smoke-01 为 2/3；campaign-01 为 15/18（provenance 形状、空候选、模型重复请求同一文件）；campaign-final-02 为 16/18（snapshot 污染历史、Responses 元数据误拒）。campaign-final-03 为 17/18，其中 powerful 的审核 HTTP 请求超出 240 秒中断，后续用同样的用例单独复验两个 profile。均没有重标为未运行或绕过门禁；外部私有证据目录保留原始结果。全量初验发现旧合同测试硬编码所有发货 YAML 都为 smol；本票新增 Pi YAML 后，将该断言更新为已支持的两个 runtime，旧字段禁用检查保留。

## Standards

独立 Standards 审查及各次增量复核通过，剩余 0 项可操作问题。

## Spec

独立 Spec 初审指出需要显式核验 native session ID；已补足三个 Worker 的非空且唯一 ID 和 root/task/instance 绑定断言，复核通过。公共修复增量亦通过，剩余 0 项。

## 交付

本票已于 2026-09-21 正式合入 main：`2ec1412c` → `96db1542`（快进，保留全部阶段提交）。固定引用 `refs/agentloom/ticket11-frozen` 指向 `96db1542bf7c869cfa32672950209ef5815c93da`。本次 `/Users/bytedance/.codex/worktrees/pi-t11-mixed/AgentLoom` 已删除，实现分支和外部验证记录保留。合入后的源码和测试与受验版本相同；main 的 Python 导入路径仍指向 main。源码冻结为 `be3c35daf29748b21696db1ac3f9c77dae46af3a`；完整真实矩阵在 `5778592b` 执行，二者差异只有 YAML 合同测试的一条断言，应用、runtime 和真实验收脚本内容一致。10/13 的 worktree 和未提交改动不在清理范围；14 仍须等待 12/13，不能把本票完成当作整体完成。

## 验证边界

本票不验证 Pi 写入/Shell（10）或恢复（12），也不宣称已完成 Pi-only 发行安装（13）。ContextRef 的源文件中途替换、根 snapshot 冻结及应用/项目 scope 隔离由真实 runtime + 受控模型的 Application 回归证明；真实 provider 矩阵覆盖两个方向的实际工具读取、并发实例、Goal、ContextRef 和记忆/历史交接。模型服务会超时或违反约定，验收会失败并保留原始记录；不承诺每次模型采样都成功。14 仍需在全部依赖已集成后复验组合。
