"""Pure Pi configuration checks; importing these never starts Node or an SDK."""
from collections.abc import Mapping
import math

from agentloom.runtime.agent_runtime import RuntimeCapabilities, RuntimeModelSelection

SDK_VERSION = "0.79.4"
CAPABILITIES = RuntimeCapabilities(True, True, False, True, goal=True, stop_hooks=True)


def validate_options(options: Mapping) -> None:
    unknown = set(options) - {"max_stop_attempts"}
    if unknown:
        raise ValueError("Unsupported pi runtime_options: " + ", ".join(sorted(unknown)))
    value = options.get("max_stop_attempts", 3)
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("pi runtime_options.max_stop_attempts must be an integer from 1 to 100")


def validate_model(model: RuntimeModelSelection) -> None:
    if model.protocol not in {"openai_chat", "openai_responses"}:
        raise ValueError(f"Pi does not support model protocol {model.protocol!r}")
    settings = model.settings
    supported = {
        "model", "adapter", "api_key", "base_url", "temperature", "max_tokens",
        "max_output_tokens", "context_window", "input_token_limit", "timeout", "num_retries",
        "retry_delay", "max_retry_delay", "extra_headers", "context_cache",
        "system_prompt_boundary", "description", "requests_per_minute", "extra_completion_params",
    }
    if set(settings) - supported:
        raise ValueError("Pi model profile contains unsupported settings")
    if settings.get("system_prompt_boundary"):
        raise ValueError("Pi does not support system_prompt_boundary")
    for name in ("timeout", "context_window", "max_output_tokens", "requests_per_minute"):
        value = settings.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"Pi model {name} must be a positive integer")
    for name in ("temperature", "retry_delay", "max_retry_delay", "num_retries"):
        value = settings.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Pi model {name} must be a finite non-negative number")
    if type(settings["num_retries"]) is not int:
        raise ValueError("Pi model num_retries must be an integer")
    if any(isinstance(value, (int, float)) and value > 2_147_483
           for value in (settings[name] for name in ("timeout", "retry_delay", "max_retry_delay"))):
        raise ValueError("Pi model timeout/retry delays exceed the Node timer limit")
    for name in ("base_url", "api_key"):
        if not isinstance(settings.get(name), str):
            raise ValueError(f"Pi model {name} must be a string")
    extra = settings.get("extra_completion_params") or {}
    if not isinstance(extra, Mapping) or set(extra) - {"extra_body", "tool_choice", "parallel_tool_calls", "top_p", "seed", "reasoning_effort"}:
        raise ValueError("Pi model extra_completion_params contains unsupported parameters")
    if extra.get("tool_choice", "auto") not in ("auto", "none") or type(extra.get("parallel_tool_calls", False)) is not bool:
        raise ValueError("Pi requires tool_choice auto/none and boolean parallel_tool_calls")
    body = extra.get("extra_body") or {}
    protected = {"model", "messages", "input", "instructions", "tools", "tool_choice", "parallel_tool_calls",
                 "stream", "stream_options", "max_tokens", "max_completion_tokens", "max_output_tokens", "temperature"}
    if not isinstance(body, Mapping) or set(body) & protected:
        raise ValueError("Pi extra_body cannot replace model, conversation, tools or mapped generation parameters")
