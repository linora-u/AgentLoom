# Pi 1.0.0 与 news_agent 的 Fast 接入记录（2026-10-03）

## 版本与路径

- 2026-10-03 查询 npm 的 `@earendil-works/pi-ai` 和 `@earendil-works/pi-coding-agent`，两者 `dist-tags.latest` 均为 `1.0.0`；[上游发布页](https://github.com/earendil-works/pi/releases)也把 v1.0.0 标为 Latest。
- 隔离工作区 `/home/lin/code/AgentLoom-pi-fast` 的桥接层将两者精确锁定为 1.0.0，安装器及 Pi CLI 均报告 1.0.0。主工作目录尚未改动。
- 上一版记录 [`news_agent_pi_fast_validation_20261002.md`](news_agent_pi_fast_validation_20261002.md) 保留了旧 `openai-codex` 端点的实际响应。旧端点发送 `priority` 后返回 `default`，因此此前不能证明实际 Fast。

## 采用公开 API

OpenAI 的[订阅登录推理文档](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)指定把新版 OAuth access token 发往 `POST https://api.openai.com/v1/responses`，要求 `store: false`、`stream: true`，并明确不能把这个登录流程指向 ChatGPT 的 `backend-api`。Pi 1.0.0 增加了 OpenAI 提供商的 **Sign in with ChatGPT** 登录；旧 `OpenAI Codex (legacy)` 凭据没有新版授权所需的 `chatgpt.tokens.use.direct` 范围。[登录流程说明](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)要求用户在浏览器同意授权。

AgentLoom 新增 `openai_chatgpt_responses` 用于公开 API 的订阅授权验证；随后 news_agent 的初筛、精筛与来源复核切回 `openai_codex_responses` 验证 Codex 原生 Fast。两个适配器均使用 Pi 保存的 OAuth 凭据，不读取付费 API key；它们都可从 YAML 接收 `service_tier`。

对公开 API，OpenAI 的 [Fast mode 文档](https://developers.openai.com/api/docs/guides/fast-mode)说明请求可以使用 `fast` 或 `priority`，响应中的 `service_tier` 表示实际处理档位；若响应为 `default`，应记为降档。桥接层从原始 `response.completed` 事件读取这个字段，并分别记录请求值与响应值。Codex 原生路径的解释见后文。

## 实测结果

- Pi 1.0.0 安装握手、TypeScript 类型检查通过。隔离环境 Pi 相关回归 **229 通过**，3 项既有 `smolagents` 恢复用例排除；这 3 项在未升级的主目录同样失败。主目录的 `agentloom-news-v23-development-20261003-schema.service` 正在使用旧版 Pi 回放，因此尚未切换其 `.venv` 和源码。最新 news_agent 代码的最小应用补丁保存在 [`patches/news_agent_pi_1_0_0.patch`](../../patches/news_agent_pi_1_0_0.patch)，应用前检查已通过。
- 用户已通过 Pi 1.0.0 **OpenAI → Sign in with ChatGPT** 完成新版授权；`pi auth check --provider openai --no-refresh --json` 返回 OAuth `ready`。
- 用隔离环境的 news_agent 初筛真实请求 `gpt-6-luna`、`service_tier: fast`：公开 API 收到 `store: false`、`stream: true`，但返回 HTTP 400 `Unsupported service_tier: fast`。这次请求没有生成预测结果。
- 同一授权直接请求公开 API：`service_tier: priority` 返回 HTTP 200，但原始 `response.completed.service_tier` 为 `default`；显式 `default` 也返回 `default`。用账号模型目录中标称支持 Fast 的 `gpt-5.6-luna` 和 `gpt-6-astra` 重试 `priority`，两者均返回 `default`。因此不能将参数接受或模型目录中的 Fast 标签当作实际 Fast 的证据。
- 曾将隔离 news_agent 暂设为公开 API 的 `service_tier: default`：真实初筛成功，1 条行情复述被分类为 `[[1,2]]`；真实来源复核完成 1 次联网搜索，并对无新增事实的模拟新闻给出不买结论。两次原始响应的有效档位均为 `default`。这验证了该公开路径的订阅标准档；隔离配置现已切回 Codex 原生 Fast 请求。
- 更新后的 news_agent 应用定向测试 8 通过。完整新闻日/月评估未运行。

**公开 API 路径的实际 Fast 尚未验收。**当前 ChatGPT 订阅授权的公开 API 要么拒绝 `fast`，要么把 `priority` 请求按 `default` 完成。它可在标准档使用，但不能据此认定订阅无法使用 Codex 原生 Fast。用户已明确要求只使用订阅额度，因此不接入付费 API 项目密钥。

## 订阅 Codex 原生路径复核（2026-10-03）

- [Codex Speed](https://learn.chatgpt.com/docs/agent-configuration/speed)明确规定订阅登录的 Codex CLI 支持 Fast，`gpt-6-luna`、`gpt-6.1-sol` 等受支持模型可用；Fast 会按 2.5 倍标准档消耗订阅额度。[Codex 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)说明配置值 `fast` 对应请求值 `priority`。因此，公开 API Fast 的按量付费说明不能直接套用到 Codex 订阅。
- OpenAI Codex 维护者[在官方仓库说明](https://github.com/openai/codex/issues/14204#issuecomment-4033184620)：ChatGPT 登录的 Codex 原生路径中，最终 `response.service_tier` 不是可靠的端到端 Fast 标记；看到 `default` 并不能证明 Fast 被忽略。这修正了此前把该原生响应字段当作最终档位的判断。桥接层现将原生 `response_tier` 单独记录，并把 `effective` 留空；公开 API 路径仍以响应字段为准。
- 使用 Pi 1.0.0 的 Codex OAuth 授权，WebSocket 模式分别对 `gpt-6-luna`、`gpt-6.1-sol`、`gpt-5.6-sol` 发出 `service_tier: priority` 小请求。三次成功，原始完成事件均写 `default`；这证明 WebSocket 传输本身不能提供更明确的档位证据。
- 隔离 news_agent 已切回 `openai_codex_responses`，初筛、精筛及来源复核均配置 `service_tier: fast`。这使 Pi 对 Codex 原生订阅端点发出 `priority`。完整定向测试 **40 通过**；新测试防止把原生 `default` 误记成有效标准档，同时保留公开 API 对实际档位的断言。应用补丁已重新生成并对当前主目录通过 `git apply --check`，仍未应用到正在运行回放的主目录。
- 真实隔离初筛 `run_20261003T055936173945Z_c8e71acc26de`：1 次请求、0 次重试、约 5.3 秒；保留的 HTTP 请求 URL 为 `https://chatgpt.com/backend-api/codex/responses`，请求层级 `priority`，输出 `{"classifications": [[1,2]]}`，原始响应层级 `default`。
- 真实隔离来源复核 `run_20261003T060034897063Z_c0e119b01838`：1 次请求、1 次实际联网搜索、约 17.8 秒；同样向 Codex 原生端点发送 `priority`，对缺少原文和数量的模拟新闻给出放弃结论，原始响应层级仍为 `default`。模拟输入不算历史投资验收。

**当前证明范围：Pi 1.0.0 已通过订阅 OAuth 把 Fast 请求送到 Codex 原生端点并完成新闻工作流。**原生完成字段无法证明服务端为该请求实际分配 Fast；目前也没有对应请求的服务端额度扣减明细。不能把“请求走通”写成“实际 Fast 已验收”。主目录仍由 Pi 0.87.1 执行八月回放，隔离工作区保留最新版和 Fast 候选配置。

## 用量记录可否补足证明

- 通过本机 Codex app-server 的只读 `account/rateLimits/read` 查询到订阅账号的 `codex` 周限额；当前 `primary.usedPercent` 为整数百分比，且没有逐请求服务档位或扣减明细。查询时该账号的八月新闻回放仍在运行，多个 Pi worker 同时请求模型，无法把前后百分比变化归给某一次隔离探测。
- 用 Pi Codex OAuth 对 `GET /backend-api/codex/usage` 做只读尝试，服务端返回 403；没有把该内部路径作为正式证据来源，也没有进一步改变认证或请求来源以绕过限制。
- 这排除了用当前可见的额度读数验证单次实际 Fast 的方法。需要服务端逐请求档位或可归因的用量记录，才可把候选配置提升为“实际 Fast 已证实”。期间继续保留原始请求、完成事件、模型和运行版本证据。
