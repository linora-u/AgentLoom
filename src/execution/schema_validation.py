"""Shared JSON Schema boundary validation."""

from __future__ import annotations

from collections.abc import Mapping

from referencing.jsonschema import DRAFT202012


def reject_remote_schema_references(
    value: object,
    *,
    field_name: str,
    path: str = "$",
) -> None:
    """Reject references that cannot be resolved within one schema document."""

    if isinstance(value, bool) or not isinstance(value, Mapping):
        return

    for keyword in ("$ref", "$dynamicRef"):
        if keyword not in value:
            continue
        reference = value[keyword]
        if not isinstance(reference, str) or (
            reference != "" and not reference.startswith("#")
        ):
            raise ValueError(
                f"{field_name} contains a remote reference at {path}.{keyword}"
            )

    for index, child in enumerate(DRAFT202012.subresources_of(value)):
        reject_remote_schema_references(
            child,
            field_name=field_name,
            path=f"{path}.<subschema>[{index}]",
        )
