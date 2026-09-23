"""Shared JSON Schema boundary validation."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from referencing.jsonschema import DRAFT202012


def rebase_local_schema_references(
    schema: Mapping[str, object],
    *,
    pointer: str,
) -> dict[str, object]:
    """Embed a Draft 2020-12 schema while preserving local reference scope."""

    def copy_data(value: object) -> object:
        if isinstance(value, Mapping):
            return {key: copy_data(child) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [copy_data(child) for child in value]
        return deepcopy(value)

    def rebase(value: object, *, enabled: bool) -> object:
        if isinstance(value, bool) or not isinstance(value, Mapping):
            return copy_data(value)

        rebase_here = enabled and "$id" not in value
        subresource_ids = {
            id(child) for child in DRAFT202012.subresources_of(value)
        }

        def copy_member(child: object) -> object:
            if id(child) in subresource_ids:
                return rebase(child, enabled=rebase_here)
            if isinstance(child, Mapping):
                return {
                    key: (
                        rebase(item, enabled=rebase_here)
                        if id(item) in subresource_ids
                        else copy_data(item)
                    )
                    for key, item in child.items()
                }
            if isinstance(child, (list, tuple)):
                return [
                    (
                        rebase(item, enabled=rebase_here)
                        if id(item) in subresource_ids
                        else copy_data(item)
                    )
                    for item in child
                ]
            return copy_data(child)

        embedded: dict[str, object] = {}
        for key, child in value.items():
            if (
                rebase_here
                and key in {"$ref", "$dynamicRef"}
                and isinstance(child, str)
                and child.startswith("#/")
            ):
                embedded[key] = pointer + child[1:]
            elif (
                rebase_here
                and key in {"$ref", "$dynamicRef"}
                and child == "#"
            ):
                embedded[key] = pointer
            else:
                embedded[key] = copy_member(child)
        return embedded

    rebased = rebase(schema, enabled=True)
    assert isinstance(rebased, dict)
    return rebased


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
