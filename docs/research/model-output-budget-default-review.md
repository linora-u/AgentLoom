# 模型输出预算默认值：缺陷、证据与修复边界

核查日期：2026-10-12。源代码基线：`94491c2d4846e1c648bd00dfd1950a0fc4bd6ed2`。
这份预算研究只分析、增加文档和离线探针，未修改产品代码、调用模型或联网；单独的全日验收已获用户授权将本地预算调为 32,768。修复合同已确认，见 [ADR](../adr/0002-explicit-output-cap-with-independent-input-reserve.md)，产品修复尚未实施。

## 结论

当前 `max_output_tokens` 未配置或配置为 `null` 时，除 Codex 外会被框架转成 `16384`。这是框架施加的请求输出上限，并非供应商真实最大输出能力。用户已确认“未指定就不额外注入请求 cap”，当前实现不符合该合同。

**仅删除 `llm_config.py` 的默认值不够，而且会引入更大的错误。** Pi bridge 会把空预算补成 `context_window`；Pi SDK 又从该 model metadata 补出请求上限。离线探针中，窗口与 metadata 都是 `500000`、请求未指定输出上限时，Responses 实际 HTTP body 包含 `max_output_tokens: 495897`，Chat 包含 `max_tokens: 495897`。显式 `32768` 对照均保留 `32768`。该数值随输入估算变化，不能表述成每次恰好发送 500000。

修复需一起打通：配置空值合同、Pi 最终 payload 策略、smol nullable 兼容、额外参数冲突、独立输入预留，以及真实 HTTP 合同验收。`context_window`、SDK model metadata 的最大输出值、用户请求 cap 是三个不同概念。

## 当前配置合同

| 输入 | 当前解析结果 | 后果 |
| --- | --- | --- |
| 非 Codex，省略 `max_output_tokens` | `16384` | 人为限制请求输出 |
| 非 Codex，`max_output_tokens: null` | `16384` | 无法用 null 解除 cap |
| Codex，省略或 null | `None` | Pi Codex 默认不发送 cap |
| 显式正整数 | 对应整数 | 正常路径要求小于 context window |
| 仅旧字段 `max_tokens` | 同值填入 context/output/input | 旧兼容合同，不能顺手重解释 |

证据：`src/config/defaults.py:4`；`src/config/llm_config.py:95–96,179–203`。直接构造 `LlmModelTypeSettings` 的字段默认也仍是 16384，不只 YAML `from_dict()` 有默认。

配置 patch 不是原始 YAML 的简单合并：`apply_profile_patch()` 从已解析的 `model_dump()` 重建，再合并 patch（`llm_config.py:313–318`），默认值也被实体化。`ModelProfileOverlay.apply()` 则过滤所有 None（`src/integrations/litellm/model_binding.py:58–79`），无法表达“把已有显式 cap 解除”。后续实现必须区分“不覆盖”和“明确解除”，并保证缓存和 profile snapshot 保留这个合同。

## Pi：从配置到 HTTP 的完整链路

核查了锁定且实际安装的 `@earendil-works/pi-ai@1.0.0`、`pi-coding-agent@1.0.0`。安装位置为 `.venv/share/pi/bridge/node_modules/@earendil-works/`。仓库与当前安装 bridge 的 `index.ts`、`model.ts` 内容一致。

1. `src/app/agent.py:139–146` 将解析后的 settings 通过 `model_dump(mode="json")` 放入 `RuntimeModelSelection`。
2. `src/runtimes/pi/runtime.py:226–231` 将 settings 复制到 wire，仅移除 `supports_structured_output`。
3. `src/runtimes/pi/bridge/index.ts:139–144` 注册普通 API model：`maxTokens: s.max_output_tokens ?? s.context_window`。这一步把“无显式 cap”混同成“输出最大值等于整个上下文窗口”。订阅模型在 `149–153` 仅显式 cap 时覆盖 SDK catalog 的 metadata。
4. `bridge/model.ts:92` 保存 session 原生 `streamFunction`；`131–133` 只有配置不为空时才传 `maxTokens`，但“未传”并不意味着 SDK 不发送。
5. 实际原生函数来自安装的 `pi-coding-agent/dist/core/sdk.js:250–260`：Agent 的 `streamFn` 调用 `modelRuntime.streamSimple()`。
6. 安装的 `pi-ai/dist/models.js:457–462` 派发到对应 provider 的 `streamSimple()`；Responses 在 `dist/api/openai-responses.js:177–188` 调用 `buildBaseOptions()`。
7. `dist/api/simple-options.js:4–14` 执行 `options?.maxTokens ?? model.maxTokens`，再按 `contextWindow - estimatedInput - 4096` clamp，最少为 1。
8. Responses 的 `buildParams()` 在 `openai-responses.js:248–249` 写 `max_output_tokens`；Chat 在 `openai-completions.js:588–594` 写 `max_tokens` 或 `max_completion_tokens`。
9. SDK 合并 sampling params 后才调用 `onPayload()`，其返回对象用于 `client.responses.create()`（Responses `124–135`）。框架 `index.ts:221–227` 对 Codex 显式 cap 另行写入 payload；当前没有通用的“未配置则删除 SDK 补出的 cap”策略。
10. `bridge/model.ts:139–151` 的 fetch wrapper 在真正发送前捕获 HTTP body。它能验证最终请求，优于只检查配置或 stream options。

普通 Responses SDK 会将小于 16 的显式输出值升为 16（`openai-responses.js:16,248–249`）；Chat 没有相同处理。该 SDK 的 OpenAI 兼容逻辑不能直接当作 Ark/其他兼容服务的供应商合同。

Codex SDK 的 `buildRequestBody()` 没有把 `options.maxTokens` 写入请求（`openai-codex-responses.js:375–433`），所以现有 Codex 例外能省略 cap。框架仅显式预算时补入 `max_output_tokens`。ChatGPT OAuth 使用普通 Responses SDK，但其 `omitUnsupportedFields` 会移除该请求字段（`openai-responses.js:235–249`）；因此“配置有整数”与“端点接受 cap”也不是一回事。

`Model.maxTokens: number` 的 TypeScript 类型要求（`pi-ai/dist/types.d.ts:929`）只能证明 SDK 需要 model metadata，不能据此推导“必须向 HTTP 发送整个 context window”。真实 metadata 未知时须保持未知或采用明示且有依据的适配策略，不得伪造供应商能力。

## 离线 wire 探针

命令：

```sh
node .agentloom/validation/news-concurrency-20261012/fault-injection/pi-output-budget-wire.mjs
```

脚本使用当前安装 SDK，fake fetch 读取 HTTP JSON 后立即抛出受控错误，绝不调用真实 fetch。4 项断言通过、退出码 0；没有网络请求、没有模型调用。每项仅发生一次 fake fetch，无重试。

| Pi API | Caller cap | Model metadata | HTTP body |
| --- | --- | --- | --- |
| Responses | 未提供 | 500000 | `max_output_tokens=495897` |
| Responses | 32768 | 500000 | `max_output_tokens=32768` |
| Chat | 未提供 | 500000 | `max_tokens=495897` |
| Chat | 32768 | 500000 | `max_tokens=32768` |

回执：`.agentloom/validation/news-concurrency-20261012/fault-injection/pi-output-budget-wire.json`。
这是 SDK payload 的反例复现，不是已修复的验收，也不证明真实供应商接受这些数值。

## smolagents / LiteLLM 链路

安装版本：`smolagents 1.26.0`、`litellm 1.104.2`。实际框架 model-turn 路径使用 AgentLoom 的 `SmolagentsModelTurnBridge`，不能只分析上游 `smolagents.LiteLLMModel`。

当前主 Application 路径：

- `model_binding._turn_options():135–158` 只在预算非 None 时加入 cap，Responses 用 `max_output_tokens`，其他支持的协议用 `max_tokens`，然后合并 `extra_completion_params`。
- `src/runtimes/smolagents/runtime_factory.py:129,140–142` 构造 bridge，并将 binding 的 context/output 预算交给 Agent 压缩逻辑。
- `src/integrations/litellm/model_turn.py:470–492,561–580` 合成 request，再调用 `litellm.completion()` 或 `litellm.responses()`（`244–253`）。当前 trace 边界是 `litellm_input`（`107–116`），不是 HTTP body。

已核查安装的 LiteLLM OpenAI 路径：

- Responses `responses/main.py:1133` 的参数默认是 None；`responses/utils.py:256–283` 和 `utils.py:4202–4244` 滤掉与 None 默认一致的值；OpenAI Responses transformation `222–223` 仅当存在 cap 时校正最小值，`283–309` 构造最终请求。HTTP handler 再在 `llms/custom_httpx/llm_http_handler.py:2414–2424` 归一请求、合并 `extra_body`，然后 POST（`2505–2512`）；后合并的 extra_body 可以覆盖预算。未发现这一条普通 OpenAI Responses 路径从 model catalog 自动补输出 cap。
- Chat 的 `completion()` / `get_optional_params()` 默认 cap 为 None，`utils.py:4268–4288` 使用同样非默认参数过滤；OpenAI transformation 透传已解析 optional params。`OpenAIConfig.get_config()` 的全局配置可再次补入 optional params（`main.py:2636–2643`），所以不可仅凭框架 options 断言 HTTP 一定无 cap。
- LiteLLM 的可选 `modify_params` 流程仅在已有 `max_tokens` 且开关为 True 时调整（`utils.py:1764–1791`），这条流程不凭空补省略字段。
- Anthropic 不同：安装的 `llms/anthropic/chat/transformation.py:331–355` 明确给必需的 `max_tokens` 补 model metadata 或 fallback。故全 adapter 策略必须按协议能力处理，不能对所有协议一律删除必要字段，也不能把此 fallback 当作供应商真实最大值。

只改共享解析器为 None 还会破坏兼容入口：

- `src/runtimes/smolagents/models/model_types.py:61` 仍使用 int 默认；`155` 的 `int(resolved.max_output_tokens)` 遇到 None 会失败。
- `models/model_manager.py:88–92` 的 overlay 要求输出预算为 int；`197` 的旧 LiteLLM 配置始终写 `max_tokens`。其 `get_smolagents_model():261–278` 也会重建共享 settings。
- `src/execution/model_binding.py:45,56–65` 要求输出预算是非负 int；`model_binding.py:191` 当前用 `None → 0` 表示无 cap。这不应被压缩模块或 SDK误读成真实的零输出能力。

## 输入预留不是请求 cap

当前共享配置用 `context_window - (max_output_tokens or 0)` 计算 `input_token_limit`（`llm_config.py:203`）。smol 压缩也以 `max_output_tokens or 0` 作为预留（`context_compression.py:1679–1682`）。把 cap 改 None 后，输入可填满整个 context window；“供应商原生行为”不等于“生成无需空间”。

Pi 原生压缩另有默认 `reserveTokens=16384`，触发条件为 `contextTokens > contextWindow - reserveTokens`（安装的 `pi-coding-agent/dist/core/compaction/compaction.js:53–56,173–176`）；摘要请求又有单独预算（`512`）。这些内部输入预留/摘要预算应保留明确职责，不可因移除用户请求的隐式 cap 被一起清空。当前 Pi bridge 未从 `settings.input_token_limit` 直接映射这些阈值。

建议以独立的输入预留策略计算压缩阈值；它必须有来源且可观测。该预留是本地容量规划值，不代表供应商最大输出，也不自动成为 HTTP cap。未知供应商上下文/输出能力应明确保留未知边界；不能承诺“未设置一定不会被供应商截断”。

## 历史：Codex 例外是明确边界

- `3ba73a384ddce859061e2c4ac3551acb4b104e08`，2026-08-12，`separate context and output token budgets`：引入 16384 默认，将输出 cap 与输入预留绑定，并保留旧 `max_tokens` 兼容。代码能证明这两个职责当时共用了一个数值；没有证据证明为什么选 16384，更不能推测作者动机。
- `e6abb079544e0234c9022cb0034fbfc38fd3c8e0`，2026-10-08，`fix(models): allow native Codex output limits`。提交正文明确写：`Treat omitted or null Codex output budgets as provider-native, omit artificial wire caps, and preserve explicit limits and other adapters defaults.`
- 现有 `tests/pi_test/test_output_budget.py:22–36` 专门断言其他 adapter 保留 16384，non-Codex null 也保留默认。因而 Codex-only 不是偶然遗漏，而是这个提交显式保留的实现合同。现有合同与如今“全模型未指定不施加 cap”的目标冲突时，应更新合同和测试，不能宣称旧测试通过就是修复成功。

## 已确认合同与最小完整修复范围

已确认公共合同：省略表示“没有调用方指定的 cap”；显式正整数表示 cap；profile patch 的 null 明确解除继承 cap。输入预留独立管理；必须提供 cap 的协议使用已验证能力或要求显式配置，未知时提前报错。实施时须统一配置文档、patch 与 overlay，并替换历史相冲突的默认值测试。

| 范围 | 必须解决的事 |
| --- | --- |
| 共享配置及 profile patch/overlay | 全 adapter 保留 unspecified 状态；区分缺失与解除；明确旧 max_tokens 迁移；直接构造和 YAML 行为一致 |
| Pi bridge model metadata | 不把 context_window冒充供应商最大输出；对 SDK 必需 metadata 制定明确来源/未知策略 |
| Pi adapter 最终 payload | 在 SDK 建参和 sampling/extra_body 合并之后应用 cap 策略；OpenAI Chat/Responses 未指定时移除 SDK补出的字段；显式值按协议支持情况验证、保留；Codex 与 ChatGPT 保持端点能力区别 |
| LiteLLM binding / smol 兼容入口 | None 可到达最终请求；修正 int(None)、overlay、0 sentinel 语义；检查 LiteLLM 全局配置/adapter fallback 不能暗中覆盖公共合同 |
| 输入压缩与摘要 | cap 与输入 reserve 分开；普通推理和内部摘要预算各有职责；改 cap 不把输入预留清零 |
| 额外参数 | 统一处理 max_tokens、max_completion_tokens、max_output_tokens 和 extra_body 中同义值；冲突报配置错误或按已写明规则归一，避免绕过显式 cap |
| 可观测性和文档 | trace 明示调用方 cap、wire cap、输入预留及来源；保留 credential redaction；公开 adapter 能力差异 |

`onPayload` 是当前 Pi 可用的最终策略边界（SDK调用它后才 HTTP序列化），比只改 stream options 更可靠；必须针对所选协议的 cap alias 处理。不能粗暴删除所有数字，也不能删除内部摘要明确指定的预算。`configureModel()` 目前以 settings cap 覆盖原始 options 的 cap（`bridge/model.ts:131–133`），而原生摘要会传入自己的 cap；修复时需识别原生内部预算与普通调用中 SDK补值的来源，不能仅凭 profile为空就无差别删除所有请求的 cap。是否采用框架边界转换或上游 SDK 增加独立“不要发送 cap”能力，可按维护成本决定，但请求合同必须相同。

对于 Anthropic 这种必需 cap 的协议：明确要求配置，或依据已验证的 model capability 解析并记录该值。供应商能力未知时应在请求前报配置错误，不能继续隐式使用一个万能默认。当前 Pi 不支持 Anthropic（`src/runtimes/pi/metadata.py:27–28`）；smol 不支持 Pi 专属订阅 adapter（`model_turn.py:806`），测试按支持矩阵执行。

## 修复验收

以下是后续修复必须补充的验收，本次未执行真实模型验收：

1. **配置合同**：逐 adapter 覆盖缺失、null、显式 cap、patch 解除/继承、overlay、旧字段迁移及 cap alias 冲突；检查序列化和缓存后 unspecified 不变成整数。
2. **真实 SDK request contract**：使用锁定 Pi SDK 和 LiteLLM 的 fake fetch/http transport，检查实际 HTTP body。OpenAI Chat/Responses 未配置时不出现输出 cap；显式 cap 使用正确字段；不得出现 context_window 被注入 cap；Codex 显式/省略分别正确，ChatGPT不支持 cap 的行为明确；Anthropic 必需值策略确定。
3. **压缩合同**：未指定 cap 时保留可解释的输入 reserve；显式 cap 与独立 reserve 一致性验证；普通推理及压缩摘要的 wire 行为分别验证。不能只断言线程池或私有 SDK 状态。
4. **真实 Application runner/CLI**：Pi 和 smol 支持路径分别运行一个真实 Application，保存最终 HTTP 字段、响应 finish/incomplete reason、usage 和产物；模型与 API适配必须来自本机真实配置。用户指定 NewsAgent 使用 DeepSeek v4.1 和 AnySearch 时，记录实际 model id 与搜索工具调用，不能用名称猜版本。
5. **完整 NewsAgent**：按其真实日流程验证所有行业/阶段、失败重试与最终产物；输出截断和队列/owner失效分别报告。并发增加不会修复截断，移除隐式 cap也不证明业务流程或持久恢复已完善。

离线反例脚本及回执只证明当前 SDK 的补值行为，不能代替以上修复验收或供应商能力验证。
