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
    # Detect duplicate keys in the source mapping before resolving YAML merge
    # keys. Flattening first would incorrectly reject ordinary ``<<`` defaults
    # whenever the receiving mapping intentionally overrides one field.
    seen: set[tuple[str, Any]] = set()
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            identity: tuple[str, Any] = ("merge", "<<")
            display_key: Any = "<<"
        else:
            display_key = loader.construct_object(key_node, deep=deep)
            identity = ("key", display_key)
        if identity in seen:
            raise ValueError(f"Duplicate YAML mapping key: {display_key!r}")
        seen.add(identity)

    loader.flatten_mapping(node)
    return yaml.constructor.BaseConstructor.construct_mapping(
        loader,
        node,
        deep=deep,
    )


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_unique_yaml(stream: Any) -> Any:
    """Load YAML safely while rejecting every duplicate mapping key."""

    return yaml.load(stream, Loader=UniqueKeySafeLoader)
