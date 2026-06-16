# AgentLoom LLM 模型配置 (`llm.yaml`) 完整参考

> **文档定位**：本文档详细说明 `config/llm.yaml` 的**每一个**配置参数。
> 关于配置文件之间的覆盖关系，请参阅 [配置体系总览](config-overview.md)。
> 关于全局系统配置，请参阅 [系统配置文档](system_config.md)。
> 关于 Agent YAML 参数，请参阅 [Agent 配置文档](agent_config.md)。

`config/llm.yaml` 是 AgentLoom 框架的**模型路由与参数配置文件**，控制不同智能体使用的 LLM 模型类型（powerful/fast/summary 或自定义类型）、API 凭据、推理参数和重试策略。

> ⚠️ **重要**：LLM 配置是**独立加载**的，不参与 `system.yaml` 的 deep merge 覆盖链。在 `config/system.yaml` 或 `applications/<app>/config/system.yaml` 中出现 `model`/`llm` 会被自动过滤并输出警告。

---

## 目录

- [快速参考：完整 YAML 结构](#快速参考完整-yaml-结构)
- [1. model.default_model_type — 全局默认模型类型](#1-modeldefault_model_type--全局默认模型类型)
- [2. 模型类型命名规则](#2-模型类型命名规则)
- [3. model.\<type\> — 模型类型配置](#3-modeltype--模型类型配置)
- [4. 参数默认值](#4-参数默认值)
- [5. langfuse — 可观测性配置（后续支持）](#5-langfuse--可观测性配置后续支持)
- [6. 重试机制详解](#6-重试机制详解)
- [7. Provider 前缀与特定行为](#7-provider-前缀与特定行为)
- [8. 默认值常量对照表](#8-默认值常量对照表)
- [附录 A：完整 Pydantic 模型](#附录-a完整-pydantic-模型)
- [附录 B：常见配置场景](#附录-b常见配置场景)

---

## 快速参考：完整 YAML 结构

以下展示 `config/llm.yaml` 的**完整结构**，所有字段及其默认值：

```yaml
# ============================================
# 模型配置
# ============================================
model:
  # 全局默认模型类型；若省略，未写 model_type 的 Agent 会直接报错
  default_model_type: "powerful"

  # ━━━ 必填：摘要模型（上下文压缩 smart_summary 功能依赖） ━━━
  summary:
    model: "openai/azure-gpt-5-chat"
    base_url: "https://llm-gateway.example.com/v1"
    api_key: "your-api-key"
    description: "摘要模型，适合总结和提取"
    temperature: 1.0
    max_tokens: 2048
    timeout: 300

  # ━━━ 以下模型类型均为可选，可删除/改名/新增 ━━━

  # 强大模型（复杂推理、代码生成）
  powerful:
    model: "anthropic/aws-claude-opus-4-5"
    base_url: "https://llm-gateway-proxy.inner.chj.cloud/llm-gateway"
    api_key: "your-api-key"
    description: "高质量推理模型，适合复杂分析/代码任务"
    temperature: 0.2
    max_tokens: 8192
    timeout: 300
    context_cache: true

  # 快速模型（意图识别、分类）
  fast:
    model: "anthropic/aws-claude-sonnet-4-5"
    base_url: "https://llm-gateway.example.com/v1"
    api_key: "your-api-key"
    description: "低延迟模型，适合简单任务"
    temperature: 1.0
    max_tokens: 1024
    timeout: 300

  # 自定义模型类型（可选，名称随意取）
  # code_review:
  #   model: "anthropic/claude-3-5-sonnet"
  #   temperature: 0.1
  #   max_tokens: 16384
```

---

## 1. model.default_model_type — 全局默认模型类型

控制整个 Agent 系统中，当 Agent YAML 未指定 `model_type` 时使用哪个模型类型。

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `model.default_model_type` | `str` | `""` | ❌ 否 | 全局默认模型类型。值必须是 `model` 块下定义的某个类型 key（如 `powerful`、`fast`、`summary`，或自定义类型名）。未配置时，未指定 `model_type` 的 Agent 会直接抛出 `ValueError` |

### 1.1 解析规则

当 Agent 需要获取模型配置时，按以下优先级解析：

```
Agent YAML 中的 model_type（如 model_type: "fast"）
       ↓ (未指定时)
config/llm.yaml 中的 default_model_type（如 default_model_type: "powerful"）
       ↓ (default_model_type 未配置时)
ValueError
```

> ⚠️ **注意**：最终解析出的模型类型必须存在于 `llm.yaml`。如果 Agent YAML 明确指定了不存在的 `model_type`，全局默认类型指向不存在的类型，或 Agent 与全局配置都没有提供模型类型，框架都会**直接报错**（`ValueError`）。

**示例**：

```yaml
model:
  default_model_type: "powerful"   # 所有 Agent 默认使用 powerful 类型
  # default_model_type: "fast"     # 如果默认用快速模型

  powerful:
    model: "anthropic/claude-3-5-sonnet"
    base_url: "https://your-gateway.com/v1"
    api_key: "your-key"
    temperature: 0.2
```

```yaml
# Agent YAML 示例
name: "my_agent"
model_type: "powerful"    # 使用 powerful 类型；如果不写，则使用已配置的 default_model_type
```

---

## 2. 模型类型命名规则

`model` 块下除 `default_model_type` 外，所有值为 dict 的 key 都是模型类型名。框架不会对类型名做特殊语义处理，`powerful`、`fast`、`summary` 只是示例命名。

- `summary` 是上下文压缩（`smart_summary`）依赖的必需类型。
- `default_model_type` 是保留 key，不是模型类型。

**示例：**

```yaml
model:
  default_model_type: "powerful"

  powerful:
    model: "openai/gpt-4o"
    base_url: "https://your-gateway.com/v1"
    api_key: "sk-your-api-key"

  fast:
    model: "openai/gpt-4o-mini"
    base_url: "https://your-gateway.com/v1"
    api_key: "sk-your-api-key"

  summary:
    model: "openai/gpt-4o-mini"
    base_url: "https://your-gateway.com/v1"
    api_key: "sk-your-api-key"
```

---

## 3. model.\<type\> — 模型类型配置

**`model` 块下的所有 key 角色一览：**

| Key | 是否必填 | 说明 |
|-----|---------|------|
| `summary` | ❗ **必填** | 上下文压缩（`smart_summary`）功能硬依赖此类型 |
| `default_model_type` | ❌ 可选 | 保留 key。没有隐式默认值；仅当所有 Agent 都显式指定 `model_type` 时才可省略 |
| 其他任意 key | ❌ 可选 | 自由定义、删除、改名，如 `powerful`、`fast`、`code_review` 等 |

除 `default_model_type` 外，`model` 块下**所有值为 dict 的 key 都会被解析为模型类型**。框架对类型名没有任何限制——`powerful`、`fast` 只是示例命名，你可以自由删除、重命名或新增。每个模型类型的 `model` 字段都是必填的。

**YAML 路径**：`model.<你的类型名>.*`（如 `model.powerful.*`、`model.my_llm.*`）
**Pydantic 模型**：`LlmModelTypeSettings`

### 3.1 完整参数列表

| 参数 | 类型 | 默认值 | 必选 | 说明 |
|------|------|--------|------|------|
| `model` | `str` | — | ❗ **必填** | **LiteLLM 模型 ID**，必须带 Provider 前缀。格式：`{provider}/{model-name}`。例如：`openai/gpt-4o`, `anthropic/claude-3-5-sonnet`, `gemini/gemini-1.5-pro`。**未配置会在加载时直接报错。** |
| `base_url` | `str` | `""` | ❌ 否 | API 网关地址。每个模型类型独立配置。**注意：字段名是 `base_url`，不是 `api_base`** |
| `api_key` | `str` | `""` | ❌ 否 | API 认证密钥。每个模型类型独立配置 |
| `description` | `str` | `"Model type '{k}' loaded from YAML config"` | ❌ 否 | 模型的人类可读描述。用于日志和文档 |
| `temperature` | `float` | `0.1` | ❌ 否 | 创造力/随机性控制 (0.0 - 2.0)。详见 [3.2 temperature 建议](#32-temperature-配置建议) |
| `max_tokens` | `int` \| `str` | `150000` | ❌ 否 | 模型单次生成的最大 Token 数。特殊值 `"max"` 表示使用模型原生最大值 |
| `timeout` | `int` | `60` | ❌ 否 | 单次 HTTP 请求超时（秒）。超过此时间未响应则中断 |
| `num_retries` | `int` | `5` | ❌ 否 | API 调用失败重试次数 |
| `retry_delay` | `float` | `15.0` | ❌ 否 | 重试初始延迟（秒）。详见 [第 6 节](#6-重试机制详解) |
| `max_retry_delay` | `float` | `100.0` | ❌ 否 | 重试最大延迟（秒）。指数退避的上限 |
| `extra_headers` | `dict` \| `null` | `null` | ❌ 否 | 自定义 HTTP 请求头。每个模型类型独立配置，不做跨类型合并 |
| `context_cache` | `bool` | `false` | ❌ 否 | 通用 Prompt 缓存优化。`true` 时框架对**所有模型**统一注入 `cache_control: {"type": "ephemeral"}`，litellm 根据 Provider 自动处理（Anthropic 保留、OpenAI 剥离、Vertex AI 转换为 Gemini 格式） |
| `system_prompt_boundary` | `str` \| `null` | `null` | ❌ 否 | 系统提示词分割标记。设置后，系统提示词以此标记分割为 **静态（缓存）** + **动态（不缓存）** 两段，提升缓存命中率。例如：`"<!-- DYNAMIC_BOUNDARY -->"` |
| `requests_per_minute` | `int` | `60` | ❌ 否 | 该模型类型的速率限制 |

### 3.2 temperature 配置建议

| 模型类型 | 推荐值 | 说明 |
|----------|--------|------|
| `powerful` | `0.2` | 逻辑推理/代码生成，需要精确性 |
| `fast` | `0.7` - `1.0` | 分类/路由等简单任务 |
| `summary` | `0.3` - `0.5` | 文本摘要和信息提取 |

> ⚠️ **特殊模型要求**：部分模型（如 OpenAI `o1`, 某些平台的 `gpt-5`）要求 `temperature: 1.0`。如果你使用的模型有此限制，请强制设置。

### 3.3 max_tokens 特殊值

| 配置值 | 行为 |
|--------|------|
| 整数（如 `8192`） | 限制为固定 Token 数 |
| `"max"` (字符串) | 委托给模型的原生最大值（由 LiteLLM 自动获取） |

> 框架使用 `IntParser` 进行宽松解析，`"max"` 是特殊的 bypass 字符串。

### 3.4 extra_headers 覆盖行为

```yaml
model:
  powerful:
    extra_headers:
      X-Model-Tier: "powerful"
      X-Biz-Tag: "demo"

  fast:
    extra_headers:
      X-Model-Tier: "fast"
```

> 也可通过**环境变量**覆盖（JSON 字符串格式）：
> ```bash
> POWERFUL_MODEL_EXTRA_HEADERS='{"X-Req-Source":"cli","X-Trace":"debug"}'
> ```

### 3.5 context_cache 行为

`context_cache: true` 时，框架对**所有模型**统一注入 `cache_control: {"type": "ephemeral"}`，不做 Provider 检测。litellm 根据 Provider 自动处理转换：

| Provider | litellm 行为 |
|----------|-------------|
| Anthropic (`anthropic/`) / Bedrock (`bedrock/`) | 保留 `cache_control`（原生支持） |
| Vertex AI (`vertex_ai/`) | 转换为 Gemini context caching 格式 |
| OpenAI (`openai/`) / Azure (`azure/`) | 自动剥离 `cache_control`（OpenAI 的缓存按前缀匹配，无需显式标记） |
| OpenRouter | 对 Claude/Gemini 保留，其他模型剥离 |
| Fireworks / 其他 | 默认剥离 |

> **设计理念**：统一对所有模型注入 `cache_control`，由 litellm 负责适配各 Provider，无需在框架层做 Provider 检测。这样新增 Provider 时零代码修改。

#### 3.5.1 system_prompt_boundary — 系统提示词分割

当系统提示词包含静态部分（如角色定义、工具说明）和动态部分（如当前任务上下文）时，可以使用 `system_prompt_boundary` 将它们分开：

```yaml
powerful:
  model: "anthropic/claude-sonnet-4-20250514"
  context_cache: true
  system_prompt_boundary: "<!-- DYNAMIC_BOUNDARY -->"
```

系统提示词中在标记前的部分会被标记为缓存（`cache_control: ephemeral`），标记后的部分不缓存。这样可以在动态内容变化时保持静态部分的缓存命中。

#### 3.5.2 cache break 检测

框架会自动检测可能导致缓存失效的变更，并在日志中输出 `[CacheBreak]` 标记：

- **system_prompt 变更**：系统提示词内容发生变化
- **tool_schemas 变更**：工具定义发生变化
- **model 变更**：模型 ID 发生切换

此检测仅用于诊断，不阻塞请求。

### 3.6 Tool Call 行为

在 `tool_call` 模式下，只要有可用工具，AgentLoom 就会发送结构化 tools schema。`tool_choice` 只是普通 provider/smolagents 请求参数，不参与能力探测，也不会作为切换到文本兜底的开关。

Provider 原生 `tool_calls` 是主路径。如果 provider 没有返回原生 `tool_calls`，但返回了结构化文本 block，AgentLoom 只接受明确的工具调用容器：

1. 标准 JSON 对象，例如 `{"name": "...", "arguments": {...}}`
2. provider dump 出来的原生 `tool_calls` / `function` 结构
3. 明确 XML 或 invoke wrapper

解析器不会从 “Calling tool X with args...” 这类自然语言里猜测工具调用。repair 只作用于参数 JSON 字符串，只修 JSON 字符串字面量里的裸 control chars 和非法 backslash。未知工具、多候选文本工具调用、prose/free-text、残缺 JSON 都会显式失败，让 agent 带着清晰错误观察重试，或者直接暴露 provider 问题。

### 3.7 自定义模型类型

你可以完全自由地定义模型类型的名称和数量。以下操作都是合法的：

- **新增**类型：在 `model` 块下添加任意名称的 key
- **删除**预定义类型：不需要 `powerful`/`fast`/`summary`，删掉即可（注意 `summary` 的特殊性，见上方说明）
- **重命名**：把 `powerful` 改成 `main`、`primary` 等任意名称

Agent YAML 通过 `model_type` 字段引用你定义的类型名。

**示例 1：完全自定义命名**

```yaml
# config/llm.yaml — 不使用 powerful/fast/summary，完全自定义
model:
  default_model_type: "main"

  main:                          # ✅ 自定义名称，替代 powerful
    model: "anthropic/claude-3-5-sonnet"
    base_url: "https://your-gateway.com/v1"
    api_key: "your-key"
    temperature: 0.2

  code_review:                   # ✅ 自定义类型
    model: "anthropic/claude-3-5-sonnet"
    base_url: "https://your-gateway.com/v1"
    api_key: "your-key"
    temperature: 0.1
    max_tokens: 16384
    timeout: 600

  translation:                   # ✅ 自定义类型
    model: "openai/gpt-4o"
    base_url: "https://api.openai.com/v1"
    api_key: "your-openai-key"
    temperature: 0.3
    max_tokens: 4096

  summary:                       # 保留 summary 以支持 smart_summary 功能
    model: "openai/gpt-4o-mini"
    base_url: "https://api.openai.com/v1"
    api_key: "your-openai-key"
    temperature: 0.3
```

```yaml
# Agent YAML — 引用自定义类型
name: "code_review_agent"
model_type: "code_review"        # 引用 llm.yaml 中定义的 code_review
```

**示例 2：最简配置（只保留一个类型）**

```yaml
# config/llm.yaml — 所有 Agent 共用一个模型
model:
  default_model_type: "default"

  default:
    model: "openai/gpt-4o"
    base_url: "https://your-gateway.com/v1"
    api_key: "your-key"
    temperature: 0.3
```

> 所有自定义类型与框架示例中的 `powerful`/`fast`/`summary` **完全等价**。每个类型独立声明自己的模型和参数。

**示例**：

```yaml
model:
  powerful:
    model: "anthropic/aws-claude-opus-4-5"
    base_url: "https://llm-gateway.example.com"
    description: "高质量推理模型"
    temperature: 0.2
    max_tokens: 8192
    timeout: 300
    num_retries: 3
    retry_delay: 10.0
    max_retry_delay: 60.0
    context_cache: true
    extra_headers:
      X-Model-Tier: "powerful"

  fast:
    model: "gemini/gemini-3_1-pro-preview"
    base_url: "https://generativelanguage.googleapis.com/v1"
    api_key: "gemini-key"
    temperature: 1.0
    max_tokens: 1024
    timeout: 60
    context_cache: true

  summary:
    model: "openai/azure-gpt-5-chat"
    base_url: "https://portal-k8s-prod.ep.chehejia.com/api/copilot/v3/openai/azure-gpt-5-chat/v1"
    api_key: "summary-key"
    temperature: 1.0
    max_tokens: 2048
    timeout: 300
```

---

## 4. 参数默认值

模型参数的最终值按以下规则解析：

```
模型类型设置 (model.powerful.xxx)
      ↓ (未设置时)
代码默认值 (defaults.py)
```

**具体默认规则**：

| 字段 | 默认行为 |
|------|----------|
| `base_url` | 未设置时为 `""` |
| `api_key` | 未设置时为 `""` |
| `temperature` | 未设置时为 `0.1` |
| `max_tokens` | 未设置时为 `150000` |
| `timeout` | 未设置时为 `60` |
| `num_retries` | 未设置时为 `5` |
| `retry_delay` | 未设置时为 `15.0` |
| `max_retry_delay` | 未设置时为 `100.0` |
| `extra_headers` | 未设置时为 `null` |
| `requests_per_minute` | 未设置时为 `60` |
| `model` | ❗ 必填，不存在默认值 |
| `description` | 未设置时自动生成 |
| `context_cache` | 未设置时为 `false` |

---

## 5. langfuse — 可观测性配置（后续支持）

> ⚠️ **当前版本尚未集成 Langfuse 自动追踪功能。** 代码中已预留 `LangfuseSettings` 配置模型，但尚未接入 LiteLLM 的回调链路。`config/llm.yaml` 中**不需要**配置 langfuse 段。

后续版本计划集成 Langfuse，届时只需在 `config/llm.yaml` 中添加以下配置即可启用：

```yaml
# 后续版本启用 Langfuse 时的配置方式（当前无效）
langfuse:
  enabled: true
  host: "https://us.cloud.langfuse.com"
  public_key: "pk-lf-xxxxxxxx"
  secret_key: "sk-lf-xxxxxxxx"
```

| 参数 | 类型 | 说明 |
|------|------|------|
| `langfuse.enabled` | `bool` | 是否启用追踪 |
| `langfuse.host` | `str` | Langfuse 服务端点 |
| `langfuse.public_key` | `str` | 公钥 |
| `langfuse.secret_key` | `str` | 密钥 |

---

## 6. 重试机制详解

框架使用**自定义重试包装器**（而非 LiteLLM 内置重试），基于 tenacity 库实现指数退避。

### 6.1 指数退避公式

$$\text{delay} = \min(\text{retry\_delay} \times 2^{\text{attempt}}, \text{max\_retry\_delay})$$

**示例**（默认参数 `retry_delay=15.0`, `max_retry_delay=100.0`）：

| 重试次数 | 计算延迟 | 实际延迟 |
|----------|---------|---------|
| 第 1 次 | 15 × 2¹ = 30s | 30s |
| 第 2 次 | 15 × 2² = 60s | 60s |
| 第 3 次 | 15 × 2³ = 120s | 100s（受 max_retry_delay 限制） |
| 第 4 次 | 15 × 2⁴ = 240s | 100s |
| 第 5 次 | 15 × 2⁵ = 480s | 100s |

### 6.2 可重试的错误类型

| 错误类型 | 说明 |
|----------|------|
| `Timeout` | HTTP 请求超时 |
| `RateLimitError` | API 速率限制 (429) |
| `APIConnectionError` | 网络连接失败 |
| `InternalServerError` | API 服务端错误 (500) |
| `ServiceUnavailableError` | 服务不可用 (503) |
| `AuthenticationError` | 认证失败 (401) |
| `PermissionDeniedError` | 权限不足 (403) |

### 6.3 重试日志

每次重试都会输出日志：
```
litellm.completion failed (attempt 2/5): RateLimitError: Rate limit exceeded. Retrying in 60s
```

> **注意**：为避免"双重重试"，框架传递给 LiteLLM 的参数中 `num_retries=0`，完全由框架自身控制重试逻辑。

---

## 7. Provider 前缀与特定行为

`model` 字段的值必须包含 **Provider 前缀**，格式为 `{provider}/{model-name}`。LiteLLM 根据前缀自动路由到对应的 API。

### 7.1 支持的 Provider 前缀

| Provider 前缀 | API 类型 | model 示例 |
|---------------|---------|-----------|
| `openai/` | OpenAI 兼容接口 | `openai/gpt-4o`, `openai/azure-gpt-5-chat` |
| `anthropic/` | Anthropic 原生接口 | `anthropic/claude-3-5-sonnet`, `anthropic/aws-claude-opus-4-5` |
| `gemini/` | Google AI Studio | `gemini/gemini-1.5-pro`, `gemini/gemini-3_1-pro-preview` |
| `vertex_ai/` | Google Vertex AI | `vertex_ai/gemini-1.5-pro` |
| `azure/` | Azure OpenAI | `azure/gpt-4-deployment` (注：model 值 = Azure 部署名) |
| `ollama/` | 本地 Ollama | `ollama/llama3`, `ollama/codellama` |

### 7.2 Provider 特定行为

| Provider | temperature 限制 | Context Cache | 特殊说明 |
|----------|-----------------|---------------|---------|
| OpenAI | 0.0 - 2.0 | ✅ 支持（hash-based） | 部分特殊模型要求 `temperature: 1.0` |
| Anthropic | 0.0 - 1.0（建议） | ✅ 支持（ephemeral） | 自动转换 OpenAI 消息格式到 Claude 格式 |
| Gemini | 部分模型要求 `1.0` | ❌ 暂不支持自动缓存 | — |
| Azure | 同 OpenAI | ✅ 同 OpenAI | `model` 参数 = Azure 部署名称（非模型名） |
| Ollama | 无限制 | ❌ | `base_url` 通常设为 `http://localhost:11434` |

---

## 8. 默认值常量对照表

以下常量定义在 `src/lib/config/defaults.py` 中，是所有模型参数的最终兜底值：

| 常量名 | 值 | 对应参数 |
|--------|-----|---------|
| `DEFAULT_MAX_TOKENS` | `150000` | `max_tokens` |
| `DEFAULT_MODEL_TEMPERATURE` | `0.1` | `temperature` |
| `DEFAULT_MODEL_TIMEOUT` | `60` | `timeout` |
| `DEFAULT_MODEL_NUM_RETRIES` | `5` | `num_retries` |
| `DEFAULT_MODEL_RETRY_DELAY` | `15.0` | `retry_delay` |
| `DEFAULT_MODEL_MAX_RETRY_DELAY` | `100.0` | `max_retry_delay` |
| `DEFAULT_MODEL_CONTEXT_CACHE` | `False` | `context_cache` |
| `DEFAULT_MODEL_REQUESTS_PER_MINUTE` | `60` | `requests_per_minute` |

---

## 附录 A：完整 Pydantic 模型

### LlmModelTypeSettings

```python
class LlmModelTypeSettings(BaseModel):
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.1           # DEFAULT_MODEL_TEMPERATURE
    max_tokens: int | str = 150000     # DEFAULT_MAX_TOKENS
    timeout: int = 60                  # DEFAULT_MODEL_TIMEOUT
    num_retries: int = 5              # DEFAULT_MODEL_NUM_RETRIES
    retry_delay: float = 15.0         # DEFAULT_MODEL_RETRY_DELAY
    max_retry_delay: float = 100.0    # DEFAULT_MODEL_MAX_RETRY_DELAY
    extra_headers: dict | None = None
    context_cache: bool = False       # DEFAULT_MODEL_CONTEXT_CACHE
    system_prompt_boundary: str | None = None
    description: str = ""
    requests_per_minute: int = 60     # DEFAULT_MODEL_REQUESTS_PER_MINUTE
```

### LangfuseSettings（后续版本启用，当前不需要配置）

```python
class LangfuseSettings(BaseModel):
    enabled: bool = True
    host: str = "https://cloud.langfuse.com"
    public_key: str = ""
    private_key: str = ""    # 首选
    secret_key: str = ""     # private_key 的备选
```

### LLMConfig（顶层容器）

```python
class LLMConfig(BaseModel):
    langfuse: LangfuseSettings           # 预留，后续启用
    default_model_type: str = ""         # 没有隐式模型类型默认值
    models: dict[str, LlmModelTypeSettings]  # {"powerful": ..., "summary": ..., 或自定义类型}
```

**运行时访问方式**：

```python
from src.lib.config.config import C

# 获取 LLMConfig 对象
llm = C.llm

# 获取指定类型的模型配置
powerful_config = C.llm.for_type("powerful")   # → LlmModelTypeSettings
fast_config = C.llm.for_type("fast")

# 获取已定义的模型类型列表（包括自定义类型）
available = C.llm.available_types              # → ["powerful", "fast", "summary", "code_review", ...]
```

---

## 附录 B：常见配置场景

### B.1 切换到 OpenAI GPT-4o

```yaml
model:
  powerful:
    model: "openai/gpt-4o"
    base_url: "https://api.openai.com/v1"
    api_key: "sk-your-openai-key"
    temperature: 0.2
    max_tokens: 8192
    timeout: 120
```

### B.2 使用本地 Ollama

```yaml
model:
  powerful:
    model: "ollama/llama3"
    base_url: "http://localhost:11434"
    temperature: 0.3
    max_tokens: 4096
    timeout: 120

  fast:
    model: "ollama/phi3"
    base_url: "http://localhost:11434"
    temperature: 0.7
    max_tokens: 1024
    timeout: 30
```

### B.3 多 Provider 混合部署

```yaml
model:
  default_model_type: "powerful"

  powerful:
    model: "anthropic/aws-claude-opus-4-5"
    base_url: "https://your-anthropic-gateway.com"
    api_key: "anthropic-specific-key"
    requests_per_minute: 30
    temperature: 0.2
    max_tokens: 8192

  fast:
    model: "openai/gpt-4o-mini"
    base_url: "https://api.openai.com/v1"
    api_key: "openai-specific-key"
    requests_per_minute: 30
    temperature: 0.7
    max_tokens: 1024

  summary:
    model: "gemini/gemini-1.5-flash"
    base_url: "https://generativelanguage.googleapis.com/v1"
    api_key: "gemini-specific-key"
    requests_per_minute: 30
    temperature: 0.3
    max_tokens: 2048
```

### B.4 最小化配置（所有模型共用同一个 API）

```yaml
model:
  powerful:
    model: "openai/gpt-4o"
    base_url: "https://your-openai-proxy.com/v1"
    api_key: "sk-your-key"
    temperature: 0.2

  fast:
    model: "openai/gpt-4o-mini"
    base_url: "https://your-openai-proxy.com/v1"
    api_key: "sk-your-key"
    temperature: 0.7

  summary:
    model: "openai/gpt-4o-mini"
    base_url: "https://your-openai-proxy.com/v1"
    api_key: "sk-your-key"
    temperature: 0.3
```
