"""Strict, immutable improvement policy and its Application configuration boundary."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

EditableComponent = Literal["system_prompt", "skills", "tools", "toolsets", "mcp_servers", "worker_agents"]
EDITABLE_COMPONENTS: tuple[EditableComponent, ...] = (
    "system_prompt", "skills", "tools", "toolsets", "mcp_servers", "worker_agents",
)


class _FrozenPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class FeedbackPolicy(_FrozenPolicy):
    mode: Literal["contrastive", "development_score"] = "contrastive"
    evaluator: StrictStr | None = None
    partition: StrictStr | None = None

    @field_validator("evaluator", "partition")
    @classmethod
    def _nonempty_reference(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("feedback references must be non-empty names without surrounding whitespace")
        return value

    @model_validator(mode="after")
    def _require_development_evidence(self, info: ValidationInfo) -> FeedbackPolicy:
        if not (info.context or {}).get("harnessing_source_only") and self.mode == "development_score":
            if not self.evaluator or not self.partition:
                raise ValueError("development_score requires feedback.evaluator and feedback.partition")
        return self


class RecursionPolicy(_FrozenPolicy):
    mode: Literal["parallel", "sequential"] = "parallel"
    k: StrictInt = Field(default=1, gt=0)


class ImprovementRoles(_FrozenPolicy):
    """Absent overrides use built-in roles; declared paths stay Application-relative."""

    general: StrictStr = ""
    domain: StrictStr = ""
    compose: StrictStr = ""
    select: StrictStr = ""
    reflect: StrictStr = ""

    @field_validator("general", "domain", "compose", "select", "reflect")
    @classmethod
    def _relative_yaml(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or value != value.strip()
            or "\\" in value
            or path.is_absolute()
            or ".." in path.parts
            or path.suffix.lower() not in {".yaml", ".yml"}
            or path.as_posix() != value
        ):
            raise ValueError("improvement roles require an Application-relative YAML path without traversal")
        return value

    def overrides(self) -> tuple[tuple[str, str], ...]:
        return tuple((name, getattr(self, name)) for name in type(self).model_fields if getattr(self, name))

    @model_serializer
    def _serialize_paths(self) -> dict[str, str]:
        return dict(self.overrides())


class HarnessingPolicy(_FrozenPolicy):
    enabled: StrictBool = False
    feedback: FeedbackPolicy = Field(default_factory=FeedbackPolicy)
    recursion: RecursionPolicy = Field(default_factory=RecursionPolicy)
    editable: tuple[EditableComponent, ...] = EDITABLE_COMPONENTS
    roles: ImprovementRoles = Field(default_factory=ImprovementRoles)

    @field_validator("editable", mode="before")
    @classmethod
    def _component_sequence(cls, value: Any) -> Any:
        if not isinstance(value, (list, tuple)):
            raise ValueError("editable must be a sequence of supported component names")
        if any(not isinstance(item, str) for item in value) or len(value) != len(set(value)):
            raise ValueError("editable must contain unique supported component names")
        return tuple(value)


def validate_harnessing_source(
    section: object, *, scope: Literal["project", "application"], source: str
) -> HarnessingPolicy:
    """Check every unmerged source, including disabled or subsequently overridden values.

    Sources may supply partial defaults; development-score dependencies are
    required only once the effective Application policy has been composed.
    """
    if not isinstance(section, Mapping):
        raise ValueError(f"{source}: harnessing must be a mapping")
    try:
        policy = HarnessingPolicy.model_validate(dict(section), context={"harnessing_source_only": True})
    except ValueError as exc:
        raise ValueError(f"{source}: invalid harnessing policy: {exc}") from exc
    if scope == "project" and policy.enabled:
        raise ValueError(f"{source}: harnessing.enabled=true belongs in the Application config/system.yaml")
    return policy


def resolve_harnessing_policy(
    project: Mapping[str, Any], application: Mapping[str, Any], *,
    project_source: str = "project config/system.yaml", application_source: str = "Application config/system.yaml",
) -> HarnessingPolicy:
    """Resolve defaults without granting project configuration application-wide enablement.

    Original source mappings remain untouched for normal configuration-layer
    inspection, provenance and candidate snapshots.
    """
    merged: dict[str, Any] = {}

    def overlay(target: dict[str, Any], values: Mapping[str, Any]) -> None:
        for key, value in values.items():
            if isinstance(value, Mapping) and isinstance(target.get(key), dict):
                overlay(target[key], value)
            else:
                target[key] = deepcopy(value)

    for values, scope, source in (
        (project, "project", project_source), (application, "application", application_source),
    ):
        if "harnessing" in values:
            section = values["harnessing"]
            validate_harnessing_source(section, scope=scope, source=source)
            overlay(merged, section)
    try:
        return HarnessingPolicy.model_validate(merged)
    except ValueError as exc:
        raise ValueError(f"{application_source}: invalid effective harnessing policy: {exc}") from exc
