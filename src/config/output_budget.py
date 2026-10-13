"""Output-cap intent and local input planning; neither invents Provider limits."""
from __future__ import annotations

from copy import deepcopy
from enum import Enum
from typing import Any

from agentloom.config.defaults import DEFAULT_MAX_OUTPUT_TOKENS


class UnsetBudget(Enum):
    VALUE = "unset"


UNSET_BUDGET = UnsetBudget.VALUE
CAP_ALIASES = frozenset({"max_tokens", "max_completion_tokens", "max_output_tokens"})


def positive_budget(value: Any, name: str) -> int | None:
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError(f"{name} must be a positive integer or null")
    return value


def resolve_cap(cap: Any = UNSET_BUDGET, extra: dict[str, Any] | None = None,
                *, source: str | None = None) -> tuple[int | None, dict[str, Any] | None, str]:
    """Unify transport option aliases; schemas and message data stay untouched.

    Only the option object and its supported ``extra_body`` chain are cap
    locations. A business/schema property with the same name is ordinary data.
    Explicit null at an option location conflicts with a configured cap.
    """
    candidates: list[tuple[str, int | None]] = []
    if cap is not UNSET_BUDGET:
        candidates.append(("max_output_tokens", positive_budget(cap, "max_output_tokens")))

    def clean(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            result = {}
            for name, item in value.items():
                location = f"{path}.{name}" if path else name
                if name in CAP_ALIASES:
                    candidates.append((location, positive_budget(item, location)))
                elif name == "extra_body" and isinstance(item, dict):
                    result[name] = clean(item, location)
                else:
                    result[name] = deepcopy(item)
            return result
        return deepcopy(value)

    cleaned = clean(extra or {}, "extra_completion_params") or None
    if candidates and any(value != candidates[0][1] for _, value in candidates):
        raise ValueError("conflicting output cap aliases or null override: " + ", ".join(name for name, _ in candidates))
    if not candidates:
        return None, cleaned, source or "omitted"
    value = candidates[0][1]
    return value, cleaned, source or ("cleared" if value is None else candidates[0][0])


def input_plan(window: int, cap: int | None, reserve: Any = UNSET_BUDGET,
               *, legacy: bool = False) -> tuple[int, int, str]:
    """Keep local generation space even when no request cap exists.

    Existing explicit cap profiles retain their prior input allowance. Otherwise
    reserve 16,384 tokens, or half of a small context window. This is a local
    planning policy, never Provider output metadata or an HTTP parameter.
    """
    if type(window) is not int or window <= 0:
        raise ValueError("context_window must be a positive integer")
    if legacy:
        return window, window, "legacy_max_tokens"
    if cap is not None and cap >= window:
        raise ValueError("max_output_tokens must be smaller than context_window")
    if reserve is UNSET_BUDGET:
        reserved = cap if cap is not None else min(DEFAULT_MAX_OUTPUT_TOKENS, max(1, window // 2))
        source = "explicit_cap_compatibility" if cap is not None else "local_input_reserve_policy"
    else:
        reserved = positive_budget(reserve, "input_token_reserve")
        if reserved is None:
            raise ValueError("input_token_reserve cannot be null")
        source = "input_token_reserve"
    required = max(reserved, cap or 0)
    if required >= window:
        raise ValueError("input_token_reserve must leave room within context_window")
    return required, window - required, source
