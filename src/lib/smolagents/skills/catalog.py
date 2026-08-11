"""Resolved Skill catalogue and activation interface."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from src.lib.logging import get_logger

from .parser import parse_skill_file

SkillScope = Literal["project", "application", "agent"]
_SCOPE_PRIORITY: dict[SkillScope, int] = {
    "project": 0,
    "application": 1,
    "agent": 2,
}
_IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "generated",
    "node_modules",
    "venv",
}


@dataclass(frozen=True, slots=True)
class SkillSource:
    path: Path
    scope: SkillScope


@dataclass(frozen=True, slots=True)
class SkillSummary:
    name: str
    description: str
    location: Path
    scope: SkillScope


@dataclass(frozen=True, slots=True)
class SkillActivation:
    name: str
    description: str
    instructions: str
    directory: Path
    files: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _SkillEntry:
    summary: SkillSummary
    instructions: str


class SkillCatalog:
    """Deep module that resolves discovery, precedence, and activation."""

    def __init__(self, entries: dict[str, _SkillEntry]) -> None:
        self._entries = dict(entries)

    @classmethod
    def empty(cls) -> SkillCatalog:
        return cls({})

    @classmethod
    def discover(cls, sources: Iterable[SkillSource], *, logger=None) -> SkillCatalog:
        log = get_logger(logger, __name__)
        entries: dict[str, _SkillEntry] = {}
        seen_files: set[Path] = set()

        for source in sorted(sources, key=lambda item: (_SCOPE_PRIORITY[item.scope], str(item.path))):
            for manifest in _discover_manifests(source.path):
                resolved = manifest.resolve()
                if resolved in seen_files:
                    continue
                seen_files.add(resolved)
                metadata, body = parse_skill_file(str(resolved), logger=log)
                summary = SkillSummary(
                    name=metadata.name,
                    description=metadata.description,
                    location=resolved,
                    scope=source.scope,
                )
                existing = entries.get(summary.name)
                if existing is not None:
                    existing_priority = _SCOPE_PRIORITY[existing.summary.scope]
                    incoming_priority = _SCOPE_PRIORITY[summary.scope]
                    if existing_priority == incoming_priority:
                        raise ValueError(
                            f"Duplicate skill name '{summary.name}' in {summary.scope} scope: "
                            f"'{existing.summary.location}' and '{summary.location}'"
                        )
                    if existing_priority > incoming_priority:
                        continue
                    log.warning(
                        "Skill '%s' from %s overrides %s scope definition",
                        summary.name,
                        summary.scope,
                        existing.summary.scope,
                    )
                entries[summary.name] = _SkillEntry(summary=summary, instructions=body)

        return cls(entries)

    def summaries(self) -> tuple[SkillSummary, ...]:
        return tuple(entry.summary for _, entry in sorted(self._entries.items()))

    def activate(self, name: str) -> SkillActivation:
        entry = self._entries.get(name)
        if entry is None:
            available = ", ".join(sorted(self._entries)) or "none"
            raise ValueError(f"Skill '{name}' not found. Available skills: {available}")
        directory = entry.summary.location.parent
        return SkillActivation(
            name=entry.summary.name,
            description=entry.summary.description,
            instructions=entry.instructions,
            directory=directory,
            files=_sample_files(directory),
        )


def _discover_manifests(path: Path) -> tuple[Path, ...]:
    path = path.expanduser().resolve()
    if not path.exists():
        return ()
    if path.is_file():
        return (path,) if path.name.lower() == "skill.md" else ()

    manifests: list[Path] = []

    def visit(directory: Path) -> None:
        entrypoints = sorted(
            (child for child in directory.iterdir() if child.is_file() and child.name.lower() == "skill.md"),
            key=lambda child: child.name.lower(),
        )
        if entrypoints:
            if len(entrypoints) > 1:
                raise ValueError(f"Ambiguous skill entrypoints in {directory}")
            manifests.append(entrypoints[0])
            return
        for child in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir() or child.name.startswith(".") or child.name in _IGNORED_DIRECTORIES:
                continue
            visit(child)

    visit(path)
    return tuple(manifests)


def _sample_files(directory: Path) -> tuple[Path, ...]:
    files = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name.lower() == "skill.md":
            continue
        relative = path.relative_to(directory)
        if any(part.startswith(".") or part in _IGNORED_DIRECTORIES for part in relative.parts):
            continue
        files.append(path.resolve())
        if len(files) == 10:
            break
    return tuple(files)
