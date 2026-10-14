# 默认受管执行任务图

本任务图实施 [Spec #122](https://github.com/linora-u/AgentLoom/issues/122)。用户已恢复实施，要求使用独立 worktree，基线必须先进入 private/public main，保留既有 output，真实日期从 2025-08-18 起且跳过已有 output 的日期。

2026-10-14 更新：只参考 OpenClaw 源码，禁止安装、执行或打包其原包／SDK／运行资产；撤销旧直接复用路线。项目环境保持最小改动，保留既有 Python 和依赖；完成验收与 review 后合入 main 并删除开发 worktree。

## 基线门槛

- private main 包含 `6e1a8a854c364d2b0fc2520739848f054f19091f`。
- [public PR #123](https://github.com/linora-u/AgentLoom/pull/123) 于 2026-10-14 合入；导出提交为 `5e33a654008aa2a33564aa107b0c6f4375921ade`，public main 合入提交为 `5628c6a48fae8e62c7b42f0a76dee59c11bb102c`。
- 四个必需 public 检查通过，已核对 public main 树与该 private 基线的公开过滤结果完全一致。
- 主开发 worktree 使用 `codex/default-managed-execution-v3`；实现者各自使用基于此 tip 的独立 worktree。

## 依赖图

| Ticket | 职责 | 前置 |
| --- | --- | --- |
| [#124](https://github.com/linora-u/AgentLoom/issues/124) | 参考源码收敛已有 Python 资源边界、宿主死亡通知与清理确认 | 无 |
| [#125](https://github.com/linora-u/AgentLoom/issues/125) | 撤销 OpenClaw 安装与专用依赖，保护标准安装和 CI | 无 |
| [#126](https://github.com/linora-u/AgentLoom/issues/126) | durable 授权顺序、清理未确认、容量及恢复 | #124 |
| [#127](https://github.com/linora-u/AgentLoom/issues/127) | 有限 map 的 live factory 与 native 资源接线 | #124 |
| [#128](https://github.com/linora-u/AgentLoom/issues/128) | NewsAgent 统一控制、trace 和 output 保留 | #125、#126、#127 |
| [#129](https://github.com/linora-u/AgentLoom/issues/129) | 完整平台／真实日期验收、双轴评审与交付 | 全部以上任务 |

实现者遵循已确认 Application／正式 CLI seam 的 TDD；一个 red→green slice 完成后再进入下一项，不堆积假想测试。实现分支完成前合并 integration 最新 tip，由 merger 合入 integration。首次实现合入后创建 draft PR，完成全部规格和验收后转 ready。

## 资源与输出约束

- 现有 2025-08-17、2025-08-24 output 已建立哈希清单，原始目录不删除、不覆盖；2025-08-24 已完成，将跳过。
- 模型和 AnySearch 凭据仅存在忽略的本地配置，复制后权限为 0600；不得进入 Git 或公开 trace。
- 真实验收从 2025-08-18 开始，采用完整新闻、完整行情、DeepSeek v4.1、真实 AnySearch 和 20／30／20。运行前记录源码、构建、配置、新闻、行情和退出证据。
- 旧原型证据不代表最终模块通过；Spec 每个实现条款、验收行和交付物需有当前构建证据，缺项保持未完成。
- 本地与 ssh dev 的 OpenClaw 运行资产、专用 Node wheel、构建包及缓存已清理；自动安装原型未提交并已撤回。保留源码研究和诊断证据，18 个既有 output 文件的哈希未变。

## 当前进展

- #124–#128 的实现已合入 integration：默认 Python 资源监护、撤销原包安装、DBOS 授权及恢复、有限 map 和 News 统一执行层。DBOS 保持唯一持久调度器，旧 SDK 路线已移除。
- #129 的本地验收完成：macOS／Linux 公开故障矩阵、真实 Pi／MCP／smol、完整 2025-08-18 DeepSeek v4.1／AnySearch、封存和正式计分均有实际证据。当前 76 个任务全部 SUCCESS，两轮 96 个作用域确认退出；18 个原始 output 文件哈希不变。
- 最终 CLI 因果修复及 43 项回归通过，Spec 和 Standards 两轴复评均无剩余发现。初筛／精筛源码为 `62dd0aeb`，校验 retry／补充任务为 `7a4d6787`，CLI 合入为 `07b93ba9`；完整日期没有因 CLI 改动重新计算。
- [验收记录与证据索引](../reviews/default-managed-worker-execution-v3-acceptance.md) 按构建、入口和范围区分已有证据。历史 SQLite 522 仍是独立未定因事项。
- [private PR #15](https://github.com/linora-u/AgentLoom-private/pull/15) 通过既有 public-sync 流程导出公开代码，四个必需 public 检查通过后协调两个 main；实际交付状态以 PR 和 Issues 为准。保留资料后删除本任务全部开发 worktree，保留同步状态分支。
