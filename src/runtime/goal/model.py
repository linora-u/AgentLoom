"""Strict Goal-mode configuration and durable state models."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Literal

GOAL_SCHEMA_VERSION = 1
GoalStatus = Literal["active", "budget_limited", "complete"]


@dataclass(frozen=True, slots=True)
class GoalConfig:
    enabled: bool = False
    token_budget: int | None = None


def normalize_goal_config(config: dict[str, Any], *, source: str) -> GoalConfig:
    """Return the strict Goal config accepted by Supervisor YAML."""

    if "goal" not in config:
        return GoalConfig()
    raw = config["goal"]
    if isinstance(raw, bool):
        return GoalConfig(enabled=raw)
    if not isinstance(raw, dict):
        raise ValueError(f"{source}.goal must be a boolean or mapping")

    unsupported = sorted(set(raw) - {"enabled", "token_budget"})
    if unsupported:
        raise ValueError(
            f"{source}.goal has unsupported field(s): {', '.join(unsupported)}"
        )
    if "enabled" not in raw:
        raise ValueError(f"{source}.goal.enabled is required when goal is a mapping")
    enabled = raw["enabled"]
    if not isinstance(enabled, bool):
        raise ValueError(f"{source}.goal.enabled must be a boolean")

    token_budget_present = "token_budget" in raw
    token_budget = raw.get("token_budget")
    if token_budget_present:
        if (
            isinstance(token_budget, bool)
            or not isinstance(token_budget, int)
            or token_budget <= 0
        ):
            raise ValueError(f"{source}.goal.token_budget must be a positive integer")
        if not enabled:
            raise ValueError(
                f"{source}.goal.token_budget is only valid when goal.enabled is true"
            )
    return GoalConfig(enabled=enabled, token_budget=token_budget)


def normalize_workflow_for_goal(workflow: str | list[str]) -> str:
    if isinstance(workflow, str):
        return workflow.strip()
    return "\n".join(f"{index}. {item.strip()}" for index, item in enumerate(workflow, 1))


def build_goal_objective(
    *,
    description: str,
    workflow: str | list[str],
    task: str,
) -> str:
    parts = [f"Description:\n{description.strip()}"]
    parts.append(f"Workflow:\n{normalize_workflow_for_goal(workflow)}")
    if task.strip():
        parts.append(f"Runtime request:\n{task.strip()}")
    return "\n\n".join(parts)


def goal_objective_fingerprint(
    *,
    description: str,
    workflow: str | list[str],
    task: str,
) -> str:
    payload = {
        "description": description.strip(),
        "workflow": normalize_workflow_for_goal(workflow),
        "task": task.strip(),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().astimezone().isoformat()


@dataclass(frozen=True, slots=True)
class GoalState:
    goal_id: str
    objective: str
    objective_fingerprint: str
    status: GoalStatus = "active"
    token_budget: int | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    evidence: str | None = None
    goal_started: bool = False
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    schema_version: int = GOAL_SCHEMA_VERSION

    @property
    def used_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @classmethod
    def create(
        cls,
        *,
        objective: str,
        objective_fingerprint: str,
        token_budget: int | None,
    ) -> GoalState:
        now = _now()
        return cls(
            goal_id=f"goal_{uuid.uuid4().hex}",
            objective=objective,
            objective_fingerprint=objective_fingerprint,
            token_budget=token_budget,
            created_at=now,
            updated_at=now,
        )

    def with_usage(self, prompt_tokens: int, completion_tokens: int) -> GoalState:
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ValueError("Goal token usage cannot be negative")
        next_prompt = self.prompt_tokens + prompt_tokens
        next_completion = self.completion_tokens + completion_tokens
        next_status: GoalStatus = self.status
        if (
            self.status != "complete"
            and self.token_budget is not None
            and next_prompt + next_completion >= self.token_budget
        ):
            next_status = "budget_limited"
        return replace(
            self,
            prompt_tokens=next_prompt,
            completion_tokens=next_completion,
            status=next_status,
            updated_at=_now(),
        )

    def with_started(self) -> GoalState:
        if self.goal_started:
            return self
        return replace(self, goal_started=True, updated_at=_now())

    def with_completion(self, evidence: str) -> GoalState:
        evidence = evidence.strip()
        if not evidence:
            raise ValueError("Goal completion evidence must be non-empty")
        if self.status == "complete":
            return self
        now = _now()
        return replace(
            self,
            status="complete",
            evidence=evidence,
            completed_at=now,
            updated_at=now,
        )

    def with_resumed_budget(self, token_budget: int | None) -> GoalState:
        if self.token_budget is None and token_budget is not None:
            raise ValueError("Goal token_budget cannot be decreased on resume")
        if (
            self.token_budget is not None
            and token_budget is not None
            and token_budget < self.token_budget
        ):
            raise ValueError("Goal token_budget cannot be decreased on resume")
        status = self.status
        if status != "complete":
            status = (
                "budget_limited"
                if token_budget is not None and self.used_tokens >= token_budget
                else "active"
            )
        return replace(self, token_budget=token_budget, status=status, updated_at=_now())

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["used_tokens"] = self.used_tokens
        payload["remaining_tokens"] = (
            None
            if self.token_budget is None
            else max(self.token_budget - self.used_tokens, 0)
        )
        return payload


def validate_goal_state(raw: Any) -> GoalState:
    if not isinstance(raw, dict):
        raise ValueError("Goal state must be a JSON object")
    allowed = {
        "schema_version",
        "goal_id",
        "objective",
        "objective_fingerprint",
        "status",
        "token_budget",
        "prompt_tokens",
        "completion_tokens",
        "evidence",
        "goal_started",
        "created_at",
        "updated_at",
        "completed_at",
        "used_tokens",
        "remaining_tokens",
    }
    unexpected = sorted(set(raw) - allowed)
    if unexpected:
        raise ValueError(f"Goal state has unsupported field(s): {', '.join(unexpected)}")
    if raw.get("schema_version") != GOAL_SCHEMA_VERSION:
        raise ValueError("Goal state schema_version is unsupported")
    for field in (
        "goal_id",
        "objective",
        "objective_fingerprint",
        "created_at",
        "updated_at",
    ):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise ValueError(f"Goal state {field} must be a non-empty string")
    status = raw.get("status")
    if status not in {"active", "budget_limited", "complete"}:
        raise ValueError("Goal state status is invalid")
    token_budget = raw.get("token_budget")
    if token_budget is not None and (
        isinstance(token_budget, bool)
        or not isinstance(token_budget, int)
        or token_budget <= 0
    ):
        raise ValueError("Goal state token_budget must be a positive integer")
    numeric: dict[str, int] = {}
    for field in ("prompt_tokens", "completion_tokens"):
        value = raw.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Goal state {field} must be a non-negative integer")
        numeric[field] = value
    if "used_tokens" in raw and raw["used_tokens"] != sum(numeric.values()):
        raise ValueError("Goal state used_tokens does not match token totals")
    expected_remaining = (
        None
        if token_budget is None
        else max(token_budget - sum(numeric.values()), 0)
    )
    if "remaining_tokens" in raw and raw["remaining_tokens"] != expected_remaining:
        raise ValueError("Goal state remaining_tokens does not match token totals")
    if not isinstance(raw.get("goal_started"), bool):
        raise ValueError("Goal state goal_started must be a boolean")
    evidence = raw.get("evidence")
    completed_at = raw.get("completed_at")
    if evidence is not None and (not isinstance(evidence, str) or not evidence.strip()):
        raise ValueError("Goal state evidence must be a non-empty string")
    if completed_at is not None and (
        not isinstance(completed_at, str) or not completed_at.strip()
    ):
        raise ValueError("Goal state completed_at must be a non-empty string")
    if status == "complete" and (evidence is None or completed_at is None):
        raise ValueError("Complete Goal state requires evidence and completed_at")
    return GoalState(
        schema_version=GOAL_SCHEMA_VERSION,
        goal_id=raw["goal_id"],
        objective=raw["objective"],
        objective_fingerprint=raw["objective_fingerprint"],
        status=status,
        token_budget=token_budget,
        prompt_tokens=numeric["prompt_tokens"],
        completion_tokens=numeric["completion_tokens"],
        evidence=evidence,
        goal_started=raw["goal_started"],
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        completed_at=completed_at,
    )
