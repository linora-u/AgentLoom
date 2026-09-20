# Ticket 01、02 集成验收与交接

日期：2026-09-20。状态：01、02 completed；按用户要求完成后停止，03–14 未启动。

两项已分别通过独立 worktree 开发并合入 `codex/pi-integration`。准备提交为 `45ddbfdc`，最终 Python 验收 revision 为 `42662ed6c93d15680bf8829048f408b3dcddf1d7`。后续本次收尾提交只更新文档；main 保持 `c697b24f602e71d56c9aa2c532e6079c0db5aa9d`，最终合并由 14 负责。

交付调整：用户随后要求放入 main 的 Changes 区。已将 integration 的完整成果展开为主工作区 main 上的未提交改动；迁移时核对内容树与 `8d63be5f` 一致，随后只更新本交接状态。没有新增 main 提交，也没有推送。

## 交付与边界

| 票据 | 已验证行为 | 证据 |
| --- | --- | --- |
| 01 | 锁定 npm 发布版 Pi 0.79.4 及依赖；真实 AgentSession、原生 provider、官方工具和自动压缩 | [实验与重放说明](../../../experiments/pi-sdk-compatibility/README.md) |
| 01 | 非法原始参数经异步 Python Hook 修正后严格校验；拒绝、失败和身份错配没有副作用；批次顺序和一次授权 | 实验的 hooks 用例，13 项 SDK 测试中的 10 项 |
| 01 | host 提交后、Pi 持久化前真实 SIGKILL；使用匹配的已提交结果恢复单调用且不重跑工具 | 实验的 recovery 用例及明确支持边界 |
| 02 | 中立模型选择、后端选项及来源；不要求所有后端持有 Python model binding；能力由实际配置推导 | [实现与过渡接口清单](02-implementation.md) |
| 02 | native fixture 经真实 Application 入口执行无工具、无 Goal 调用；Worker 并发构造具有独立实例与 Hook Run | native Application 验收与 Worker 并发测试 |
| 02 | Todo 和 terminal 注入归属 smol 适配器；旧 YAML、真实 smol 循环、文件工具和 Goal 保持可用 | smol Application 验收，Goal 开关两种情形 |

01 证明 SDK 接入能力，02 的 native fixture 证明公共构造可扩展。生产 Pi 注册属于 07，治理接线属于后续票据，完整恢复矩阵属于 12。线上 provider smoke 为 NOT-RUN。

## 集成验证

独立 validation worktree 从锁文件安装 Python 依赖，确认导入自己的源码，只复制示例 LLM 配置。在 revision `42662ed6` 执行：

```sh
uv run pytest tests/ \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_capsule.py \
  applications/memory_feature_validation/scripts/test_memory_review_campaign_contract.py \
  -v --tb=short --color=no
```

结果：**4,164 passed、1 skipped、3 warnings，exit 0，268.66 秒**。唯一跳过项是依赖 wexpect 的 Windows Shell 编码测试，当前平台为 macOS，Python 3.12.13。

第一轮全量曾暴露非法 `toolsets` 类型在校验前被逐字符解析的问题；`a5e40b3a` 将形状校验前置，保留原来的明确错误诊断。失败日志保留，修复后重新执行了上述完整测试。

协调者另行执行 `npm ci --ignore-scripts --no-audit --no-fund` 和 `npm run verify`，安装成功，TypeScript 检查及 **13 项真实 SDK 用例通过**。本次实际环境为 Node 25.9.0、npm 11.12.1、Python 3.9.6；没有把声明的最低 Node 版本当作已测试版本。

对 13 个变更生产模块执行 mypy：仍有 **12 项基线错误，无新增**。协调者在生产源码未变的准备基线上使用相同参数复验，忽略行号后诊断完全一致；不能据此声称全仓类型检查零错误。变更 Python 的 Ruff F 检查及 `git diff --check` 通过。

机器可读版本、源码树、命令及原始日志 SHA-256 见 [验证记录](01-02-validation.json)。日志留在仓库外同级 `AgentLoom-worktrees/evidence/t01-t02/`，SDK 独立重放结果位于实验目录的忽略文件 `evidence/result.json`。

## Standards

独立审查结论：0 项需修复发现。公共运行契约、Hook 生命周期和 smol 所属工具遵循 CONTEXT 与 ADR；smol 新增工具仍走 Gateway 治理。Pi 实验以独立执行门处理 SDK 吞掉扩展异常的情形。兼容字段及 lazy import 属于 expand 阶段的明确过渡形式。

## Spec

独立审查结论：0 项需修复发现。01 使用实际发布 SDK 并制造真实故障窗口，记录了证明范围；02 覆盖无 binding Application、并发 Worker、配置冲突和旧 smol 行为，没有提前宣称生产 Pi 或改写长期记忆。

两路审查范围为 `45ddbfdc...4295d185` 及 `78cf986d` 的 prompt 归一化修复。之后协调者检查了 `a5e40b3a` 的局部 toolset 校验修复，并在最终 revision 重跑全量测试。模型请求头交接提醒已写入 03 与 02 的实现记录。

审查计数：Standards 0，Spec 0；两个轴均无待修复发现。

## 后续交接：本次不启动 03

03 汇合这两项结果，冻结公共基线后，04、05、07、08 才能开新 session 并行。03 必须保留以下实证约束：

1. 参数修正在 Pi 初次校验前的异步 `message_end` 完成；`tool_call` 太晚，且扩展异常会被 SDK 捕获。执行门必须独立核对最终参数和一次授权。
2. 最终 schema 校验不得隐式转换类型。实验的顺序批次不等于已经证明并行工具结算；原始参数与最终参数都需要治理证据。
3. 恢复仅在 host 提交证据与 session、Run、call、tool、参数及 native parent 全部吻合时成立。实验只证明单调用成功恢复；不确定状态不得自动重放。
4. `RuntimeModelSelection.settings` 只包含选定 profile。03/07 仍需将有效 `model_request_headers` 策略按实例投影，保持优先级并避免凭证进入公共元数据或日志。

按用户要求，开发 worktree `t01`、`t02` 和独立 `validation` 已删除。已核对它们的提交全部包含在 integration 分支；该分支、两个实现分支和仓库外验收日志保留。新 session 需先确认 main 的未提交成果已进入所选基线，或从 integration 的已验证提交开始。
