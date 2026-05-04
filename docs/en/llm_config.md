# AgentLoom LLM Model Configuration (`llm.yaml`) Complete Reference

> **Document scope**: This document details **every** configuration parameter in `config/llm.yaml`.
> For override relationships between configuration files, see [Configuration System Overview](config-overview.md).
> For global system configuration, see [System Configuration Reference](system_config.md).
> For Agent YAML parameters, see [Agent Configuration Reference](agent_config.md).

`config/llm.yaml` is the AgentLoom framework's **model routing and parameter configuration file**, controlling LLM model types (powerful/fast/summary or custom types), API credentials, inference parameters, and retry policies used by different agents.

> ⚠️ **Important**: LLM configuration is **loaded independently** and does not participate in the `system.yaml` deep merge override chain. If `model`/`llm` appears in `config/system.yaml` or `applications/<app>/config/system.yaml`, it will be automatically filtered and a warning will be printed.

---

## Table of Contents

- [Quick Reference: Complete YAML Structure](#quick-reference-complete-yaml-structure)
- [1. model.default_model_type — Global Default Model Type](#1-modeldefault_model_type--global-default-model-type)
- [2. model.common — Common Fallback Configuration](#2-modelcommon--common-fallback-configuration)
- [3. model.\<type\> — Model Type Configuration](#3-modeltype--model-type-configuration)
- [4. Parameter Inheritance Chain](#4-parameter-inheritance-chain)
- [5. langfuse — Observability Configuration (Future Support)](#5-langfuse--observability-configuration-future-support)
- [6. Retry Mechanism Details](#6-retry-mechanism-details)
- [7. Provider Prefixes and Specific Behaviors](#7-provider-prefixes-and-specific-behaviors)
- [8. Default Value Constants Reference Table](#8-default-value-constants-reference-table)
- [Appendix A: Complete Pydantic Models](#appendix-a-complete-pydantic-models)
- [Appendix B: Common Configuration Scenarios](#appendix-b-common-configuration-scenarios)

---

## Quick Reference: Complete YAML Structure

The following shows the **complete structure** of `config/llm.yaml`, with all fields and their default values:

```yaml
# ============================================
# Model Configuration
# ============================================
model:
  # Global default model type (optional, defaults to "common")
  default_model_type: "powerful"

  # ━━━ Required: Common shared configuration (also a regular model type) ━━━
  # Fields missing from other model types are automatically inherited from common
  # Agents can also directly use model_type: "common"
  common:
    model: "openai/gpt-4o"            # ❗ Required: LiteLLM model ID
    base_url: "https://llm-gateway-proxy.inner.chj.cloud/llm-gateway/v1"
    api_key: "your-api-key"
    requests_per_minute: 10
    num_retries: 5
    retry_delay: 15.0
    max_retry_delay: 100.0

  # ━━━ Required: Summary model (smart_summary context compression depends on this) ━━━
  summary:
    model: "openai/azure-gpt-5-chat"
    description: "Summary model, suitable for summarization and extraction"
    temperature: 1.0
    max_tokens: 2048
    timeout: 300

  # ━━━ The following model types are all optional — can be deleted/renamed/added ━━━

  # Powerful model (complex reasoning, code generation)
  powerful:
    model: "anthropic/aws-claude-opus-4-5"
    base_url: "https://llm-gateway-proxy.inner.chj.cloud/llm-gateway"
    description: "High-quality reasoning model, suitable for complex analysis/code tasks"
    temperature: 0.2
    max_tokens: 8192
    timeout: 300
    context_cache: true

  # Fast model (intent recognition, classification)
  fast:
    model: "anthropic/aws-claude-sonnet-4-5"
    description: "Low-latency model, suitable for simple tasks"
    temperature: 1.0
    max_tokens: 1024
    timeout: 300

  # Custom model type (optional, name freely)
  # code_review:
  #   model: "anthropic/claude-3-5-sonnet"
  #   temperature: 0.1
  #   max_tokens: 16384
```

---

## 1. model.default_model_type — Global Default Model Type

Controls which model type the entire Agent system uses when an Agent YAML does not specify `model_type`.

| Parameter | Type | Default | Required | Description |
|------|------|--------|------|------|
| `model.default_model_type` | `str` | `"common"` | ❌ No | Global default model type. Must be a type key defined under the `model` block (e.g., `powerful`, `fast`, `summary`, or a custom type name). Defaults to `"common"` when not configured, using the common shared configuration as fallback |

### 1.1 Complete Fallback Chain

When an Agent needs to obtain model configuration, resolution follows this priority:

```
model_type in Agent YAML (e.g., model_type: "fast")
       ↓ (when not specified)
default_model_type in config/llm.yaml (e.g., default_model_type: "powerful")
       ↓ (when also not configured)
common shared configuration as fallback
```

> ⚠️ **Note**: If an Agent YAML explicitly specifies `model_type` but that type does not exist in `llm.yaml`, the framework will **raise an error** (`ValueError`) directly, without silently falling back. This is to expose configuration errors as early as possible.

**Example**:

```yaml
model:
  default_model_type: "powerful"   # All Agents default to the powerful type
  # default_model_type: "fast"     # If defaulting to the fast model

  common:
    base_url: "https://your-gateway.com/v1"
    api_key: "your-key"

  powerful:
    model: "anthropic/claude-3-5-sonnet"
    temperature: 0.2
```

```yaml
# Agent YAML example
name: "my_agent"
model_type: "powerful"    # Uses the powerful type; if omitted, uses default_model_type
```

---

## 2. model.common — Common Shared Configuration (Required)

`common` is a **required model type** with a dual role:

1. **Regular model type**: Agents can use it directly via `model_type: "common"`, just like `powerful`/`fast`
2. **Shared parameter pool**: Fields missing from other model types (e.g., `base_url`, `api_key`) are automatically inherited from `common`

> `common` is **exactly the same** as other model types, with the same field list (see [3.1 Complete Parameter List](#31-complete-parameter-list)). The `model` field is also required.

**Example**:

```yaml
model:
  common:
    model: "openai/gpt-4o"              # ❗ Required: LiteLLM model ID
    base_url: "https://your-gateway.com/v1"
    api_key: "sk-your-api-key"
    requests_per_minute: 10
    num_retries: 5
    retry_delay: 15.0
    max_retry_delay: 100.0
```

**Parameter inheritance example**:

```yaml
model:
  common:
    model: "openai/gpt-4o"              # Default model
    base_url: "https://your-gateway.com/v1"
    api_key: "sk-your-key"

  powerful:
    model: "anthropic/claude-3-5-sonnet"  # Overrides common's model
    temperature: 0.2                      # Overrides common's temperature
    # base_url and api_key not set → automatically inherited from common
```

---

## 3. model.\<type\> — Model Type Configuration

**Overview of all keys under the `model` block:**

| Key | Required | Description |
|-----|---------|------|
| `common` | ❗ **Required** | Regular model type + parameter inheritance source for other types; `default_model_type` points to it by default |
| `summary` | ❗ **Required** | Context compression (`smart_summary`) feature has a hard dependency on this type |
| `default_model_type` | ❌ Optional | Reserved key, defaults to `"common"` |
| Any other key | ❌ Optional | Freely define, delete, or rename, e.g., `powerful`, `fast`, `code_review`, etc. |

Except for `default_model_type`, **all keys under the `model` block are parsed as model types** (including `common`). The framework has no restrictions on type names — `powerful` and `fast` are just example names. You can freely delete, rename, or add new ones. The `model` field for each model type (including `common`) is required.

**YAML path**: `model.<your-type-name>.*` (e.g., `model.powerful.*`, `model.my_llm.*`)
**Pydantic model**: `LlmModelTypeSettings`

### 3.1 Complete Parameter List

| Parameter | Type | Default | Required | Inherited from common | Description |
|------|------|--------|------|--------------|------|
| `model` | `str` | — | ❗ **Required** | ❌ Not inherited | **LiteLLM model ID**, must include Provider prefix. Format: `{provider}/{model-name}`. Examples: `openai/gpt-4o`, `anthropic/claude-3-5-sonnet`, `gemini/gemini-1.5-pro`. **Raises an error if not configured.** |
| `base_url` | `str` | `""` | ❌ No | ✅ Inherited | API gateway address. Inherits `common.base_url` when not set. **Note: field name is `base_url`, not `api_base`** |
| `api_key` | `str` | `""` | ❌ No | ✅ Inherited | API authentication key. Inherits `common.api_key` when not set |
| `description` | `str` | `"Model type '{k}' loaded from YAML config"` | ❌ No | ❌ Not inherited | Human-readable model description. Used in logs and documentation |
| `temperature` | `float` | `0.1` | ❌ No | ✅ Inherited | Creativity/randomness control (0.0 - 2.0). See [3.2 temperature Recommendations](#32-temperature-configuration-recommendations) |
| `max_tokens` | `int` \| `str` | `150000` | ❌ No | ✅ Inherited | Maximum tokens per single model generation. Special value `"max"` uses the model's native maximum |
| `timeout` | `int` | `60` | ❌ No | ✅ Inherited | Single HTTP request timeout (seconds). Interrupted if no response within this time |
| `num_retries` | `int` | `5` | ❌ No | ✅ Inherited | Number of retries on API call failure |
| `retry_delay` | `float` | `15.0` | ❌ No | ✅ Inherited | Initial retry delay (seconds). See [Section 6](#6-retry-mechanism-details) |
| `max_retry_delay` | `float` | `100.0` | ❌ No | ✅ Inherited | Maximum retry delay (seconds). Upper limit for exponential backoff |
| `extra_headers` | `dict` \| `null` | `null` | ❌ No | ✅ Inherited (no merge) | Custom HTTP request headers. **⚠️ Model-level `extra_headers` completely overrides `common.extra_headers`, no merging** |
| `context_cache` | `bool` | `false` | ❌ No | ❌ Not inherited | Universal Prompt cache optimization. When `true`, the framework injects `cache_control: {"type": "ephemeral"}` for **ALL models** universally. litellm handles per-provider behavior automatically (Anthropic preserves, OpenAI strips, Vertex AI converts to Gemini format) |
| `system_prompt_boundary` | `str` \| `null` | `null` | ❌ No | ❌ Not inherited | System prompt split marker. When set, the system prompt is split into **static (cached)** + **dynamic (uncached)** segments at this marker, improving cache hit rates. Example: `"<!-- DYNAMIC_BOUNDARY -->"` |
| `requests_per_minute` | `int` | `60` | ❌ No | ✅ Inherited | Rate limit for this model type |
| `supports_native_tool_calls` | `str` | `"auto"` | ❌ No | ✅ Inherited | Native tool_calls capability flag. Tri-state: `"auto"` auto-detects on first call, `"true"` always uses native path, `"false"` always uses text parsing fallback. See [3.6 supports_native_tool_calls Behavior](#36-supports_native_tool_calls-behavior) |

### 3.2 temperature Configuration Recommendations

| Model Type | Recommended Value | Description |
|----------|--------|------|
| `powerful` | `0.2` | Logic reasoning/code generation, requires precision |
| `fast` | `0.7` - `1.0` | Simple tasks like classification/routing |
| `summary` | `0.3` - `0.5` | Text summarization and information extraction |

> ⚠️ **Special model requirements**: Some models (e.g., OpenAI `o1`, certain platforms' `gpt-5`) require `temperature: 1.0`. If your model has this restriction, set it explicitly.

### 3.3 max_tokens Special Values

| Config Value | Behavior |
|--------|------|
| Integer (e.g., `8192`) | Limits to a fixed token count |
| `"max"` (string) | Delegates to the model's native maximum (automatically obtained by LiteLLM) |

> The framework uses `IntParser` for lenient parsing; `"max"` is a special bypass string.

### 3.4 extra_headers Override Behavior

```yaml
model:
  common:
    extra_headers:
      X-Project: "AgentLoom"
      X-Env: "dev"

  powerful:
    extra_headers:          # ⚠️ Completely overrides common's extra_headers
      X-Model-Tier: "powerful"
      X-Biz-Tag: "demo"
    # Final powerful headers = {X-Model-Tier: "powerful", X-Biz-Tag: "demo"}
    # common's {X-Project, X-Env} are NOT merged in

  fast:
    # extra_headers not set → inherits common's {X-Project: "AgentLoom", X-Env: "dev"}
```

> You can also override via **environment variables** (JSON string format):
> ```bash
> POWERFUL_MODEL_EXTRA_HEADERS='{"X-Req-Source":"cli","X-Trace":"debug"}'
> ```

### 3.5 context_cache Behavior

When `context_cache: true`, the framework injects `cache_control: {"type": "ephemeral"}` for **ALL models** universally, with no provider detection. litellm handles per-provider transformation automatically:

| Provider | litellm Behavior |
|----------|-------------|
| Anthropic (`anthropic/`) / Bedrock (`bedrock/`) | Preserves `cache_control` (native support) |
| Vertex AI (`vertex_ai/`) | Converts to Gemini context caching format |
| OpenAI (`openai/`) / Azure (`azure/`) | Strips `cache_control` (OpenAI caching is automatic by prefix match, no explicit marker needed) |
| OpenRouter | Preserves for Claude/Gemini, strips for others |
| Fireworks / Others | Strips by default |

> **Design philosophy**: Inject `cache_control` universally for all models and let litellm handle per-provider adaptation. No provider detection needed at the framework level. Adding new providers requires zero code changes.

#### 3.5.1 system_prompt_boundary — System Prompt Splitting

When the system prompt contains both static parts (e.g., role definition, tool descriptions) and dynamic parts (e.g., current task context), use `system_prompt_boundary` to split them:

```yaml
powerful:
  model: "anthropic/claude-sonnet-4-20250514"
  context_cache: true
  system_prompt_boundary: "<!-- DYNAMIC_BOUNDARY -->"
```

Content before the marker is tagged as cached (`cache_control: ephemeral`), content after is uncached. This preserves cache hits for static content even when dynamic content changes.

#### 3.5.2 Cache Break Detection

The framework automatically detects changes that may invalidate the cache, logging `[CacheBreak]` markers:

- **system_prompt changed**: System prompt content changed
- **tool_schemas changed**: Tool definitions changed
- **model changed**: Model ID switched

This detection is diagnostic only and does not block requests.

### 3.6 supports_native_tool_calls Behavior

`supports_native_tool_calls` controls how an Agent in tool_call mode handles LLM tool call output:

| Value | Behavior |
|-------|----------|
| `"auto"` (default) | On the first API call, attempts the native path and checks whether the response contains `tool_calls`. If yes, subsequent calls use the native path directly; if no, subsequent calls use the multi-strategy text parsing fallback. Detection result is cached on the model instance and not repeated within the same session |
| `"true"` | Skip detection, always assume the model returns native `tool_calls`. Suitable for models known to support native tool calling (e.g., OpenAI GPT-4o, Anthropic Claude) |
| `"false"` | Skip detection, always use multi-strategy text parsing. Suitable for models known not to return native `tool_calls` (e.g., non-Anthropic models connected via Anthropic-compatible endpoints) |

#### Multi-Strategy Text Parsing

When the model does not return native `tool_calls`, the framework's built-in multi-strategy parsing chain handles text-based tool calls, trying approaches in priority order:

1. **JSON** — Standard JSON format
2. **XML/bracket** — XML-wrapped formats (e.g., `<minimax:tool_call>[...]</minimax:tool_call>`), auto-delegating to nested parsing
3. **Regex** — Regular expression extraction
4. **Structural extraction** — Bracket-depth based parsing, handles large text with apostrophes or special characters

The parser uses a generic design not tied to any specific model vendor — regardless of XML namespace prefix or tag format the model outputs, it is automatically recognized. The first successful strategy returns the result; only when all fail does it report an error (using the smolagents native error feedback retry flow).

**Configuration example**:

```yaml
# Model known not to support native tool_calls
powerful:
  model: "anthropic/MiniMax-M2.7"
  base_url: "https://api.minimaxi.com/anthropic"
  supports_native_tool_calls: "false"   # Skip detection, use multi-strategy parsing

# Model supporting native tool_calls
fast:
  model: "openai/gpt-4o"
  supports_native_tool_calls: "true"    # Skip detection, use native path directly
```

### 3.7 Custom Model Types

You can freely define model type names and quantities. All of the following operations are valid:

- **Add** types: Add any named key under the `model` block
- **Delete** predefined types: Don't need `powerful`/`fast`/`summary`, just remove them (note `summary`'s special role, see above)
- **Rename**: Change `powerful` to `main`, `primary`, or any other name

Agent YAML references your defined type names through the `model_type` field.

**Example 1: Fully custom naming**

```yaml
# config/llm.yaml — Not using powerful/fast/summary, fully custom
model:
  default_model_type: "main"

  common:
    base_url: "https://your-gateway.com/v1"
    api_key: "your-key"

  main:                          # ✅ Custom name, replaces powerful
    model: "anthropic/claude-3-5-sonnet"
    temperature: 0.2

  code_review:                   # ✅ Custom type
    model: "anthropic/claude-3-5-sonnet"
    temperature: 0.1
    max_tokens: 16384
    timeout: 600

  translation:                   # ✅ Custom type
    model: "openai/gpt-4o"
    temperature: 0.3
    max_tokens: 4096

  summary:                       # Keep summary to support smart_summary feature
    model: "openai/gpt-4o-mini"
    temperature: 0.3
```

```yaml
# Agent YAML — Reference custom type
name: "code_review_agent"
model_type: "code_review"        # References code_review defined in llm.yaml
```

**Example 2: Minimal configuration (all Agents share one model)**

```yaml
# config/llm.yaml — All Agents share a single model
model:
  default_model_type: "default"

  common:
    base_url: "https://your-gateway.com/v1"
    api_key: "your-key"

  default:
    model: "openai/gpt-4o"
    temperature: 0.3
```

> All custom types are **fully equivalent** to the framework examples' `powerful`/`fast`/`summary`, with the same common inheritance mechanism.

**Example**:

```yaml
model:
  powerful:
    model: "anthropic/aws-claude-opus-4-5"
    base_url: "https://llm-gateway.example.com"
    description: "High-quality reasoning model"
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
    temperature: 1.0
    max_tokens: 1024
    timeout: 60
    context_cache: true
    # base_url and api_key not set, inherited from common

  summary:
    model: "openai/azure-gpt-5-chat"
    base_url: "https://portal-k8s-prod.ep.chehejia.com/api/copilot/v3/openai/azure-gpt-5-chat/v1"
    temperature: 1.0
    max_tokens: 2048
    timeout: 300
```

---

## 4. Parameter Inheritance Chain

The final value of model parameters is resolved in the following priority chain from high to low:

```
Model type settings (model.powerful.xxx)
       ↓ (fallback when not set)
Common settings (model.common.xxx)
       ↓ (fallback when not set)
Code default values (defaults.py)
```

**Specific inheritance rules**:

| Field | Inheritance Behavior |
|------|----------|
| `base_url` | model type → common → `""` |
| `api_key` | model type → common → `""` |
| `temperature` | model type → common → `0.1` |
| `max_tokens` | model type → common → `150000` |
| `timeout` | model type → common → `60` |
| `num_retries` | model type → common → `5` |
| `retry_delay` | model type → common → `15.0` |
| `max_retry_delay` | model type → common → `100.0` |
| `extra_headers` | model type → common → `null` (**overrides, no merge**) |
| `requests_per_minute` | model type → common → `60` |
| `model` | ❌ **Not inherited**, set independently per type |
| `description` | ❌ **Not inherited**, set independently per type |
| `context_cache` | ❌ **Not inherited**, defaults to `false` |

---

## 5. langfuse — Observability Configuration (Future Support)

> ⚠️ **The current version has not yet integrated Langfuse automatic tracing.** The code includes a pre-allocated `LangfuseSettings` configuration model, but it has not been connected to LiteLLM's callback pipeline. There is **no need** to configure the langfuse section in `config/llm.yaml`.

Future versions plan to integrate Langfuse. At that time, simply add the following configuration to `config/llm.yaml`:

```yaml
# Configuration for enabling Langfuse in future versions (currently inactive)
langfuse:
  enabled: true
  host: "https://us.cloud.langfuse.com"
  public_key: "pk-lf-xxxxxxxx"
  secret_key: "sk-lf-xxxxxxxx"
```

| Parameter | Type | Description |
|------|------|------|
| `langfuse.enabled` | `bool` | Whether to enable tracing |
| `langfuse.host` | `str` | Langfuse service endpoint |
| `langfuse.public_key` | `str` | Public key |
| `langfuse.secret_key` | `str` | Secret key |

---

## 6. Retry Mechanism Details

The framework uses a **custom retry wrapper** (not LiteLLM's built-in retry), implemented with exponential backoff based on the tenacity library.

### 6.1 Exponential Backoff Formula

$$\text{delay} = \min(\text{retry\_delay} \times 2^{\text{attempt}}, \text{max\_retry\_delay})$$

**Example** (default parameters `retry_delay=15.0`, `max_retry_delay=100.0`):

| Retry Attempt | Calculated Delay | Actual Delay |
|----------|---------|---------|
| 1st | 15 × 2¹ = 30s | 30s |
| 2nd | 15 × 2² = 60s | 60s |
| 3rd | 15 × 2³ = 120s | 100s (capped by max_retry_delay) |
| 4th | 15 × 2⁴ = 240s | 100s |
| 5th | 15 × 2⁵ = 480s | 100s |

### 6.2 Retryable Error Types

| Error Type | Description |
|----------|------|
| `Timeout` | HTTP request timeout |
| `RateLimitError` | API rate limit (429) |
| `APIConnectionError` | Network connection failure |
| `InternalServerError` | API server error (500) |
| `ServiceUnavailableError` | Service unavailable (503) |
| `AuthenticationError` | Authentication failure (401) |
| `PermissionDeniedError` | Permission denied (403) |

### 6.3 Retry Logging

Each retry outputs a log message:
```
litellm.completion failed (attempt 2/5): RateLimitError: Rate limit exceeded. Retrying in 60s
```

> **Note**: To avoid "double retrying", the framework passes `num_retries=0` to LiteLLM, fully controlling retry logic itself.

---

## 7. Provider Prefixes and Specific Behaviors

The `model` field value must include a **Provider prefix** in the format `{provider}/{model-name}`. LiteLLM automatically routes to the corresponding API based on the prefix.

### 7.1 Supported Provider Prefixes

| Provider Prefix | API Type | model Example |
|---------------|---------|-----------|
| `openai/` | OpenAI-compatible interface | `openai/gpt-4o`, `openai/azure-gpt-5-chat` |
| `anthropic/` | Anthropic native interface | `anthropic/claude-3-5-sonnet`, `anthropic/aws-claude-opus-4-5` |
| `gemini/` | Google AI Studio | `gemini/gemini-1.5-pro`, `gemini/gemini-3_1-pro-preview` |
| `vertex_ai/` | Google Vertex AI | `vertex_ai/gemini-1.5-pro` |
| `azure/` | Azure OpenAI | `azure/gpt-4-deployment` (Note: model value = Azure deployment name) |
| `ollama/` | Local Ollama | `ollama/llama3`, `ollama/codellama` |

### 7.2 Provider-Specific Behaviors

| Provider | temperature Restrictions | Context Cache | Special Notes |
|----------|-----------------|---------------|---------|
| OpenAI | 0.0 - 2.0 | ✅ Supported (hash-based) | Some special models require `temperature: 1.0` |
| Anthropic | 0.0 - 1.0 (recommended) | ✅ Supported (ephemeral) | Automatically converts OpenAI message format to Claude format |
| Gemini | Some models require `1.0` | ❌ Auto-caching not yet supported | — |
| Azure | Same as OpenAI | ✅ Same as OpenAI | `model` parameter = Azure deployment name (not model name) |
| Ollama | No restrictions | ❌ | `base_url` typically set to `http://localhost:11434` |

---

## 8. Default Value Constants Reference Table

The following constants are defined in `src/lib/config/defaults.py` and serve as the ultimate fallback values for all model parameters:

| Constant Name | Value | Corresponding Parameter |
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

## Appendix A: Complete Pydantic Models

### LlmCommonSettings

```python
class LlmCommonSettings(BaseModel):
    base_url: str = ""
    api_key: str = ""
    requests_per_minute: int = 60  # DEFAULT_MODEL_REQUESTS_PER_MINUTE
```

> **Note**: Although the Pydantic model above only explicitly declares these three fields, the actual parsing logic (`LLMConfig.from_dict`) reads the raw `common` dictionary from YAML directly, providing fallback inheritance for additional parameters like `temperature`, `timeout`, `extra_headers`, etc.

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

### LangfuseSettings (Future version, currently no configuration needed)

```python
class LangfuseSettings(BaseModel):
    enabled: bool = True
    host: str = "https://cloud.langfuse.com"
    public_key: str = ""
    private_key: str = ""    # Preferred
    secret_key: str = ""     # Alias for private_key
```

### LLMConfig (Top-level container)

```python
class LLMConfig(BaseModel):
    langfuse: LangfuseSettings           # Reserved, future activation
    common: LlmCommonSettings
    default_model_type: str = "common"   # Defaults to "common" when not configured
    models: dict[str, LlmModelTypeSettings]  # {"common": ..., "powerful": ..., "summary": ..., or custom types}
```

**Runtime access**:

```python
from src.lib.config.config import C

# Get the LLMConfig object
llm = C.llm

# Get model configuration for a specific type
powerful_config = C.llm.for_type("powerful")   # → LlmModelTypeSettings
fast_config = C.llm.for_type("fast")

# Get list of defined model types (including custom types)
available = C.llm.available_types              # → ["powerful", "fast", "summary", "code_review", ...]
```

---

## Appendix B: Common Configuration Scenarios

### B.1 Switching to OpenAI GPT-4o

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

### B.2 Using Local Ollama

```yaml
model:
  common:
    base_url: "http://localhost:11434"

  powerful:
    model: "ollama/llama3"
    temperature: 0.3
    max_tokens: 4096
    timeout: 120

  fast:
    model: "ollama/phi3"
    temperature: 0.7
    max_tokens: 1024
    timeout: 30
```

### B.3 Multi-Provider Hybrid Deployment

```yaml
model:
  default_model_type: "powerful"

  common:
    api_key: "default-key"
    requests_per_minute: 30

  powerful:
    model: "anthropic/aws-claude-opus-4-5"
    base_url: "https://your-anthropic-gateway.com"
    api_key: "anthropic-specific-key"
    temperature: 0.2
    max_tokens: 8192

  fast:
    model: "openai/gpt-4o-mini"
    base_url: "https://api.openai.com/v1"
    api_key: "openai-specific-key"
    temperature: 0.7
    max_tokens: 1024

  summary:
    model: "gemini/gemini-1.5-flash"
    base_url: "https://generativelanguage.googleapis.com/v1"
    api_key: "gemini-specific-key"
    temperature: 0.3
    max_tokens: 2048
```

### B.4 Minimal Configuration (All models share the same API)

```yaml
model:
  common:
    base_url: "https://your-openai-proxy.com/v1"
    api_key: "sk-your-key"

  powerful:
    model: "openai/gpt-4o"
    temperature: 0.2

  fast:
    model: "openai/gpt-4o-mini"
    temperature: 0.7

  summary:
    model: "openai/gpt-4o-mini"
    temperature: 0.3
```
