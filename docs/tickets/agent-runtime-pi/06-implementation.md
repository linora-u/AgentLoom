# 06 实施与交接

基线：main `7965cc00`。分支：`codex/pi-t06-governance`。本票不修改 Pi bridge、09 SDK 调用或 wire v1 合同。

## 归属与接口

- `runtime/tool_governance/files.py`：通用文件权限与 stale-write 判定。smol 的读缓存、换行处理和写入算法继续留在适配器。
- `runtime/tool_governance/shell/`：已有命令安全、路径、白名单、AST 和只读判定。smol 原模块保留兼容 alias；Shell 会话、进程、watchdog、输出和审计存储仍在 smol。公共策略通过 `policy_audit` 接受调用方提供的审计对象。
- `runtime/tool_governance/search.py`：共用配置源与搜索排除匹配。smol 负责 ripgrep 参数生成及 Python 遍历，在实际读取前使用排除规则；原生 Shell 搜索无法证明排除范围时拒绝运行。
- `NativeToolHost` 扩展 05 的生产主机，`NativeReadToolHost` 仍是兼容名字。绑定的 manifest 决定 logical name、路径和命令参数；模型可见别名不会替代逻辑授权名。
- 写入流程：Hook → 最终解码 → CoreToolGuard → stale 检查与备份 → grant → 执行前再次检查路径和文件身份 → 外部 executor → durable settle。执行前文件变更记为 cancelled + blocked dispatch receipt，不能继续使用原 grant。
- 成功且执行期间文件版本未变化的读取，才成为本 host 的覆盖许可；每次覆盖后失效，需要重新读取。取消、不确定或错误结果不提供成功记忆证据。
- 备份位于 Run 的 `native-file-history/<instance hash>/`，独立于是否开启 checkpoint；已有 checkpoint 同时使用最终参数和 manifest 路径名。

## 10 接入前必须验证

06 验证的是生产治理层 + 独立文件/子进程 executor fixture，**不代表真 Pi 写入/Shell 已接通**。

1. 文件读写必须消费 `start_execution()` 返回的精确参数；写前已读证明要求同一 host 保存，不能在每次调用间重建 host。
2. 原生 Shell 调用必须声明 `logical_name=shell_tool`、`operation=shell` 和 `command_parameter`。需要 sandbox 或带 Shell 排除规则时当前拒绝授权，待 10 证明真实执行映射后扩展。不会静默绕过隔离。smol 请求不可用 sandbox 也会阻止执行。
3. 原生目录查询带排除规则时当前拒绝执行；真 Pi grep/find 的递归排除映射必须另行证明，不能把顶层目录通过授权当成递归查询已获准。
4. 原生 `settle` 只保存 executor 本次提交的原始结果，绝不重新执行来补原文。`result_scope` 独立记录 query limits、展示截断、原始 journal 位置与摘要；`source_completeness=unknown` 明确不把 SDK 已截断数据声明为完整。10 必须在 Pi 截断前捕获数据或登记原始产物，不能把未知覆盖当作完整覆盖。
5. 允许任意解释器的 Shell 规则本身不构成 OS sandbox；沿用既有安全模型，隔离要求必须由真实 executor 满足，否则拒绝。

## 验证入口

- `tests/lib_test/runtime/test_native_write_shell_host.py`：真实文件修改、写前备份、stale、拒绝、执行前文件变化、Shell 子进程、隔离拒绝。
- `tests/application_test/test_native_write_shell_application.py`：14 个场景通过真实 `execute_app`、Hook 和生产 host；模型为 CI fixture。
- `tests/application_test/run_native_write_shell_live.py`：远程模型驱动相同 Application，保留每次运行记录及失败；不是 SDK 测试。
- `tests/tools_test/search/test_query_governance.py`：绝对排除路径与 include 优先级在 ripgrep / Python 两个执行器均不泄漏。

## 审查后的补充

- 备份复制失败必须向上抛出并阻止授权；备份索引持久化失败后，即使内存已有记录，新调用也必须重试索引，不能绕过。
- 查询排除同时匹配逻辑路径、符号链接目标和绝对/相对排除路径的真实目标；保留 `/` 根目录，`.` 与父目录按查询根和 workspace 规范化。ripgrep 的排除参数位于用户 include 之后，Python 在打开文件前检查。
- 配置查询排除后，Shell 使用正向字面语法（包括拒绝花括号展开），只放行不含扩展的字面量 `printf/echo/pwd/true/false` 和逐路径检查的直接 `cat`。其他命令、包装器、输入重定向拒绝执行，使用专门搜索工具；未配置排除时沿用现有命令政策。此限制避免不断增加包装器黑名单。
- 原始产物引用提供实际 snapshot 路径与 `/raw_output` JSON pointer，写入 receipt 和 ToolCallRecord metadata。限额内的第一份原始结果可直接取回，后续文件变更不会改写它。
- 命名文件版本字段用于核对 inode、时间、大小和解析后路径。

## 真实 Application 验证

两个实际配置的模型 profile：`powerful`、`powerful2`。生产入口均为 `execute_app`；原生场景使用真实模型 + 独立文件/子进程 executor fixture，smol 场景使用真实 smol runtime。原生全部通过不等于 Pi SDK 端到端通过。

| 批次 | 验证内容 | 通过 / 总数 |
| --- | --- | --- |
| campaign-1 | 14 场景 × 2 模型 × 2 次，另有 8 次 smol 读取对照 | 64 / 64 |
| smol-1 | 创建、读取后覆盖、拒绝命令、排除搜索 × 2 模型 | 8 / 8 |
| campaign-final | 审查修复后的覆盖/stale/命令/查询/隔离/大输出 | 14 / 14 |
| campaign-after-review | 最终代码 `d3c37aa5` 的相同关键场景 | 14 / 14 |
| smol-after-review | 最终代码的 8 次真实 smol 对照 | 7 / 8 |

正式验收合计 **107 / 108**。原始结果位于项目外 `AgentLoom-validation/t06/`；凭证配置不纳入 Git。

失败没有删除或算作通过：`smol_powerful2_search_excluded` 达到 5 步上限。另行诊断重放再次失败（独立于上述 108 次），Hook ToolCallRecord 显示 5 次调用都只含 `pattern`，省略要求的可选 `path/include`，因此搜索默认 workspace，随后重复调用而未结束。诊断原始结果未包含该应用明确排除的 `files/secrets/hidden.txt`，也未包含凭证配置文件；排除规则生效，但此模型的整次任务完成能力仍有限制。工具层之外在哪一层省略参数，本票未作未经验证的归因，也没有通过改掉旧 YAML/default 参数语义来掩盖问题。

## Standards

两路独立 code-review 中的 Standards 复核通过：未发现剩余硬性规范问题。公共治理无 smol 反向依赖，资源按实例/Run 关闭，位置数组已改为命名文件版本字段；原先的备份失败与查询范围问题已由公开入口回归关闭。

## Spec

Spec 复核通过：未发现剩余本票规格阻塞。备份存储故障、索引失败重试、包装命令、符号链接、相对/绝对排除别名、`/`/`.` 与 Shell 展开均有定向覆盖。真 Pi 映射仍归 10，并明确保留未验证映射的拒绝行为。

审查余项：Standards 0，Spec 0；两轴均无剩余最高级别问题。

## 最终验证与交付

- 最终源码提交：`d3c37aa552a0c089a4c88526571ec32d44a4430e`；随后只补交付文档。
- 全量回归：**4382 passed、1 skipped、3 warnings，328.61 秒**。命令：`.venv/bin/python -m pytest tests/ applications/memory_feature_validation/scripts/test_memory_review_campaign_capsule.py applications/memory_feature_validation/scripts/test_memory_review_campaign_contract.py -q --tb=short`。
- 原候选全量为 4376 passed、1 skipped；最后审查新增路径边界修复后重跑最终全量，未用旧结果代替。
- `uv run --with mypy mypy src/runtime/native_tool_host.py src/runtime/tool_governance/files.py src/runtime/tool_governance/search.py --follow-imports=silent --ignore-missing-imports`：通过。
- 独立 worktree 使用 `uv sync --locked --all-groups`；Pi 依赖经 `uv run --locked loom install-runtime pi` 安装，SDK `0.79.4`，未依赖主目录 node_modules。
- 三个源码阶段提交 `6e946190`、`f70eb8ec`、`d3c37aa5` 保留，正式合入 main；冻结入口 `refs/agentloom/ticket06-frozen`。本票 worktree 在合入后清理，不清理其他票据的工作区。
- 10 的 06 前置已满足，仍必须等待 09 的已验证集成提交；不提前宣告真 Pi 写入/Shell 已完成。
