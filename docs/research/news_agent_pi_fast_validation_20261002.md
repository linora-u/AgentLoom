# news_agent Pi Fast 隔离验证（2026-10-02）

## 范围

- 主仓库基线：`81db2404568de994e252a5e16156e19c22e2b419`；验证分支：`codex/news-pi-fast-20261002`，路径：`/home/lin/code/AgentLoom-pi-fast`。
- 从当前 `applications/news_agent` 工作目录复制约 419 KB 的代码、Prompt、Skill、workflow 和配置；未复制嵌套 `.git`、新闻/行情数据、历史评估与输出。根配置 `config/llm.yaml` 以权限 `0600` 复制，仅供隔离运行；Pi OAuth 文件未复制，运行时沿用已有 Pi 登录。
- 原 news_agent 工作目录在验证期间仍有其他改动；隔离副本以本次刷新时的代码为快照。后续合入前应再检查应用侧补丁与当时文件的差异，并重跑相应测试。
- `applications/news_agent` 是独立 Git 仓库，且其当前工作目录有未提交改动。主仓库分支不会自动包含该项目的配置修改；准确的两文件变更保存在 [`patches/news_agent_pi_fast.patch`](../../patches/news_agent_pi_fast.patch)。原目录执行 `git apply --check --unidiff-zero` 已通过，尚未应用。

## 改动

- Pi Codex 模型配置接受 `service_tier: fast` 或 `default`；非 Codex 模型拒绝此项。
- 桥接层在已有 `onPayload` 请求投影中把 `fast` 映射为 `priority`，逐次请求传给 Codex；未配置时维持原行为。Codex 官方配置文档明确说明 `fast` 映射为请求值 `priority`。
- 副本中的 news_agent 三个模型档案通过 YAML 锚点启用 `service_tier: fast`。模型配置参与 news_agent 版本指纹，因此此改动使旧缓存失效。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 独立虚拟环境及 Pi SDK 0.87.1 安装、桥接层 TypeScript 编译 | 通过 |
| `tests/pi_test/test_codex_application.py` | 29 通过；覆盖 Fast/Standard、联网开关、非法参数与原有 Codex 行为 |
| `applications/news_agent/tests/test_baseline_pi.py` | 6 通过；初筛、精筛、来源复核及重试的请求都携带 `priority`，切换层级会改变模型版本指纹 |
| Python Ruff、`git diff --check` | 通过 |
| 当前 news_agent 初筛的真实 Pi 请求 | `run_20261002T153847748722Z_17d138e057a6` 成功；1 次请求、0 次重试，约 4.6 秒；原始请求记录显示 `gpt-6-luna`、`service_tier: priority`，输出符合当前 `selected_ids`/`rejected` 合同 |
| 独立 news_agent 补丁应用检查 | `git apply --check --unidiff-zero` 通过；未改原目录 |

探测过直接将 `fast` 发给当前 ChatGPT Codex 端点：HTTP 400，返回 `Unsupported service_tier: fast`。按官方映射发送 `priority`：HTTP 200、模型完成；原始响应仍标记 `service_tier: default`。因此本次只证明 **Fast 对应的请求参数已正确发送并被接受**，未证明实际推理获得加速或按 Fast 层级计费。Pi 的简化会话接口不将请求层级传入 SDK 的费用估算；应用记录的 token 数可用，SDK 费用估算不作为 Fast 计费证据。

## 2026-10-03 追加：实际层级核验

- 同一账号的 Codex 模型目录中，`gpt-6-luna` 公布 `additional_speed_tiers: ["fast"]` 和 `service_tiers: [{"id": "priority", "name": "Fast", "description": "1.5x speed"}]`。Pi 与 Codex CLI 的账号 ID 相同。官方 Codex CLI 指定 `service_tier="fast"` 的小请求成功，但 CLI 的 JSON 事件不显示实际服务层级。
- 对 ChatGPT Codex 端点做相同模型、提示、低推理强度和请求体的交错对照：`default, priority, priority, default`。四次均为 HTTP 200、输入 37 tokens、输出 503 tokens，原始 `response.created.service_tier` 均为 `auto`，`response.completed.service_tier` 均为 `default`。

| 请求层级 | 首字时间 | 总耗时 | 首字后输出速度 |
| --- | ---: | ---: | ---: |
| `default` | 2.28 秒 | 11.47 秒 | 54.7 tokens/秒 |
| `priority` | 1.27 秒 | 7.41 秒 | 81.9 tokens/秒 |
| `priority` | 2.19 秒 | 11.87 秒 | 52.0 tokens/秒 |
| `default` | 1.89 秒 | 10.68 秒 | 57.2 tokens/秒 |

- 另用 `gpt-6.1-sol`、`gpt-6-sol`、`gpt-5.6-sol` 各发一个 `priority` 小请求，均为 HTTP 200，原始完成响应也均写 `default`。这表明当前端点的响应字段无法区分本次探测中的请求，但不能据此断定服务端必然按 Fast 处理。
- 响应头无服务层级标记。额度使用百分比为整数，本次小请求的用量变化未能分辨层级。Pi SDK 的 `resolveCodexServiceTier()` 在请求 `priority` 且响应 `default` 时会**自行改写为 `priority`**；这是 SDK 假设，不是服务端证明。

**结论：实际 Fast 尚未证实。**交错对照的 `priority` 速度波动较大，不能作为加速或计费证明。OpenAI API 文档说明其公开 API 响应中的 `default` 可代表降级；Pi 使用的 ChatGPT Codex 端点没有公开同等语义保证，不能把两者混用。此分支维持隔离，暂不合入或启用 news_agent 的 `service_tier: fast`；需取得该端点的逐请求实际层级/计费证据，或官方对其响应字段的说明后再验收。

本次未运行完整新闻日/月评估，也没有修改主工作目录或合入任何主分支。

官方依据：[Codex Speed](https://learn.chatgpt.com/docs/agent-configuration/speed)、[Codex Configuration Reference](https://learn.chatgpt.com/docs/config-file/config-reference)、[API Fast mode](https://developers.openai.com/api/docs/guides/fast-mode)（仅用于区分公开 API 的响应语义）。
