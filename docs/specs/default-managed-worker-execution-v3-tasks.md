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

- 已完成基线门槛和开发 worktree 准备。
- #124 按源码参考路线重新核对最小实现，#125 移除安装接法；旧 SDK 路线不再实施。
- #126–#129 尚待实施和验收。
