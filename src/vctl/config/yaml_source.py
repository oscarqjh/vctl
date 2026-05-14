"""Read a YAML file safely and return its dict."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
import yaml.constructor
import yaml.resolver


# D13: custom loader that rejects duplicate mapping keys
class _StrictLoader(yaml.SafeLoader):
    pass


def _no_duplicate_keys(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    keys: set[object] = set()
    for key_node, _ in node.value:
        key: object = loader.construct_object(key_node, deep=True)
        if key in keys:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"duplicate key {key!r}",
                key_node.start_mark,
            )
        keys.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _no_duplicate_keys,
)


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.load(fh, Loader=_StrictLoader)  # noqa: S506
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping, got {type(data).__name__}")
    return data
