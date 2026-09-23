"""Public projection of execution configuration; never reused as runtime input."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentloom.config.config import EffectiveAgentConfigSnapshot

_SOURCE_NAMES = {"global_system": "global", "application_system": "application", "agent": "agent"}
_PRIVATE_KEYS = {"model", "llm", "langfuse"}
_SECRET_KEYS = {
    "api_key",
    "password",
    "secret",
    "secret_key",
    "private_key",
    "access_token",
    "refresh_token",
    "authorization",
    "cookie",
    "headers",
    "extra_headers",
    "env",
    "base_url",
    # Connection URLs can embed userinfo, signed queries or path credentials.
    # Their public projection is never used as the execution configuration.
    "url",
}


def display_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def public_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[redacted]"
            if str(key).lower() in _SECRET_KEYS
            or any(marker in str(key).lower() for marker in ("password", "secret", "api_key", "authorization"))
            else public_value(item)
            for key, item in value.items()
            if not str(key).startswith("_") and key not in _PRIVATE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [public_value(item) for item in value]
    return value


def configuration_projection(snapshot: EffectiveAgentConfigSnapshot, root: Path) -> dict[str, Any]:
    sources: dict[str, dict[str, str]] = {}

    def record(value: Mapping, prefix: str, source: dict[str, str]):
        for key, child in value.items():
            if str(key).startswith("_") or key in _PRIVATE_KEYS:
                continue
            path = f"{prefix}.{key}" if prefix else str(key)
            if not isinstance(child, Mapping):
                for prior in tuple(sources):
                    if prior.startswith(path + "."):
                        del sources[prior]
            sources[path] = source.copy()
            if isinstance(child, Mapping):
                record(child, path, source)

    for layer in snapshot.layers:
        record(
            layer.data,
            "",
            {"source": _SOURCE_NAMES.get(layer.name, layer.name), "source_path": display_path(layer.source_path, root)},
        )
    values = public_value(snapshot.values)
    # Hooks compose by stable ID, not by recursive dict/list merging. Present
    # the same compiled plan that execution consumes, with per-handler origins.
    from agentloom.execution.hooks.config import HookConfigLayer, HookPlanCompiler

    plan = HookPlanCompiler().compile(
        tuple(
            HookConfigLayer(layer.name, layer.data, layer.root, layer.source_path, index)
            for index, layer in enumerate(snapshot.layers)
        )
    )
    hook_plan = []
    if any("hooks" in layer.data for layer in snapshot.layers):
        values["hooks"] = {}
    for handler in plan.handlers:
        spec = handler.shell_spec
        if spec is None:
            continue
        entry = {"id": spec.hook_id, "matcher": spec.matcher, "timeout": spec.timeout}
        values["hooks"].setdefault(spec.event.value, []).append(entry)
        hook_plan.append(
            {
                **entry,
                "event": spec.event.value,
                "source": _SOURCE_NAMES.get(spec.layer_name, spec.layer_name),
                "source_path": display_path(spec.source_path, root),
            }
        )
    return {"values": values, "sources": sources, "hook_plan": hook_plan}
