"""Compile explicit Hook configuration and standalone Hook Bundles.

This module is the only configuration seam for executable Hooks.  Skills are
deliberately absent: a Hook is authorized either by a direct top-level
declaration or by an explicit Bundle reference.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .runtime import HookPlan
from .types import HOOK_EVENT_NAMES, HookEvent, HookHandler

DEFAULT_HOOK_TIMEOUT_SECONDS = 20.0
_BUNDLE_MANIFEST_NAME = "HOOK.yaml"
_TOOL_EVENTS = frozenset(
    {
        HookEvent.PRE_TOOL_USE,
        HookEvent.POST_TOOL_USE,
        HookEvent.POST_TOOL_USE_FAILURE,
    }
)
_ENTRY_FIELDS = frozenset({"id", "matcher", "command", "timeout", "enabled"})
_BUNDLE_REFERENCE_FIELDS = frozenset({"path", "enabled"})
_BUNDLE_MANIFEST_FIELDS = frozenset({"name", "description", "hooks"})


def _freeze(value: Any) -> Any:
    """Return a recursively immutable snapshot of YAML-compatible data."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class HookConfigLayer:
    """One unmerged configuration layer with source provenance.

    ``config`` is the complete raw layer mapping.  The compiler intentionally
    extracts only its top-level ``hooks`` key so callers cannot accidentally
    pass an already-merged view and erase layer semantics.
    """

    name: str
    config: Mapping[str, Any]
    agent_root: Path | str
    source_path: Path | str
    priority: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("HookConfigLayer.name must be a non-empty string")
        if not isinstance(self.config, Mapping):
            raise ValueError(f"Hook config layer {self.name!r} must be a mapping")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValueError(f"Hook config layer {self.name!r} priority must be an int")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "config", _freeze(self.config))
        object.__setattr__(
            self,
            "agent_root",
            Path(self.agent_root).expanduser().resolve(),
        )
        object.__setattr__(
            self,
            "source_path",
            Path(self.source_path).expanduser().resolve(),
        )


@dataclass(frozen=True, slots=True)
class ShellHookSpec:
    """Validated immutable representation of one executable Shell Hook."""

    hook_id: str
    event: HookEvent
    matcher: str
    command: str
    timeout: float
    cwd: Path
    project_root: Path
    source_path: Path
    layer_name: str
    priority: int
    bundle_name: str | None = None
    _compiled_matcher: re.Pattern[str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def matches(self, tool_name: str) -> bool:
        if self.matcher == "*":
            return True
        assert self._compiled_matcher is not None
        return self._compiled_matcher.fullmatch(tool_name) is not None

    def fingerprint_value(self) -> dict[str, Any]:
        return {
            "id": self.hook_id,
            "event": self.event.value,
            "matcher": self.matcher,
            "command": self.command,
            "timeout": self.timeout,
            "cwd": str(self.cwd),
            "project_root": str(self.project_root),
            "source_path": str(self.source_path),
            "layer_name": self.layer_name,
            "priority": self.priority,
            "bundle_name": self.bundle_name,
        }


@dataclass(frozen=True, slots=True)
class _HookDeclaration:
    hook_id: str
    event: HookEvent
    spec: ShellHookSpec | None


@dataclass(frozen=True, slots=True)
class _BundleUpdate:
    name: str
    specs: tuple[ShellHookSpec, ...] | None


@dataclass(frozen=True, slots=True)
class _ParsedLayer:
    layer: HookConfigLayer
    bundles: tuple[_BundleUpdate, ...]
    direct: tuple[_HookDeclaration, ...]


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects silently overwritten mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"Duplicate YAML mapping key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


class HookPlanCompiler:
    """Compile ordered raw layers into one immutable, executable Hook Plan."""

    def compile(
        self,
        layers: Iterable[HookConfigLayer],
        internal_handlers: Iterable[HookHandler] = (),
    ) -> HookPlan:
        indexed_layers = list(enumerate(layers))
        for _index, layer in indexed_layers:
            if not isinstance(layer, HookConfigLayer):
                raise TypeError("HookPlanCompiler layers must contain HookConfigLayer values")
        indexed_layers.sort(key=lambda item: (item[1].priority, item[0]))
        parsed = tuple(self._parse_layer(layer) for _index, layer in indexed_layers)

        effective: list[ShellHookSpec] = []
        id_events: dict[str, HookEvent] = {}
        for parsed_layer in parsed:
            for update in parsed_layer.bundles:
                effective = [
                    spec for spec in effective if spec.bundle_name != update.name
                ]
                if update.specs is None:
                    continue
                for spec in update.specs:
                    self._bind_global_event(id_events, spec.hook_id, spec.event)
                    effective = self._replace_by_id(effective, spec)

            for declaration in parsed_layer.direct:
                self._bind_global_event(
                    id_events,
                    declaration.hook_id,
                    declaration.event,
                )
                effective = [
                    spec for spec in effective if spec.hook_id != declaration.hook_id
                ]
                if declaration.spec is not None:
                    effective.append(declaration.spec)

        trusted = tuple(internal_handlers)
        configured = tuple(self._handler_for(spec) for spec in effective)
        handlers = (*trusted, *configured)
        fingerprint = self._fingerprint(trusted, effective)
        return HookPlan(handlers, fingerprint=fingerprint)

    @staticmethod
    def _replace_by_id(
        specs: list[ShellHookSpec],
        replacement: ShellHookSpec,
    ) -> list[ShellHookSpec]:
        return [
            spec for spec in specs if spec.hook_id != replacement.hook_id
        ] + [replacement]

    @staticmethod
    def _bind_global_event(
        id_events: dict[str, HookEvent],
        hook_id: str,
        event: HookEvent,
    ) -> None:
        previous = id_events.setdefault(hook_id, event)
        if previous is not event:
            raise ValueError(
                f"Hook id {hook_id!r} is used for both {previous.value} and {event.value}"
            )

    def _parse_layer(self, layer: HookConfigLayer) -> _ParsedLayer:
        if "hooks" not in layer.config:
            return _ParsedLayer(layer, (), ())
        raw_hooks = layer.config["hooks"]
        if not isinstance(raw_hooks, Mapping):
            raise ValueError(
                f"Top-level hooks in {layer.source_path} must be a mapping"
            )

        unknown = [
            str(key)
            for key in raw_hooks
            if key != "bundles" and key not in HOOK_EVENT_NAMES
        ]
        if unknown:
            raise ValueError(
                f"Unknown hook event or field in {layer.source_path}: {', '.join(unknown)}"
            )

        bundles = self._parse_bundle_references(layer, raw_hooks.get("bundles"))
        direct: list[_HookDeclaration] = []
        seen_ids: dict[str, HookEvent] = {}
        for update in bundles:
            for spec in update.specs or ():
                self._bind_layer_id(seen_ids, spec.hook_id, spec.event, layer)

        for event_name, entries in raw_hooks.items():
            if event_name == "bundles":
                continue
            event = HookEvent(event_name)
            for index, raw in enumerate(self._require_entry_sequence(entries, event, layer)):
                declaration = self._parse_entry(
                    raw,
                    event=event,
                    layer=layer,
                    cwd=Path(layer.agent_root),
                    source_path=Path(layer.source_path),
                    location=f"hooks.{event.value}[{index}]",
                    bundle_name=None,
                )
                self._bind_layer_id(
                    seen_ids,
                    declaration.hook_id,
                    declaration.event,
                    layer,
                )
                direct.append(declaration)
        return _ParsedLayer(layer, tuple(bundles), tuple(direct))

    @staticmethod
    def _bind_layer_id(
        seen: dict[str, HookEvent],
        hook_id: str,
        event: HookEvent,
        layer: HookConfigLayer,
    ) -> None:
        previous = seen.get(hook_id)
        if previous is None:
            seen[hook_id] = event
            return
        if previous is not event:
            raise ValueError(
                f"Hook id {hook_id!r} is used for both {previous.value} and {event.value} "
                f"in layer {layer.name!r}"
            )
        raise ValueError(f"duplicate Hook id {hook_id!r} in layer {layer.name!r}")

    def _parse_bundle_references(
        self,
        layer: HookConfigLayer,
        raw_bundles: Any,
    ) -> list[_BundleUpdate]:
        if raw_bundles is None:
            return []
        if not isinstance(raw_bundles, Mapping):
            raise ValueError(f"hooks.bundles in {layer.source_path} must be a mapping")

        updates: list[_BundleUpdate] = []
        for raw_name, raw_reference in raw_bundles.items():
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError(f"Bundle key in {layer.source_path} must be non-empty")
            name = raw_name.strip()
            location = f"hooks.bundles.{name}"
            if not isinstance(raw_reference, Mapping):
                raise ValueError(f"{location} must be a mapping")
            self._reject_extra_fields(
                raw_reference,
                _BUNDLE_REFERENCE_FIELDS,
                location,
            )
            enabled = self._parse_enabled(raw_reference, location)
            if not enabled:
                if set(raw_reference) != {"enabled"}:
                    raise ValueError(
                        f"Disabled Bundle {location} must be an enabled:false tombstone"
                    )
                updates.append(_BundleUpdate(name, None))
                continue

            raw_path = raw_reference.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ValueError(f"Enabled Bundle {location} requires a non-empty path")
            bundle_root = Path(raw_path).expanduser()
            if not bundle_root.is_absolute():
                bundle_root = Path(layer.agent_root) / bundle_root
            bundle_root = bundle_root.resolve()
            if not bundle_root.is_dir():
                raise ValueError(f"Hook Bundle directory does not exist: {bundle_root}")
            manifest_path = bundle_root / _BUNDLE_MANIFEST_NAME
            if not manifest_path.is_file():
                raise ValueError(f"Hook Bundle is missing {_BUNDLE_MANIFEST_NAME}: {bundle_root}")
            specs = self._load_bundle(layer, name, bundle_root, manifest_path)
            updates.append(_BundleUpdate(name, specs))
        return updates

    def _load_bundle(
        self,
        layer: HookConfigLayer,
        reference_name: str,
        bundle_root: Path,
        manifest_path: Path,
    ) -> tuple[ShellHookSpec, ...]:
        try:
            loaded = yaml.load(
                manifest_path.read_text(encoding="utf-8"),
                Loader=_UniqueKeyLoader,
            )
        except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
            raise ValueError(f"Invalid Hook Bundle manifest {manifest_path}: {exc}") from exc
        if not isinstance(loaded, Mapping):
            raise ValueError(f"Hook Bundle manifest must be a mapping: {manifest_path}")
        self._reject_extra_fields(loaded, _BUNDLE_MANIFEST_FIELDS, str(manifest_path))

        manifest_name = loaded.get("name")
        if manifest_name != reference_name:
            raise ValueError(
                f"Bundle key {reference_name!r} does not match "
                f"{_BUNDLE_MANIFEST_NAME} name {manifest_name!r}"
            )
        description = loaded.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError(f"Hook Bundle description must be a string: {manifest_path}")
        hooks = loaded.get("hooks")
        if not isinstance(hooks, Mapping):
            raise ValueError(f"Hook Bundle hooks must be a mapping: {manifest_path}")
        if "bundles" in hooks:
            raise ValueError(f"Hook Bundle {reference_name!r} cannot declare bundles")
        unknown = [str(key) for key in hooks if key not in HOOK_EVENT_NAMES]
        if unknown:
            raise ValueError(
                f"Unknown hook event in {manifest_path}: {', '.join(unknown)}"
            )

        specs: list[ShellHookSpec] = []
        seen: dict[str, HookEvent] = {}
        for event_name, entries in hooks.items():
            event = HookEvent(event_name)
            sequence = self._require_entry_sequence(entries, event, layer, manifest_path)
            for index, raw in enumerate(sequence):
                declaration = self._parse_entry(
                    raw,
                    event=event,
                    layer=layer,
                    cwd=bundle_root,
                    source_path=manifest_path.resolve(),
                    location=f"{manifest_path}:hooks.{event.value}[{index}]",
                    bundle_name=reference_name,
                )
                if declaration.spec is None:
                    raise ValueError(
                        f"Hook Bundle entries cannot be tombstones: {declaration.hook_id!r}"
                    )
                self._bind_layer_id(seen, declaration.hook_id, event, layer)
                specs.append(declaration.spec)
        return tuple(specs)

    @staticmethod
    def _require_entry_sequence(
        value: Any,
        event: HookEvent,
        layer: HookConfigLayer,
        source_path: Path | None = None,
    ) -> Sequence[Any]:
        if not isinstance(value, (list, tuple)):
            source = source_path or Path(layer.source_path)
            raise ValueError(f"hooks.{event.value} in {source} must be a list")
        return value

    def _parse_entry(
        self,
        raw: Any,
        *,
        event: HookEvent,
        layer: HookConfigLayer,
        cwd: Path,
        source_path: Path,
        location: str,
        bundle_name: str | None,
    ) -> _HookDeclaration:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{location} must be a mapping")
        self._reject_extra_fields(raw, _ENTRY_FIELDS, location)
        raw_id = raw.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise ValueError(f"{location} requires a non-empty id")
        hook_id = raw_id.strip()
        enabled = self._parse_enabled(raw, location)
        if not enabled:
            if set(raw) != {"id", "enabled"}:
                raise ValueError(
                    f"Disabled Hook {location} must be an id + enabled:false tombstone"
                )
            return _HookDeclaration(hook_id, event, None)

        raw_command = raw.get("command")
        if not isinstance(raw_command, str) or not raw_command.strip():
            raise ValueError(f"Enabled Hook {location} requires a non-empty command")
        command = raw_command.strip()

        raw_matcher = raw.get("matcher", "*")
        if not isinstance(raw_matcher, str) or not raw_matcher.strip():
            raise ValueError(f"{location}.matcher must be a non-empty string")
        matcher = raw_matcher.strip()
        compiled: re.Pattern[str] | None = None
        if "matcher" in raw and event not in _TOOL_EVENTS:
            raise ValueError(
                f"{location}.matcher is only valid for tool hook events"
            )
        if matcher != "*":
            try:
                compiled = re.compile(matcher)
            except re.error as exc:
                raise ValueError(f"{location} has invalid matcher {matcher!r}: {exc}") from exc

        raw_timeout = raw.get("timeout", DEFAULT_HOOK_TIMEOUT_SECONDS)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            raise ValueError(f"{location}.timeout must be a positive number")
        timeout = float(raw_timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError(f"{location}.timeout must be a positive finite number")

        spec = ShellHookSpec(
            hook_id=hook_id,
            event=event,
            matcher=matcher,
            command=command,
            timeout=timeout,
            cwd=cwd.resolve(),
            project_root=Path(layer.agent_root),
            source_path=source_path.resolve(),
            layer_name=layer.name,
            priority=layer.priority,
            bundle_name=bundle_name,
            _compiled_matcher=compiled,
        )
        return _HookDeclaration(hook_id, event, spec)

    @staticmethod
    def _parse_enabled(raw: Mapping[str, Any], location: str) -> bool:
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{location}.enabled must be a boolean")
        return enabled

    @staticmethod
    def _reject_extra_fields(
        raw: Mapping[str, Any],
        allowed: frozenset[str],
        location: str,
    ) -> None:
        extra = sorted(str(key) for key in raw if key not in allowed)
        if extra:
            raise ValueError(
                f"{location} contains unsupported field(s): {', '.join(extra)}"
            )

    @staticmethod
    def _handler_for(spec: ShellHookSpec) -> HookHandler:
        from .shell import create_shell_hook_executor

        return HookHandler(
            event=spec.event,
            pattern=spec.matcher,
            callback=create_shell_hook_executor(spec),
            source=f"{spec.layer_name}:{spec.source_path}#{spec.hook_id}",
            hook_id=spec.hook_id,
            source_path=str(spec.source_path),
            cwd=str(spec.cwd),
            shell_spec=spec,
        )

    @staticmethod
    def _fingerprint(
        internal_handlers: tuple[HookHandler, ...],
        specs: list[ShellHookSpec],
    ) -> str:
        payload = {
            "internal": [
                {
                    "event": handler.event.value,
                    "pattern": handler.pattern,
                    "source": handler.source,
                    "hook_id": handler.hook_id,
                }
                for handler in internal_handlers
            ],
            "shell": [spec.fingerprint_value() for spec in specs],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DEFAULT_HOOK_TIMEOUT_SECONDS",
    "HookConfigLayer",
    "HookPlanCompiler",
    "ShellHookSpec",
]
