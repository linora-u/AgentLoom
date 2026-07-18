"""Safe YAML loading that rejects silently overwritten mapping keys."""

from __future__ import annotations

from typing import Any

import yaml


class UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader variant that treats duplicate mapping keys as invalid."""


def _construct_unique_mapping(
    loader: UniqueKeySafeLoader,
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


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_unique_yaml(stream: Any) -> Any:
    """Load YAML safely while rejecting every duplicate mapping key."""

    return yaml.load(stream, Loader=UniqueKeySafeLoader)
