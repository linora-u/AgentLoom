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
- [2. Model Type Naming Rules](#2-model-type-naming-rules)
- [3. model.\<type\> — Model Type Configuration](#3-modeltype--model-type-configuration)
- [4. Parameter Defaults](#4-parameter-defaults)
- [5. langfuse — Observability Configuration (Future Support)](#5-langfuse--observability-configuration-future-support)
- [6. Retry Mechanism Details](#6-retry-mechanism-details)
- [7. Provider Prefixes and Specific Behaviors](#7-provider-prefixes-and-specific-behaviors)
- [8. Default Value Constants Reference Table](#8-default-value-constants-reference-table)
- [Appendix A: Complete Pydantic Models](#appendix-a-complete-pydantic-models)
- [Appendix B: Typical Configuration Scenarios](#appendix-b-typical-configuration-scenarios)

---

## Quick Reference: Complete YAML Structure

The following shows the **complete structure** of `config/llm.yaml`, with all fields and their default values:

```yaml
# ============================================
# Model Configuration
# ============================================
model:
  # Global default model type. If omitted, Agents without model_type fail fast.
  default_model_type: "powerful"

  # ━━━ Required: Summary model (smart_summary context compression depends on this) ━━━
  summary:
    model: "openai/azure-gpt-5-chat"
    base_url: "https://llm-gateway.example.com/v1"
    api_key: "your-api-key"
    description: "Summary model, suitable for summarization and extraction"
    temperature: 1.0
    max_tokens: 2048
    timeout: 300

  # ━━━ The following model types are all optional — can be deleted/renamed/added ━━━

  # Powerful model (complex reasoning, code generation)
  powerful:
    model: "anthropic/aws-claude-opus-4-5"
    base_url: "https://llm-gateway-proxy.inner.chj.cloud/llm-gateway"
    api_key: "your-api-key"
    description: "High-quality reasoning model, suitable for complex analysis/code tasks"
    temperature: 0.2
    max_tokens: 8192
    timeout: 300
    context_cache: true

  # Fast model (intent recognition, classification)
  fast:
    model: "anthropic/aws-claude-sonnet-4-5"
    base_url: "https://llm-gateway.example.com/v1"
    api_key: "your-api-key"
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
| `model.default_model_type` | `str` | `""` | ❌ No | Global default model type. Must be a type key defined under the `model` block (e.g., `powerful`, `fast`, `summary`, or a custom type name). If omitted, Agents that do not specify `model_type` raise `ValueError` directly |

### 1.1 Resolution Rules

When an Agent needs to obtain model configuration, resolution follows this priority:

```
model_type in Agent YAML (e.g., model_type: "fast")
       ↓ (when not specified)
default_model_type in config/llm.yaml (e.g., default_model_type: "powerful")
       ↓ (when default_model_type is not configured)
ValueError
```

> ⚠️ **Note**: The resolved model type must exist in `llm.yaml`. If an Agent YAML explicitly specifies a missing `model_type`, the global default points to a missing type, or neither Agent nor global config provides a model type, the framework will **raise an error** (`ValueError`) directly.

**Example**:

```yaml
model:
  default_model_type: "powerful"   # All Agents default to the powerful type
  # default_model_type: "fast"     # If defaulting to the fast model

  powerful:
    model: "anthropic/claude-3-5-sonnet"
    base_url: "https://your-gateway.com/v1"
    api_key: "your-key"
    temperature: 0.2
```

```yaml
# Agent YAML example
name: "my_agent"
model_type: "powerful"    # Uses the powerful type; if omitted, uses configured default_model_type
```

---

## 2. Model Type Naming Rules

Under the `model` block, every dict-valued key except `default_model_type` is a model type name. The framework does not assign special semantics to type names; `powerful`, `fast`, and `summary` are examples.

- `summary` is required by context compression (`smart_summary`).
- `default_model_type` is a reserved key, not a model type.

**Example:**

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

## 3. model.\<type\> — Model Type Configuration

**Overview of all keys under the `model` block:**

| Key | Required | Description |
|-----|---------|------|
| `summary` | ❗ **Required** | Context compression (`smart_summary`) feature has a hard dependency on this type |
| `default_model_type` | ❌ Optional | Reserved key. No implicit default; omit only when every Agent specifies `model_type` |
| Any other key | ❌ Optional | Freely define, delete, or rename, e.g., `powerful`, `fast`, `code_review`, etc. |

Except for `default_model_type`, **all dict-valued keys under the `model` block are parsed as model types**. The framework has no restrictions on type names — `powerful` and `fast` are just example names. You can freely delete, rename, or add new ones. The `model` field for each model type is required.

**YAML path**: `model.<your-type-name>.*` (e.g., `model.powerful.*`, `model.my_llm.*`)
**Pydantic model**: `LlmModelTypeSettings`

### 3.1 Complete Parameter List

| Parameter | Type | Default | Required | Description |
|------|------|--------|------|------|
| `model` | `str` | — | ❗ **Required** | **LiteLLM model ID**, must include Provider prefix. Format: `{provider}/{model-name}`. Examples: `openai/gpt-4o`, `anthropic/claude-3-5-sonnet`, `gemini/gemini-1.5-pro`. **Raises an error if not configured.** |
| `base_url` | `str` | `""` | ❌ No | API gateway address. Configure independently for each model type. **Note: field name is `base_url`, not `api_base`** |
| `api_key` | `str` | `""` | ❌ No | API authentication key. Configure independently for each model type |
| `description` | `str` | `"Model type '{k}' loaded from YAML config"` | ❌ No | Human-readable model description. Used in logs and documentation |
| `temperature` | `float` | `0.1` | ❌ No | Creativity/randomness control (0.0 - 2.0). See [3.2 temperature Recommendations](#32-temperature-configuration-recommendations) |
| `max_tokens` | `int` \| `str` | `150000` | ❌ No | Maximum tokens per single model generation. Special value `"max"` uses the model's native maximum |
| `timeout` | `int` | `60` | ❌ No | Single HTTP request timeout (seconds). Interrupted if no response within this time |
| `num_retries` | `int` | `5` | ❌ No | Number of retries on API call failure |
| `retry_delay` | `float` | `15.0` | ❌ No | Initial retry delay (seconds). See [Section 6](#6-retry-mechanism-details) |
| `max_retry_delay` | `float` | `100.0` | ❌ No | Maximum retry delay (seconds). Upper limit for exponential backoff |
| `extra_headers` | `dict` \| `null` | `null` | ❌ No | Custom HTTP request headers. Configure independently for each model type; no cross-type merge is performed |
| `context_cache` | `bool` | `false` | ❌ No | Universal Prompt cache optimization. When `true`, the framework injects `cache_control: {"type": "ephemeral"}` for **ALL models** universally. litellm handles per-provider behavior automatically (Anthropic preserves, OpenAI strips, Vertex AI converts to Gemini format) |
| `system_prompt_boundary` | `str` \| `null` | `null` | ❌ No | System prompt split marker. When set, the system prompt is split into **static (cached)** + **dynamic (uncached)** segments at this marker, improving cache hit rates. Example: `"<!-- DYNAMIC_BOUNDARY -->"` |
| `requests_per_minute` | `int` | `60` | ❌ No | Rate limit for this model type |
| `supports_native_tool_calls` | `str` | `"auto"` | ❌ No | Native tool_calls capability flag. Tri-state: `"auto"` auto-detects on first call, `"true"` always uses native path, `"false"` always uses text parsing fallback. See [3.6 supports_native_tool_calls Behavior](#36-supports_native_tool_calls-behavior) |

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
  powerful:
    extra_headers:
      X-Model-Tier: "powerful"
      X-Biz-Tag: "demo"

  fast:
    extra_headers:
      X-Model-Tier: "fast"
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

  main:                          # ✅ Custom name, replaces powerful
    model: "anthropic/claude-3-5-sonnet"
    base_url: "https://your-gateway.com/v1"
    api_key: "your-key"
    temperature: 0.2

  code_review:                   # ✅ Custom type
    model: "anthropic/claude-3-5-sonnet"
    base_url: "https://your-gateway.com/v1"
    api_key: "your-key"
    temperature: 0.1
    max_tokens: 16384
    timeout: 600

  translation:                   # ✅ Custom type
    model: "openai/gpt-4o"
    base_url: "https://api.openai.com/v1"
    api_key: "your-openai-key"
    temperature: 0.3
    max_tokens: 4096

  summary:                       # Keep summary to support smart_summary feature
    model: "openai/gpt-4o-mini"
    base_url: "https://api.openai.com/v1"
    api_key: "your-openai-key"
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

  default:
    model: "openai/gpt-4o"
    base_url: "https://your-gateway.com/v1"
    api_key: "your-key"
    temperature: 0.3
```

> All custom types are **fully equivalent** to the framework examples' `powerful`/`fast`/`summary`. Each type declares its own model and parameters independently.

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

## 4. Parameter Defaults

The final value of model parameters is resolved as follows:

```
Model type settings (model.powerful.xxx)
      ↓ (when not set)
Code default values (defaults.py)
```

**Specific default rules**:

| Field | Default Behavior |
|------|----------|
| `base_url` | `""` when unset |
| `api_key` | `""` when unset |
| `temperature` | `0.1` when unset |
| `max_tokens` | `150000` when unset |
| `timeout` | `60` when unset |
| `num_retries` | `5` when unset |
| `retry_delay` | `15.0` when unset |
| `max_retry_delay` | `100.0` when unset |
| `extra_headers` | `null` when unset |
| `requests_per_minute` | `60` when unset |
| `model` | ❗ Required; no default |
| `description` | Auto-generated when unset |
| `context_cache` | `false` when unset |

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
    default_model_type: str = ""         # No implicit model-type default
    models: dict[str, LlmModelTypeSettings]  # {"powerful": ..., "summary": ..., or custom types}
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

## Appendix B: Typical Configuration Scenarios

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

### B.3 Multi-Provider Hybrid Deployment

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

### B.4 Minimal Configuration (All models share the same API)

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
