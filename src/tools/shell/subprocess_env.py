"""Subprocess environment builder for shell tool.

Filters sensitive environment variables from child processes and injects
protective defaults.  Aligned with Claude Code's ``subprocessEnv.ts``
which strips API keys and CI tokens before spawning shell commands.

Two scrubbing strategies:
- **Exact match**: Variables whose name matches exactly.
- **Prefix match**: Variables whose name starts with a known prefix
  (e.g. ``AWS_`` catches ``AWS_SECRET_ACCESS_KEY``, ``AWS_SESSION_TOKEN``,
  and any future ``AWS_*`` variables).
"""

import os

from src.lib.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Scrubbing rules
# ---------------------------------------------------------------------------

# Variables removed by exact name match.
_SCRUB_EXACT: frozenset[str] = frozenset({
    # LLM provider keys
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_ORG_ID",
    "AZURE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "COHERE_API_KEY",
    "MISTRAL_API_KEY",
    "DEEPSEEK_API_KEY",
    "HUGGING_FACE_HUB_TOKEN",
    "HF_TOKEN",

    # Cloud provider secrets
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GCP_SERVICE_ACCOUNT_KEY",
    "AZURE_CLIENT_SECRET",

    # CI/CD tokens (GitHub Actions specific)
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
    "ALL_INPUTS",

    # Observability tokens (may contain Bearer tokens)
    "OTEL_EXPORTER_OTLP_HEADERS",

    # Generic secret patterns
    "DATABASE_URL",
    "DB_PASSWORD",
    "SECRET_KEY",
    "PRIVATE_KEY",
    # Internal release-campaign config transports (legacy and one-shot FD).
    "AGENTLOOM_MEMORY_CAMPAIGN_LLM_CONFIG_SECRET",
    "AGENTLOOM_MEMORY_CAMPAIGN_LLM_CONFIG_FD",
})

# Variables removed by prefix match.  Any env var whose name starts with
# one of these prefixes is scrubbed.
_SCRUB_PREFIXES: tuple[str, ...] = (
    "ANTHROPIC_FOUNDRY_",
    "GOOGLE_CLOUD_",
)

# ---------------------------------------------------------------------------
# Injection rules
# ---------------------------------------------------------------------------

# Variables injected into every subprocess.
_INJECT: dict[str, str] = {
    # Prevent git from opening an interactive editor (e.g. during rebase)
    "GIT_EDITOR": "true",
    # Detection flag so scripts can detect they run inside AgentLoom
    "AGENT_LOOM": "1",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_subprocess_env() -> dict[str, str]:
    """Build a sanitised environment dict for child processes.

    Returns:
        A copy of ``os.environ`` with sensitive variables removed and
        protective variables injected.
    """
    env = os.environ.copy()

    removed: list[str] = []

    # Exact-match scrub
    for key in _SCRUB_EXACT:
        if key in env:
            del env[key]
            removed.append(key)

    # Prefix-match scrub
    if _SCRUB_PREFIXES:
        for key in list(env.keys()):
            if any(key.startswith(prefix) for prefix in _SCRUB_PREFIXES):
                del env[key]
                removed.append(key)

    if removed:
        logger.debug(
            "Scrubbed %d sensitive env vars from subprocess: %s",
            len(removed), removed,
        )

    # Inject protective overrides
    env.update(_INJECT)

    return env
