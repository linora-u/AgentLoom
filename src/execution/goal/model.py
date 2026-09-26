"""Strict Goal-mode configuration and durable state models."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Literal

GOAL_SCHEMA_VERSION = 1
GoalStatus = Literal["active", "complete"]


@dataclass(frozen=True, slots=True)
class GoalConfig:
    enabled: bool = False


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
    return GoalConfig(enabled=enabled)


def build_goal_objective(
    *,
    task: str,
) -> str:
    return task.strip()


def goal_objective_fingerprint(
    *,
    task: str,
) -> str:
    payload = {"task": task.strip()}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().astimezone().isoformat()


@dataclass(frozen=True, slots=True)
class GoalState:
    goal_id: str
    objective: str
    objective_fingerprint: str
    phase_index: int = 0
    status: GoalStatus = "active"
    evidence: str | None = None
    goal_started: bool = False
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    schema_version: int = GOAL_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        objective: str,
        objective_fingerprint: str,
        phase_index: int = 0,
    ) -> GoalState:
        now = _now()
        return cls(
            goal_id=f"goal_{uuid.uuid4().hex}",
            objective=objective,
            objective_fingerprint=objective_fingerprint,
            phase_index=phase_index,
            created_at=now,
            updated_at=now,
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_goal_state(raw: Any) -> GoalState:
    if not isinstance(raw, dict):
        raise ValueError("Goal state must be a JSON object")
    allowed = {
        "schema_version",
        "goal_id",
        "objective",
        "objective_fingerprint",
        "phase_index",
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
    phase_index = raw.get("phase_index", 0)
    if isinstance(phase_index, bool) or not isinstance(phase_index, int) or phase_index < 0:
        raise ValueError("Goal state phase_index must be a non-negative integer")
    # Old budget stops resume as active Goals; budget metadata is ignored.
    if status == "budget_limited":
        status = "active"
    if status not in {"active", "complete"}:
        raise ValueError("Goal state status is invalid")
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
        phase_index=phase_index,
        status=status,
        evidence=evidence,
        goal_started=raw["goal_started"],
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        completed_at=completed_at,
    )

def goal_continuation_prompt(state: Any) -> str:
    """Render the runtime-neutral continuation request for an active Goal."""

    return (
        "Continue working toward the active Goal using the existing conversation "
        "and tool state. Do not restart or repeat completed work.\n\n"
        f"Goal ID: {state.goal_id}\n"
        "Objective: unchanged from the initial task context; call get_goal only "
        "if you need to inspect the canonical objective again.\n"
        f"Goal status: {state.status}\n"
        "\n"
        "A normal final answer does not complete the Goal. Only after the entire "
        "objective is delivered and verified, call update_goal with status="
        "'complete' and concise evidence."
    )
