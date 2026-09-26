"""Model wire adapter identifiers accepted by AgentLoom configuration."""

from __future__ import annotations

from typing import Literal, get_args

type AdapterKind = Literal[
    "openai_chat",
    "openai_responses",
    "openai_codex_responses",
    "anthropic_messages",
]

MODEL_ADAPTERS: tuple[AdapterKind, ...] = get_args(AdapterKind.__value__)
