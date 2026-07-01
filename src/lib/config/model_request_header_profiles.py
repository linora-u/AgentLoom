"""AgentLoom-maintained model request header profiles."""

from __future__ import annotations

MODEL_REQUEST_HEADER_PROFILES: dict[str, dict[str, str]] = {
    # Verified on ssh dev with @cline/cli-linux-x64@3.0.34 against the current
    # OpenAI-compatible Ark endpoint in config/llm.yaml.
    "cline": {
        "User-Agent": "ai-sdk/openai-compatible/2.0.51 ai-sdk/provider-utils/4.0.30 runtime/bun/1.3.13",
    },
    # Verified on ssh dev with @moonshot-ai/kimi-code@0.21.1 against the
    # current OpenAI-compatible Ark endpoint in config/llm.yaml.
    "kimicode": {
        "User-Agent": "kimi-code-cli/0.21.1",
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "6.34.0",
        "X-Stainless-OS": "Linux",
        "X-Stainless-Arch": "x64",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v22.16.0",
    },
    # Verified on ssh dev with opencode-ai@1.17.12 against the current
    # OpenAI-compatible Ark endpoint in config/llm.yaml.
    "opencode": {
        "User-Agent": "opencode/1.17.12 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14",
        "X-Session-Affinity": "${agentloom.session_token}",
        "X-Session-Id": "${agentloom.session_token}",
    },
    # Verified on ssh dev with openclaw@2026.6.11 and node@22.19.0 against the
    # current OpenAI-compatible Ark endpoint in config/llm.yaml.
    "openclaw": {
        "User-Agent": "OpenAI/JS 6.39.1",
        "X-Stainless-Lang": "js",
        "X-Stainless-Package-Version": "6.39.1",
        "X-Stainless-OS": "Linux",
        "X-Stainless-Arch": "x64",
        "X-Stainless-Runtime": "node",
        "X-Stainless-Runtime-Version": "v22.19.0",
    },
    # Verified on ssh dev with Roo Code 3.53.0 OpenAiHandler source against the
    # current OpenAI-compatible Ark endpoint in config/llm.yaml.
    "roo": {
        "HTTP-Referer": "https://github.com/RooVetGit/Roo-Cline",
        "X-Title": "Roo Code",
        "User-Agent": "RooCode/3.53.0",
    },
}

MODEL_REQUEST_HEADER_PROFILE_NAMES = frozenset(MODEL_REQUEST_HEADER_PROFILES)
VERIFIED_MODEL_REQUEST_HEADER_PROFILE_NAMES = frozenset(
    {"cline", "kimicode", "opencode", "openclaw", "roo"}
)
