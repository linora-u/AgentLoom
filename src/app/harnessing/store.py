"""Crash-safe experiment records over the existing runtime storage primitives."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agentloom.execution.storage import SecureDirectory
from .models import fingerprint, json_value


class ExperimentStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.storage = SecureDirectory(self.root)

    def close(self):
        self.storage.close()

    def lock(self):
        return self.storage.advisory_file_lock("controller.lock", create=True, blocking=False)

    def read(self, name: str, default=None):
        try:
            return self.storage.read_json(name)
        except FileNotFoundError:
            return default

    def write(self, name: str, value: Any):
        self.storage.atomic_write_json(name, json_value(value))

    def immutable(self, name: str, value: Any):
        value = json_value(value)
        old = self.read(name)
        if old is not None:
            if old != value:
                raise ValueError(f"Persisted harness identity conflict: {name}")
            return old
        self.write(name, value)
        return value

    @staticmethod
    def _stage_name(key: str):
        if not key or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for char in key):
            raise ValueError("Invalid experiment stage key")
        return f"stages/{key}.json"

    def begin(self, key: str, inputs: Any):
        name = self._stage_name(key)
        identity = fingerprint(inputs)
        old = self.read(name)
        if old is not None:
            if old["input_id"] != identity:
                raise ValueError(f"Stage input changed on recovery: {key}")
            return old
        record = {"stage": key, "input_id": identity, "inputs": json_value(inputs), "status": "running"}
        self.write(name, record)
        return record

    def finish(self, key: str, result: Any, *, status="completed"):
        name = self._stage_name(key)
        old = self.read(name)
        if old is None:
            raise ValueError("Stage must have a persisted intent before completion")
        record = {**old, "status": status, "result": json_value(result)}
        if old["status"] != "running" and old != record:
            raise ValueError(f"Completed stage cannot be overwritten: {key}")
        self.write(name, record)
        return record

    def stages(self):
        try:
            names = self.storage.regular_file_names("stages")
        except FileNotFoundError:
            return []
        return [self.read(f"stages/{name}") for name in sorted(names) if name.endswith(".json")]

    def observe_usage(self, observations):
        """Merge cumulative snapshots by actual invocation/attempt, never by stage count."""
        records = self.read("usage.json", {})
        for observation in observations:
            item = json_value(observation)
            key = fingerprint([item["invocation_id"], item.get("attempt_id", "")])
            old = records.get(key)
            if old is None:
                records[key] = item
                continue
            sequence = item.get("sequence", 0)
            old_sequence = old.get("sequence", 0)
            if sequence < old_sequence:
                continue
            merged = dict(old)
            inconsistent = bool(old.get("inconsistent", False))
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                previous, current = old.get(field), item.get(field)
                if current is not None:
                    if previous is not None and current < previous:
                        inconsistent = True
                    merged[field] = max(previous or 0, current)
            merged.update(sequence=sequence, complete=bool(item.get("complete")) and not inconsistent,
                inconsistent=inconsistent)
            if item.get("duration_seconds") is not None:
                merged["duration_seconds"] = max(old.get("duration_seconds") or 0, item["duration_seconds"])
            records[key] = merged
        self.write("usage.json", records)
        summary = {field: sum(item[field] for item in records.values() if item.get(field) is not None)
            for field in ("input_tokens", "output_tokens", "total_tokens")}
        summary.update(invocations=len(records), complete=bool(records) and all(
            item.get("complete") and not item.get("inconsistent") for item in records.values()),
            unknown_invocations=sum(not item.get("complete", False) for item in records.values()),
            observations=list(records.values()))
        return summary
