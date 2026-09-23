"""Shared JSON Schema boundary validation."""

from __future__ import annotations

from collections.abc import Mapping


def reject_remote_schema_references(
    value: object,
    *,
    field_name: str,
    path: str = "$",
) -> None:
    """Reject references that cannot be resolved within one schema document."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in {"$ref", "$dynamicRef"} and (
                not isinstance(child, str) or not child.startswith("#")
            ):
                raise ValueError(
                    f"{field_name} contains a remote reference at {child_path}"
                )
            reject_remote_schema_references(
                child,
                field_name=field_name,
                path=child_path,
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_remote_schema_references(
                child,
                field_name=field_name,
                path=f"{path}[{index}]",
            )
