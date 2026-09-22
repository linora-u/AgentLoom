"""Schedule-owned validation and mutations shared by all entry points."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from .schedule import cron_schedule, interval_schedule, once_schedule
from .schema import (
    ValidatedScheduleTarget,
    normalize_schedule_job_name,
)
from .store import ScheduleStore

ScheduleMutation = Literal["pause", "resume", "remove"]
ScheduleTargetResolver = Callable[[str | Path], ValidatedScheduleTarget]


def normalize_schedule_spec(raw: Any) -> dict[str, Any]:
    """Normalize one external schedule specification to its durable form."""

    if not isinstance(raw, dict):
        raise ValueError("schedule must be an object")
    kind = raw.get("kind")
    if not isinstance(kind, str):
        raise ValueError("schedule.kind must be a string")
    expected_by_kind = {
        "once": {"kind", "at", "timezone"},
        "interval": {"kind", "every", "timezone"},
        "cron": {"kind", "expression", "timezone"},
    }
    expected = expected_by_kind.get(kind)
    if expected is None:
        raise ValueError(f"unknown schedule kind: {kind!r}")
    missing = sorted(expected - set(raw))
    unexpected = sorted(set(raw) - expected)
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise ValueError(f"schedule params are invalid ({'; '.join(details)})")

    timezone = _required_spec_string(raw["timezone"], field="schedule.timezone")
    if timezone != timezone.strip():
        raise ValueError("schedule.timezone must not contain surrounding whitespace")
    if kind == "once":
        at = _required_spec_string(raw["at"], field="schedule.at")
        if at != at.strip():
            raise ValueError("schedule.at must not contain surrounding whitespace")
        return once_schedule(at, timezone=timezone)
    if kind == "interval":
        every = _required_spec_string(raw["every"], field="schedule.every")
        if every != every.strip():
            raise ValueError("schedule.every must not contain surrounding whitespace")
        return interval_schedule(every, timezone=timezone)
    expression = _required_spec_string(
        raw["expression"],
        field="schedule.expression",
    )
    if expression != expression.strip():
        raise ValueError("schedule.expression must not contain surrounding whitespace")
    return cron_schedule(expression, timezone=timezone)


def _required_spec_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


class ScheduleMutationService:
    """Normalize schedule writes and apply them through one durable store."""

    def __init__(
        self,
        source: str | Path | ScheduleStore,
        *,
        target_resolver: ScheduleTargetResolver | None = None,
    ):
        self.store = source if isinstance(source, ScheduleStore) else None
        self.project_root = (
            source.project_root if isinstance(source, ScheduleStore) else Path(source).expanduser().resolve()
        )
        self.target_resolver = target_resolver

    def add(
        self,
        *,
        yaml_path: str,
        name: str,
        schedule: Any,
    ) -> dict[str, Any]:
        if not isinstance(name, str):
            raise ValueError("name must be a string")
        if self.target_resolver is None:
            raise RuntimeError("Schedule add requires an Application Supervisor resolver")
        if not isinstance(yaml_path, (str, Path)) or not str(yaml_path).strip():
            raise ValueError("yaml_path must be a non-empty string")
        normalized_name = normalize_schedule_job_name(
            name,
            fallback=Path(yaml_path).stem,
        )
        normalized = normalize_schedule_spec(schedule)
        target = self._supervisor_target(yaml_path)

        def revalidate_target(_job: dict[str, Any]) -> None:
            current = self._supervisor_target(target.yaml_path)
            if current != target:
                raise ValueError("Schedule target changed before commit")

        with self._store() as store:
            return store.add_job(
                name=normalized_name,
                yaml_path=target,
                schedule=normalized,
                validate_before_commit=revalidate_target,
            )

    def mutate(self, action: ScheduleMutation, *, job_id: str) -> dict[str, Any]:
        if action not in {"pause", "resume", "remove"}:
            raise ValueError(f"unsupported schedule mutation: {action}")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id must be a non-empty string")
        if job_id != job_id.strip():
            raise ValueError("job_id must not contain surrounding whitespace")
        with self._store() as store:
            return getattr(store, action)(job_id)

    @contextmanager
    def _store(self) -> Iterator[ScheduleStore]:
        if self.store is not None:
            yield self.store
            return
        with ScheduleStore(self.project_root) as store:
            yield store

    def _supervisor_target(
        self,
        yaml_path: str | Path,
    ) -> ValidatedScheduleTarget:
        if not isinstance(yaml_path, (str, Path)) or not str(yaml_path).strip():
            raise ValueError("yaml_path must be a non-empty string")
        if self.target_resolver is None:
            raise RuntimeError("Schedule add requires an Application Supervisor resolver")
        target = self.target_resolver(yaml_path)
        if not isinstance(target, ValidatedScheduleTarget):
            raise TypeError(
                "Schedule target resolver must return ValidatedScheduleTarget"
            )
        return target
