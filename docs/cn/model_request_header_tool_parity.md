# 模型请求头伪装验证记录

本文记录 `model_request_headers` 的验证边界。结论先行：AgentLoom 现在自己维护
模型请求头 profile；`opencode`、`cline`、`kimicode`、`openclaw` 已用真实工具在
`ssh dev` 上请求当前 `llm.yaml` 的 Ark OpenAI-compatible endpoint 验证过，`roo`
已用 Roo Code `3.53.0` OpenAI provider 源码真实请求验证过。Claude Code 不能用
当前 `/api/v3` + `ep-...` 证明与真实 Claude Code 协议级一致，因此不作为内置
profile。

## 当前实现能保证什么

- `config/system.yaml` 可以选择 `opencode`、`cline`、`kimicode`、`openclaw`、`roo`、
  `agentloom`、`none`。
- `opencode`、`cline`、`kimicode`、`openclaw` 的请求头值来自真实工具在 `ssh dev`
  上的当前版本抓包验证；`roo` 来自 Roo Code `OpenAiHandler` provider 源码真实
  请求抓包验证。
- AgentLoom 管理的 HTTP headers 会进入 `litellm.completion()`、smolagents 模型
  调用，以及 hook 中直接调用的 `litellm.completion()`。
- `config/llm.yaml` 中单个模型的 `extra_headers` 会按 header 名大小写不敏感覆盖
  system 级默认值。

## 当前实现不能保证什么

仅改 HTTP headers 不能保证以下内容与真实工具一致：

- 请求路径，例如 OpenAI-compatible `/chat/completions` 与 Anthropic-compatible
  `/messages`。
- 请求体 schema，例如 `messages/tools/stream_options` 与 Claude Code 的
  `system/thinking/anthropic_beta`。
- SDK 自动 headers，例如 `x-stainless-*`、session headers、trace headers。
- Header 顺序、TLS 指纹、HTTP runtime、连接池行为。
- 真实工具版本升级后的 `User-Agent` 漂移。

因此，`profile: "opencode"`、`profile: "cline"`、`profile: "kimicode"`、
`profile: "openclaw"`、`profile: "roo"` 的含义是“使用当前验证过的真实工具或
provider HTTP headers”。Claude Code 当前走的是另一种 endpoint 形态，不作为内置
profile。

## 已验证事实

当前 `config/llm.yaml` 默认模型是 Ark OpenAI-compatible endpoint：

```yaml
base_url: "https://ark.cn-beijing.volces.com/api/v3"
model: "openai/ep-20260530114906-fj6fl"
```

AgentLoom 通过本地抓包代理转发到真实 Ark endpoint 验证过以下 profile，均能真实
请求当前大模型并返回 marker：

| AgentLoom profile | 实际请求路径 | AgentLoom 发出的关键 headers |
| --- | --- | --- |
| `opencode` | `/api/v3/chat/completions` | `User-Agent: opencode/1.17.12 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14`，并发送同 session 的 `X-Session-Id` / `X-Session-Affinity` |
| `cline` | `/api/v3/chat/completions` | `User-Agent: ai-sdk/openai-compatible/2.0.51 ai-sdk/provider-utils/4.0.30 runtime/bun/1.3.13` |
| `kimicode` | `/api/v3/chat/completions` | `User-Agent: kimi-code-cli/0.21.1`，并发送 Kimi Code 当前 JS SDK headers |
| `openclaw` | `/api/v3/chat/completions` | `User-Agent: OpenAI/JS 6.39.1`，并发送 OpenClaw direct runtime 当前 OpenAI JS SDK headers |
| `roo` | `/api/v3/chat/completions` | `HTTP-Referer: https://github.com/RooVetGit/Roo-Cline`、`X-Title: Roo Code`、`User-Agent: RooCode/3.53.0` |

在 `ssh dev` 上用真实工具和同一套当前 Ark key/model/base 做过对比：

| 真实工具 | 真实工具结果 | 真实工具观测到的关键差异 |
| --- | --- | --- |
| Claude Code `2.1.159` | 不能用当前 `/api/v3` + `ep-...` 组合完成同协议验证 | Claude Code 需要 Anthropic-compatible plan/coding endpoint；`--model ep-...` 会本地拒绝；debug 显示它走 `/api/plan/v1/messages` 并返回 401 |
| Cline CLI `3.0.34` | 能真实请求当前 Ark OpenAI-compatible endpoint 并返回 marker | AgentLoom 的 `cline` profile 已改为该真实工具当前 OpenAI-compatible runtime `User-Agent` |
| Kimi Code `0.21.1` | 能真实请求当前 Ark OpenAI-compatible endpoint 并返回 marker | AgentLoom 的 `kimicode` profile 已改为该真实工具当前 headers |
| OpenCode `1.17.12` | 能真实请求当前 Ark OpenAI-compatible endpoint 并返回 marker | AgentLoom 的 `opencode` profile 已改为该真实工具当前 headers |
| OpenClaw npm `2026.6.11` + Node `22.19.0` | 能真实请求当前 Ark OpenAI-compatible endpoint 并返回 marker | AgentLoom 的 `openclaw` profile 已改为该真实工具当前 direct runtime headers |
| Codex npm `@openai/codex@0.142.5` | 未验证通过 | 当前尝试没有在超时时间内完成，不能作为可支持 profile |
| Roo Code `3.53.0` 源码 OpenAiHandler | 能真实请求当前 Ark OpenAI-compatible endpoint 并返回 marker | AgentLoom 的 `roo` profile 已改为该 provider 源码当前默认 headers；公开 npm 无官方 Roo CLI，仓库 CLI 当前支持列表不含 OpenAI-compatible base URL 配置，所以这不是完整 VS Code 扩展宿主验证 |

## 工程结论

如果目标是“减少 AgentLoom 自身身份暴露”，当前 headers profile 功能是有效的。

如果目标是“让供应商在协议层无法区分 AgentLoom 与真实工具”，仅设置 headers 仍然
不充分；需要为每类工具实现对应的 transport/client/protocol。尤其是 Claude Code，
它和当前 `llm.yaml` 的 OpenAI-compatible endpoint 不属于同一种协议。

所以在没有协议级客户端适配前，不应该内置 `claudecode` 并宣称它与真实 Claude
Code 当前版本完全一致。`opencode`、`cline`、`kimicode`、`openclaw` 当前只验证
HTTP header 与真实工具抓包对齐，并验证双方都能真实请求当前大模型返回 marker；
`roo` 验证到 Roo Code OpenAI provider 源码真实请求边界。
