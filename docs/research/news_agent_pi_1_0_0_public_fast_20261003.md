# Pi 1.0.0 与 news_agent 的 Fast 接入记录（2026-10-03）

## 版本与路径

- 2026-10-03 查询 npm 的 `@earendil-works/pi-ai` 和 `@earendil-works/pi-coding-agent`，两者 `dist-tags.latest` 均为 `1.0.0`；[上游发布页](https://github.com/earendil-works/pi/releases)也把 v1.0.0 标为 Latest。
- 隔离工作区 `/home/lin/code/AgentLoom-pi-fast` 的桥接层将两者精确锁定为 1.0.0，安装器及 Pi CLI 均报告 1.0.0。主工作目录尚未改动。
- 上一版记录 [`news_agent_pi_fast_validation_20261002.md`](news_agent_pi_fast_validation_20261002.md) 保留了旧 `openai-codex` 端点的实际响应。旧端点发送 `priority` 后返回 `default`，因此此前不能证明实际 Fast。

## 采用公开 API

OpenAI 的[订阅登录推理文档](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)指定把新版 OAuth access token 发往 `POST https://api.openai.com/v1/responses`，要求 `store: false`、`stream: true`，并明确不能把这个登录流程指向 ChatGPT 的 `backend-api`。Pi 1.0.0 增加了 OpenAI 提供商的 **Sign in with ChatGPT** 登录；旧 `OpenAI Codex (legacy)` 凭据没有新版授权所需的 `chatgpt.tokens.use.direct` 范围。[登录流程说明](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)要求用户在浏览器同意授权。

AgentLoom 新增 `openai_chatgpt_responses`，news_agent 的初筛、精筛与来源复核均选用它；旧 `openai_codex_responses` 仍可供已有应用使用。新适配器只接受 Pi 自己保存的 OAuth 凭据，不读取 OpenAI API key。它通过 Pi SDK 请求公开 API，并可显式发送 YAML 中的 `service_tier`。

OpenAI 的 [Fast mode 文档](https://developers.openai.com/api/docs/guides/fast-mode)说明请求可以使用 `fast` 或 `priority`，但响应中的 `service_tier` 才代表实际处理档位；若响应为 `default`，应记为降档。桥接层从原始 `response.completed` 事件读取这个字段，保存在模型响应证据及运行事件中，分别显示 `requested` 和 `effective`。不会用请求值覆盖响应值。

## 实测结果

- Pi 1.0.0 安装握手、TypeScript 类型检查通过。隔离环境 Pi 相关回归 **229 通过**，3 项既有 `smolagents` 恢复用例排除；这 3 项在未升级的主目录同样失败。主目录的 `agentloom-news-v23-development-20261003-schema.service` 正在使用旧版 Pi 回放，因此尚未切换其 `.venv` 和源码。最新 news_agent 代码的最小应用补丁保存在 [`patches/news_agent_pi_1_0_0.patch`](../../patches/news_agent_pi_1_0_0.patch)，应用前检查已通过。
- 用户已通过 Pi 1.0.0 **OpenAI → Sign in with ChatGPT** 完成新版授权；`pi auth check --provider openai --no-refresh --json` 返回 OAuth `ready`。
- 用隔离环境的 news_agent 初筛真实请求 `gpt-6-luna`、`service_tier: fast`：公开 API 收到 `store: false`、`stream: true`，但返回 HTTP 400 `Unsupported service_tier: fast`。这次请求没有生成预测结果。
- 同一授权直接请求公开 API：`service_tier: priority` 返回 HTTP 200，但原始 `response.completed.service_tier` 为 `default`；显式 `default` 也返回 `default`。用账号模型目录中标称支持 Fast 的 `gpt-5.6-luna` 和 `gpt-6-astra` 重试 `priority`，两者均返回 `default`。因此不能将参数接受或模型目录中的 Fast 标签当作实际 Fast 的证据。
- 将隔离 news_agent 配置设为 `service_tier: default` 后，真实初筛成功，1 条行情复述被分类为 `[[1,2]]`；真实来源复核成功完成 1 次联网搜索，并对无新增事实的模拟新闻给出不买结论。两次原始响应的有效档位均为 `default`。新版授权、模型和联网路径在标准档可用。
- 更新后的 news_agent 应用定向测试 8 通过。完整新闻日/月评估未运行。

**实际 Fast 尚未验收。**当前 ChatGPT 订阅授权的公开 API 要么拒绝 `fast`，要么把 `priority` 请求按 `default` 完成。为避免 news_agent 每次请求都遇到 400，隔离副本选用真实可用的标准档。用户已明确要求只使用订阅额度，因此不接入付费 API 项目密钥。最终 Fast 验收仍需服务端在订阅请求的原始完成响应中确认 `fast` 或等价 Fast 标记。
