"""Per-turn budget policy at LiteLLM's final HTTP boundary, after global defaults."""
from __future__ import annotations

import json

import httpx
from agentloom.config.output_budget import CAP_ALIASES
from agentloom.execution.output_budget import WireBudget


class BudgetHTTPTransport(httpx.BaseTransport):
    """Rebuild only model HTTP bodies; never mutate a process-global SDK setting."""

    def __init__(self, policy: WireBudget, *, verify: object, capture=None):
        self.policy = policy
        # A Client retains HTTP(S)_PROXY / NO_PROXY routing. Passing an explicit
        # transport to the outer Client disables HTTPX's environment mounts.
        self.inner = httpx.Client(verify=verify)
        self.capture = capture

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith(("/chat/completions", "/responses", "/messages")):
            body = json.loads(request.read())
            chat_field = "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
            for alias in CAP_ALIASES:
                body.pop(alias, None)
            if self.policy.cap is not None:
                key = "max_output_tokens" if self.policy.adapter == "openai_responses" else (
                    chat_field if self.policy.adapter == "openai_chat" else "max_tokens")
                body[key] = self.policy.cap
            elif self.policy.adapter == "anthropic_messages":
                raise ValueError("anthropic_messages requires an explicit or verified output cap")
            headers = dict(request.headers)
            headers.pop("content-length", None)
            request = httpx.Request(request.method, request.url, headers=headers,
                                    json=body, extensions=request.extensions)
        if self.capture is not None:
            self.capture(request)
        return self.inner.send(request, stream=True)

    def close(self) -> None:
        self.inner.close()
